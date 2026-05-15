# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIA: Dynamic Momentum Breakout — LONG — ENTRATA INTRACANDLE
#
# Logica:
#   - Ogni 2 min chiama GET /v5/market/tickers (TUTTI i futures lineari, ~400 coin)
#   - Filtra: volume 24h > 5M USDT, gain 24h tra +3% e +50%
#   - Per i top 30 candidati: verifica segnale breakout su 15min IN CORSO
#     • Candela 15min CORRENTE (non chiusa): gain +1.5% → +5.0% dall'open
#     • Volume accumulato > 3× media 20 candele chiuse precedenti
#     • Live price > massimo delle 20 candele chiuse precedenti
#     • RSI(14) ultima chiusa < 75 (non esaurito)
#     • EMA20 slope positiva (ultima chiusa)
#   - Entrata DURANTE la candela — non aspetta la chiusura
#   - SL = 2×ATR(14) sotto entry | Trail = 2.5×ATR dal max, attiva a 1R
#   - MAX 5 posizioni | RISK 1% per trade | Leva 10×
#   - BTC filter leggero: blocca solo se BTC 1h cala > 4% (crollo grave)
# ─────────────────────────────────────────────────────────────────────────────

import os
import time
import hmac
import hashlib
import json
import threading
from decimal import Decimal, ROUND_DOWN
from typing import Optional

import requests
import pandas as pd
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ── ENV VARS ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN     = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
KEY                = os.getenv("BYBIT_API_KEY", "")
SECRET             = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET      = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL     = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()

# ── PARAMETRI STRATEGIA ───────────────────────────────────────────────────────
INTERVAL_MINUTES   = 15       # timeframe analisi: 15min
RISK_PCT           = 0.0100   # 1% rischio per trade
DEFAULT_LEVERAGE   = 10
MAX_OPEN_POSITIONS = 5
MARGIN_USE_PCT     = 0.35
ORDER_USDT_MAX     = float(os.getenv("ORDER_USDT_MAX", "1000"))

SL_ATR_MULT    = 2.0    # SL = 2×ATR sotto entry
TRAIL_ATR_MULT = 2.5    # trailing = 2.5×ATR dal massimo
TRAIL_START_R  = 1.0    # attiva trailing quando gain >= 1R

# Filtri scansione
MIN_VOL_24H_USDT = 5_000_000   # volume minimo 24h per evitare illiquide
VOL_SPIKE_MIN    = 3.0          # spike volume candela corrente vs media 20 chiuse
CHANGE_15M_MIN   = 1.5          # % gain candela 15min in corso — minimo
CHANGE_15M_MAX   = 5.0          # % gain candela 15min in corso — massimo
CHANGE_24H_MIN   = 3.0          # % gain 24h minimo (filtra da tickers API)
CHANGE_24H_MAX   = 50.0         # % gain 24h massimo (evita già esplosi)
RSI_MAX_ENTRY    = 75.0         # RSI max per evitare entrate su overbought
BARS_BREAKOUT    = 20           # breakout sopra max di queste N barre
MAX_CANDIDATES   = 30           # quanti candidati verificare per scan

# BTC filter
BTC_CRASH_PCT    = -4.0         # blocca nuovi long se BTC 1h cala > 4%

# Timing
SCAN_INTERVAL_SEC  = 120   # 2 min tra scan (intracandle detection)
TRAIL_SLEEP_SEC    = 30
SL_WATCH_SLEEP_SEC = 300   # 5 min
ATR_WINDOW         = 14
LONG_IDX           = 1

# Escludi token leva, stablecoin, inversi
EXCLUDE_SUBSTRINGS = ["USDC", "BUSD", "DAI", "TUSD", "FRAX",
                      "3LUSDT", "3SUSDT", "BULLUSDT", "BEARUSDT"]

# ── STATO GLOBALE ─────────────────────────────────────────────────────────────
open_positions:   set  = set()
position_data:    dict = {}
_state_lock             = threading.RLock()
_instr_lock             = threading.RLock()
_instrument_cache: dict = {}
_price_cache:     dict  = {}
_price_lock             = threading.RLock()
_last_log_times:  dict  = {}
_btc_ok:          bool  = True
_btc_ts:          float = 0.0

# ── HTTP SESSION ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
_retry  = Retry(total=3, backoff_factor=0.5,
                status_forcelist=[429, 500, 502, 503, 504],
                allowed_methods=["GET", "POST"])
SESSION.mount("https://", HTTPAdapter(max_retries=_retry, pool_maxsize=30))


# ── LOG ───────────────────────────────────────────────────────────────────────
def log(msg: str) -> None:
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg, flush=True)


def tlog(key: str, msg: str, interval_sec: int = 60) -> None:
    now = time.time()
    if now - _last_log_times.get(key, 0) >= interval_sec:
        _last_log_times[key] = now
        log(msg)


def notify_telegram(msg: str) -> None:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": f"[MOMENTUM] {msg}"},
            timeout=10,
        )
    except Exception as e:
        log(f"[TELEGRAM] err: {e}")


# ── FIRMA BYBIT ───────────────────────────────────────────────────────────────
def _bybit_signed_get(path: str, params: dict):
    from urllib.parse import urlencode
    qs   = urlencode(sorted(params.items()))
    ts   = str(int(time.time() * 1000))
    rw   = "10000"
    sign = hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{qs}".encode(),
                    hashlib.sha256).hexdigest()
    headers = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
               "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw}
    return SESSION.get(f"{BYBIT_BASE_URL}{path}",
                       headers=headers, params=params, timeout=10)


def _bybit_signed_post(path: str, body: dict):
    ts        = str(int(time.time() * 1000))
    rw        = "10000"
    body_json = json.dumps(body, separators=(",", ":"))
    sign      = hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{body_json}".encode(),
                         hashlib.sha256).hexdigest()
    headers   = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
                 "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw,
                 "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json"}
    return SESSION.post(f"{BYBIT_BASE_URL}{path}",
                        headers=headers, data=body_json, timeout=10)


# ── HELPERS STATO ─────────────────────────────────────────────────────────────
def get_position(symbol: str) -> Optional[dict]:
    with _state_lock:
        return position_data.get(symbol)


def set_position(symbol: str, entry: dict) -> None:
    with _state_lock:
        position_data[symbol] = entry


def add_open(symbol: str) -> None:
    with _state_lock:
        open_positions.add(symbol)


def discard_open(symbol: str) -> None:
    with _state_lock:
        open_positions.discard(symbol)


# ── INSTRUMENT INFO ───────────────────────────────────────────────────────────
def get_instrument_info(symbol: str) -> dict:
    now = time.time()
    with _instr_lock:
        cached = _instrument_cache.get(symbol)
        if cached and now - cached["ts"] < 300:
            return cached["data"]
    fallback = {"min_qty": 0.01, "qty_step": 0.01, "precision": 4,
                "price_step": 0.01, "min_order_amt": 5.0}
    try:
        resp = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/instruments-info",
                           params={"category": "linear", "symbol": symbol},
                           timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return fallback
        info = data["result"]["list"][0]
        lot  = info.get("lotSizeFilter", {})
        pf   = info.get("priceFilter", {})
        parsed = {
            "min_qty":       float(lot.get("minOrderQty",      0.01) or 0.01),
            "qty_step":      float(lot.get("qtyStep",         "0.01") or "0.01"),
            "precision":     int(info.get("priceScale",           4)  or 4),
            "price_step":    float(pf.get("tickSize",         "0.01") or "0.01"),
            "min_order_amt": float(lot.get("minNotionalValue",   "5") or "5"),
        }
        with _instr_lock:
            _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed
    except Exception:
        return fallback


def format_price_bybit(price: float, tick_size: float) -> str:
    step    = Decimal(str(tick_size))
    p       = Decimal(str(price))
    floored = (p // step) * step
    dec     = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    return f"{floored:.{dec}f}"


def _format_qty_with_step(qty: float, step: float) -> str:
    step_dec = Decimal(str(step))
    q        = Decimal(str(qty))
    floored  = (q // step_dec) * step_dec
    sd = -step_dec.as_tuple().exponent if step_dec.as_tuple().exponent < 0 else 0
    pattern  = Decimal("1." + "0" * sd) if sd > 0 else Decimal("1")
    floored  = floored.quantize(pattern, rounding=ROUND_DOWN)
    return f"{floored:.{sd}f}" if sd > 0 else str(int(floored))


# ── PREZZO ────────────────────────────────────────────────────────────────────
def get_last_price(symbol: str) -> Optional[float]:
    now = time.time()
    with _price_lock:
        c = _price_cache.get(symbol)
        if c and now - c["ts"] <= 2:
            return c["price"]
    try:
        resp = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/tickers",
                           params={"category": "linear", "symbol": symbol},
                           timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            item  = data["result"]["list"][0]
            price = float(item["lastPrice"])
            bid1  = float(item.get("bid1Price") or price)
            ask1  = float(item.get("ask1Price") or price)
            with _price_lock:
                _price_cache[symbol] = {"price": price, "bid1": bid1,
                                        "ask1": ask1, "ts": now}
            return price
    except Exception:
        pass
    return None


def get_bid_price(symbol: str) -> Optional[float]:
    get_last_price(symbol)
    with _price_lock:
        c = _price_cache.get(symbol, {})
        return c.get("bid1") or c.get("price")


# ── BILANCIO ──────────────────────────────────────────────────────────────────
def get_usdt_balance() -> float:
    try:
        resp = _bybit_signed_get("/v5/account/wallet-balance",
                                 {"accountType": BYBIT_ACCOUNT_TYPE})
        data = resp.json()
        acct = data.get("result", {}).get("list", [{}])[0]
        for c in acct.get("coin", []):
            if c.get("coin") == "USDT":
                v = (c.get("availableToWithdraw")
                     or c.get("availableBalance")
                     or c.get("walletBalance") or "0")
                return float(v)
        return float(acct.get("totalAvailableBalance") or 0.0)
    except Exception:
        return 0.0


def get_total_equity() -> float:
    try:
        resp = _bybit_signed_get("/v5/account/wallet-balance",
                                 {"accountType": BYBIT_ACCOUNT_TYPE})
        data = resp.json()
        acct = data.get("result", {}).get("list", [{}])[0]
        return float(acct.get("totalEquity")
                     or acct.get("totalAvailableBalance") or 0.0)
    except Exception:
        return get_usdt_balance()


# ── KLINES ────────────────────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: int = 60,
                 limit: int = 60) -> Optional[pd.DataFrame]:
    try:
        resp = SESSION.get(
            f"{BYBIT_BASE_URL}/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": str(interval), "limit": limit},
            timeout=10,
        )
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return None
        klines = list(reversed(data["result"]["list"]))
        df = pd.DataFrame(klines,
                          columns=["timestamp", "Open", "High", "Low",
                                   "Close", "Volume", "Turnover"])
        for col in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
            df[col] = df[col].astype(float)
        return df
    except Exception:
        return None


# ── POSIZIONI BYBIT ───────────────────────────────────────────────────────────
def get_open_long_qty(symbol: str) -> float:
    try:
        resp = _bybit_signed_get("/v5/position/list",
                                 {"category": "linear", "symbol": symbol})
        data = resp.json()
        if data.get("retCode") != 0:
            return 0.0
        for pos in data.get("result", {}).get("list", []):
            if pos.get("side") == "Buy":
                return float(pos.get("size", 0) or 0)
    except Exception:
        pass
    return 0.0


# ── BTC FILTER (leggero) ──────────────────────────────────────────────────────
def _update_btc_filter() -> None:
    """Blocca nuovi long solo se BTC perde > 4% nell'ultima 1h chiusa."""
    global _btc_ok, _btc_ts
    if time.time() - _btc_ts < 120:
        return
    try:
        df = fetch_klines("BTCUSDT", interval=60, limit=5)
        if df is not None and len(df) >= 2:
            last   = df.iloc[-2]   # ultima candela chiusa
            change = (float(last["Close"]) - float(last["Open"])) / float(last["Open"]) * 100
            _btc_ok = change >= BTC_CRASH_PCT
            tlog("btc_filter",
                 f"[BTC] 1h change={change:+.2f}% | gate={'OK' if _btc_ok else 'CHIUSO (crollo)'}", 120)
    except Exception:
        pass
    _btc_ts = time.time()


# ── SCANSIONE TOP MOVERS ──────────────────────────────────────────────────────
def scan_top_movers() -> list:
    """
    1 sola chiamata API → riceve tutti i futures lineari con prezzi e volume.
    Filtra e restituisce i top candidati per gain 24h, ordinati per forza.
    """
    try:
        resp = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/tickers",
                           params={"category": "linear"}, timeout=15)
        data = resp.json()
        if data.get("retCode") != 0:
            return []
        tickers = data["result"]["list"]
    except Exception as e:
        log(f"[SCAN] Errore fetch tickers: {e}")
        return []

    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        if any(ex in sym for ex in EXCLUDE_SUBSTRINGS):
            continue
        if sym in open_positions:
            continue
        try:
            vol24h = float(t.get("turnover24h", 0) or 0)
            # price24hPcnt è decimale (0.03 = +3%)
            chg24h = float(t.get("price24hPcnt", 0) or 0) * 100
            price  = float(t.get("lastPrice", 0) or 0)
        except Exception:
            continue

        if vol24h  < MIN_VOL_24H_USDT:          continue
        if chg24h  < CHANGE_24H_MIN:             continue
        if chg24h  > CHANGE_24H_MAX:             continue
        if price   <= 0:                         continue

        candidates.append({"symbol": sym, "change24h": chg24h,
                            "volume24h": vol24h})

    candidates.sort(key=lambda x: x["change24h"], reverse=True)
    return candidates[:MAX_CANDIDATES]


# ── SIGNAL CHECK ──────────────────────────────────────────────────────────────
def check_entry_signal(symbol: str) -> Optional[dict]:
    """
    Verifica breakout sulla candela 1h appena chiusa.
    Usa iloc[-2] (ultima chiusa) per evitare lookahead sulla candela corrente parziale.

    Condizioni:
    1. Candela 1h chiusa: gain +2.5% → +8%
    2. Volume candela chiusa > 3× media 20 candele precedenti
    3. Close > massimo delle 20 barre precedenti (breakout di struttura)
    4. RSI(14) < 75 (non esaurito)
    5. EMA20 slope positiva
    """
    df = fetch_klines(symbol, interval=INTERVAL_MINUTES, limit=28)
    if df is None or len(df) < 24:
        return None

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    v = df["Volume"]
    o = df["Open"]

    ema20      = c.ewm(span=20, adjust=False).mean()
    atr_series = AverageTrueRange(high=h, low=l, close=c,
                                  window=ATR_WINDOW).average_true_range()
    rsi_series = RSIIndicator(close=c, window=14).rsi()

    # Ultima candela CHIUSA
    last_close = float(c.iloc[-2])
    last_open  = float(o.iloc[-2])
    last_high  = float(h.iloc[-2])
    last_vol   = float(v.iloc[-2])
    last_rsi   = float(rsi_series.iloc[-2])
    last_atr   = float(atr_series.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    prev_ema20 = float(ema20.iloc[-3])

    if pd.isna(last_rsi) or pd.isna(last_atr) or last_atr <= 0:
        return None

    # 1) Gain dell'ultima candela 1h
    change_1h = (last_close - last_open) / last_open * 100
    if not (CHANGE_1H_MIN <= change_1h <= CHANGE_1H_MAX):
        return None

    # 2) Volume spike
    vol_ref = v.iloc[-23:-2].mean()   # media 20 barre prima dell'ultima chiusa
    if pd.isna(vol_ref) or vol_ref <= 0:
        return None
    vol_spike = last_vol / vol_ref
    if vol_spike < VOL_SPIKE_MIN:
        return None

    # 3) Breakout sopra massimo delle 20 barre precedenti
    high_20 = h.iloc[-23:-2].max()
    if last_close <= high_20:
        return None

    # 4) RSI non esaurito
    if last_rsi >= RSI_MAX_ENTRY:
        return None

    # 5) EMA20 in salita
    if last_ema20 <= prev_ema20:
        return None

    # Prezzo corrente (candela parziale) come approssimazione entry
    entry_price = float(c.iloc[-1])
    if entry_price <= 0:
        return None

    return {
        "entry_price": entry_price,
        "sl_price":    entry_price - SL_ATR_MULT * last_atr,
        "r_dist":      SL_ATR_MULT * last_atr,
        "atr":         last_atr,
        "change_1h":   change_1h,
        "vol_spike":   vol_spike,
        "rsi":         last_rsi,
    }


# ── ORDINI ────────────────────────────────────────────────────────────────────
def set_position_stoploss_long(symbol: str, sl_price: float) -> bool:
    cur = get_last_price(symbol)
    if cur and sl_price >= cur:
        tlog(f"sl_skip:{symbol}",
             f"[SL] {symbol} SL={sl_price:.6f} >= prezzo={cur:.6f}, skip", 300)
        return False
    info     = get_instrument_info(symbol)
    stop_str = format_price_bybit(sl_price, info.get("price_step", 0.01))
    body = {"category": "linear", "symbol": symbol,
            "stopLoss": stop_str, "slTriggerBy": "MarkPrice",
            "positionIdx": LONG_IDX, "tpslMode": "Full"}
    try:
        data = _bybit_signed_post("/v5/position/trading-stop", body).json()
        ret  = data.get("retCode")
        if ret == 0:                    return True
        if ret in (34040, 10001):       return True
        log(f"[SL] {symbol} FAIL retCode={ret} {data.get('retMsg')}")
        return False
    except Exception as e:
        log(f"[SL] {symbol} exc: {e}")
        return False


def place_trailing_stop_long(symbol: str, trailing_dist: float) -> bool:
    body = {"category": "linear", "symbol": symbol,
            "trailingStop": str(trailing_dist), "positionIdx": LONG_IDX}
    try:
        data = _bybit_signed_post("/v5/position/trading-stop", body).json()
        return data.get("retCode") == 0
    except Exception:
        return False


def set_leverage(symbol: str) -> None:
    try:
        _bybit_signed_post("/v5/position/set-leverage", {
            "category": "linear", "symbol": symbol,
            "buyLeverage":  str(DEFAULT_LEVERAGE),
            "sellLeverage": str(DEFAULT_LEVERAGE),
        })
    except Exception:
        pass


def market_long(symbol: str, usdt_amount: float) -> Optional[float]:
    price = get_last_price(symbol)
    if not price:
        return None
    info         = get_instrument_info(symbol)
    qty_step     = float(info.get("qty_step",      0.01))
    min_qty      = float(info.get("min_qty",        qty_step))
    min_notional = float(info.get("min_order_amt",  5.0))
    step_dec     = Decimal(str(qty_step))

    avail        = get_usdt_balance()
    max_notional = avail * DEFAULT_LEVERAGE * MARGIN_USE_PCT
    amount       = min(usdt_amount, max_notional, ORDER_USDT_MAX)

    raw_qty     = Decimal(str(amount)) / Decimal(str(price))
    qty_aligned = (raw_qty // step_dec) * step_dec
    if float(qty_aligned) < min_qty:
        qty_aligned = Decimal(str(min_qty))

    # Tenta limit maker (PostOnly)
    bid = get_bid_price(symbol) or 0.0
    if bid > 0:
        bid_str = format_price_bybit(bid, info.get("price_step", 0.01))
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) > 0:
            body = {"category": "linear", "symbol": symbol,
                    "side": "Buy", "orderType": "Limit",
                    "timeInForce": "PostOnly",
                    "qty": qty_str, "price": bid_str,
                    "positionIdx": LONG_IDX}
            try:
                data = _bybit_signed_post("/v5/order/create", body).json()
                if data.get("retCode") == 0:
                    order_id = data.get("result", {}).get("orderId", "")
                    for _ in range(6):
                        time.sleep(0.5)
                        filled = get_open_long_qty(symbol)
                        if filled and filled > 0:
                            return filled
                    if order_id:
                        try:
                            _bybit_signed_post("/v5/order/cancel",
                                               {"category": "linear",
                                                "symbol": symbol,
                                                "orderId": order_id})
                        except Exception:
                            pass
            except Exception:
                pass

    # Fallback: ordine market
    for _ in range(3):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) <= 0:
            return None
        body = {"category": "linear", "symbol": symbol,
                "side": "Buy", "orderType": "Market",
                "qty": qty_str, "positionIdx": LONG_IDX}
        data = _bybit_signed_post("/v5/order/create", body).json()
        if data.get("retCode") == 0:
            return float(qty_str)
        ret = data.get("retCode")
        if ret == 110007:
            tlog(f"bal_err:{symbol}",
                 f"[LONG] saldo insufficiente per {symbol}", 300)
            break
        if ret == 170137:
            with _instr_lock:
                _instrument_cache.pop(symbol, None)
            info      = get_instrument_info(symbol)
            qty_step  = float(info.get("qty_step", qty_step))
            step_dec  = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            continue
        tlog(f"long_err:{symbol}:{ret}",
             f"[LONG] retCode={ret} {data.get('retMsg')}", 300)
        break
    return None


def market_close_long(symbol: str, qty: float) -> bool:
    info      = get_instrument_info(symbol)
    qty_step  = float(info.get("qty_step", 0.01))
    step_dec  = Decimal(str(qty_step))
    qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec
    qty_str   = _format_qty_with_step(float(qty_aligned), qty_step)
    body = {"category": "linear", "symbol": symbol,
            "side": "Sell", "orderType": "Market",
            "qty": qty_str, "reduceOnly": True, "positionIdx": LONG_IDX}
    try:
        data = _bybit_signed_post("/v5/order/create", body).json()
        return data.get("retCode") == 0
    except Exception:
        return False


# ── TRAILING WORKER ───────────────────────────────────────────────────────────
def trailing_worker() -> None:
    log("[TRAIL] avviato")
    while True:
        try:
            for symbol in list(open_positions):
                entry = get_position(symbol)
                if not entry or entry.get("trailing_active"):
                    continue
                price_now   = get_last_price(symbol)
                if not price_now:
                    continue
                entry_price = float(entry.get("entry_price", 0))
                r_dist      = float(entry.get("r_dist", 0))
                if r_dist <= 0:
                    continue
                if price_now >= entry_price + TRAIL_START_R * r_dist:
                    df = fetch_klines(symbol, interval=INTERVAL_MINUTES, limit=20)
                    atr_val = r_dist / max(1e-9, SL_ATR_MULT)
                    if df is not None and len(df) > ATR_WINDOW + 2:
                        try:
                            atr_val = float(
                                AverageTrueRange(
                                    high=df["High"], low=df["Low"],
                                    close=df["Close"], window=ATR_WINDOW,
                                ).average_true_range().iloc[-1]
                            )
                        except Exception:
                            pass
                    trail_dist = atr_val * TRAIL_ATR_MULT
                    if place_trailing_stop_long(symbol, trail_dist):
                        entry["trailing_active"] = True
                        set_position(symbol, entry)
                        notify_telegram(
                            f"🎯 Trail attivato {symbol}\n"
                            f"Prezzo: {price_now:.4f} | Trail dist: {trail_dist:.6f}"
                        )
        except Exception as e:
            log(f"[TRAIL] exc: {e}")
        time.sleep(TRAIL_SLEEP_SEC)


# ── SL WATCHDOG ───────────────────────────────────────────────────────────────
def sl_watchdog() -> None:
    """Ogni 5 min verifica che ogni posizione LONG aperta abbia lo stop-loss."""
    log("[SL-WATCH] avviato")
    while True:
        time.sleep(SL_WATCH_SLEEP_SEC)
        try:
            resp = _bybit_signed_get("/v5/position/list",
                                     {"category": "linear", "settleCoin": "USDT"})
            data = resp.json()
            if data.get("retCode") != 0:
                continue
            for pos in data.get("result", {}).get("list", []):
                if pos.get("side") != "Buy":
                    continue
                qty = float(pos.get("size", 0) or 0)
                if qty <= 0:
                    continue
                symbol = pos.get("symbol", "")
                sl_val = float(pos.get("stopLoss", 0) or 0)
                if sl_val > 0:
                    continue
                entry = get_position(symbol)
                if not entry:
                    continue
                sl_price = float(entry.get("sl_price", 0))
                if sl_price <= 0:
                    ep       = float(entry.get("entry_price", 0))
                    rd       = float(entry.get("r_dist", ep * 0.04))
                    sl_price = ep - rd
                cur = get_last_price(symbol)
                if cur and sl_price >= cur:
                    sl_price = cur * 0.98
                ok = set_position_stoploss_long(symbol, sl_price)
                if not ok:
                    notify_telegram(
                        f"🚨 SL MANCANTE {symbol} — reimpostazione FALLITA!\n"
                        f"SL target: {sl_price:.4f} — VERIFICA MANUALE"
                    )
        except Exception as e:
            log(f"[SL-WATCH] exc: {e}")


# ── SYNC POSIZIONI ALL'AVVIO ──────────────────────────────────────────────────
def sync_positions_from_wallet() -> None:
    log("[SYNC] Scansione posizioni LONG aperte...")
    try:
        resp     = _bybit_signed_get("/v5/position/list",
                                     {"category": "linear", "settleCoin": "USDT"})
        data     = resp.json()
        pos_list = (data.get("result", {}).get("list", [])
                    if data.get("retCode") == 0 else [])
    except Exception as e:
        log(f"[SYNC] errore: {e}")
        pos_list = []

    trovate = 0
    for pos in pos_list:
        if pos.get("side") != "Buy":
            continue
        qty = float(pos.get("size", 0) or 0)
        if qty <= 0:
            continue
        symbol      = pos["symbol"]
        entry_price = float(pos.get("avgPrice") or pos.get("entryPrice") or 0)
        if entry_price <= 0:
            continue
        df      = fetch_klines(symbol, interval=INTERVAL_MINUTES, limit=20)
        atr_val = entry_price * 0.02
        if df is not None and len(df) > ATR_WINDOW + 2:
            try:
                atr_val = float(
                    AverageTrueRange(
                        high=df["High"], low=df["Low"],
                        close=df["Close"], window=ATR_WINDOW,
                    ).average_true_range().iloc[-1]
                )
            except Exception:
                pass
        r_dist          = atr_val * SL_ATR_MULT
        sl_price        = entry_price - r_dist
        trailing_active = float(pos.get("trailingStop", 0) or 0) > 0
        set_position(symbol, {
            "entry_price":     entry_price,
            "sl_price":        sl_price,
            "r_dist":          r_dist,
            "qty":             qty,
            "entry_time":      time.time(),
            "trailing_active": trailing_active,
        })
        add_open(symbol)
        set_position_stoploss_long(symbol, sl_price)
        trovate += 1
        log(f"[SYNC] LONG: {symbol} qty={qty} entry={entry_price:.4f} "
            f"SL={sl_price:.4f} trail={'SI' if trailing_active else 'NO'}")

    log(f"[SYNC] {trovate} posizioni recuperate")


# ── CICLO PRINCIPALE ──────────────────────────────────────────────────────────
def main_loop() -> None:
    last_scan_ts = 0.0

    while True:
        now = time.time()

        # Aggiorna BTC filter
        _update_btc_filter()

        # ── Controlla chiusure (SL/trail colpiti su Bybit) ────────────────────
        try:
            resp = _bybit_signed_get("/v5/position/list",
                                     {"category": "linear", "settleCoin": "USDT"})
            rdata = resp.json()
            if rdata.get("retCode") == 0:
                live_longs = {
                    p["symbol"]
                    for p in rdata["result"]["list"]
                    if p.get("side") == "Buy" and float(p.get("size", 0) or 0) > 0
                }
                for sym in list(open_positions):
                    if sym not in live_longs:
                        entry = get_position(sym)
                        ep    = float(entry.get("entry_price", 0)) if entry else 0
                        cur   = get_last_price(sym) or 0
                        pnl   = (cur - ep) / ep * 100 if ep else 0
                        log(f"[CLOSE] {sym} chiusa ~{pnl:+.1f}%")
                        notify_telegram(
                            f"📊 Chiusa {sym}\n"
                            f"PnL ~{pnl:+.1f}% | Entry: {ep:.4f} | Uscita ~{cur:.4f}"
                        )
                        discard_open(sym)
                        with _state_lock:
                            position_data.pop(sym, None)
        except Exception as e:
            tlog("pos_check_err", f"[MAIN] check pos exc: {e}", 120)

        # ── Attendi tra scan ──────────────────────────────────────────────────
        if now - last_scan_ts < SCAN_INTERVAL_SEC:
            time.sleep(5)
            continue

        last_scan_ts = now
        n_open = len(open_positions)
        log(f"[SCAN] ─── Avvio scansione ─── open: {n_open}/{MAX_OPEN_POSITIONS}")

        if not _btc_ok:
            log("[SCAN] BTC in crollo — scan saltata")
            tlog("btc_crash_skip", "⚠️ BTC in forte ribasso, scan sospesa", 900)
            continue

        if n_open >= MAX_OPEN_POSITIONS:
            tlog("max_open", f"[SCAN] MAX {MAX_OPEN_POSITIONS} posizioni aperte, attendo", 300)
            continue

        # 1) Top movers da tickers (1 API call)
        candidates = scan_top_movers()
        log(f"[SCAN] {len(candidates)} candidati (vol>{MIN_VOL_24H_USDT/1e6:.0f}M, "
            f"gain24h {CHANGE_24H_MIN:.0f}%-{CHANGE_24H_MAX:.0f}%)")

        if not candidates:
            log("[SCAN] nessun candidato trovato — mercato fermo")
            continue

        # 2) Verifica segnale breakout coin per coin
        entered = 0
        for c in candidates:
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            sym = c["symbol"]
            if sym in open_positions:
                continue

            signal = check_entry_signal(sym)
            if not signal:
                continue

            # 3) Calcola size
            equity    = get_total_equity()
            if equity <= 0:
                continue
            risk_usdt = equity * RISK_PCT
            r_dist    = signal["r_dist"]
            entry_px  = signal["entry_price"]
            # Notional = (risk_usdt / r_dist) * entry_px
            usdt_val  = (risk_usdt / r_dist) * entry_px

            log(f"[ENTRY] {sym} | 24h:{c['change24h']:+.1f}% | "
                f"15m:{signal['change_15m']:+.1f}% | vol:{signal['vol_spike']:.1f}× | "
                f"RSI:{signal['rsi']:.0f} | size:{usdt_val:.1f} USDT")

            # 4) Imposta leva e apri
            set_leverage(sym)
            qty = market_long(sym, usdt_val)
            if not qty or qty <= 0:
                log(f"[ENTRY] {sym} — ordine fallito")
                continue

            # 5) Salva stato e imposta SL
            sl_price = signal["sl_price"]
            set_position(sym, {
                "entry_price":     entry_px,
                "sl_price":        sl_price,
                "r_dist":          r_dist,
                "qty":             qty,
                "entry_time":      time.time(),
                "trailing_active": False,
                "change24h":       c["change24h"],
            })
            add_open(sym)
            time.sleep(0.3)
            set_position_stoploss_long(sym, sl_price)

            notify_telegram(
                f"🚀 ENTRY {sym}\n"
                f"Prezzo: {entry_px:.6f} | SL: {sl_price:.6f}\n"
                f"24h: {c['change24h']:+.1f}% | 15m: {signal['change_15m']:+.1f}% "
                f"| Vol: {signal['vol_spike']:.1f}× | RSI: {signal['rsi']:.0f}\n"
                f"Risk: {risk_usdt:.2f} USDT"
            )
            entered += 1
            time.sleep(0.5)

        log(f"[SCAN] completata — {entered} ingressi | posizioni aperte: {len(open_positions)}")


# ── AVVIO ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 60)
    log("  MOMENTUM BREAKOUT BOT — Dynamic Universe")
    log("=" * 60)
    log(f"  Timeframe : {INTERVAL_MINUTES}min | Scan ogni {SCAN_INTERVAL_SEC}s (intracandle)")
    log(f"  Filtri    : vol24h>{MIN_VOL_24H_USDT/1e6:.0f}M | "
        f"15m_gain={CHANGE_15M_MIN}-{CHANGE_15M_MAX}% | "
        f"vol_spike>{VOL_SPIKE_MIN}× | RSI<{RSI_MAX_ENTRY}")
    log(f"  Risk      : {RISK_PCT*100:.1f}%/trade | MAX={MAX_OPEN_POSITIONS} pos | "
        f"SL={SL_ATR_MULT}×ATR | Trail={TRAIL_ATR_MULT}×ATR@{TRAIL_START_R}R")
    log("=" * 60)

    equity0 = get_total_equity()
    log(f"[AVVIO] Equity: {equity0:.2f} USDT")

    notify_telegram(
        f"🚀 MOMENTUM BOT AVVIATO\n"
        f"Scansiona TUTTI i futures Bybit ogni 15min\n"
        f"Filtri: vol>5M, 1h gain 2.5-8%, vol spike 3×, breakout 20 barre\n"
        f"Risk: {RISK_PCT*100:.1f}% | Max {MAX_OPEN_POSITIONS} posizioni\n"
        f"Equity: {equity0:.2f} USDT"
    )

    sync_positions_from_wallet()
    _update_btc_filter()

    threading.Thread(target=trailing_worker, daemon=True).start()
    threading.Thread(target=sl_watchdog,     daemon=True).start()

    main_loop()
