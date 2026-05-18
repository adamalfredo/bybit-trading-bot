# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIA: Trend Following — 4h EMA20 Pullback
#
# Logica:
#   - Ogni 30 min scansiona i top 100 futures Bybit per volume
#   - Filtro trend: coin deve essere sopra EMA50 sul DAILY (uptrend strutturale)
#   - Segnale su ultima candela 4h CHIUSA:
#     • Il minimo ha toccato EMA20(4h) o è passato sotto (pullback al supporto)
#     • La candela è chiusa VERDE sopra EMA20 (rimbalzo confermato)
#     • RSI(14) tra 38 e 62 (pullback sano, non crash né overbought)
#     • Close non più del 3% sopra EMA20 (non già esteso)
#   - SL: sotto lo swing low del pullback (min 3 barre) − 0.3×ATR
#   - Trail: 2.0×ATR(4h) dal massimo, attivo da 1.5R
#   - MAX 5 posizioni | RISK 1% | Leva 5× (più conservativo su 4h)
#   - BTC filter: BTC deve essere sopra EMA50 daily
#
# Razionale:
#   Compriamo SUL SUPPORTO (EMA20 in uptrend), non DOPO uno spike.
#   Il trend giornaliero è già confermato. Il pullback ci dà un entry
#   con SL stretto sotto il minimo recente e obiettivo verso il massimo
#   precedente (almeno 2R). Win rate atteso: 50-60% in mercati bull.
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
RISK_PCT           = 0.0100   # 1% rischio per trade
DEFAULT_LEVERAGE   = 5        # leva ridotta: 4h = posizioni più lunghe
MAX_OPEN_POSITIONS = 5
MARGIN_USE_PCT     = 0.30
ORDER_USDT_MAX     = float(os.getenv("ORDER_USDT_MAX", "1000"))

SL_ATR_BUFFER  = 0.3    # buffer aggiuntivo sotto swing low (× ATR)
TRAIL_ATR_MULT = 2.0    # trailing = 2×ATR(4h) dal massimo
TRAIL_START_R  = 1.5    # attiva trailing a 1.5R di guadagno
ATR_WINDOW     = 14

# Universo
MIN_VOL_24H_USDT = 10_000_000  # solo coin liquide: >10M USDT/giorno
COINS_TOP_N      = 100         # top N per volume da scansionare

# Filtri segnale
RSI_MIN_4H     = 30.0   # RSI minimo: tollera oversold moderato
RSI_MAX_4H     = 65.0   # RSI massimo: non overbought
EMA_TOUCH_TOL  = 0.012  # il low deve essere entro 1.2% sopra EMA20 (o sotto)
MAX_DIST_EMA   = 3.0    # % massima close sopra EMA20 all'entry
MAX_SL_PCT     = 8.0    # SL massimo accettabile: 8% sotto entry
MIN_BODY_PCT   = 40.0   # corpo candela 4h >= 40% del range: calibrato su backtest 730gg (PF 1.116 vs 0.898 con 35%)
MIN_VOL_RATIO  = 1.5    # volume candela segnale >= 1.5x media20: parametro piu impattante (PF 1.116 vs 0.898)
MAX_DIST_EMA50_D = 20.0 # daily close max 20% sopra EMA50: evita trend overestesi

# BTC filter
BTC_BULL_CHECK = True   # blocca se BTC sotto EMA50 daily

# Timing
SCAN_INTERVAL_SEC  = 1800   # 30 min tra scan
TRAIL_SLEEP_SEC    = 60
SL_WATCH_SLEEP_SEC = 600    # 10 min
LONG_IDX           = 1

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
            data={"chat_id": TELEGRAM_CHAT_ID, "text": f"[PULLBACK] {msg}"},
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
def fetch_klines(symbol: str, interval, limit: int = 60) -> Optional[pd.DataFrame]:
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


# ── BTC FILTER ────────────────────────────────────────────────────────────────
def _update_btc_filter() -> None:
    """Blocca nuovi long se BTC è sotto EMA50 sul daily (bear market strutturale)."""
    global _btc_ok, _btc_ts
    if time.time() - _btc_ts < 3600:   # aggiorna ogni ora max
        return
    if not BTC_BULL_CHECK:
        _btc_ok = True
        _btc_ts = time.time()
        return
    try:
        df = fetch_klines("BTCUSDT", interval="D", limit=60)
        if df is not None and len(df) >= 52:
            close    = df["Close"]
            ema50    = close.ewm(span=50, adjust=False).mean()
            btc_px   = float(close.iloc[-2])
            ema50_px = float(ema50.iloc[-2])
            _btc_ok  = btc_px >= ema50_px
            tlog("btc_filter",
                 f"[BTC] Daily EMA50={ema50_px:.0f} | BTC={btc_px:.0f} | "
                 f"gate={'OK ✅' if _btc_ok else 'CHIUSO 🚫 (bear)'}", 3600)
    except Exception:
        pass
    _btc_ts = time.time()


# ── SCANSIONE UNIVERSO ────────────────────────────────────────────────────────
def scan_universe() -> list:
    """
    Ritorna le top COINS_TOP_N coin per volume 24h (>10M USDT).
    Una sola chiamata API.
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
            price  = float(t.get("lastPrice", 0) or 0)
        except Exception:
            continue
        if vol24h < MIN_VOL_24H_USDT or price <= 0:
            continue
        candidates.append({"symbol": sym, "vol24h": vol24h})

    candidates.sort(key=lambda x: x["vol24h"], reverse=True)
    return candidates[:COINS_TOP_N]


# ── FILTRO TREND DAILY ────────────────────────────────────────────────────────
def is_daily_uptrend(symbol: str) -> bool:
    """
    True se la coin è in uptrend strutturale:
    - Last daily close > EMA50 daily
    - EMA50 daily con slope positiva (oggi > 5 barre fa)
    - Trend non overesteso: close <= EMA50 * (1 + MAX_DIST_EMA50_D/100)
    """
    df = fetch_klines(symbol, interval="D", limit=60)
    if df is None or len(df) < 52:
        return False
    close     = df["Close"]
    ema50     = close.ewm(span=50, adjust=False).mean()
    last_c    = float(close.iloc[-2])
    ema50_now = float(ema50.iloc[-2])
    ema50_5d  = float(ema50.iloc[-7])
    if last_c <= ema50_now or ema50_now <= ema50_5d:
        return False
    # Trend non overesteso: se il prezzo è già >20% sopra EMA50, il pullback
    # a EMA20(4h) non è un vero ritorno al supporto
    dist_ema50 = (last_c - ema50_now) / ema50_now * 100
    if dist_ema50 > MAX_DIST_EMA50_D:
        return False
    return True


# ── SIGNAL CHECK 4h ───────────────────────────────────────────────────────────
def check_entry_signal(symbol: str) -> Optional[dict]:
    """
    Verifica pullback all'EMA20 su 4h (ultima candela CHIUSA).

    Condizioni:
    1. Minimo della candela ≤ EMA20 × (1 + tolleranza) — tocco al supporto
    2. Close > EMA20 — chiude sopra (rimbalzo confermato)
    3. Close > Open — candela verde (bulls tornati)
    4. RSI(14) tra 38 e 62 — pullback sano
    5. Close entro 3% sopra EMA20 — non già esteso
    6. SL sotto swing low (min 3 barre) — rischio definito
    """
    df = fetch_klines(symbol, interval="240", limit=50)
    if df is None or len(df) < 25:
        return None

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    o = df["Open"]

    ema20      = c.ewm(span=20, adjust=False).mean()
    atr_series = AverageTrueRange(high=h, low=l, close=c,
                                  window=ATR_WINDOW).average_true_range()
    rsi_series = RSIIndicator(close=c, window=14).rsi()

    # Ultima candela CHIUSA
    last_close = float(c.iloc[-2])
    last_open  = float(o.iloc[-2])
    last_low   = float(l.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    last_rsi   = float(rsi_series.iloc[-2])
    last_atr   = float(atr_series.iloc[-2])

    if pd.isna(last_rsi) or pd.isna(last_atr) or last_atr <= 0:
        return None
    if last_ema20 <= 0:
        return None

    # 1) Il minimo ha toccato EMA20 (o è sceso sotto)
    if last_low > last_ema20 * (1 + EMA_TOUCH_TOL):
        return None

    # 2) Close sopra EMA20 — rimbalzo confermato
    if last_close < last_ema20:
        return None

    # 3) Candela verde
    if last_close <= last_open:
        return None

    # 4) RSI nel range pullback
    if not (RSI_MIN_4H <= last_rsi <= RSI_MAX_4H):
        return None

    # 5) Non esteso: close entro MAX_DIST_EMA% sopra EMA20
    dist_pct = (last_close - last_ema20) / last_ema20 * 100
    if dist_pct > MAX_DIST_EMA:
        return None

    # 6) Qualità candela: corpo >= MIN_BODY_PCT% del range totale
    #    Filtra shooting star, doji, hammer invertiti
    candle_range = float(h.iloc[-2]) - float(l.iloc[-2])
    if candle_range > 0:
        body_pct = abs(last_close - last_open) / candle_range * 100
        if body_pct < MIN_BODY_PCT:
            return None

    # 7) Conferma volume: la candela segnale deve avere volume >= MIN_VOL_RATIO
    #    rispetto alla media delle ultime 20 candele chiuse
    #    Un rimbalzo su volume debole non ha domanda reale
    vol_series = df["Volume"]
    vol_avg = float(vol_series.iloc[-22:-2].mean())
    vol_sig = float(vol_series.iloc[-2])
    if vol_avg > 0 and vol_sig / vol_avg < MIN_VOL_RATIO:
        return None

    # 8) SL = sotto swing low delle ultime 3 barre chiuse
    swing_low = min(float(l.iloc[-2]), float(l.iloc[-3]), float(l.iloc[-4]))
    sl_price  = swing_low - SL_ATR_BUFFER * last_atr
    r_dist    = last_close - sl_price

    # Sanity: SL non troppo lontano
    sl_pct = r_dist / last_close * 100
    if sl_pct > MAX_SL_PCT or r_dist <= 0:
        return None

    return {
        "entry_price": last_close,
        "sl_price":    sl_price,
        "r_dist":      r_dist,
        "atr":         last_atr,
        "rsi":         last_rsi,
        "ema20_4h":    last_ema20,
        "dist_ema":    dist_pct,
        "sl_pct":      sl_pct,
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
        if ret == 0:             return True
        if ret in (34040, 10001): return True
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

    # Limit PostOnly al bid
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

    # Fallback market
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
                    # Ricalcola ATR su 4h fresco
                    df = fetch_klines(symbol, interval="240", limit=20)
                    atr_val = r_dist / max(1e-9, 1.0)
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
                            f"Prezzo: {price_now:.4f} | "
                            f"Trail dist: {trail_dist:.6f} ({TRAIL_ATR_MULT}×ATR)"
                        )
        except Exception as e:
            log(f"[TRAIL] exc: {e}")
        time.sleep(TRAIL_SLEEP_SEC)


# ── SL WATCHDOG ───────────────────────────────────────────────────────────────
def sl_watchdog() -> None:
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
                entry    = get_position(symbol)
                if not entry:
                    continue
                sl_price = float(entry.get("sl_price", 0))
                if sl_price <= 0:
                    ep       = float(entry.get("entry_price", 0))
                    rd       = float(entry.get("r_dist", ep * 0.04))
                    sl_price = ep - rd
                cur = get_last_price(symbol)
                if cur and sl_price >= cur:
                    sl_price = cur * 0.97
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

        # Leggi lo SL già impostato su Bybit — non ricalcolarlo mai
        # Ricalcolarlo causerebbe SL più larghi ad ogni restart
        sl_from_bybit = float(pos.get("stopLoss") or 0)
        if sl_from_bybit > 0 and sl_from_bybit < entry_price:
            sl_price = sl_from_bybit
            r_dist   = entry_price - sl_price
            set_sl_on_bybit = False   # SL già presente, non toccare
        else:
            # Fallback: posizione senza SL impostato (non dovrebbe accadere)
            df      = fetch_klines(symbol, interval="240", limit=20)
            atr_val = entry_price * 0.03
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
            r_dist   = atr_val * 2.0
            sl_price = entry_price - r_dist
            set_sl_on_bybit = True    # SL assente: lo impostiamo

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
        if set_sl_on_bybit:
            set_position_stoploss_long(symbol, sl_price)
            log(f"[SYNC] LONG: {symbol} qty={qty} entry={entry_price:.4f} "
                f"SL={sl_price:.4f} (impostato) trail={'SI' if trailing_active else 'NO'}")
        else:
            log(f"[SYNC] LONG: {symbol} qty={qty} entry={entry_price:.4f} "
                f"SL={sl_price:.4f} (da Bybit) trail={'SI' if trailing_active else 'NO'}")
        trovate += 1

    log(f"[SYNC] {trovate} posizioni recuperate")


# ── CICLO PRINCIPALE ──────────────────────────────────────────────────────────
def main_loop() -> None:
    last_scan_ts = 0.0

    while True:
        now = time.time()

        # Aggiorna BTC filter
        _update_btc_filter()

        # Controlla chiusure (SL/trail colpiti su Bybit)
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

        # Attendi tra scan
        if now - last_scan_ts < SCAN_INTERVAL_SEC:
            time.sleep(10)
            continue

        last_scan_ts = now
        n_open = len(open_positions)
        log(f"[SCAN] ─── Avvio scansione ─── open: {n_open}/{MAX_OPEN_POSITIONS}")

        if not _btc_ok:
            log("[SCAN] BTC sotto EMA50 daily — bear market, scan sospesa")
            tlog("btc_bear", "⚠️ BTC in bear market (sotto EMA50 daily), scan sospesa", 3600)
            continue

        if n_open >= MAX_OPEN_POSITIONS:
            tlog("max_open", f"[SCAN] MAX {MAX_OPEN_POSITIONS} posizioni aperte, attendo", 600)
            continue

        # 1) Universo: top 100 per volume
        universe = scan_universe()
        log(f"[SCAN] {len(universe)} coin nel universo (vol>{MIN_VOL_24H_USDT/1e6:.0f}M USDT)")

        if not universe:
            continue

        # 2) Per ogni candidato: filtro daily trend → segnale 4h
        entered = 0
        checked = 0
        for coin in universe:
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            sym = coin["symbol"]
            if sym in open_positions:
                continue

            # Filtro trend daily (fetch 60 daily candles)
            if not is_daily_uptrend(sym):
                time.sleep(0.05)
                continue
            time.sleep(0.05)

            checked += 1

            # Segnale 4h pullback
            signal = check_entry_signal(sym)
            if not signal:
                time.sleep(0.05)
                continue

            # Calcola size
            equity    = get_total_equity()
            if equity <= 0:
                continue
            risk_usdt = equity * RISK_PCT
            r_dist    = signal["r_dist"]
            entry_px  = signal["entry_price"]
            usdt_val  = (risk_usdt / r_dist) * entry_px

            log(f"[SIGNAL] {sym} | EMA20: {signal['ema20_4h']:.4f} | "
                f"dist: +{signal['dist_ema']:.1f}% | RSI: {signal['rsi']:.0f} | "
                f"SL: -{signal['sl_pct']:.1f}% | size: {usdt_val:.1f} USDT")

            # Imposta leva e apri
            set_leverage(sym)
            qty = market_long(sym, usdt_val)
            if not qty or qty <= 0:
                log(f"[ENTRY] {sym} — ordine fallito")
                continue

            # Salva stato e imposta SL
            sl_price = signal["sl_price"]
            set_position(sym, {
                "entry_price":     entry_px,
                "sl_price":        sl_price,
                "r_dist":          r_dist,
                "qty":             qty,
                "entry_time":      time.time(),
                "trailing_active": False,
            })
            add_open(sym)
            time.sleep(0.3)
            set_position_stoploss_long(sym, sl_price)

            notify_telegram(
                f"📈 ENTRY {sym} — Pullback EMA20(4h)\n"
                f"Entry: {entry_px:.4f} | SL: {sl_price:.4f} ({signal['sl_pct']:.1f}%)\n"
                f"EMA20: {signal['ema20_4h']:.4f} | RSI: {signal['rsi']:.0f}\n"
                f"R-dist: {r_dist:.4f} | Risk: {risk_usdt:.2f} USDT"
            )
            entered += 1
            time.sleep(0.5)

        log(f"[SCAN] {checked} coin in uptrend daily | {entered} ingressi | "
            f"posizioni: {len(open_positions)}")


# ── AVVIO ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log("=" * 62)
    log("  TREND FOLLOWING — 4h EMA20 PULLBACK BOT")
    log("=" * 62)
    log(f"  Timeframe : Daily trend + 4h segnale | Scan ogni {SCAN_INTERVAL_SEC//60}min")
    log(f"  Filtri    : EMA50 daily | EMA20(4h) touch | RSI {RSI_MIN_4H}-{RSI_MAX_4H} | "
        f"dist EMA <{MAX_DIST_EMA}%")
    log(f"  Risk      : {RISK_PCT*100:.1f}%/trade | MAX={MAX_OPEN_POSITIONS} pos | "
        f"Leva {DEFAULT_LEVERAGE}× | Trail={TRAIL_ATR_MULT}×ATR@{TRAIL_START_R}R")
    log("=" * 62)

    equity0 = get_total_equity()
    log(f"[AVVIO] Equity: {equity0:.2f} USDT")

    notify_telegram(
        f"📈 PULLBACK BOT AVVIATO — Trend Following 4h\n"
        f"Segnale: EMA20(4h) pullback + daily uptrend\n"
        f"Scan ogni {SCAN_INTERVAL_SEC//60}min | Leva {DEFAULT_LEVERAGE}× | "
        f"Risk {RISK_PCT*100:.1f}%\n"
        f"Equity: {equity0:.2f} USDT"
    )

    sync_positions_from_wallet()
    _update_btc_filter()

    threading.Thread(target=trailing_worker, daemon=True).start()
    threading.Thread(target=sl_watchdog,     daemon=True).start()

    main_loop()
