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

# Filtri segnale 4h — RILASSATI per trend following sui top losers
RSI_MIN_4H    = 10.0   # RSI minimo: tolera oversold su dump
RSI_MAX_4H    = 90.0   # RSI massimo: tolera overbought temporaneo
EMA_TOUCH_TOL = 0.017  # il HIGH deve essere entro 1.7% sotto EMA20 (o sopra)
MAX_DIST_EMA  = 3.0    # % massima close SOTTO EMA20 all'entry (rifiuto fresco)
CLOSE_ABOVE_EMA_TOL = 0.003  # tolleranza 0.3%: accetta close lievemente sopra EMA20
MAX_SL_PCT    = 8.0    # SL massimo accettabile: 8% sopra entry
MIN_BODY_PCT  = 0.0    # corpo candela: TOLTO — i top losers hanno candle piccole
MIN_VOL_RATIO = 0.0    # volume candela: TOLTO — i top losers su quella candela possono avere vol basso 20
MAX_DIST_EMA50_D = 20.0  # daily close max 20% SOTTO EMA50 (non in freefall)
TOP_MOMENTUM_FALLBACK_RANK = 10
MAX_DIST_EMA_MOMENTUM = 12.0

# Regime BTC: attiva short solo quando BTC è strutturalmente bearish
# Se False: bot sempre attivo (solo per testing)
BTC_SHORT_REGIME_CHECK = False

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

_btc_short_ok: bool  = True   # True = BTC in bear regime → short attivi
_btc_ts:       float = 0.0

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
    global _btc_short_ok, _btc_ts
    if time.time() - _btc_ts < 3600:
        return
    if not BTC_SHORT_REGIME_CHECK:
        _btc_short_ok = True
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
            below_ema = btc_d < ema50_now          # BTC sotto EMA50
            slope_neg = ema50_now < ema50_5d       # EMA50 in discesa
            _btc_short_ok = below_ema and slope_neg
            gap_pct   = (btc_d - ema50_now) / ema50_now * 100
            slope_pct = (ema50_now - ema50_5d) / ema50_5d * 100
            tlog("btc_regime",
                 f"[REGIME] BTC={btc_d:,.0f} EMA50d={ema50_now:,.0f} "
                 f"({gap_pct:+.1f}%) slope={slope_pct:+.3f}%/5gg | "
                 f"SHORT={'✅ ON' if _btc_short_ok else '⏸️ OFF (BTC non bearish)'}",
                 3600)
        else:
            _btc_short_ok = False  # dati insufficienti: non shortare
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


# ── SIGNAL CHECK 4h SHORT ─────────────────────────────────────────────────────
def check_short_signal(symbol: str, reject_stats: Optional[dict] = None) -> Optional[dict]:
    """
    Verifica bounce rejection all'EMA20 su 4h (ultima candela CHIUSA).

    Condizioni (speculari al long):
    1. Massimo della candela >= EMA20 × (1 - toleranza): il rimbalzo ha toccato la resistenza
    2. Close < EMA20: chiude sotto (rifiuto confermato, i bears hanno ripreso)
    3. Close < Open: candela rossa
    4. RSI tra 35 e 65: bounce sano, non oversold né overbought
    5. Close entro MAX_DIST_EMA% sotto EMA20: rifiuto fresco, non già esteso
    6. Corpo >= MIN_BODY_PCT del range: niente doji/pin bar
    7. Volume >= MIN_VOL_RATIO × media 20: distribuzione reale, non rimbalzo su vuoto
    8. SL = swing high (max 3 barre) + 0.3×ATR
    """
    def reject(reason: str) -> Optional[dict]:
        if reject_stats is not None:
            reject_stats[reason] = reject_stats.get(reason, 0) + 1
        return None

    df = fetch_klines(symbol, interval="240", limit=50)
    if df is None or len(df) < 25:
        return reject("kline_insufficient")

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    o = df["Open"]

    ema20      = c.ewm(span=20, adjust=False).mean()
    atr_series = AverageTrueRange(high=h, low=l, close=c,
                                  window=ATR_WINDOW).average_true_range()
    rsi_series = RSIIndicator(close=c, window=14).rsi()

    # Ultima candela CHIUSA (non quella in formazione)
    last_close = float(c.iloc[-2])
    last_open  = float(o.iloc[-2])
    last_high  = float(h.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    last_rsi   = float(rsi_series.iloc[-2])
    last_atr   = float(atr_series.iloc[-2])

    if pd.isna(last_rsi) or pd.isna(last_atr) or last_atr <= 0:
        return reject("invalid_rsi_or_atr")
    if last_ema20 <= 0:
        return reject("invalid_ema20")

    # 1) Il massimo ha toccato EMA20 da sotto (bounce verso resistenza)
    #    Accetta se high >= EMA20 * (1 - tolleranza) — anche se non ha sfondato
    if last_high < last_ema20 * (1.0 - EMA_TOUCH_TOL):
        return reject("ema20_not_touched")

    # 2) Close sotto EMA20 — rifiuto confermato
    #    tollera piccola violazione (0.2%) per evitare falsi scarti su wick/rounding.
    if last_close > last_ema20 * (1.0 + CLOSE_ABOVE_EMA_TOL):
        return reject("close_above_ema20")

    # 3) Candela rossa
    if last_close >= last_open:
        return reject("not_red_candle")

    # 4) RSI nel range bounce sano
    if not (RSI_MIN_4H <= last_rsi <= RSI_MAX_4H):
        return reject("rsi_out_of_range")

    # 5) Close non troppo lontano da EMA20 (rifiuto fresco)
    dist_pct = (last_ema20 - last_close) / last_ema20 * 100
    if dist_pct > MAX_DIST_EMA:
        return reject("distance_from_ema_too_high")

    # 6) Qualità candela: corpo reale, non doji
    candle_range = float(h.iloc[-2]) - float(l.iloc[-2])
    if candle_range > 0:
        body_pct = abs(last_close - last_open) / candle_range * 100
        if body_pct < MIN_BODY_PCT:
            return reject("body_too_small")

    # 7) Volume: distribuzione reale (non rimbalzo tecnico su aria)
    vol_series = df["Volume"]
    vol_avg = float(vol_series.iloc[-22:-2].mean())
    vol_sig = float(vol_series.iloc[-2])
    if vol_avg > 0 and vol_sig / vol_avg < MIN_VOL_RATIO:
        return reject("volume_too_low")

    # 8) SL = sopra swing high delle ultime 3 barre chiuse
    swing_high = max(float(h.iloc[-2]), float(h.iloc[-3]), float(h.iloc[-4]))
    sl_price   = swing_high + SL_ATR_BUFFER * last_atr
    r_dist     = sl_price - last_close

    sl_pct = r_dist / last_close * 100
    if sl_pct > MAX_SL_PCT or r_dist <= 0:
        return reject("sl_too_wide_or_invalid")

    # Diagnostica slope EMA20(4h)
    ema20_3ago = float(ema20.iloc[-5])
    slope_pct  = (last_ema20 - ema20_3ago) / ema20_3ago * 100 if ema20_3ago > 0 else 0.0
    slope_ok   = slope_pct < 0   # per short: slope negativa è conferma bearish
    log(f"[DIAG-SLOPE] {symbol}: EMA20_slope={slope_pct:+.3f}% "
        f"({'OK discesa' if slope_ok else 'WARN salita'}) | "
        f"RSI={last_rsi:.1f} dist={dist_pct:.2f}% sl={sl_pct:.2f}%")

    # Diag-only: slope non blocca l'entry.

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
    }


def check_short_momentum_signal(symbol: str, reject_stats: Optional[dict] = None) -> Optional[dict]:
    def reject(reason: str) -> Optional[dict]:
        if reject_stats is not None:
            reject_stats[reason] = reject_stats.get(reason, 0) + 1
        return None

    df = fetch_klines(symbol, interval="240", limit=50)
    if df is None or len(df) < 25:
        return reject("kline_insufficient")

    c = df["Close"]
    h = df["High"]
    l = df["Low"]
    o = df["Open"]

    ema20 = c.ewm(span=20, adjust=False).mean()
    atr_series = AverageTrueRange(high=h, low=l, close=c,
                                  window=ATR_WINDOW).average_true_range()
    rsi_series = RSIIndicator(close=c, window=14).rsi()

    last_close = float(c.iloc[-2])
    last_open = float(o.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    last_rsi = float(rsi_series.iloc[-2])
    last_atr = float(atr_series.iloc[-2])

    if pd.isna(last_rsi) or pd.isna(last_atr) or last_atr <= 0 or last_ema20 <= 0:
        return reject("invalid_rsi_or_atr")

    if last_close >= last_open:
        return reject("not_red_candle")

    dist_pct = (last_ema20 - last_close) / last_ema20 * 100
    if dist_pct < 0 or dist_pct > MAX_DIST_EMA_MOMENTUM:
        return reject("distance_from_ema_too_high")

    swing_high = max(float(h.iloc[-2]), float(h.iloc[-3]), float(h.iloc[-4]))
    sl_price = swing_high + SL_ATR_BUFFER * last_atr
    r_dist = sl_price - last_close
    sl_pct = r_dist / last_close * 100
    if sl_pct > MAX_SL_PCT or r_dist <= 0:
        return reject("sl_too_wide_or_invalid")

    log(f"[SIGNAL-MOMO] {symbol} SHORT | EMA20: {last_ema20:.4f} | dist: -{dist_pct:.1f}% | "
        f"RSI: {last_rsi:.0f} | SL: +{sl_pct:.1f}%")

    return {
        "entry_price": last_close,
        "sl_price": sl_price,
        "r_dist": r_dist,
        "atr": last_atr,
        "rsi": last_rsi,
        "ema20_4h": last_ema20,
        "dist_ema": dist_pct,
        "sl_pct": sl_pct,
        "ema20_slope": 0.0,
    }


# ── ORDINI ────────────────────────────────────────────────────────────────────
def set_position_stoploss_short(symbol: str, sl_price: float) -> bool:
    """
    Imposta SL per posizione short.
    Per uno short, lo SL deve essere SOPRA il prezzo corrente.
    Man mano che il trade va in profitto (prezzo scende), lo SL viene abbassato.
    """
    cur = get_last_price(symbol)
    if cur and sl_price <= cur * 1.0005:
        tlog(f"sl_skip:{symbol}",
             f"[SL] {symbol} SL={sl_price:.6f} <= prezzo={cur:.6f}, skip", 300)
        return False
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
                                f"P&L: {pnl_lev:+.1f}% lev\n"
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

        # Circuit breaker disabilitato: attendendo verifica ranking
        # Reabilitare dopo aver confermato che il ranking funziona correttamente
        # if check_circuit_breaker():
        #     tlog("circuit_breaker", "🚨 Circuit breaker attivo — scan bloccata", 1800)
        #     continue

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

        # 2) Per ogni candidato: ranking 24h → segnale 4h
        entered = 0
        checked = 0
        reject_stats_scan = {}
        for rank_idx, coin in enumerate(universe, start=1):
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            sym = coin["symbol"]
            if sym in open_positions:
                continue

            # REMOVED: daily downtrend filter on SHORT
            # If a coin is a top loser by 24h momentum, it's already in downtrend.
            # The 4h bounce rejection signal is sufficient quality gate.
            # Previously: if not is_daily_downtrend(sym): continue
            
            time.sleep(0.05)

            checked += 1

            signal = check_short_signal(sym, reject_stats_scan)
            if not signal and rank_idx <= TOP_MOMENTUM_FALLBACK_RANK:
                signal = check_short_momentum_signal(sym, reject_stats_scan)
            if not signal:
                time.sleep(0.05)
                continue

            equity    = get_total_equity()
            if equity <= 0:
                continue
            risk_usdt = equity * RISK_PCT
            r_dist    = signal["r_dist"]
            entry_px  = signal["entry_price"]
            usdt_val  = (risk_usdt / r_dist) * entry_px

            log(f"[SIGNAL] {sym} SHORT | EMA20: {signal['ema20_4h']:.4f} | "
                f"dist: -{signal['dist_ema']:.1f}% | RSI: {signal['rsi']:.0f} | "
                f"SL: +{signal['sl_pct']:.1f}% | size: {usdt_val:.1f} USDT")

            set_leverage(sym)
            qty = market_short(sym, usdt_val)
            if not qty or qty <= 0:
                log(f"[ENTRY] {sym} SHORT — ordine fallito")
                continue

            sl_price = signal["sl_price"]
            set_position(sym, {
                "entry_price":       entry_px,
                "sl_price":          sl_price,
                "r_dist":            r_dist,
                "orig_r_dist":       r_dist,
                "qty":               qty,
                "entry_time":        time.time(),
                "trailing_active":   False,
                "breakeven_active":  False,
                "partial_tp_active": False,
            })
            add_open(sym)
            time.sleep(0.3)
            set_position_stoploss_short(sym, sl_price)

            notify_telegram(
                f"📉 ENTRY SHORT {sym} — Bounce EMA20(4h)\n"
                f"Entry: {entry_px:.4f} | SL: {sl_price:.4f} (+{signal['sl_pct']:.1f}%)\n"
                f"EMA20: {signal['ema20_4h']:.4f} | RSI: {signal['rsi']:.0f}\n"
                f"R-dist: {r_dist:.4f} | Risk: {risk_usdt:.2f} USDT"
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
    log("=" * 62)
    log("  TREND FOLLOWING SHORT — 4h EMA20 BOUNCE REJECTION BOT")
    log("=" * 62)
    log(f"  Timeframe : Daily downtrend + 4h segnale | Scan ogni {SCAN_INTERVAL_SEC//60}min")
    log(f"  Filtri    : EMA50 daily (slope−) | EMA20(4h) bounce reject | "
        f"RSI {RSI_MIN_4H:.0f}-{RSI_MAX_4H:.0f}")
    log(f"  Risk      : {RISK_PCT*100:.1f}%/trade | MAX={MAX_OPEN_POSITIONS} pos | "
        f"Leva {DEFAULT_LEVERAGE}× | Ratchet floor fissi")
    first_trigger, first_floor = RATCHET_TABLE[0]
    log(f"  Exits     : Ratchet(≥{first_trigger}%→+{first_floor}% ... ≥150%→+120%) + "
        f"Partial TP {PARTIAL_TP_PCT*100:.0f}%@{PARTIAL_TP_R:.1f}R")
    log(f"  Regime    : BTC daily < EMA50 + slope negativa → SHORT ON")
    log("=" * 62)

    equity0 = get_total_equity()
    log(f"[AVVIO] Equity: {equity0:.2f} USDT")

    notify_telegram(
        f"📉 SHORT PULLBACK BOT AVVIATO\n"
        f"Segnale: EMA20(4h) bounce rejection + daily downtrend\n"
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
