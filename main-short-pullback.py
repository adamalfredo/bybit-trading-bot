# ─────────────────────────────────────────────────────────────────────────────
# STRATEGIA: Trend Following SHORT — 4h EMA20 Bounce Rejection
#
# Logica:
#   - Ogni 30 min scansiona i top 100 futures Bybit per volume
#   - Regime gate: BTC daily close < EMA50 daily + slope negativa (bear regime)
#     Se BTC esce dal regime short (rimbalza sopra EMA50), il bot va in idle.
#   - Filtro trend: coin deve essere SOTTO EMA50 DAILY (downtrend strutturale)
#   - Segnale su ultima candela 4h CHIUSA:
#     • Il massimo ha sfiorato/toccato EMA20(4h) dalla parte bassa (bounce rejection)
#     • La candela è chiusa ROSSA sotto EMA20 (rifiuto confermato)
#     • RSI(14) tra 32 e 68 (bounce sano, non oversold né overbought)
#     • Close entro 3% sotto EMA20 (rifiuto fresco, non già esteso al ribasso)
#     • Body >= 30% del range (niente doji/shooting star)
#     • Volume >= 1.2× media 20 candele (distribuzione reale)
#   - SL: sopra swing high (max 3 barre chiuse) + 0.3×ATR
#   - Trail: 2.0×ATR(4h) dal minimo (low_water), attivo dal primo ratchet tier
#   - Partial TP: 50% posizione chiusa a 1.5R (prezzo sceso di 1.5×r_dist)
#   - MAX 5 posizioni | RISK 1% | Leva 5×
#
# Razionale:
#   Vendiamo ALLA RESISTENZA (EMA20 in downtrend), non dopo uno spike al rialzo.
#   Il trend giornaliero è già confermato bearish. Il bounce ci dà entry con
#   SL stretto sopra il massimo del rimbalzo. Obiettivo: minimo precedente (≥2R).
#   Speculare esatto al bot long main-pullback.py.
# ─────────────────────────────────────────────────────────────────────────────

import os
import time
import hmac
import hashlib
import json
import re
import threading
from decimal import Decimal, ROUND_DOWN, ROUND_UP
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
DEFAULT_LEVERAGE   = 5
MAX_OPEN_POSITIONS = 5
MARGIN_USE_PCT     = 0.30
ORDER_USDT_MAX     = float(os.getenv("ORDER_USDT_MAX", "1000"))
MAX_TOTAL_OPEN_RISK_PCT = float(os.getenv("MAX_TOTAL_OPEN_RISK_PCT", "0.04"))

SL_ATR_BUFFER  = 0.3    # buffer sopra swing high (× ATR)
TRAIL_ATR_MULT = 2.0    # moltiplicatore ATR per il trailing stop dal minimo
PARTIAL_TP_R   = 1.5    # partial TP: chiude 50% posizione a +1.5R
PARTIAL_TP_PCT = 0.25

# Ratchet floor fissi: (roi_lev_trigger%, floor_lev_garantito%)
# Per short: al trigger si sposta SL verso il basso (profit lock)
RATCHET_TABLE = [
    ( 10,   7),
    ( 25,  15),
    ( 40,  25),
    ( 60,  40),
    ( 80,  60),
    (100,  80),
    (125, 100),
    (150, 120),
    (175, 148),
    (200, 173),
    (250, 223),
    (300, 273),
    (400, 370),
    (500, 465),
]
ATR_WINDOW = 14

# Universo
MIN_VOL_24H_USDT = 10_000_000
COINS_TOP_N      = 100
TRADE_TOP_N      = 12

# Filtri segnale 4h — RILASSATI per trend following sui top losers
RSI_MIN_4H    = 30.0   # range più selettivo: evita estremi rumorosi
RSI_MAX_4H    = 70.0
EMA_TOUCH_TOL = 0.012  # bounce più preciso sulla EMA20
MAX_DIST_EMA  = 3.0    # % massima close SOTTO EMA20 all'entry (rifiuto fresco)
CLOSE_ABOVE_EMA_TOL = 0.003  # tolleranza 0.3%: accetta close lievemente sopra EMA20
MAX_SL_PCT    = 8.0    # SL massimo accettabile: 8% sopra entry
MIN_BODY_PCT  = 25.0   # evita doji e rejection deboli
MIN_VOL_RATIO = 0.8    # richiede almeno volume vicino alla media
MAX_DIST_EMA50_D = 20.0  # daily close max 20% SOTTO EMA50 (non in freefall)
REQUIRE_SLOPE_CONFIRMATION = True
MAX_CHG_1H_PCT = -0.4
MAX_CHG_4H_PCT = -1.0
BASE_LOOKBACK_BARS = 8
SL_BASE_ATR_BUFFER = 0.2

# Adaptive engine (percentili + ATR-normalized momentum)
ADAPTIVE_LOOKBACK_BARS = 48
ADAPTIVE_BASE_WIDTH_PCTL = 0.75
ADAPTIVE_RVOL_PCTL = 0.45
ADAPTIVE_MOM_PCTL_SHORT = 0.50
ADAPTIVE_MIN_NORM_Z_SHORT = -0.20
ADAPTIVE_BASE_MIN_PCT = 0.8
ADAPTIVE_BASE_MAX_PCT = 6.5
ADAPTIVE_RVOL_MIN = 0.70
ADAPTIVE_RVOL_MAX = 1.25
MAX_CHG_1H_PCT_CEIL = -0.15
MAX_CHG_1H_PCT_FLOOR = -2.50
MAX_CHG_4H_PCT_CEIL = -0.40
MAX_CHG_4H_PCT_FLOOR = -4.00

# Regime BTC: attiva short solo quando BTC è strutturalmente bearish
# Se False: bot sempre attivo (solo per testing)
BTC_SHORT_REGIME_CHECK = True
BTC_SHORT_REGIME_SCORE_MIN = 0.55

# Timing
SCAN_INTERVAL_SEC  = 1800   # 30 min
TRAIL_SLEEP_SEC    = 60
SL_WATCH_SLEEP_SEC = 600    # 10 min

# Bybit hedge mode: positionIdx=2 per short
SHORT_IDX = 2

# Time stop
TIME_STOP_DAYS    = 10
TIME_STOP_MIN_LEV = 10.0

# Circuit breaker
CIRCUIT_BREAKER_PCT        = 3.0
CIRCUIT_BREAKER_COOLDOWN_H = 24

EXCLUDE_SUBSTRINGS = ["USDC", "BUSD", "DAI", "TUSD", "FRAX",
                      "3LUSDT", "3SUSDT", "BULLUSDT", "BEARUSDT"]
EXCLUDE_SYMBOLS = {
    s.strip().upper() for s in os.getenv("EXCLUDE_SYMBOLS", "").split(",") if s.strip()
}
MIN_ABS_24H_CHANGE = float(os.getenv("MIN_ABS_24H_CHANGE", "3.5"))

# ── STATO GLOBALE ─────────────────────────────────────────────────────────────
open_positions:    set  = set()
blocked_symbols:   set  = set()
position_data:     dict = {}
_state_lock              = threading.RLock()
_instr_lock              = threading.RLock()
_instrument_cache: dict = {}
_price_cache:      dict = {}
_price_lock              = threading.RLock()
_last_log_times:   dict = {}
_bybit_ts_offset_ms: int = 0

_btc_short_ok: bool  = True   # True = BTC in bear regime → short attivi
_btc_ts:       float = 0.0
_btc_short_score: float = 1.0

_cb_equity_day_start: float = 0.0
_cb_last_day:         str   = ""
_cb_triggered:        bool  = False
_cb_triggered_at:     float = 0.0

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
            data={"chat_id": TELEGRAM_CHAT_ID, "text": f"[SHORT-PB] {msg}"},
            timeout=10,
        )
    except Exception as e:
        log(f"[TELEGRAM] err: {e}")


def run_startup_self_checks() -> None:
    errs = []
    if not (0 < RISK_PCT <= 0.05):
        errs.append(f"RISK_PCT fuori range: {RISK_PCT}")
    if not (1 <= DEFAULT_LEVERAGE <= 25):
        errs.append(f"DEFAULT_LEVERAGE fuori range: {DEFAULT_LEVERAGE}")
    if not (0 < PARTIAL_TP_PCT <= 1.0):
        errs.append(f"PARTIAL_TP_PCT fuori range: {PARTIAL_TP_PCT}")
    if not (0 <= RSI_MIN_4H < RSI_MAX_4H <= 100):
        errs.append(f"RSI range invalido: {RSI_MIN_4H}-{RSI_MAX_4H}")
    if not (1 <= TRADE_TOP_N <= COINS_TOP_N):
        errs.append("TRADE_TOP_N invalido")
    if BASE_LOOKBACK_BARS < 4:
        errs.append("BASE_LOOKBACK_BARS troppo basso")
    if MAX_CHG_4H_PCT > MAX_CHG_1H_PCT:
        errs.append("MAX_CHG_4H_PCT deve essere <= MAX_CHG_1H_PCT")
    if ADAPTIVE_LOOKBACK_BARS < 24:
        errs.append("ADAPTIVE_LOOKBACK_BARS troppo basso")
    if not (0.0 < ADAPTIVE_BASE_WIDTH_PCTL < 1.0):
        errs.append("ADAPTIVE_BASE_WIDTH_PCTL fuori range")
    if not (0.0 < ADAPTIVE_RVOL_PCTL < 1.0):
        errs.append("ADAPTIVE_RVOL_PCTL fuori range")
    if not (0.0 < ADAPTIVE_MOM_PCTL_SHORT < 1.0):
        errs.append("ADAPTIVE_MOM_PCTL_SHORT fuori range")
    if not (0.0 <= BTC_SHORT_REGIME_SCORE_MIN <= 1.0):
        errs.append("BTC_SHORT_REGIME_SCORE_MIN fuori range")
    for i in range(1, len(RATCHET_TABLE)):
        prev_t, prev_f = RATCHET_TABLE[i - 1]
        cur_t, cur_f = RATCHET_TABLE[i]
        if cur_t <= prev_t or cur_f <= prev_f:
            errs.append(f"RATCHET_TABLE non crescente in posizione {i}")
            break
    if errs:
        for e in errs:
            log(f"[SELF-CHECK] ❌ {e}")
        raise RuntimeError("Self-check startup fallito")
    log("[SELF-CHECK] ✅ configurazione valida")


# ── FIRMA BYBIT ───────────────────────────────────────────────────────────────
def _signed_ts_ms() -> int:
    return int(time.time() * 1000) + _bybit_ts_offset_ms


def _maybe_adjust_ts_offset(ret_msg: str) -> None:
    global _bybit_ts_offset_ms
    m = re.search(r"req_timestamp\[(\d+)\],server_timestamp\[(\d+)\]", ret_msg)
    if not m:
        return
    req_ts = int(m.group(1))
    srv_ts = int(m.group(2))
    delta = srv_ts - req_ts
    if abs(delta) >= 50:
        _bybit_ts_offset_ms += delta
        tlog("ts_offset",
             f"[TIME] offset aggiustato di {delta}ms (tot={_bybit_ts_offset_ms}ms)",
             120)


def _bybit_signed_get(path: str, params: dict):
    from urllib.parse import urlencode
    last_resp = None
    for _ in range(3):
        qs   = urlencode(sorted(params.items()))
        ts   = str(_signed_ts_ms())
        rw   = "30000"
        sign = hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{qs}".encode(),
                        hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
                   "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw}
        last_resp = SESSION.get(f"{BYBIT_BASE_URL}{path}",
                                headers=headers, params=params, timeout=10)
        try:
            data = last_resp.json()
            if data.get("retCode") == 0:
                return last_resp
            msg = str(data.get("retMsg") or "")
            if "server timestamp" in msg or "recv_window" in msg:
                _maybe_adjust_ts_offset(msg)
                continue
        except Exception:
            return last_resp
        return last_resp
    return last_resp


def _bybit_signed_post(path: str, body: dict):
    body_json = json.dumps(body, separators=(",", ":"))
    last_resp = None
    for _ in range(3):
        ts        = str(_signed_ts_ms())
        rw        = "30000"
        sign      = hmac.new(SECRET.encode(), f"{ts}{KEY}{rw}{body_json}".encode(),
                             hashlib.sha256).hexdigest()
        headers   = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign,
                     "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": rw,
                     "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json"}
        last_resp = SESSION.post(f"{BYBIT_BASE_URL}{path}",
                                 headers=headers, data=body_json, timeout=10)
        try:
            data = last_resp.json()
            if data.get("retCode") == 0:
                return last_resp
            msg = str(data.get("retMsg") or "")
            if "server timestamp" in msg or "recv_window" in msg:
                _maybe_adjust_ts_offset(msg)
                continue
        except Exception:
            return last_resp
        return last_resp
    return last_resp


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


def format_price_floor(price: float, tick_size: float) -> str:
    """Arrotonda al tick inferiore (usato per entry e prezzi generici)."""
    step    = Decimal(str(tick_size))
    p       = Decimal(str(price))
    floored = (p // step) * step
    dec     = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    return f"{floored:.{dec}f}"


def format_price_ceil(price: float, tick_size: float) -> str:
    """Arrotonda al tick superiore (usato per SL short: deve stare SOPRA il prezzo)."""
    step   = Decimal(str(tick_size))
    p      = Decimal(str(price))
    ceiled = (p / step).to_integral_value(rounding=ROUND_UP) * step
    dec    = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    return f"{ceiled:.{dec}f}"


def _format_qty_with_step(qty: float, step: float) -> str:
    step_dec = Decimal(str(step))
    q        = Decimal(str(qty))
    floored  = (q // step_dec) * step_dec
    sd       = -step_dec.as_tuple().exponent if step_dec.as_tuple().exponent < 0 else 0
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


def get_ask_price(symbol: str) -> Optional[float]:
    """Prezzo ask (usato per entry short PostOnly: vendi all'ask = maker)."""
    get_last_price(symbol)
    with _price_lock:
        c = _price_cache.get(symbol, {})
        return c.get("ask1") or c.get("price")


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


def estimate_open_risk_usdt() -> float:
    """Somma la perdita teorica fino allo SL di tutte le posizioni SHORT aperte."""
    total = 0.0
    for sym in list(open_positions):
        entry = get_position(sym)
        if not entry:
            continue
        qty = float(entry.get("qty", 0) or 0)
        ep = float(entry.get("entry_price", 0) or 0)
        sl = float(entry.get("sl_price", 0) or 0)
        if qty <= 0 or ep <= 0 or sl <= 0:
            continue
        per_unit_risk = sl - ep
        if per_unit_risk <= 0:
            continue
        total += per_unit_risk * qty
    return total


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


def get_open_short_fill(symbol: str) -> tuple[float, float]:
    try:
        resp = _bybit_signed_get("/v5/position/list",
                                 {"category": "linear", "symbol": symbol})
        data = resp.json()
        if data.get("retCode") != 0:
            return 0.0, 0.0
        for pos in data.get("result", {}).get("list", []):
            if pos.get("side") == "Sell":
                qty = float(pos.get("size", 0) or 0)
                entry_price = float(pos.get("avgPrice", 0) or 0)
                return qty, entry_price
    except Exception:
        pass
    return 0.0, 0.0


# ── BTC REGIME ────────────────────────────────────────────────────────────────
def _update_btc_regime() -> None:
    """
    Controlla il regime di mercato per gli short.
    SHORT regime ATTIVO quando:
      - BTC daily close < EMA50 daily (prezzo strutturalmente sotto la media)
      - EMA50 slope negativa (EMA50 oggi < EMA50 di 5 giorni fa)
    Se una delle due condizioni manca (BTC rimbalza sopra EMA50 o slope torna
    positiva), il bot va in idle: nessun nuovo short fino a regime ristabilito.
    """
    global _btc_short_ok, _btc_ts, _btc_short_score
    if time.time() - _btc_ts < 3600:
        return
    if not BTC_SHORT_REGIME_CHECK:
        _btc_short_ok = True
        _btc_short_score = 1.0
        _btc_ts = time.time()
        return
    try:
        df_d = fetch_klines("BTCUSDT", interval="D", limit=60)
        if df_d is not None and len(df_d) >= 52:
            close_d   = df_d["Close"]
            ema50_d   = close_d.ewm(span=50, adjust=False).mean()
            btc_d     = float(close_d.iloc[-2])   # ultima candela CHIUSA
            ema50_now = float(ema50_d.iloc[-2])
            ema50_5d  = float(ema50_d.iloc[-7])
            below_ema = btc_d < ema50_now
            slope_neg = ema50_now < ema50_5d
            gap_pct   = (btc_d - ema50_now) / ema50_now * 100
            slope_pct = (ema50_now - ema50_5d) / ema50_5d * 100

            # Regime score continuo [0..1]: combina distanza sotto EMA50 e slope negativa.
            gap_score = _clamp((-gap_pct) / 2.0, 0.0, 1.0)
            slope_score = _clamp((-slope_pct) / 0.50, 0.0, 1.0)
            _btc_short_score = (gap_score + slope_score) / 2.0
            _btc_short_ok = _btc_short_score >= BTC_SHORT_REGIME_SCORE_MIN

            tlog("btc_regime",
                 f"[REGIME] BTC={btc_d:,.0f} EMA50d={ema50_now:,.0f} "
                 f"({gap_pct:+.1f}%) slope={slope_pct:+.3f}%/5gg | "
                 f"score={_btc_short_score:.2f} | "
                 f"SHORT={'✅ ON' if _btc_short_ok else '⏸️ OFF (score basso)'} | "
                 f"gate_raw={'ON' if (below_ema and slope_neg) else 'OFF'}",
                 3600)
        else:
            _btc_short_ok = False  # dati insufficienti: non shortare
            _btc_short_score = 0.0
    except Exception:
        pass
    _btc_ts = time.time()


# ── SCANSIONE UNIVERSO ────────────────────────────────────────────────────────
def scan_universe() -> list:
    """
    Ritorna le top COINS_TOP_N coin per momentum ribassista 24h,
    mantenendo il filtro di liquidità (>10M USDT).
    Nessuna soglia hard su momentum: ordina per variazione 24h e prende i peggiori.
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
        if sym in EXCLUDE_SYMBOLS:
            continue
        if sym in blocked_symbols:
            continue
        if sym in open_positions:
            continue
        try:
            vol24h = float(t.get("turnover24h", 0) or 0)
            price  = float(t.get("lastPrice", 0) or 0)
            chg24h = float(t.get("price24hPcnt", 0) or 0) * 100.0
        except Exception:
            continue
        if vol24h < MIN_VOL_24H_USDT or price <= 0:
            continue
        candidates.append({"symbol": sym, "vol24h": vol24h, "chg24h": chg24h})

    # Top losers prima, poi volume per spezzare i pari-merito.
    candidates.sort(key=lambda x: (x["chg24h"], -x["vol24h"]))
    return candidates[:COINS_TOP_N]


# ── FILTRO TREND DAILY DOWNTREND ──────────────────────────────────────────────
def is_daily_downtrend(symbol: str) -> bool:
    """
    True se la coin è in downtrend strutturale:
    - Last daily close < EMA50 daily (prezzo sotto la media mobile lenta)
    - EMA50 daily con slope negativa (media in discesa: oggi < 5 giorni fa)
    - Trend non overesteso: close non più di MAX_DIST_EMA50_D% sotto EMA50
      (evita coin già in freefall: rimbalzo violento = squeeze sugli short)
    """
    df = fetch_klines(symbol, interval="D", limit=60)
    if df is None or len(df) < 52:
        return False
    close     = df["Close"]
    ema50     = close.ewm(span=50, adjust=False).mean()
    last_c    = float(close.iloc[-2])
    ema50_now = float(ema50.iloc[-2])
    ema50_5d  = float(ema50.iloc[-7])
    # Deve essere sotto EMA50 con slope negativa
    if last_c >= ema50_now or ema50_now >= ema50_5d:
        return False
    # Non in freefall: evita coin già -20% sotto EMA50
    dist_below = (ema50_now - last_c) / ema50_now * 100
    if dist_below > MAX_DIST_EMA50_D:
        return False
    return True


def _clamp(value: float, min_value: float, max_value: float) -> float:
    return max(min_value, min(max_value, value))


def _safe_quantile(values: list[float], q: float, fallback: float) -> float:
    if not values:
        return fallback
    s = pd.Series(values).dropna()
    if s.empty:
        return fallback
    return float(s.quantile(q))


def _compute_short_adaptive_thresholds(
    c: pd.Series,
    h: pd.Series,
    l: pd.Series,
    v: pd.Series,
    atr_series: pd.Series,
    last_idx: int,
) -> dict:
    lookback = max(24, min(ADAPTIVE_LOOKBACK_BARS, last_idx - 6))

    base_hist = []
    base_start_j = max(BASE_LOOKBACK_BARS, last_idx - lookback + 1)
    for j in range(base_start_j, last_idx + 1):
        close_j = float(c.iloc[j])
        if close_j <= 0:
            continue
        bh = float(h.iloc[j - BASE_LOOKBACK_BARS:j].max())
        bl = float(l.iloc[j - BASE_LOOKBACK_BARS:j].min())
        base_hist.append((bh - bl) / close_j * 100.0)

    rvol_hist = []
    rvol_start_j = max(22, last_idx - lookback + 1)
    for j in range(rvol_start_j, last_idx + 1):
        vol_avg = float(v.iloc[j - 20:j].mean())
        if vol_avg <= 0:
            continue
        rvol_hist.append(float(v.iloc[j]) / vol_avg)

    chg1h_hist = (c.pct_change(1) * 100.0).iloc[max(1, last_idx - lookback + 1):last_idx + 1]
    chg4h_hist = (c.pct_change(4) * 100.0).iloc[max(4, last_idx - lookback + 1):last_idx + 1]
    chg1h_vals = [float(x) for x in chg1h_hist.dropna().tolist()]
    chg4h_vals = [float(x) for x in chg4h_hist.dropna().tolist()]

    norm_hist = []
    norm_start_j = max(1, last_idx - lookback + 1)
    for j in range(norm_start_j, last_idx + 1):
        close_j = float(c.iloc[j])
        atr_j = float(atr_series.iloc[j])
        prev_j = float(c.iloc[j - 1])
        if close_j <= 0 or prev_j <= 0 or atr_j <= 0:
            continue
        atr_pct = atr_j / close_j * 100.0
        if atr_pct <= 0:
            continue
        ret_1h = (close_j / prev_j - 1.0) * 100.0
        norm_hist.append((-ret_1h) / atr_pct)

    base_thr = _clamp(
        _safe_quantile(base_hist, ADAPTIVE_BASE_WIDTH_PCTL, MAX_DIST_EMA),
        ADAPTIVE_BASE_MIN_PCT,
        ADAPTIVE_BASE_MAX_PCT,
    )
    rvol_thr = _clamp(
        _safe_quantile(rvol_hist, ADAPTIVE_RVOL_PCTL, MIN_VOL_RATIO),
        ADAPTIVE_RVOL_MIN,
        ADAPTIVE_RVOL_MAX,
    )
    max_chg_1h = _clamp(
        _safe_quantile(chg1h_vals, ADAPTIVE_MOM_PCTL_SHORT, MAX_CHG_1H_PCT),
        MAX_CHG_1H_PCT_FLOOR,
        MAX_CHG_1H_PCT_CEIL,
    )
    max_chg_4h = _clamp(
        _safe_quantile(chg4h_vals, ADAPTIVE_MOM_PCTL_SHORT, MAX_CHG_4H_PCT),
        MAX_CHG_4H_PCT_FLOOR,
        MAX_CHG_4H_PCT_CEIL,
    )

    norm_series = pd.Series(norm_hist).dropna()
    norm_mu = float(norm_series.mean()) if not norm_series.empty else 0.0
    norm_std = float(norm_series.std(ddof=0)) if len(norm_series) > 1 else 0.0

    return {
        "base_max_pct": base_thr,
        "min_rvol": rvol_thr,
        "max_chg_1h": max_chg_1h,
        "max_chg_4h": min(max_chg_4h, max_chg_1h),
        "norm_mu": norm_mu,
        "norm_std": norm_std,
    }


# ── SIGNAL CHECK 1h (ANTICIPAZIONE BREAKDOWN) ────────────────────────────────
def check_short_signal(symbol: str, reject_stats: Optional[dict] = None) -> Optional[dict]:
    """Entry SHORT di anticipazione con soglie adattive su breakdown 1h."""
    def reject(reason: str) -> Optional[dict]:
        if reject_stats is not None:
            reject_stats[reason] = reject_stats.get(reason, 0) + 1
        return None

    df = fetch_klines(symbol, interval="60", limit=80)
    if df is None or len(df) < 40:
        return reject("kline_insufficient")

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    o = df["Open"]
    v = df["Volume"]

    ema20      = c.ewm(span=20, adjust=False).mean()
    atr_series = AverageTrueRange(high=h, low=l, close=c,
                                  window=ATR_WINDOW).average_true_range()
    rsi_series = RSIIndicator(close=c, window=14).rsi()

    # Ultima candela CHIUSA su 1h
    last_close = float(c.iloc[-2])
    last_open  = float(o.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    last_rsi   = float(rsi_series.iloc[-2])
    last_atr   = float(atr_series.iloc[-2])

    if pd.isna(last_rsi) or pd.isna(last_atr) or last_atr <= 0:
        return reject("invalid_rsi_or_atr")
    if last_ema20 <= 0:
        return reject("invalid_ema20")

    last_idx = len(df) - 2
    if last_idx - BASE_LOOKBACK_BARS < 0 or last_idx - 6 < 0:
        return reject("kline_window_too_short")

    adaptive = _compute_short_adaptive_thresholds(c, h, l, v, atr_series, last_idx)

    base_high = float(h.iloc[last_idx - BASE_LOOKBACK_BARS:last_idx].max())
    base_low = float(l.iloc[last_idx - BASE_LOOKBACK_BARS:last_idx].min())
    base_range_pct = (base_high - base_low) / last_close * 100 if last_close > 0 else 0.0
    if base_range_pct > adaptive["base_max_pct"]:
        return reject("base_too_wide")

    # Breakdown confermato su chiusura.
    if last_close >= base_low:
        return reject("breakdown_not_confirmed")

    # Candela di conferma rossa.
    if last_close >= last_open:
        return reject("not_red_candle")

    # RSI in area di debolezza ma non estrema.
    if not (RSI_MIN_4H <= last_rsi <= RSI_MAX_4H):
        return reject("rsi_out_of_range")

    # Anti-chase: breakdown non deve essere troppo esteso sotto EMA20.
    dist_pct = (last_ema20 - last_close) / last_ema20 * 100
    if dist_pct < 0:
        return reject("above_ema20")
    if dist_pct > MAX_DIST_EMA:
        return reject("distance_from_ema_too_high")

    # Corpo minimo per evitare false rotture.
    candle_range = float(h.iloc[-2]) - float(l.iloc[-2])
    if candle_range > 0:
        body_pct = abs(last_close - last_open) / candle_range * 100
        if body_pct < MIN_BODY_PCT:
            return reject("body_too_small")

    vol_avg = float(v.iloc[-22:-2].mean())
    vol_sig = float(v.iloc[-2])
    rvol = (vol_sig / vol_avg) if vol_avg > 0 else 0.0
    if rvol < adaptive["min_rvol"]:
        return reject("volume_too_low")

    chg_1h_pct = (last_close / float(c.iloc[-3]) - 1.0) * 100.0
    chg_4h_pct = (last_close / float(c.iloc[-6]) - 1.0) * 100.0
    if chg_1h_pct > adaptive["max_chg_1h"]:
        return reject("chg1h_not_negative_enough")
    if chg_4h_pct > adaptive["max_chg_4h"]:
        return reject("chg4h_not_negative_enough")

    atr_pct = (last_atr / last_close * 100.0) if last_close > 0 else 0.0
    if atr_pct <= 0:
        return reject("invalid_atr_pct")
    norm_move = (-chg_1h_pct) / atr_pct
    norm_std = adaptive["norm_std"]
    norm_z = ((norm_move - adaptive["norm_mu"]) / norm_std) if norm_std > 1e-9 else 0.0
    if norm_z < ADAPTIVE_MIN_NORM_Z_SHORT:
        return reject("norm_move_z_too_low")

    # Slope EMA20: vogliamo trend locale già in deterioramento.
    ema20_3ago = float(ema20.iloc[last_idx - 3])
    slope_pct  = (last_ema20 - ema20_3ago) / ema20_3ago * 100 if ema20_3ago > 0 else 0.0
    if REQUIRE_SLOPE_CONFIRMATION and slope_pct >= 0:
        return reject("ema20_slope_not_down")

    # SL sopra la base del breakdown, con piccolo buffer ATR.
    sl_price = base_high + SL_BASE_ATR_BUFFER * last_atr
    r_dist     = sl_price - last_close
    sl_pct = r_dist / last_close * 100
    if sl_pct > MAX_SL_PCT or r_dist <= 0:
        return reject("sl_too_wide_or_invalid")

    log(f"[SETUP-ANTI] {symbol} SHORT | base={base_range_pct:.2f}%<=<{adaptive['base_max_pct']:.2f}% "
        f"rvol={rvol:.2f}>=<{adaptive['min_rvol']:.2f} "
        f"chg1h={chg_1h_pct:+.2f}% chg4h={chg_4h_pct:+.2f}% "
        f"normZ={norm_z:+.2f} dist={dist_pct:.2f}% RSI={last_rsi:.1f} slope={slope_pct:+.3f}%")

    return {
        "entry_price": last_close,
        "sl_price":    sl_price,
        "r_dist":      r_dist,
        "atr":         last_atr,
        "rsi":         last_rsi,
        "ema20_4h":    last_ema20,
        "dist_ema":    dist_pct,
        "sl_pct":      sl_pct,
        "ema20_slope": slope_pct,
        "chg_1h":      chg_1h_pct,
        "chg_4h":      chg_4h_pct,
        "rvol":        rvol,
        "base_range":  base_range_pct,
        "max_chg_1h":  adaptive["max_chg_1h"],
        "max_chg_4h":  adaptive["max_chg_4h"],
        "min_rvol":    adaptive["min_rvol"],
        "base_max":    adaptive["base_max_pct"],
        "norm_z":      norm_z,
    }


# ── ORDINI ────────────────────────────────────────────────────────────────────
def set_position_stoploss_short(symbol: str, sl_price: float) -> bool:
    """
    Imposta SL per posizione short.
    Per uno short, lo SL deve essere SOPRA il prezzo corrente.
    Man mano che il trade va in profitto (prezzo scende), lo SL viene abbassato.
    """
    cur = get_last_price(symbol)
    if cur and sl_price <= cur:
        # Fail-safe: se lo SL calcolato è finito sotto/al prezzo corrente
        # (slippage o drift), riallinealo appena sopra il mercato.
        sl_price = cur * 1.001
        log(f"[SL] {symbol} riallineato sopra mercato: {sl_price:.6f}")
    info     = get_instrument_info(symbol)
    # Ceiling: arrotonda al tick superiore (SL short deve stare SOPRA)
    stop_str = format_price_ceil(sl_price, info.get("price_step", 0.01))
    body = {"category": "linear", "symbol": symbol,
            "stopLoss": stop_str, "slTriggerBy": "MarkPrice",
            "positionIdx": SHORT_IDX, "tpslMode": "Full"}
    try:
        data = _bybit_signed_post("/v5/position/trading-stop", body).json()
        ret  = data.get("retCode")
        if ret == 0:              return True
        if ret in (34040, 10001): return True
        log(f"[SL] {symbol} FAIL retCode={ret} {data.get('retMsg')}")
        return False
    except Exception as e:
        log(f"[SL] {symbol} exc: {e}")
        return False


def get_atr_4h(symbol: str) -> Optional[float]:
    """ATR(14) sull'ultima candela 4h chiusa."""
    df = fetch_klines(symbol, interval="240", limit=30)
    if df is None or len(df) < ATR_WINDOW + 2:
        return None
    try:
        atr_s = AverageTrueRange(
            high=df["High"], low=df["Low"], close=df["Close"],
            window=ATR_WINDOW).average_true_range()
        val = float(atr_s.iloc[-2])
        return val if not pd.isna(val) and val > 0 else None
    except Exception:
        return None


def market_close_short(symbol: str, qty: float) -> bool:
    """Chiude parzialmente la posizione short (Buy reduce-only)."""
    info     = get_instrument_info(symbol)
    qty_step = float(info.get("qty_step", 0.01))
    qty_str  = _format_qty_with_step(qty, qty_step)
    if float(qty_str) <= 0:
        return False
    body = {"category": "linear", "symbol": symbol,
            "side": "Buy", "orderType": "Market",
            "qty": qty_str, "reduceOnly": True,
            "positionIdx": SHORT_IDX}
    try:
        data = _bybit_signed_post("/v5/order/create", body).json()
        ret  = data.get("retCode")
        if ret == 0:
            return True
        log(f"[CLOSE-SHORT] {symbol} FAIL retCode={ret} {data.get('retMsg')}")
        return False
    except Exception as e:
        log(f"[CLOSE-SHORT] {symbol} exc: {e}")
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


def market_short(symbol: str, usdt_amount: float) -> Optional[float]:
    """
    Apre una posizione short (Sell).
    Primo tentativo: Limit PostOnly all'ask (maker → fee negativa su Bybit).
    Fallback: Market order.
    """
    price = get_last_price(symbol)
    if not price:
        return None
    info         = get_instrument_info(symbol)
    qty_step     = float(info.get("qty_step",     0.01))
    min_qty      = float(info.get("min_qty",       qty_step))
    step_dec     = Decimal(str(qty_step))

    avail        = get_usdt_balance()
    max_notional = avail * DEFAULT_LEVERAGE * MARGIN_USE_PCT
    amount       = min(usdt_amount, max_notional, ORDER_USDT_MAX)

    raw_qty     = Decimal(str(amount)) / Decimal(str(price))
    qty_aligned = (raw_qty // step_dec) * step_dec
    if float(qty_aligned) < min_qty:
        qty_aligned = Decimal(str(min_qty))

    # Limit PostOnly all'ask (maker sell)
    ask = get_ask_price(symbol) or 0.0
    if ask > 0:
        ask_str = format_price_ceil(ask, info.get("price_step", 0.01))
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) > 0:
            body = {"category": "linear", "symbol": symbol,
                    "side": "Sell", "orderType": "Limit",
                    "timeInForce": "PostOnly",
                    "qty": qty_str, "price": ask_str,
                    "positionIdx": SHORT_IDX}
            try:
                data = _bybit_signed_post("/v5/order/create", body).json()
                if data.get("retCode") == 0:
                    order_id = data.get("result", {}).get("orderId", "")
                    for _ in range(6):
                        time.sleep(0.5)
                        filled = get_open_short_qty(symbol)
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
                "side": "Sell", "orderType": "Market",
                "qty": qty_str, "positionIdx": SHORT_IDX}
        data = _bybit_signed_post("/v5/order/create", body).json()
        if data.get("retCode") == 0:
            return float(qty_str)
        ret = data.get("retCode")
        if ret == 110007:
            tlog(f"bal_err:{symbol}",
                 f"[SHORT] saldo insufficiente per {symbol}", 300)
            break
        if ret == 170137:
            with _instr_lock:
                _instrument_cache.pop(symbol, None)
            info      = get_instrument_info(symbol)
            qty_step  = float(info.get("qty_step", qty_step))
            step_dec  = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            continue
        if ret in (110125, 110126):
            blocked_symbols.add(symbol)
            tlog(f"short_blocked:{symbol}",
                 f"[SHORT] {symbol} esclusa dai prossimi scan: {data.get('retMsg')}", 3600)
            break
        tlog(f"short_err:{symbol}:{ret}",
             f"[SHORT] retCode={ret} {data.get('retMsg')}", 300)
        break
    return None


# ── TRAILING WORKER (SHORT) ───────────────────────────────────────────────────
def trailing_worker() -> None:
    """
    SL management per short: ratchet floor + ATR trail dal minimo.

    Per short, "proteggere il profitto" = abbassare lo SL (da sopra entry
    verso sotto entry). Lo SL non sale mai — solo scende.

    Logica duale:
      1. Ratchet floor: entry × (1 - floor_lev/100/lev)
         Es. floor=7%, lev=5 → SL a entry × 0.986 (1.4% sotto entry = +7% lev locked)
      2. ATR trail dal minimo: low_water + 2×ATR
         Segue il minimo visto + buffer, si abbassa man mano che il prezzo scende.

    Si usa min(ratchet, trail): lo SL più basso = più profitto locked.
    Attivo dal primo trigger ratchet (+15% lev).
    """
    log("[TRAIL] avviato — ratchet + ATR trail (SHORT)")
    while True:
        try:
            for symbol in list(open_positions):
                entry = get_position(symbol)
                if not entry:
                    continue

                # Allinea eventuali drift tra stato interno e avgPrice reale Bybit.
                ex_qty, ex_entry = get_open_short_fill(symbol)
                if ex_entry > 0:
                    saved_entry = float(entry.get("entry_price", 0) or 0)
                    if saved_entry > 0:
                        drift_pct = abs(ex_entry - saved_entry) / saved_entry * 100
                        if drift_pct >= 0.05:
                            entry["entry_price"] = ex_entry
                            if (not entry.get("breakeven_active")
                                    and not entry.get("partial_tp_active")):
                                sl_now = float(entry.get("sl_price", 0) or 0)
                                if sl_now > ex_entry:
                                    new_r = sl_now - ex_entry
                                    entry["r_dist"] = new_r
                                    entry["orig_r_dist"] = new_r
                            set_position(symbol, entry)
                            log(f"[SYNC-ENTRY] {symbol} avgPrice Bybit {saved_entry:.6f} → {ex_entry:.6f} "
                                f"(drift {drift_pct:.3f}%)")
                            entry = get_position(symbol) or entry

                price_now = get_last_price(symbol)
                if not price_now:
                    continue

                entry_price = float(entry.get("entry_price", 0))
                if entry_price <= 0:
                    continue

                # P&L leveraged per short: positivo quando prezzo scende
                pnl_lev = (entry_price - price_now) / entry_price * 100.0 * DEFAULT_LEVERAGE

                # ── Low water mark (minimo visto) ─────────────────────────────
                low_water = min(price_now, float(entry.get("low_water", price_now)))
                entry["low_water"] = low_water

                # ── Ratchet: trova il tier più alto applicabile ───────────────
                best_trigger_lev = None
                best_floor_lev   = None
                for trigger_lev, floor_lev in RATCHET_TABLE:
                    if pnl_lev >= trigger_lev:
                        best_trigger_lev = trigger_lev
                        best_floor_lev   = floor_lev

                # floor_price SHORT: entry × (1 - floor_lev/100/lev)
                # Questo è SOTTO entry = profitto minimo garantito se il prezzo risale
                floor_price = (
                    entry_price * (1.0 - best_floor_lev / 100.0 / DEFAULT_LEVERAGE)
                    if best_floor_lev is not None else float("inf")
                )

                # ── ATR trail dal minimo (solo dopo primo ratchet) ───────────
                trail_price = float("inf")
                atr_4h_val  = 0.0
                if entry.get("trailing_active"):
                    atr_4h = get_atr_4h(symbol)
                    if atr_4h and atr_4h > 0:
                        atr_4h_val  = atr_4h
                        trail_price = low_water + TRAIL_ATR_MULT * atr_4h

                # ── Candidato: min tra ratchet e ATR trail ────────────────────
                # Per short: SL più basso = più profitto bloccato
                # Entrambi infiniti → niente da fare (primo ticker, pnl negativo)
                if floor_price == float("inf") and trail_price == float("inf"):
                    continue

                new_sl_cand = min(floor_price, trail_price)
                current_sl  = float(entry.get("sl_price", float("inf")))

                # Aggiorna solo se: SL scende (<) e rimane almeno 0.1% sopra prezzo
                if (new_sl_cand < current_sl * 0.9995
                        and new_sl_cand > price_now * 1.001):
                    ok = set_position_stoploss_short(symbol, new_sl_cand)
                    if ok:
                        entry["sl_price"]        = new_sl_cand
                        entry["breakeven_active"] = True
                        if best_floor_lev is not None:
                            entry["trailing_active"] = True
                        set_position(symbol, entry)

                        if trail_price < floor_price:
                            log(f"[TRAIL] {symbol} ✅ ATR trail SHORT: "
                                f"lwm={low_water:.4f} atr={atr_4h_val:.4f} "
                                f"SL→{new_sl_cand:.4f} P&L={pnl_lev:+.1f}%")
                            notify_telegram(
                                f"🎯 Trail SHORT {symbol}\n"
                                f"Prezzo: {price_now:.4f} | Min: {low_water:.4f}\n"
                                f"Trail: {TRAIL_ATR_MULT:.1f}×ATR={TRAIL_ATR_MULT*atr_4h_val:.6f}\n"
                                f"SL → {new_sl_cand:.4f}"
                            )
                        else:
                            log(f"[TRAIL] {symbol} ✅ Ratchet SHORT: "
                                f"P&L={pnl_lev:+.1f}% → floor +{best_floor_lev}% lev "
                                f"SL→{new_sl_cand:.4f}")
                            notify_telegram(
                                f"🔒 Ratchet SHORT {symbol}\n"
                                f"P&L al trigger: {pnl_lev:+.1f}% lev\n"
                                f"Floor garantito: +{best_floor_lev}% lev\n"
                                f"SL → {new_sl_cand:.4f}"
                            )
                    else:
                        log(f"[TRAIL] {symbol} ⚠️ SL update FAIL "
                            f"cand={new_sl_cand:.4f} pnl={pnl_lev:+.1f}%")

                # ── TIME STOP ─────────────────────────────────────────────────
                days_open = (time.time() - float(entry.get("entry_time", time.time()))) / 86400
                if (days_open >= TIME_STOP_DAYS
                        and pnl_lev < TIME_STOP_MIN_LEV
                        and price_now <= entry_price * 1.001):
                    cur_qty = float(entry.get("qty", 0))
                    if cur_qty > 0:
                        ok = market_close_short(symbol, cur_qty)
                        if ok:
                            discard_open(symbol)
                            log(f"[TIME-STOP] {symbol} ✅ chiuso dopo {days_open:.1f}gg "
                                f"pnl={pnl_lev:+.1f}%")
                            notify_telegram(
                                f"⏱️ Time Stop SHORT {symbol}\n"
                                f"Aperto da {days_open:.0f} giorni senza slancio\n"
                                f"P&L: {pnl_lev:+.1f}% lev | Chiuso a {price_now:.4f}"
                            )
                    continue

                # ── PARTIAL TP a 2R ───────────────────────────────────────────
                if (entry.get("trailing_active")
                        and not entry.get("partial_tp_active")):
                    orig_r_dist = float(entry.get("orig_r_dist") or entry.get("r_dist", 0))
                    if orig_r_dist > 0:
                        # Per short: TP è SOTTO entry (prezzo deve scendere di 2R)
                        partial_trigger = entry_price - PARTIAL_TP_R * orig_r_dist
                        if price_now <= partial_trigger:
                            cur_qty   = float(entry.get("qty", 0))
                            close_qty = cur_qty * PARTIAL_TP_PCT
                            if close_qty > 0:
                                instr    = get_instrument_info(symbol)
                                qty_step = float(instr.get("qty_step", 0.01))
                                qty_chk  = _format_qty_with_step(close_qty, qty_step)
                                if float(qty_chk) <= 0:
                                    entry["partial_tp_active"] = True
                                    set_position(symbol, entry)
                                    log(f"[PARTIAL-TP] {symbol} ⚠️ qty troppo piccola, skip")
                                    continue
                                ok = market_close_short(symbol, close_qty)
                                if ok:
                                    entry["partial_tp_active"] = True
                                    entry["qty"] = cur_qty * (1.0 - PARTIAL_TP_PCT)
                                    set_position(symbol, entry)
                                    log(f"[PARTIAL-TP] {symbol} ✅ {PARTIAL_TP_PCT*100:.0f}% chiuso a "
                                        f"+{PARTIAL_TP_R:.1f}R prezzo={price_now:.4f}")
                                    notify_telegram(
                                        f"💰 Partial TP SHORT {symbol}\n"
                                        f"{PARTIAL_TP_PCT*100:.0f}% chiuso a +{PARTIAL_TP_R:.1f}R | "
                                        f"Prezzo: {price_now:.4f}\n"
                                        f"Resto protetto dal ratchet"
                                    )
                                else:
                                    log(f"[PARTIAL-TP] {symbol} ⚠️ FAIL prezzo={price_now:.4f}")
        except Exception as e:
            log(f"[TRAIL] exc: {e}")
        time.sleep(TRAIL_SLEEP_SEC)


# ── SL WATCHDOG ───────────────────────────────────────────────────────────────
def sl_watchdog() -> None:
    """Ogni 10 min verifica che ogni short aperto abbia uno SL impostato su Bybit."""
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
                if pos.get("side") != "Sell":
                    continue
                qty = float(pos.get("size", 0) or 0)
                if qty <= 0:
                    continue
                symbol = pos.get("symbol", "")
                sl_val = float(pos.get("stopLoss", 0) or 0)
                if sl_val > 0:
                    continue   # SL già presente: ok
                entry    = get_position(symbol)
                if not entry:
                    continue
                sl_price = float(entry.get("sl_price", 0))
                if sl_price <= 0:
                    ep       = float(entry.get("entry_price", 0))
                    rd       = float(entry.get("r_dist", ep * 0.04))
                    sl_price = ep + rd   # per short: SL sopra entry
                cur = get_last_price(symbol)
                if cur and sl_price <= cur:
                    sl_price = cur * 1.03   # fallback: 3% sopra
                ok = set_position_stoploss_short(symbol, sl_price)
                if not ok:
                    notify_telegram(
                        f"🚨 SL MANCANTE SHORT {symbol} — reimpostazione FALLITA!\n"
                        f"SL target: {sl_price:.4f} — VERIFICA MANUALE"
                    )
        except Exception as e:
            log(f"[SL-WATCH] exc: {e}")


# ── SYNC POSIZIONI ALL'AVVIO ──────────────────────────────────────────────────
def sync_positions_from_wallet() -> None:
    """
    Al restart, recupera le posizioni short aperte da Bybit.
    Rispetta lo SL già impostato (non lo sovrascrive mai).
    Rileva se il partial TP è già stato eseguito in precedenza.
    """
    log("[SYNC] Scansione posizioni SHORT aperte...")
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
        if pos.get("side") != "Sell":
            continue
        qty = float(pos.get("size", 0) or 0)
        if qty <= 0:
            continue
        symbol      = pos["symbol"]
        entry_price = float(pos.get("avgPrice") or pos.get("entryPrice") or 0)
        if entry_price <= 0:
            continue

        sl_from_bybit   = float(pos.get("stopLoss") or 0)
        trailing_active = float(pos.get("trailingStop", 0) or 0) > 0

        if sl_from_bybit > 0 and sl_from_bybit > entry_price * 1.001:
            # SL sopra entry: normale per short non ancora in profitto
            sl_price        = sl_from_bybit
            r_dist          = sl_price - entry_price
            orig_r_dist     = r_dist
            breakeven_active = False
            set_sl_on_bybit = False
        elif sl_from_bybit > 0 and sl_from_bybit <= entry_price * 1.001:
            # SL a/sotto entry: breakeven già passato (trade in profitto)
            sl_price        = sl_from_bybit
            df_s            = fetch_klines(symbol, interval="240", limit=20)
            atr_s           = entry_price * 0.03
            if df_s is not None and len(df_s) > ATR_WINDOW + 2:
                try:
                    atr_s = float(
                        AverageTrueRange(
                            high=df_s["High"], low=df_s["Low"],
                            close=df_s["Close"], window=ATR_WINDOW,
                        ).average_true_range().iloc[-1]
                    )
                except Exception:
                    pass
            orig_r_dist     = atr_s * 2.0
            r_dist          = orig_r_dist
            breakeven_active = True
            set_sl_on_bybit = False
            log(f"[SYNC] {symbol}: SL={sl_price:.4f} ≤ entry — profitto già bloccato")
        else:
            # Fallback: posizione senza SL — imposta da zero
            df_s    = fetch_klines(symbol, interval="240", limit=20)
            atr_s   = entry_price * 0.03
            if df_s is not None and len(df_s) > ATR_WINDOW + 2:
                try:
                    atr_s = float(
                        AverageTrueRange(
                            high=df_s["High"], low=df_s["Low"],
                            close=df_s["Close"], window=ATR_WINDOW,
                        ).average_true_range().iloc[-1]
                    )
                except Exception:
                    pass
            orig_r_dist     = atr_s * 2.0
            r_dist          = orig_r_dist
            sl_price        = entry_price + r_dist   # per short: SL sopra entry
            breakeven_active = False
            set_sl_on_bybit = True

        if trailing_active:
            breakeven_active = True

        # Partial TP check: se prezzo già sceso di 2R, il partial è già avvenuto
        price_now_sync  = get_last_price(symbol) or 0.0
        partial_trigger = entry_price - PARTIAL_TP_R * orig_r_dist  # sotto entry
        partial_tp_done = (trailing_active
                           and price_now_sync > 0
                           and price_now_sync <= partial_trigger)
        if partial_tp_done:
            log(f"[SYNC] {symbol}: prezzo {price_now_sync:.4f} <= trigger {partial_trigger:.4f} "
                f"— partial TP già eseguito, skip al restart")

        set_position(symbol, {
            "entry_price":       entry_price,
            "sl_price":          sl_price,
            "r_dist":            r_dist,
            "orig_r_dist":       orig_r_dist,
            "qty":               qty,
            "entry_time":        time.time(),
            "trailing_active":   trailing_active,
            "breakeven_active":  breakeven_active,
            "partial_tp_active": partial_tp_done,
        })
        add_open(symbol)
        if set_sl_on_bybit:
            set_position_stoploss_short(symbol, sl_price)
            log(f"[SYNC] SHORT: {symbol} qty={qty} entry={entry_price:.4f} "
                f"SL={sl_price:.4f} (impostato) trail={'SI' if trailing_active else 'NO'}")
        else:
            log(f"[SYNC] SHORT: {symbol} qty={qty} entry={entry_price:.4f} "
                f"SL={sl_price:.4f} (da Bybit) trail={'SI' if trailing_active else 'NO'}")
        trovate += 1

    log(f"[SYNC] {trovate} posizioni SHORT recuperate")


# ── CIRCUIT BREAKER ───────────────────────────────────────────────────────────
def check_circuit_breaker() -> bool:
    global _cb_equity_day_start, _cb_last_day, _cb_triggered, _cb_triggered_at

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if today != _cb_last_day:
        _cb_last_day          = today
        _cb_equity_day_start  = get_total_equity()
        _cb_triggered         = False
        _cb_triggered_at      = 0.0
        log(f"[CB] Reset giornaliero — equity start: {_cb_equity_day_start:.2f} USDT")
        return False

    if _cb_triggered:
        elapsed_h = (time.time() - _cb_triggered_at) / 3600
        if elapsed_h >= CIRCUIT_BREAKER_COOLDOWN_H:
            _cb_triggered        = False
            _cb_equity_day_start = get_total_equity()
            log("[CB] Cooldown scaduto — circuit breaker resettato")
            notify_telegram("✅ Circuit breaker SHORT resettato — trading riattivato")
        return _cb_triggered

    if _cb_equity_day_start <= 0:
        _cb_equity_day_start = get_total_equity()
        return False

    current_equity = get_total_equity()
    if current_equity <= 0:
        return False

    drawdown_pct = (_cb_equity_day_start - current_equity) / _cb_equity_day_start * 100

    if drawdown_pct >= CIRCUIT_BREAKER_PCT:
        _cb_triggered    = True
        _cb_triggered_at = time.time()
        log(f"[CB] 🔴 CIRCUIT BREAKER SHORT — drawdown={drawdown_pct:.2f}%")

        closed = []
        for symbol in list(open_positions):
            pos = get_position(symbol)
            if pos:
                qty = float(pos.get("qty", 0))
                if qty > 0 and market_close_short(symbol, qty):
                    discard_open(symbol)
                    closed.append(symbol)
                    log(f"[CB] {symbol} chiusa")
                else:
                    log(f"[CB] {symbol} ⚠️ FAIL chiusura — verifica manuale!")

        notify_telegram(
            f"🚨 CIRCUIT BREAKER SHORT ATTIVATO\n"
            f"Drawdown: -{drawdown_pct:.1f}%\n"
            f"Chiuse: {', '.join(closed) if closed else 'nessuna'}\n"
            f"Trading bloccato per {CIRCUIT_BREAKER_COOLDOWN_H}h"
        )
        return True

    return False


# ── CICLO PRINCIPALE ──────────────────────────────────────────────────────────
def main_loop() -> None:
    last_scan_ts = 0.0

    while True:
        now = time.time()

        # Aggiorna regime BTC (aggiorna al max ogni 60 min)
        _update_btc_regime()

        # Controlla chiusure (SL colpito su Bybit)
        try:
            resp = _bybit_signed_get("/v5/position/list",
                                     {"category": "linear", "settleCoin": "USDT"})
            rdata = resp.json()
            if rdata.get("retCode") == 0:
                live_shorts = {
                    p["symbol"]
                    for p in rdata["result"]["list"]
                    if p.get("side") == "Sell" and float(p.get("size", 0) or 0) > 0
                }
                for sym in list(open_positions):
                    if sym not in live_shorts:
                        entry = get_position(sym)
                        ep    = float(entry.get("entry_price", 0)) if entry else 0
                        cur   = get_last_price(sym) or 0
                        # Per short: profitto quando prezzo scende sotto entry
                        pnl   = (ep - cur) / ep * 100 if ep else 0
                        log(f"[CLOSE] {sym} SHORT chiusa ~{pnl:+.1f}%")
                        notify_telegram(
                            f"📊 Chiusa SHORT {sym}\n"
                            f"PnL ~{pnl:+.1f}% | Entry: {ep:.4f} | Uscita ~{cur:.4f}"
                        )
                        discard_open(sym)
                        with _state_lock:
                            position_data.pop(sym, None)
        except Exception as e:
            tlog("pos_check_err", f"[MAIN] check pos exc: {e}", 120)

        if now - last_scan_ts < SCAN_INTERVAL_SEC:
            time.sleep(10)
            continue

        last_scan_ts = now
        n_open = len(open_positions)
        log(f"[SCAN] ─── Avvio scansione SHORT ─── open: {n_open}/{MAX_OPEN_POSITIONS}")

        if check_circuit_breaker():
            tlog("circuit_breaker", "🚨 Circuit breaker attivo — scan bloccata", 1800)
            continue

        # Regime gate: solo se BTC in bear regime
        if not _btc_short_ok:
            tlog("btc_regime_off",
                 "⏸️ SHORT BOT IDLE — BTC non in bear regime (sopra EMA50 o slope positiva)",
                 3600)
            continue

        if n_open >= MAX_OPEN_POSITIONS:
            tlog("max_open",
                 f"[SCAN] MAX {MAX_OPEN_POSITIONS} posizioni aperte, attendo", 600)
            continue

        equity_scan = get_total_equity()
        if equity_scan > 0:
            open_risk_usdt = estimate_open_risk_usdt()
            open_risk_pct = open_risk_usdt / equity_scan
            if open_risk_pct >= MAX_TOTAL_OPEN_RISK_PCT:
                tlog("risk_cap_open",
                     f"[RISK-CAP] open risk={open_risk_pct*100:.1f}% >= "
                     f"{MAX_TOTAL_OPEN_RISK_PCT*100:.1f}% — stop nuovi ingressi",
                     300)
                continue

        # 1) Universo: top 100 per volume
        universe = scan_universe()
        log(f"[SCAN] {len(universe)} coin nel universo (vol>{MIN_VOL_24H_USDT/1e6:.0f}M USDT)")
        
        # Diagnostica: mostra top 10 losers
        if universe:
            top10 = universe[:10]
            top10_str = " | ".join([f"{c['symbol']}:{c['chg24h']:+.1f}%" for c in top10])
            log(f"[SCAN] Top 10 losers: {top10_str}")
            log(f"[SCAN] *** #1 LOSER TARGET: {universe[0]['symbol']} ({universe[0]['chg24h']:+.2f}%) ***")

        if not universe:
            continue

        # 2) Per ogni candidato: ranking 24h → segnale di anticipazione 1h
        entered = 0
        checked = 0
        reject_stats_scan = {}
        for rank_idx, coin in enumerate(universe, start=1):
            if rank_idx > TRADE_TOP_N:
                break
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            sym = coin["symbol"]
            chg24h = float(coin["chg24h"])
            if sym in open_positions:
                continue

            # REMOVED: daily downtrend filter on SHORT
            # If a coin is a top loser by 24h momentum, it's already in downtrend.
            # The 4h bounce rejection signal is sufficient quality gate.
            # Previously: if not is_daily_downtrend(sym): continue
            
            time.sleep(0.05)

            checked += 1

            if abs(chg24h) < MIN_ABS_24H_CHANGE:
                reject_stats_scan["chg24h_too_low"] = reject_stats_scan.get("chg24h_too_low", 0) + 1
                continue
            signal = check_short_signal(sym, reject_stats_scan)
            signal_source = "SIGNAL-ANTI"
            if not signal:
                time.sleep(0.05)
                continue

            equity    = get_total_equity()
            if equity <= 0:
                continue
            risk_usdt = equity * RISK_PCT
            open_risk_usdt = estimate_open_risk_usdt()
            if (open_risk_usdt + risk_usdt) > equity * MAX_TOTAL_OPEN_RISK_PCT:
                reject_stats_scan["portfolio_risk_cap"] = reject_stats_scan.get("portfolio_risk_cap", 0) + 1
                continue
            r_dist    = signal["r_dist"]
            entry_px  = signal["entry_price"]
            usdt_val  = (risk_usdt / r_dist) * entry_px

            log(f"[SIGNAL] {sym} SHORT rank#{rank_idx} chg24h={chg24h:+.2f}% src={signal_source} | "
                f"chg1h={signal['chg_1h']:+.2f}% chg4h={signal['chg_4h']:+.2f}% "
                f"rvol={signal['rvol']:.2f}/{signal['min_rvol']:.2f} "
                f"base={signal['base_range']:.2f}%/{signal['base_max']:.2f}% "
                f"normZ={signal['norm_z']:+.2f} | "
                f"EMA20: {signal['ema20_4h']:.4f} | "
                f"dist: -{signal['dist_ema']:.1f}% | RSI: {signal['rsi']:.0f} | "
                f"SL: +{signal['sl_pct']:.1f}% | size: {usdt_val:.1f} USDT")

            set_leverage(sym)
            qty = market_short(sym, usdt_val)
            if not qty or qty <= 0:
                log(f"[ENTRY] {sym} SHORT — ordine fallito")
                continue

            actual_qty, actual_entry_px = get_open_short_fill(sym)
            if actual_qty > 0:
                qty = actual_qty
            if actual_entry_px > 0:
                entry_px = actual_entry_px

            actual_r_dist = signal["sl_price"] - entry_px
            if actual_r_dist <= 0:
                log(f"[ENTRY] {sym} SHORT — r_dist non valido dopo fill reale")
                continue

            sl_price = signal["sl_price"]
            sl_pct = actual_r_dist / entry_px * 100
            set_position(sym, {
                "entry_price":       entry_px,
                "sl_price":          sl_price,
                "r_dist":            actual_r_dist,
                "orig_r_dist":       actual_r_dist,
                "qty":               qty,
                "entry_time":        time.time(),
                "trailing_active":   False,
                "breakeven_active":  False,
                "partial_tp_active": False,
            })
            add_open(sym)
            time.sleep(0.3)
            sl_ok = set_position_stoploss_short(sym, sl_price)
            if not sl_ok:
                log(f"[ENTRY] {sym} SHORT ⚠️ SL non impostato — chiusura di sicurezza")
                market_close_short(sym, qty)
                discard_open(sym)
                with _state_lock:
                    position_data.pop(sym, None)
                continue

            notify_telegram(
                f"📉 ENTRY SHORT {sym} — Anticipation Breakdown (1h)\n"
                f"Rank: #{rank_idx} | 24h: {chg24h:+.2f}% | Src: {signal_source}\n"
                f"1h: {signal['chg_1h']:+.2f}% | 4h: {signal['chg_4h']:+.2f}% | RVOL: {signal['rvol']:.2f}\n"
                f"Entry: {entry_px:.4f} | SL: {sl_price:.4f} (+{sl_pct:.1f}%)\n"
                f"EMA20: {signal['ema20_4h']:.4f} | RSI: {signal['rsi']:.0f}\n"
                f"R-dist: {actual_r_dist:.4f} | Risk: {risk_usdt:.2f} USDT"
            )
            entered += 1
            time.sleep(0.5)

        log(f"[SCAN] {checked} coin verificate | {entered} ingressi | "
            f"posizioni: {len(open_positions)}")
        if reject_stats_scan:
            top_rejects = sorted(reject_stats_scan.items(), key=lambda x: x[1], reverse=True)[:5]
            reject_msg = ", ".join(f"{k}:{v}" for k, v in top_rejects)
            log(f"[REJECT] SHORT top motivi: {reject_msg}")


# ── AVVIO ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_startup_self_checks()
    log("=" * 62)
    log("  TREND FOLLOWING SHORT — 1h ANTICIPATION BREAKDOWN BOT")
    log("=" * 62)
    log(f"  Timeframe : Daily downtrend + 1h segnale | Scan ogni {SCAN_INTERVAL_SEC//60}min")
    log(f"  Filtri    : breakdown base {BASE_LOOKBACK_BARS}h (adaptive p{ADAPTIVE_BASE_WIDTH_PCTL:.2f}) | "
        f"RSI {RSI_MIN_4H:.0f}-{RSI_MAX_4H:.0f} | RVOL adaptive p{ADAPTIVE_RVOL_PCTL:.2f}")
    log(f"  Risk      : {RISK_PCT*100:.1f}%/trade | MAX={MAX_OPEN_POSITIONS} pos | "
        f"Leva {DEFAULT_LEVERAGE}× | Ratchet floor fissi")
    first_trigger, first_floor = RATCHET_TABLE[0]
    log(f"  Exits     : Ratchet(≥{first_trigger}%→+{first_floor}% ... ≥150%→+120%) + "
        f"Partial TP {PARTIAL_TP_PCT*100:.0f}%@{PARTIAL_TP_R:.1f}R")
    log(f"  Regime    : BTC score >= {BTC_SHORT_REGIME_SCORE_MIN:.2f} (EMA-gap + slope)")
    log("=" * 62)

    equity0 = get_total_equity()
    log(f"[AVVIO] Equity: {equity0:.2f} USDT")

    notify_telegram(
        f"📉 SHORT ANTICIPATION BOT AVVIATO\n"
        f"Segnale: breakdown da base compressa + daily downtrend\n"
        f"Regime: BTC daily < EMA50 (slope−) → SHORT attivi\n"
        f"Exit: Ratchet ≥{first_trigger}%→+{first_floor}% ... ≥150%→+120% | "
        f"Partial 50%@{PARTIAL_TP_R:.1f}R\n"
        f"Scan ogni {SCAN_INTERVAL_SEC//60}min | Leva {DEFAULT_LEVERAGE}× | "
        f"Risk {RISK_PCT*100:.1f}%\n"
        f"Equity: {equity0:.2f} USDT"
    )

    sync_positions_from_wallet()
    _update_btc_regime()

    threading.Thread(target=trailing_worker, daemon=True).start()
    threading.Thread(target=sl_watchdog,     daemon=True).start()

    main_loop()
