# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIA: EMA20-Pullback 4h — Walk-Forward Run H
# TRAIN 2024-01-01→2025-06-30 | TEST 2025-07-01→2026-05-13
# Test: 77 trade, WR 57.1%, Ratio 1.68x, Exp +0.3655R
# Parametri: SL=2×ATR, Trail=3×ATR@1.0R, RSI<50, Vol>1.2×MA, EMA20>EMA50+slope
#
# RISCRITTURA COMPLETA — nessun filtro extra rispetto al backtest.
# Rimossi: F&G gate, dump guard, daily regime BTC, ALT worker,
#           BE lock automatico, ratchet ROI, weekly DD, TP parziale,
#           strength score, cooldown post-loss, blacklist.
# Mantenuti (infrastruttura tecnica necessaria):
#   - BTC gate EMA50 4h (era nel backtest)
#   - MAX_OPEN_POSITIONS=4 (era nel backtest)
#   - Daily DD cap 5% (sicurezza emergenza, non blocca posizioni aperte)
#   - SL watchdog (sicurezza operativa)
#   - Trailing worker (attiva @1.0R, distanza 3×ATR — esatto backtest)
# ─────────────────────────────────────────────────────────────────────────────

import os
import time
import hmac
import hashlib
import json
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from typing import Optional

import requests
import pandas as pd
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# ── ENV VARS ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
KEY    = os.getenv("BYBIT_API_KEY", "")
SECRET = os.getenv("BYBIT_API_SECRET", "")
BYBIT_TESTNET      = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL     = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()

# ── PARAMETRI BACKTEST (Run H) ────────────────────────────────────────────────
INTERVAL_MINUTES   = 240          # 4h
ATR_WINDOW         = 14
SL_ATR_MULT        = float(os.getenv("SL_ATR_MULT",    "2.0"))
TRAIL_ATR_MULT     = float(os.getenv("TRAIL_ATR_MULT", "3.0"))
TRAIL_START_R      = float(os.getenv("TRAIL_START_R",  "1.0"))
RISK_PCT           = float(os.getenv("RISK_PCT",       "0.0075"))  # 0.75%
MAX_OPEN_POSITIONS = 4
DEFAULT_LEVERAGE   = 10
MARGIN_USE_PCT     = 0.35
ORDER_USDT_MAX     = float(os.getenv("ORDER_USDT_MAX", "1000"))

# ── SICUREZZA OPERATIVA (non nel backtest, indispensabili in live) ────────────
DAILY_DD_CAP_PCT = 0.05   # blocca nuovi LONG se equity cala >5% dall'apertura giornata
FEES_TAKER_PCT   = 0.0006

# ── COSTANTI BYBIT ────────────────────────────────────────────────────────────
LONG_IDX  = 1
SHORT_IDX = 2

# ── LISTA COIN FISSA (Run H — 61 coin validate) ──────────────────────────────
COINS_V2 = [
    "BTCUSDT",  "ETHUSDT",  "SOLUSDT",  "XRPUSDT",  "ADAUSDT",
    "DOTUSDT",  "AVAXUSDT", "LINKUSDT", "UNIUSDT",  "NEARUSDT",
    "TONUSDT",  "SUIUSDT",  "TAOUSDT",  "ONDOUSDT", "ENAUSDT",
    "1000PEPEUSDT", "AAVEUSDT", "LDOUSDT", "COMPUSDT",
    "WLDUSDT",  "INJUSDT",  "TIAUSDT",  "SEIUSDT",
    "OPUSDT",   "ARBUSDT",  "STXUSDT",  "APTUSDT",
    "RENDERUSDT", "JUPUSDT", "PYTHUSDT", "WIFUSDT",  "BOMEUSDT",
    "NOTUSDT",  "EIGENUSDT","POLUSDT",  "HYPEUSDT", "MOVEUSDT",
    "LTCUSDT",  "ATOMUSDT", "XLMUSDT",  "VETUSDT",  "FILUSDT",
    "ICPUSDT",  "RUNEUSDT", "ARUSDT",   "CRVUSDT",  "SANDUSDT",
    "MANAUSDT", "AXSUSDT",  "GALAUSDT", "SNXUSDT",  "GRTUSDT",
    "APEUSDT",  "GMTUSDT",  "ORDIUSDT", "1000BONKUSDT", "CFXUSDT",
    "KASUSDT",  "PENDLEUSDT","BLURUSDT", "FLUXUSDT",
]

# ── STATO GLOBALE ─────────────────────────────────────────────────────────────
ASSETS: list           = []
open_positions: set    = set()
position_data: dict    = {}
_state_lock            = threading.RLock()
_instr_lock            = threading.RLock()
_instrument_cache: dict = {}
_price_cache: dict     = {}
_price_lock            = threading.RLock()
_last_log_times: dict  = {}

# BTC gate: True se BTC 4h sopra EMA50 (era nel backtest)
_btc_favorable_long: bool = True   # default True per non bloccare avvio
_btc_prev_favorable: bool  = True   # per rilevare cambio regime
_btc_ctx_ts: float         = 0.0

# Daily DD
_daily_start_equity: Optional[float] = None
_main_loop_last_day: str = ""

# ── HTTP SESSION ──────────────────────────────────────────────────────────────
SESSION = requests.Session()
_retry = Retry(
    total=3, backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
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
            data={"chat_id": TELEGRAM_CHAT_ID, "text": f"[LONG] {msg}"},
            timeout=10,
        )
    except Exception as e:
        log(f"[TELEGRAM] Errore: {e}")

# ── FIRMA BYBIT ───────────────────────────────────────────────────────────────
def _bybit_signed_get(path: str, params: dict):
    from urllib.parse import urlencode
    qs = urlencode(sorted(params.items()))
    ts = str(int(time.time() * 1000))
    rw = "10000"
    sign = hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{qs}".encode(),
                    hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw,
    }
    return SESSION.get(f"{BYBIT_BASE_URL}{path}",
                       headers=headers, params=params, timeout=10)


def _bybit_signed_post(path: str, body: dict):
    ts = str(int(time.time() * 1000))
    rw = "10000"
    body_json = json.dumps(body, separators=(",", ":"))
    sign = hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{body_json}".encode(),
                    hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw,
        "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json",
    }
    return SESSION.post(f"{BYBIT_BASE_URL}{path}",
                        headers=headers, data=body_json, timeout=10)

# ── HELPERS ATOMICI STATO ─────────────────────────────────────────────────────
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

# ── STRUMENTI BYBIT ───────────────────────────────────────────────────────────
def get_instrument_info(symbol: str) -> dict:
    now = time.time()
    with _instr_lock:
        cached = _instrument_cache.get(symbol)
        if cached and now - cached["ts"] < 300:
            return cached["data"]
    _fallback = {
        "min_qty": 0.01, "qty_step": 0.01,
        "precision": 4, "price_step": 0.01, "min_order_amt": 5.0,
    }
    try:
        resp = SESSION.get(
            f"{BYBIT_BASE_URL}/v5/market/instruments-info",
            params={"category": "linear", "symbol": symbol}, timeout=10,
        )
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return _fallback
        info = data["result"]["list"][0]
        lot  = info.get("lotSizeFilter", {})
        pf   = info.get("priceFilter", {})
        parsed = {
            "min_qty":       float(lot.get("minOrderQty",      0.01) or 0.01),
            "qty_step":      float(lot.get("qtyStep",         "0.01") or "0.01"),
            "precision":     int(info.get("priceScale",          4)  or 4),
            "price_step":    float(pf.get("tickSize",         "0.01") or "0.01"),
            "min_order_amt": float(lot.get("minNotionalValue", "5")   or "5"),
        }
        with _instr_lock:
            _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed
    except Exception:
        return _fallback


def format_price_bybit(price: float, tick_size: float) -> str:
    step    = Decimal(str(tick_size))
    p       = Decimal(str(price))
    floored = (p // step) * step
    dec = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    return f"{floored:.{dec}f}"


def _format_qty_with_step(qty: float, step: float) -> str:
    step_dec = Decimal(str(step))
    q        = Decimal(str(qty))
    floored  = (q // step_dec) * step_dec
    sd = -step_dec.as_tuple().exponent if step_dec.as_tuple().exponent < 0 else 0
    pattern = Decimal("1." + "0" * sd) if sd > 0 else Decimal("1")
    floored = floored.quantize(pattern, rounding=ROUND_DOWN)
    return f"{floored:.{sd}f}" if sd > 0 else str(int(floored))

# ── PREZZO ────────────────────────────────────────────────────────────────────
def get_last_price(symbol: str) -> Optional[float]:
    now = time.time()
    with _price_lock:
        c = _price_cache.get(symbol)
        if c and now - c["ts"] <= 2:
            return c["price"]
    try:
        resp = SESSION.get(
            f"{BYBIT_BASE_URL}/v5/market/tickers",
            params={"category": "linear", "symbol": symbol}, timeout=10,
        )
        data = resp.json()
        if data.get("retCode") == 0:
            item  = data["result"]["list"][0]
            price = float(item["lastPrice"])
            bid1  = float(item.get("bid1Price") or price)
            ask1  = float(item.get("ask1Price") or price)
            with _price_lock:
                _price_cache[symbol] = {
                    "price": price, "bid1": bid1, "ask1": ask1, "ts": now
                }
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

# ── STORICO KLINES ────────────────────────────────────────────────────────────
def fetch_history(symbol: str, interval: int = INTERVAL_MINUTES,
                  limit: int = 400) -> Optional[pd.DataFrame]:
    try:
        resp = SESSION.get(
            f"{BYBIT_BASE_URL}/v5/market/kline",
            params={"category": "linear", "symbol": symbol,
                    "interval": str(interval), "limit": limit}, timeout=10,
        )
        data = resp.json()
        if data.get("retCode") == 10006:
            time.sleep(1.2)
            return None
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return None
        klines = list(reversed(data["result"]["list"]))   # oldest-first
        df = pd.DataFrame(
            klines,
            columns=["timestamp", "Open", "High", "Low", "Close", "Volume", "Turnover"],
        )
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


def get_open_short_qty(symbol: str) -> float:
    try:
        resp = _bybit_signed_get("/v5/position/list",
                                 {"category": "linear", "symbol": symbol})
        data = resp.json()
        if data.get("retCode") != 0:
            return 0.0
        for pos in data.get("result", {}).get("list", []):
            if pos.get("side") == "Sell":
                return float(pos.get("size", 0) or 0)
    except Exception:
        pass
    return 0.0

# ── ORDINI ────────────────────────────────────────────────────────────────────
def set_position_stoploss_long(symbol: str, sl_price: float) -> bool:
    cur = get_last_price(symbol)
    if cur and sl_price >= cur:
        tlog(f"sl_skip:{symbol}",
             f"[POS-SL] {symbol} SL={sl_price:.6f} >= prezzo={cur:.6f}, skip", 300)
        return False
    info     = get_instrument_info(symbol)
    stop_str = format_price_bybit(sl_price, info.get("price_step", 0.01))
    body = {
        "category": "linear", "symbol": symbol,
        "stopLoss": stop_str, "slTriggerBy": "MarkPrice",
        "positionIdx": LONG_IDX, "tpslMode": "Full",
    }
    try:
        data = _bybit_signed_post("/v5/position/trading-stop", body).json()
        ret  = data.get("retCode")
        if ret == 0:
            return True
        if ret == 34040:
            return True   # già impostato
        if ret == 10001 and "zero" in (data.get("retMsg") or "").lower():
            return True   # posizione già chiusa
        log(f"[POS-SL] {symbol} FAIL retCode={ret} {data.get('retMsg')}")
        return False
    except Exception as e:
        log(f"[POS-SL] {symbol} exc: {e}")
        return False


def place_trailing_stop_long(symbol: str, trailing_dist: float) -> bool:
    body = {
        "category": "linear", "symbol": symbol,
        "trailingStop": str(trailing_dist), "positionIdx": LONG_IDX,
    }
    try:
        data = _bybit_signed_post("/v5/position/trading-stop", body).json()
        ok   = data.get("retCode") == 0
        tlog(f"trail:{symbol}",
             f"[TRAIL] {symbol} dist={trailing_dist:.6f} ok={ok}", 30)
        return ok
    except Exception:
        return False


def cancel_all_orders(symbol: str) -> bool:
    try:
        resp = _bybit_signed_post("/v5/order/cancel-all",
                                  {"category": "linear", "symbol": symbol})
        return resp.json().get("retCode") == 0
    except Exception:
        return False


def _try_limit_entry(symbol: str, qty_str: str, bid_str: str) -> Optional[float]:
    body = {
        "category": "linear", "symbol": symbol,
        "side": "Buy", "orderType": "Limit", "timeInForce": "PostOnly",
        "qty": qty_str, "price": bid_str, "positionIdx": LONG_IDX,
    }
    try:
        data = _bybit_signed_post("/v5/order/create", body).json()
        if data.get("retCode") != 0:
            return None
        order_id = data.get("result", {}).get("orderId", "")
        for _ in range(6):
            time.sleep(0.5)
            filled = get_open_long_qty(symbol)
            if filled and filled > 0:
                return filled
        if order_id:
            try:
                _bybit_signed_post("/v5/order/cancel",
                                   {"category": "linear", "symbol": symbol,
                                    "orderId": order_id})
            except Exception:
                pass
    except Exception:
        pass
    return None


def market_long(symbol: str, usdt_amount: float) -> Optional[float]:
    price = get_last_price(symbol)
    if not price:
        return None
    info         = get_instrument_info(symbol)
    qty_step     = float(info.get("qty_step", 0.01))
    min_qty      = float(info.get("min_qty", qty_step))
    min_notional = float(info.get("min_order_amt", 5.0))
    step_dec     = Decimal(str(qty_step))

    avail        = get_usdt_balance()
    max_notional = avail * DEFAULT_LEVERAGE * MARGIN_USE_PCT
    amount       = min(usdt_amount, max_notional, ORDER_USDT_MAX)

    raw_qty     = Decimal(str(amount)) / Decimal(str(price))
    qty_aligned = (raw_qty // step_dec) * step_dec
    if float(qty_aligned) < min_qty:
        qty_aligned = Decimal(str(min_qty))

    # Tentativo Limit maker
    bid = get_bid_price(symbol) or 0.0
    if bid > 0:
        bid_str = format_price_bybit(bid, info.get("price_step", 0.01))
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) > 0:
            filled = _try_limit_entry(symbol, qty_str, bid_str)
            if filled:
                return filled

    # Fallback Market
    for _ in range(3):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) <= 0:
            return None
        body = {
            "category": "linear", "symbol": symbol,
            "side": "Buy", "orderType": "Market",
            "qty": qty_str, "positionIdx": LONG_IDX,
        }
        data = _bybit_signed_post("/v5/order/create", body).json()
        if data.get("retCode") == 0:
            return float(qty_str)
        ret = data.get("retCode")
        if ret == 170137:
            with _instr_lock:
                _instrument_cache.pop(symbol, None)
            info      = get_instrument_info(symbol)
            qty_step  = float(info.get("qty_step", qty_step))
            step_dec  = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            continue
        if ret == 170140:
            needed = max(Decimal(str(min_qty)),
                         Decimal(str(min_notional)) / Decimal(str(price)))
            qty_aligned = ((needed // step_dec) + 1) * step_dec
            continue
        if ret == 110007:
            tlog(f"bal_err:{symbol}", f"[LONG] saldo insufficiente per {symbol}", 300)
            break
        tlog(f"long_err:{symbol}:{ret}",
             f"[LONG] errore non gestito retCode={ret} {data.get('retMsg')}", 300)
        break
    return None


def market_close_long(symbol: str, qty: float) -> bool:
    info      = get_instrument_info(symbol)
    qty_step  = float(info.get("qty_step", 0.01))
    step_dec  = Decimal(str(qty_step))
    qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec
    for _ in range(3):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        body = {
            "category": "linear", "symbol": symbol,
            "side": "Sell", "orderType": "Market",
            "qty": qty_str, "reduceOnly": True, "positionIdx": LONG_IDX,
        }
        data = _bybit_signed_post("/v5/order/create", body).json()
        if data.get("retCode") == 0:
            return True
        tlog(f"close_err:{symbol}",
             f"[CLOSE-LONG] {symbol} retCode={data.get('retCode')}", 300)
        break
    return False

# ── BTC GATE — EMA50 4h (era nel backtest) ────────────────────────────────────
def _update_btc_context() -> None:
    global _btc_favorable_long, _btc_prev_favorable, _btc_ctx_ts
    if time.time() - _btc_ctx_ts < 180:
        return
    try:
        resp = SESSION.get(
            f"{BYBIT_BASE_URL}/v5/market/kline",
            params={"category": "linear", "symbol": "BTCUSDT",
                    "interval": "240", "limit": 60}, timeout=10,
        )
        data = resp.json()
        if data.get("retCode") == 0 and data["result"]["list"]:
            raw    = list(reversed(data["result"]["list"]))
            closes = pd.Series([float(r[4]) for r in raw])
            ema50  = closes.ewm(span=50, adjust=False).mean().iloc[-1]
            new_val = float(closes.iloc[-1]) > ema50
            if new_val != _btc_prev_favorable:
                if new_val:
                    notify_telegram(
                        f"🟢 REGIME LONG — BTC 4h sopra EMA50\n"
                        f"Gate APERTO: il bot può aprire LONG"
                    )
                else:
                    notify_telegram(
                        f"🔴 REGIME BEAR — BTC 4h sotto EMA50\n"
                        f"Gate CHIUSO: nessun nuovo LONG fino al recovery"
                    )
                _btc_prev_favorable = new_val
            _btc_favorable_long = new_val
    except Exception:
        pass
    _btc_ctx_ts = time.time()
    tlog("btc_ctx", f"[BTC-CTX] btc_favorable={_btc_favorable_long}", 180)

# ── UPDATE ASSETS ─────────────────────────────────────────────────────────────
def update_assets() -> None:
    global ASSETS
    ASSETS = list(COINS_V2)
    tlog("assets_v2", f"[ASSETS-V2] {len(ASSETS)} coin fissi (Run H)", 600)

# ── ANALYZE ASSET — logica backtest pura ─────────────────────────────────────
def analyze_asset(symbol: str):
    """
    EMA20-Pullback 4h — 6 condizioni esatte del backtest Run H.
    Ritorna ("entry", "EMA20-Pullback-4h", price) oppure (None, None, None).
    """
    try:
        if not _btc_favorable_long:
            return None, None, None

        df = fetch_history(symbol, interval=240, limit=220)
        if df is None or len(df) < 55:
            return None, None, None

        c = df["Close"]
        h = df["High"]
        l = df["Low"]
        v = df["Volume"]

        df["ema20"]       = c.ewm(span=20, adjust=False).mean()
        df["ema50"]       = c.ewm(span=50, adjust=False).mean()
        df["ema20_slope"] = df["ema20"].diff(3)
        df["atr"]         = AverageTrueRange(
            high=h, low=l, close=c, window=14).average_true_range()
        df["rsi"]         = RSIIndicator(close=c, window=14).rsi()
        df["vol_ma"]      = v.rolling(20).mean()

        df.dropna(inplace=True)
        if len(df) < 3:
            return None, None, None

        prev = df.iloc[-2]   # candela appena chiusa
        row  = df.iloc[-1]   # candela corrente

        atr   = float(prev["atr"])
        ema20 = float(prev["ema20"])
        if atr <= 0:
            return None, None, None

        # Sei condizioni — esatta replica del backtest
        near_ema20  = float(row["Close"]) <= ema20 + 2.0 * atr
        above_floor = float(row["Close"]) >= ema20 - 2.0 * atr
        rsi_ok      = float(row["rsi"]) < 50.0
        vol_ok      = float(row["Volume"]) > 1.2 * float(prev["vol_ma"])
        micro_up    = (float(prev["ema20"]) > float(prev["ema50"])
                       and float(prev["ema20_slope"]) > 0)
        bull_candle = float(row["Close"]) > float(row["Open"])

        if near_ema20 and above_floor and rsi_ok and vol_ok and micro_up and bull_candle:
            tlog(f"entry:{symbol}",
                 f"[ANALYZE-V2] {symbol} ENTRY C={row['Close']:.4f} "
                 f"EMA20={ema20:.4f} ATR={atr:.4f} RSI={row['rsi']:.1f} "
                 f"Vol={row['Volume']/prev['vol_ma']:.2f}x", 60)
            return "entry", "EMA20-Pullback-4h", float(row["Close"])

    except Exception as e:
        tlog(f"analyze_exc:{symbol}", f"[ANALYZE-V2] {symbol} exc: {e}", 300)
    return None, None, None

# ── TRAILING WORKER ───────────────────────────────────────────────────────────
def trailing_worker() -> None:
    """
    Attiva il trailing stop quando price >= entry + TRAIL_START_R * r_dist.
    Distanza trailing = TRAIL_ATR_MULT * ATR(14) — esatto dal backtest.
    """
    log("[TRAIL-WORKER] avviato")
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
                    df = fetch_history(symbol, interval=INTERVAL_MINUTES, limit=50)
                    atr_val = r_dist / max(1e-9, SL_ATR_MULT)   # fallback
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
                            f"🎯 Trailing attivato LONG {symbol}\n"
                            f"Prezzo: {price_now:.4f} | Trail dist: {trail_dist:.6f}"
                        )
        except Exception as e:
            log(f"[TRAIL-WORKER] exc: {e}")
        time.sleep(2)

# ── SL WATCHDOG ───────────────────────────────────────────────────────────────
def sl_watchdog() -> None:
    """Ogni 5 min controlla che ogni posizione LONG aperta abbia il position-SL."""
    log("[SL-WATCHDOG] avviato")
    while True:
        time.sleep(300)
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
                    continue   # SL già impostato
                entry = get_position(symbol)
                if not entry:
                    continue
                sl_price = float(entry.get("sl", 0))
                if sl_price <= 0:
                    ep       = float(entry.get("entry_price", 0))
                    rd       = float(entry.get("r_dist", ep * 0.04))
                    sl_price = ep - rd
                cur = get_last_price(symbol)
                if cur and sl_price >= cur:
                    sl_price = cur * 0.98
                ok = set_position_stoploss_long(symbol, sl_price)
                if ok:
                    log(f"[SL-WATCHDOG] ✅ SL reimpostato {symbol} @ {sl_price:.6f}")
                else:
                    notify_telegram(
                        f"🚨 SL MANCANTE {symbol} — reimpostazione FALLITA!\n"
                        f"SL target: {sl_price:.4f} — VERIFICA MANUALE"
                    )
        except Exception as e:
            log(f"[SL-WATCHDOG] exc: {e}")

# ── SYNC POSIZIONI ALL'AVVIO ──────────────────────────────────────────────────
def sync_positions_from_wallet() -> None:
    log("[SYNC] Scansione posizioni LONG dal conto...")
    try:
        from urllib.parse import urlencode
        params = {"category": "linear", "settleCoin": "USDT"}
        qs   = urlencode(sorted(params.items()))
        ts   = str(int(time.time() * 1000))
        rw   = "10000"
        sign = hmac.new(
            SECRET.encode(),
            f"{ts}{KEY}{rw}{qs}".encode(),
            hashlib.sha256,
        ).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw,
        }
        resp     = requests.get(f"{BYBIT_BASE_URL}/v5/position/list",
                                headers=headers, params=params, timeout=10)
        data     = resp.json()
        pos_list = (data.get("result", {}).get("list", [])
                    if data.get("retCode") == 0 else [])
    except Exception as e:
        log(f"[SYNC] errore fetch pos/list: {e}")
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

        df      = fetch_history(symbol, interval=INTERVAL_MINUTES)
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
        final_sl        = entry_price - r_dist
        trailing_active = float(pos.get("trailingStop", 0) or 0) > 0

        set_position(symbol, {
            "entry_price":     entry_price,
            "sl":              final_sl,
            "r_dist":          r_dist,
            "qty":             qty,
            "entry_time":      time.time(),
            "trailing_active": trailing_active,
        })
        add_open(symbol)
        set_position_stoploss_long(symbol, final_sl)
        trovate += 1
        log(f"[SYNC] LONG trovato: {symbol} qty={qty} "
            f"entry={entry_price:.4f} SL={final_sl:.4f} "
            f"trailing={'SI' if trailing_active else 'NO'}")

    log(f"[SYNC] Totale posizioni LONG recuperate: {trovate}")

# ─────────────────────────────────────────────────────────────────────────────
# AVVIO
# ─────────────────────────────────────────────────────────────────────────────
update_assets()
sync_positions_from_wallet()
_update_btc_context()

_daily_start_equity = get_total_equity()
log(f"[AVVIO] Equity iniziale: {_daily_start_equity:.2f} USDT")

log("🤖 BOT LONG v2 AVVIATO — EMA20-Pullback 4h | Run H | Backtest puro")
notify_telegram("🤖 BOT [LONG] AVVIATO - In ascolto per segnali di ingresso/uscita")

threading.Thread(target=trailing_worker, daemon=True).start()
threading.Thread(target=sl_watchdog,     daemon=True).start()

# ─────────────────────────────────────────────────────────────────────────────
# LOOP PRINCIPALE
# ─────────────────────────────────────────────────────────────────────────────
while True:
    try:
        _update_btc_context()
        equity = get_total_equity()
        usdt   = get_usdt_balance()

        # Reset equity giornaliera a mezzanotte UTC
        today_str = time.strftime("%Y-%m-%d", time.gmtime())
        if _main_loop_last_day != today_str:
            # Report giornaliero al cambio di giorno
            if _main_loop_last_day != "":
                pnl_day = equity - (_daily_start_equity or equity)
                pnl_pct = (pnl_day / max(1e-9, _daily_start_equity or equity)) * 100
                regime_str = "✅ LONG gate aperto" if _btc_favorable_long else "🔴 LONG gate chiuso (BTC < EMA50 4h)"
                notify_telegram(
                    f"📋 Report giornaliero LONG — {_main_loop_last_day}\n"
                    f"📈 PnL giorno: {pnl_day:+.2f} USDT ({pnl_pct:+.2f}%)\n"
                    f"💰 Equity: {equity:.2f} USDT\n"
                    f"📂 Posizioni aperte: {len(open_positions)}\n"
                    f"🔍 Regime BTC: {regime_str}"
                )
            _daily_start_equity = equity
            _main_loop_last_day = today_str
            log(f"[DAY-RESET] Nuovo giorno {today_str} | equity={equity:.2f} USDT")

        # Daily DD circuit breaker (sicurezza emergenza)
        if _daily_start_equity and equity < _daily_start_equity * (1.0 - DAILY_DD_CAP_PCT):
            tlog("dd_cap",
                 f"[DD-CAP] equity={equity:.2f} < "
                 f"{_daily_start_equity*(1-DAILY_DD_CAP_PCT):.2f} "
                 f"(−{DAILY_DD_CAP_PCT*100:.0f}%), skip nuovi LONG",
                 600)
            time.sleep(180)
            continue

        tlog("loop_status",
             f"[LOOP] equity={equity:.2f} USDT | "
             f"pos={len(open_positions)}/{MAX_OPEN_POSITIONS} | "
             f"btc_fav={_btc_favorable_long}",
             300)

        # Analizza asset in parallelo solo se c'è spazio
        eligible = [s for s in ASSETS if s not in open_positions]
        results: dict = {}
        if eligible and len(open_positions) < MAX_OPEN_POSITIONS:
            with ThreadPoolExecutor(max_workers=4) as ex:
                fut_map = {ex.submit(analyze_asset, s): s for s in eligible}
                for fut in as_completed(fut_map):
                    s = fut_map[fut]
                    try:
                        results[s] = fut.result()
                    except Exception as e:
                        tlog(f"fut_exc:{s}", f"[FUT-EXC] {s} {e}", 300)

        # Elabora segnali di entry
        for symbol in eligible:
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break

            signal, strategy, price = results.get(symbol, (None, None, None))
            if signal != "entry" or price is None:
                continue
            if symbol in open_positions:
                continue
            if get_open_short_qty(symbol) > 0:
                continue

            # ── Sizing ATR-based (identico al backtest) ────────────────────
            df = fetch_history(symbol, interval=INTERVAL_MINUTES, limit=50)
            if df is None or len(df) < ATR_WINDOW + 2:
                continue
            try:
                atr_val = float(
                    AverageTrueRange(
                        high=df["High"], low=df["Low"],
                        close=df["Close"], window=ATR_WINDOW,
                    ).average_true_range().iloc[-1]
                )
            except Exception:
                continue
            if atr_val <= 0:
                continue

            r_dist     = atr_val * SL_ATR_MULT
            risk_usdt  = equity * RISK_PCT
            price_now  = get_last_price(symbol) or price
            qty_target = risk_usdt / max(1e-9, r_dist)
            notional   = qty_target * price_now

            info_i       = get_instrument_info(symbol)
            min_notional = float(info_i.get("min_order_amt", 5.0))
            if notional < min_notional:
                notional = min_notional * 1.01

            max_by_margin = usdt * DEFAULT_LEVERAGE * MARGIN_USE_PCT
            order_amount  = min(notional, max_by_margin, ORDER_USDT_MAX)
            if order_amount < min_notional:
                tlog(f"skip_notional:{symbol}",
                     f"[SKIP] {symbol} notional {order_amount:.2f} < min {min_notional:.2f}",
                     300)
                continue

            # ── Apri LONG ──────────────────────────────────────────────────
            qty = market_long(symbol, order_amount)
            if not qty or qty <= 0:
                continue

            price_now = get_last_price(symbol) or price_now
            final_sl  = price_now - r_dist

            ok_sl = set_position_stoploss_long(symbol, final_sl)
            if not ok_sl:
                log(f"🚨 [SL-FAIL] {symbol} SL non impostato! "
                    f"Entry={price_now:.4f} SL={final_sl:.4f}")
                notify_telegram(
                    f"🚨 SL NON IMPOSTATO {symbol} LONG!\n"
                    f"Entry={price_now:.4f} SL={final_sl:.4f}\n"
                    "⚠️ IMPOSTA MANUALMENTE!"
                )

            set_position(symbol, {
                "entry_price":     price_now,
                "sl":              final_sl,
                "r_dist":          r_dist,
                "qty":             qty,
                "entry_time":      time.time(),
                "trailing_active": False,
            })
            add_open(symbol)

            log(f"[ENTRY] {symbol} | Entry={price_now:.4f} | SL={final_sl:.4f} | "
                f"r_dist={r_dist:.4f} | qty={qty} | notional≈{qty*price_now:.2f} USDT")
            notify_telegram(
                f"🟢📈 LONG aperto {symbol}\n"
                f"Prezzo: {price_now:.4f}\n"
                f"SL: {final_sl:.4f} ({SL_ATR_MULT}×ATR)\n"
                f"Trailing: attivo a +{TRAIL_START_R}R ({TRAIL_ATR_MULT}×ATR)\n"
                f"Rischio: {risk_usdt:.2f} USDT ({RISK_PCT*100:.2f}% equity)"
            )
            time.sleep(2)

        # ── Cleanup: posizioni chiuse dall'exchange (SL/TP hit) ───────────
        for symbol in list(open_positions):
            qty_live = get_open_long_qty(symbol)
            info_i   = get_instrument_info(symbol)
            min_qty  = float(info_i.get("min_qty", 0.0))
            if qty_live is not None and qty_live < max(min_qty, 1e-9):
                entry       = get_position(symbol)
                entry_price = float((entry or {}).get("entry_price", 0))
                exit_price  = get_last_price(symbol) or 0.0
                pnl_roi = (
                    (exit_price - entry_price) / max(1e-9, entry_price)
                    * 100 * DEFAULT_LEVERAGE
                ) if entry_price else 0.0
                log(f"[CLEANUP] {symbol} chiusa dall'exchange | ROI≈{pnl_roi:.1f}%")
                notify_telegram(
                    f"🔴📈 Posizione LONG chiusa dall'exchange (SL/TP)\n"
                    f"Simbolo: {symbol}\n"
                    f"Entry: {entry_price:.4f} | Uscita: {exit_price:.4f}\n"
                    f"ROI stimato: {pnl_roi:.1f}%"
                )
                discard_open(symbol)
                with _state_lock:
                    position_data.pop(symbol, None)
                cancel_all_orders(symbol)

    except Exception as e:
        log(f"[LOOP-CRASH] {e}")

    time.sleep(180)
