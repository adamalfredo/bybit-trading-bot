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
import re
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

SL_ATR_BUFFER    = 0.3   # buffer aggiuntivo sotto swing low (× ATR)
TRAIL_ATR_MULT   = 2.0   # moltiplicatore ATR per il trailing stop dal massimo
PARTIAL_TP_R     = 1.5  # partial TP: chiude 50% posizione a +1.5R di guadagno
PARTIAL_TP_PCT   = 0.25 # quota da chiudere al partial TP

# Ratchet floor fissi: (roi_lev_trigger%, floor_lev_garantito%)
# Quando il P&L leveraged supera il trigger, SL si sposta al floor garantito
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
ATR_WINDOW       = 14

# Universo
MIN_VOL_24H_USDT = 10_000_000  # solo coin liquide: >10M USDT/giorno
COINS_TOP_N      = 100         # top N per volume da scansionare
TRADE_TOP_N      = 12          # apre trade solo entro i primi N del ranking 24h

# Filtri segnale — RILASSATI per trend following sui top mover
RSI_MIN_4H     = 10.0   # RSI minimo: tolera oversold su pump
RSI_MAX_4H     = 90.0   # RSI massimo: tolera overbought su pump
EMA_TOUCH_TOL  = 0.017  # il low deve essere entro 1.7% sopra EMA20 (o sotto)
MAX_DIST_EMA   = 3.0    # % massima close sopra EMA20 all'entry
CLOSE_BELOW_EMA_TOL = 0.003  # tolleranza 0.3%: accetta close lievemente sotto EMA20
MAX_SL_PCT     = 8.0    # SL massimo accettabile: 8% sotto entry
MIN_BODY_PCT   = 0.0    # corpo candela: TOLTO — i top mover hanno candle piccole
MIN_VOL_RATIO  = 0.0    # volume candela: TOLTO — i top mover su quella candela possono avere vol basso
MAX_DIST_EMA50_D = 20.0 # daily close max 20% sopra EMA50: evita trend overestesi
TOP_MOMENTUM_FALLBACK_RANK = 10
MAX_DIST_EMA_MOMENTUM = 12.0

# BTC filter — disabilitato: qualsiasi EMA a lungo periodo su BTC è sopra 65k
# per mesi dopo il picco a 100k. Il filtro individuale daily EMA50 per ogni coin
# è già sufficiente come protezione.
BTC_BULL_CHECK          = False  # disabilitato
BTC_WEEKLY_EMA200_CHECK = False  # disabilitato

# Timing
SCAN_INTERVAL_SEC  = 1800   # 30 min tra scan
TRAIL_SLEEP_SEC    = 60
SL_WATCH_SLEEP_SEC = 600    # 10 min
LONG_IDX           = 1

# Time stop: chiude i trade "coricati" che non vanno da nessuna parte
TIME_STOP_DAYS    = 10     # giorni massimi in posizione senza slancio
TIME_STOP_MIN_LEV = 10.0  # soglia: se P&L lev < 10% dopo N giorni → esci a breakeven

# Circuit breaker: daily loss limit
CIRCUIT_BREAKER_PCT       = 3.0   # drawdown % giornaliero max prima di bloccare tutto
CIRCUIT_BREAKER_COOLDOWN_H = 24   # ore di blocco dopo attivazione

# Time stop: chiude i trade "coricati" che non vanno da nessuna parte
TIME_STOP_DAYS    = 10     # giorni massimi in posizione
TIME_STOP_MIN_LEV = 10.0  # se dopo N giorni P&L lev < 10%, esce a breakeven

EXCLUDE_SUBSTRINGS = ["USDC", "BUSD", "DAI", "TUSD", "FRAX",
                      "3LUSDT", "3SUSDT", "BULLUSDT", "BEARUSDT"]

# ── STATO GLOBALE ─────────────────────────────────────────────────────────────
open_positions:   set  = set()
blocked_symbols:  set  = set()
position_data:    dict = {}
_state_lock             = threading.RLock()
_instr_lock             = threading.RLock()
_instrument_cache: dict = {}
_price_cache:     dict  = {}
_price_lock             = threading.RLock()
_last_log_times:  dict  = {}
_btc_ok:          bool  = True
_btc_ts:          float = 0.0
_bybit_ts_offset_ms: int = 0

# Circuit breaker state
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
            data={"chat_id": TELEGRAM_CHAT_ID, "text": f"[PULLBACK] {msg}"},
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
    if not (1 <= TOP_MOMENTUM_FALLBACK_RANK <= COINS_TOP_N):
        errs.append("TOP_MOMENTUM_FALLBACK_RANK invalido")
    if not (1 <= TRADE_TOP_N <= COINS_TOP_N):
        errs.append("TRADE_TOP_N invalido")
    if TRADE_TOP_N < TOP_MOMENTUM_FALLBACK_RANK:
        errs.append("TRADE_TOP_N deve essere >= TOP_MOMENTUM_FALLBACK_RANK")
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


def get_open_long_fill(symbol: str) -> tuple[float, float]:
    try:
        resp = _bybit_signed_get("/v5/position/list",
                                 {"category": "linear", "symbol": symbol})
        data = resp.json()
        if data.get("retCode") != 0:
            return 0.0, 0.0
        for pos in data.get("result", {}).get("list", []):
            if pos.get("side") == "Buy":
                qty = float(pos.get("size", 0) or 0)
                entry_price = float(pos.get("avgPrice", 0) or 0)
                return qty, entry_price
    except Exception:
        pass
    return 0.0, 0.0


# ── BTC FILTER ────────────────────────────────────────────────────────────────
def _update_btc_filter() -> None:
    """
    Filtro regime semplificato:
      BTC daily close (ultima candela chiusa) > EMA200 daily.
      EMA200 rappresenta il trend strutturale di lungo periodo.
      Nessun check di slope: la slope EMA200 è quasi sempre positiva in bull,
      negativa in bear — non serve calcolarla esplicitamente.
    """
    global _btc_ok, _btc_ts
    if time.time() - _btc_ts < 3600:
        return
    if not BTC_BULL_CHECK:
        _btc_ok = True
        _btc_ts = time.time()
        return
    try:
        df_d = fetch_klines("BTCUSDT", interval="D", limit=210)
        if df_d is not None and len(df_d) >= 201:
            close_d    = df_d["Close"]
            ema200_d   = close_d.ewm(span=200, adjust=False).mean()
            btc_d      = float(close_d.iloc[-2])   # ultima candela CHIUSA
            ema200_now = float(ema200_d.iloc[-2])
            _btc_ok    = btc_d >= ema200_now
            gap_pct    = (btc_d - ema200_now) / ema200_now * 100
            tlog("btc_filter",
                 f"[BTC] EMA200d={ema200_now:,.0f} | BTC={btc_d:,.0f} "
                 f"({gap_pct:+.1f}%) | "
                 f"gate={'OK ✅' if _btc_ok else 'CHIUSO 🚫 (sotto EMA200d)'}", 3600)
        else:
            _btc_ok = True  # dati insufficienti: non bloccare
    except Exception:
        pass
    _btc_ts = time.time()


# ── SCANSIONE UNIVERSO ────────────────────────────────────────────────────────
def scan_universe() -> list:
    """
    Ritorna le top COINS_TOP_N coin per momentum rialzista 24h,
    mantenendo il filtro di liquidità (>10M USDT).
    Nessuna soglia hard su momentum: ordina per variazione 24h e prende i migliori.
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

    # Top gainers prima, poi volume per spezzare i pari-merito.
    candidates.sort(key=lambda x: (x["chg24h"], x["vol24h"]), reverse=True)
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
def check_entry_signal(symbol: str, reject_stats: Optional[dict] = None) -> Optional[dict]:
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

    # Ultima candela CHIUSA
    last_close = float(c.iloc[-2])
    last_open  = float(o.iloc[-2])
    last_low   = float(l.iloc[-2])
    last_ema20 = float(ema20.iloc[-2])
    last_rsi   = float(rsi_series.iloc[-2])
    last_atr   = float(atr_series.iloc[-2])

    if pd.isna(last_rsi) or pd.isna(last_atr) or last_atr <= 0:
        return reject("invalid_rsi_or_atr")
    if last_ema20 <= 0:
        return reject("invalid_ema20")

    # 1) Il minimo ha toccato EMA20 (o è sceso sotto)
    if last_low > last_ema20 * (1 + EMA_TOUCH_TOL):
        return reject("ema20_not_touched")

    # 2) Close sopra EMA20 — rimbalzo confermato
    #    tollera piccola violazione (0.2%) per evitare falsi scarti su wick/rounding.
    if last_close < last_ema20 * (1.0 - CLOSE_BELOW_EMA_TOL):
        return reject("close_below_ema20")

    # 3) Candela verde
    if last_close <= last_open:
        return reject("not_green_candle")

    # 4) RSI nel range pullback
    if not (RSI_MIN_4H <= last_rsi <= RSI_MAX_4H):
        return reject("rsi_out_of_range")

    # 5) Non esteso: close entro MAX_DIST_EMA% sopra EMA20
    dist_pct = (last_close - last_ema20) / last_ema20 * 100
    if dist_pct > MAX_DIST_EMA:
        return reject("distance_from_ema_too_high")

    # 6) Qualità candela: corpo >= MIN_BODY_PCT% del range totale
    #    Filtra shooting star, doji, hammer invertiti
    candle_range = float(h.iloc[-2]) - float(l.iloc[-2])
    if candle_range > 0:
        body_pct = abs(last_close - last_open) / candle_range * 100
        if body_pct < MIN_BODY_PCT:
            return reject("body_too_small")

    # 7) Conferma volume: la candela segnale deve avere volume >= MIN_VOL_RATIO
    #    rispetto alla media delle ultime 20 candele chiuse
    #    Un rimbalzo su volume debole non ha domanda reale
    vol_series = df["Volume"]
    vol_avg = float(vol_series.iloc[-22:-2].mean())
    vol_sig = float(vol_series.iloc[-2])
    if vol_avg > 0 and vol_sig / vol_avg < MIN_VOL_RATIO:
        return reject("volume_too_low")

    # 8) SL = sotto swing low delle ultime 3 barre chiuse
    swing_low = min(float(l.iloc[-2]), float(l.iloc[-3]), float(l.iloc[-4]))
    sl_price  = swing_low - SL_ATR_BUFFER * last_atr
    r_dist    = last_close - sl_price

    # Sanity: SL non troppo lontano
    sl_pct = r_dist / last_close * 100
    if sl_pct > MAX_SL_PCT or r_dist <= 0:
        return reject("sl_too_wide_or_invalid")

    # ── DIAG: slope EMA20(4h) — non filtra ancora, solo logging ──────────────
    # slope = variazione % EMA20 tra candela corrente e 3 barre fa
    # slope > 0 = EMA20 in salita (pullback sano)
    # slope ≤ 0 = EMA20 piatta/in calo (potenziale breakdown, da filtrare)
    ema20_3ago   = float(ema20.iloc[-5])   # 3 barre chiuse fa
    slope_pct    = (last_ema20 - ema20_3ago) / ema20_3ago * 100 if ema20_3ago > 0 else 0.0
    slope_ok     = slope_pct > 0
    log(f"[DIAG-SLOPE] {symbol}: EMA20_slope={slope_pct:+.3f}% "
        f"({'OK salita' if slope_ok else 'WARN piatta/discesa'}) | "
        f"RSI={last_rsi:.1f} dist_ema={dist_pct:.2f}% sl_pct={sl_pct:.2f}%")

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


def check_momentum_entry_signal(symbol: str, reject_stats: Optional[dict] = None) -> Optional[dict]:
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

    if last_close <= last_open:
        return reject("not_green_candle")

    dist_pct = (last_close - last_ema20) / last_ema20 * 100
    if dist_pct < 0 or dist_pct > MAX_DIST_EMA_MOMENTUM:
        return reject("distance_from_ema_too_high")

    swing_low = min(float(l.iloc[-2]), float(l.iloc[-3]), float(l.iloc[-4]))
    sl_price = swing_low - SL_ATR_BUFFER * last_atr
    r_dist = last_close - sl_price
    sl_pct = r_dist / last_close * 100
    if sl_pct > MAX_SL_PCT or r_dist <= 0:
        return reject("sl_too_wide_or_invalid")

    log(f"[SIGNAL-MOMO] {symbol} | EMA20: {last_ema20:.4f} | dist: +{dist_pct:.1f}% | "
        f"RSI: {last_rsi:.0f} | SL: -{sl_pct:.1f}%")

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
        ret  = data.get("retCode")
        if ret == 0:
            return True
        log(f"[TRAIL] {symbol} API FAIL retCode={ret} msg={data.get('retMsg')}")
        return False
    except Exception as e:
        log(f"[TRAIL] {symbol} exc: {e}")
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


def market_close_partial(symbol: str, qty: float) -> bool:
    """Chiude parzialmente la posizione long (reduce-only, market order)."""
    info     = get_instrument_info(symbol)
    qty_step = float(info.get("qty_step", 0.01))
    qty_str  = _format_qty_with_step(qty, qty_step)
    if float(qty_str) <= 0:
        return False
    body = {"category": "linear", "symbol": symbol,
            "side": "Sell", "orderType": "Market",
            "qty": qty_str, "reduceOnly": True,
            "positionIdx": LONG_IDX}
    try:
        data = _bybit_signed_post("/v5/order/create", body).json()
        ret  = data.get("retCode")
        if ret == 0:
            return True
        log(f"[PARTIAL-TP] {symbol} FAIL retCode={ret} {data.get('retMsg')}")
        return False
    except Exception as e:
        log(f"[PARTIAL-TP] {symbol} exc: {e}")
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
        if ret in (110125, 110126):
            blocked_symbols.add(symbol)
            tlog(f"long_blocked:{symbol}",
                 f"[LONG] {symbol} esclusa dai prossimi scan: {data.get('retMsg')}", 3600)
            break
        tlog(f"long_err:{symbol}:{ret}",
             f"[LONG] retCode={ret} {data.get('retMsg')}", 300)
        break
    return None


# ── TRAILING WORKER ───────────────────────────────────────────────────────────
def trailing_worker() -> None:
    """
    SL management: ratchet floor fissi + ATR trail dal massimo.
    Il SL non scende mai — solo sale. Usa il migliore tra:
      1. Ratchet floor garantito (tabella fissa)
      2. ATR trail: high_water - 2×ATR(4h), attivo appena il ratchet scatta ≥15% lev
    """
    log("[TRAIL] avviato — ratchet + ATR trail")
    while True:
        try:
            for symbol in list(open_positions):
                entry = get_position(symbol)
                if not entry:
                    continue

                # Allinea eventuali drift tra stato interno e avgPrice reale Bybit.
                ex_qty, ex_entry = get_open_long_fill(symbol)
                if ex_entry > 0:
                    saved_entry = float(entry.get("entry_price", 0) or 0)
                    if saved_entry > 0:
                        drift_pct = abs(ex_entry - saved_entry) / saved_entry * 100
                        if drift_pct >= 0.05:
                            entry["entry_price"] = ex_entry
                            if (not entry.get("breakeven_active")
                                    and not entry.get("partial_tp_active")):
                                sl_now = float(entry.get("sl_price", 0) or 0)
                                if 0 < sl_now < ex_entry:
                                    new_r = ex_entry - sl_now
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

                # P&L leveraged corrente (%)
                pnl_lev = (price_now - entry_price) / entry_price * 100.0 * DEFAULT_LEVERAGE

                # ── High water mark (massimo visto dalla prima attivazione) ──
                high_water = max(price_now, float(entry.get("high_water", price_now)))
                entry["high_water"] = high_water

                # ── Ratchet: trova il floor più alto applicabile ──────────
                best_trigger_lev = None
                best_floor_lev   = None
                for trigger_lev, floor_lev in RATCHET_TABLE:
                    if pnl_lev >= trigger_lev:
                        best_trigger_lev = trigger_lev
                        best_floor_lev   = floor_lev

                floor_price = (
                    entry_price * (1.0 + best_floor_lev / 100.0 / DEFAULT_LEVERAGE)
                    if best_floor_lev is not None else 0.0
                )

                # ── ATR trail dal massimo (solo quando ratchet già scattato) ──
                trail_price = 0.0
                atr_4h_val  = 0.0
                if entry.get("trailing_active"):
                    atr_4h = get_atr_4h(symbol)
                    if atr_4h and atr_4h > 0:
                        atr_4h_val  = atr_4h
                        trail_price = high_water - TRAIL_ATR_MULT * atr_4h

                # ── Candidato migliore: max tra ratchet e ATR trail ────────
                new_sl_cand = max(floor_price, trail_price)
                current_sl  = float(entry.get("sl_price", 0))

                if new_sl_cand > current_sl * 1.0005:
                    ok = set_position_stoploss_long(symbol, new_sl_cand)
                    if ok:
                        entry["sl_price"]         = new_sl_cand
                        entry["breakeven_active"]  = True
                        if best_floor_lev is not None:
                            entry["trailing_active"] = True  # abilita partial TP
                        set_position(symbol, entry)

                        if trail_price > floor_price:
                            # ATR trail più stretto del ratchet
                            log(f"[TRAIL] {symbol} ✅ ATR trail: "
                                f"hwm={high_water:.4f} atr={atr_4h_val:.4f} "
                                f"SL→{new_sl_cand:.4f} P&L={pnl_lev:+.1f}%")
                            notify_telegram(
                                f"🎯 Trail attivato {symbol}\n"
                                f"Prezzo: {price_now:.4f} | High: {high_water:.4f}\n"
                                f"Trail dist: {TRAIL_ATR_MULT * atr_4h_val:.6f} "
                                f"({TRAIL_ATR_MULT:.1f}×ATR)\n"
                                f"SL → {new_sl_cand:.4f}"
                            )
                        else:
                            # Ratchet floor più alto
                            log(f"[TRAIL] {symbol} ✅ Ratchet: P&L={pnl_lev:+.1f}% "
                                f"→ floor +{best_floor_lev}% lev "
                                f"SL→{new_sl_cand:.4f}")
                            notify_telegram(
                                f"🔒 Ratchet {symbol}\n"
                                f"P&L al trigger: {pnl_lev:+.1f}% lev\n"
                                f"Floor garantito: +{best_floor_lev}% lev\n"
                                f"SL → {new_sl_cand:.4f}"
                            )
                    else:
                        log(f"[TRAIL] {symbol} ⚠️ SL update FAIL "
                            f"cand={new_sl_cand:.4f} pnl={pnl_lev:+.1f}%")

                # ── TIME STOP: trade coricato dopo N giorni ───────────────────
                days_open = (time.time() - float(entry.get("entry_time", time.time()))) / 86400
                if (days_open >= TIME_STOP_DAYS
                        and pnl_lev < TIME_STOP_MIN_LEV
                        and price_now >= entry_price * 0.999):
                    cur_qty = float(entry.get("qty", 0))
                    if cur_qty > 0:
                        ok = market_close_partial(symbol, cur_qty)
                        if ok:
                            discard_open(symbol)
                            log(f"[TIME-STOP] {symbol} ✅ chiuso dopo {days_open:.1f}gg "
                                f"pnl={pnl_lev:+.1f}% prezzo={price_now:.4f}")
                            notify_telegram(
                                f"⏱️ Time Stop {symbol}\n"
                                f"Trade aperto da {days_open:.0f} giorni senza slancio\n"
                                f"P&L: {pnl_lev:+.1f}% lev | Chiuso a {price_now:.4f}\n"
                                f"Capitale liberato per nuove opportunità"
                            )
                        else:
                            log(f"[TIME-STOP] {symbol} ⚠️ FAIL chiusura dopo {days_open:.1f}gg")
                    continue

                # ── PARTIAL TP a 2R ───────────────────────────────────────────
                if (entry.get("trailing_active")
                        and not entry.get("partial_tp_active")):
                    orig_r_dist = float(entry.get("orig_r_dist") or entry.get("r_dist", 0))
                    if orig_r_dist > 0:
                        partial_trigger = entry_price + PARTIAL_TP_R * orig_r_dist
                        if price_now >= partial_trigger:
                            cur_qty   = float(entry.get("qty", 0))
                            close_qty = cur_qty * PARTIAL_TP_PCT
                            if close_qty > 0:
                                # Controlla se qty è esprimibile con il qty_step del simbolo.
                                # Se è troppo piccola (es. dopo restart con residuo già dimezzato),
                                # segnala e skippa per evitare il loop infinito.
                                instr     = get_instrument_info(symbol)
                                qty_step  = float(instr.get("qty_step", 0.01))
                                qty_check = _format_qty_with_step(close_qty, qty_step)
                                if float(qty_check) <= 0:
                                    entry["partial_tp_active"] = True
                                    set_position(symbol, entry)
                                    log(f"[PARTIAL-TP] {symbol} ⚠️ qty {close_qty:.6f} "
                                        f"< step {qty_step} — partial già fatto, skip")
                                    continue
                                ok = market_close_partial(symbol, close_qty)
                                if ok:
                                    entry["partial_tp_active"] = True
                                    entry["qty"] = cur_qty * (1.0 - PARTIAL_TP_PCT)
                                    set_position(symbol, entry)
                                    log(f"[PARTIAL-TP] {symbol} ✅ {PARTIAL_TP_PCT*100:.0f}% chiuso a "
                                        f"+{PARTIAL_TP_R:.1f}R prezzo={price_now:.4f} "
                                        f"qty={close_qty:.4f}")
                                    notify_telegram(
                                        f"💰 Partial TP {symbol}\n"
                                        f"{PARTIAL_TP_PCT*100:.0f}% chiuso a +{PARTIAL_TP_R:.1f}R | "
                                        f"Prezzo: {price_now:.4f}\n"
                                        f"Resto protetto dal ratchet"
                                    )
                                else:
                                    log(f"[PARTIAL-TP] {symbol} ⚠️ FAIL "
                                        f"prezzo={price_now:.4f}")
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
        sl_from_bybit   = float(pos.get("stopLoss") or 0)
        trailing_active = float(pos.get("trailingStop", 0) or 0) > 0

        if sl_from_bybit > 0 and sl_from_bybit < entry_price * 0.999:
            # Caso normale: SL sotto entry (non ancora breakeven)
            sl_price        = sl_from_bybit
            r_dist          = entry_price - sl_price
            orig_r_dist     = r_dist
            breakeven_active = False
            set_sl_on_bybit = False
        elif sl_from_bybit >= entry_price * 0.999:
            # SL a/sopra entry: il breakeven è già stato applicato.
            # NON sovrascrivere il SL — stima orig_r_dist via ATR.
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
            log(f"[SYNC] {symbol}: SL={sl_price:.4f} ≥ entry — breakeven già attivo")
        else:
            # Fallback: posizione senza SL impostato (non dovrebbe accadere)
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
            sl_price        = entry_price - r_dist
            breakeven_active = False
            set_sl_on_bybit = True

        # Se trailing già attivo su Bybit, anche breakeven è certamente passato
        if trailing_active:
            breakeven_active = True

        # Determina se il partial TP è già stato eseguito in precedenti run.
        # Se il trailing è attivo e il prezzo corrente è già sopra la soglia 2R,
        # il partial è quasi certamente avvenuto — evita un secondo fire al restart.
        price_now_sync   = get_last_price(symbol) or 0.0
        partial_trigger  = entry_price + PARTIAL_TP_R * orig_r_dist
        partial_tp_done  = (trailing_active
                            and price_now_sync > 0
                            and price_now_sync >= partial_trigger)
        if partial_tp_done:
            log(f"[SYNC] {symbol}: prezzo {price_now_sync:.4f} >= trigger {partial_trigger:.4f} "
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
            set_position_stoploss_long(symbol, sl_price)
            log(f"[SYNC] LONG: {symbol} qty={qty} entry={entry_price:.4f} "
                f"SL={sl_price:.4f} (impostato) trail={'SI' if trailing_active else 'NO'} "
                f"be={'SI' if breakeven_active else 'NO'}")
        else:
            log(f"[SYNC] LONG: {symbol} qty={qty} entry={entry_price:.4f} "
                f"SL={sl_price:.4f} (da Bybit) trail={'SI' if trailing_active else 'NO'} "
                f"be={'SI' if breakeven_active else 'NO'}")
        trovate += 1

    log(f"[SYNC] {trovate} posizioni recuperate")


# ── CIRCUIT BREAKER ───────────────────────────────────────────────────────────
def check_circuit_breaker() -> bool:
    """
    Controlla daily loss limit. Se equity scende > CIRCUIT_BREAKER_PCT%
    dal valore di inizio giornata: chiude tutto e blocca per 24h.
    Returns True se il trading è bloccato.
    """
    global _cb_equity_day_start, _cb_last_day, _cb_triggered, _cb_triggered_at

    from datetime import datetime, timezone
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Reset giornaliero a mezzanotte UTC
    if today != _cb_last_day:
        _cb_last_day          = today
        _cb_equity_day_start  = get_total_equity()
        _cb_triggered         = False
        _cb_triggered_at      = 0.0
        log(f"[CB] Reset giornaliero — equity start: {_cb_equity_day_start:.2f} USDT")
        return False

    # Cooldown scaduto → riattiva trading
    if _cb_triggered:
        elapsed_h = (time.time() - _cb_triggered_at) / 3600
        if elapsed_h >= CIRCUIT_BREAKER_COOLDOWN_H:
            _cb_triggered        = False
            _cb_equity_day_start = get_total_equity()
            log("[CB] Cooldown scaduto — circuit breaker resettato, trading riattivato")
            notify_telegram("✅ Circuit breaker resettato — trading riattivato")
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
        log(f"[CB] 🔴 CIRCUIT BREAKER — drawdown={drawdown_pct:.2f}% "
            f"({_cb_equity_day_start:.2f} → {current_equity:.2f} USDT)")

        # Chiudi tutte le posizioni aperte
        closed = []
        for symbol in list(open_positions):
            pos = get_position(symbol)
            if pos:
                qty = float(pos.get("qty", 0))
                if qty > 0 and market_close_partial(symbol, qty):
                    discard_open(symbol)
                    closed.append(symbol)
                    log(f"[CB] {symbol} chiusa")
                else:
                    log(f"[CB] {symbol} ⚠️ FAIL chiusura — verifica manuale!")

        notify_telegram(
            f"🚨 CIRCUIT BREAKER ATTIVATO\n"
            f"Drawdown giornaliero: -{drawdown_pct:.1f}%\n"
            f"Equity: {_cb_equity_day_start:.2f} → {current_equity:.2f} USDT\n"
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

        # Circuit breaker disabilitato: attendendo verifica ranking
        # Reabilitare dopo aver confermato che il ranking funziona correttamente
        # if check_circuit_breaker():
        #     tlog("circuit_breaker", "🚨 Circuit breaker attivo — scan bloccata", 1800)
        #     continue

        if not _btc_ok:
            log("[SCAN] BTC filter attivo — scan sospesa")
            tlog("btc_bear", "⚠️ BTC filter: scan sospesa", 3600)
            continue

        if n_open >= MAX_OPEN_POSITIONS:
            tlog("max_open", f"[SCAN] MAX {MAX_OPEN_POSITIONS} posizioni aperte, attendo", 600)
            continue

        # 1) Universo: top 100 per volume
        universe = scan_universe()
        log(f"[SCAN] {len(universe)} coin nel universo (vol>{MIN_VOL_24H_USDT/1e6:.0f}M USDT)")
        
        # Diagnostica: mostra top 10 gainers
        if universe:
            top10 = universe[:10]
            top10_str = " | ".join([f"{c['symbol']}:{c['chg24h']:+.1f}%" for c in top10])
            log(f"[SCAN] Top 10 gainers: {top10_str}")

        if not universe:
            continue

        # 2) Per ogni candidato: ranking 24h → segnale 4h
        entered = 0
        checked = 0
        reject_stats_scan = {}
        for rank_idx, coin in enumerate(universe, start=1):
            if rank_idx > TRADE_TOP_N:
                break
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                break
            sym = coin["symbol"]
            if sym in open_positions:
                continue
            time.sleep(0.05)

            checked += 1

            # Segnale 4h pullback
            signal = check_entry_signal(sym, reject_stats_scan)
            if not signal and rank_idx <= TOP_MOMENTUM_FALLBACK_RANK:
                signal = check_momentum_entry_signal(sym, reject_stats_scan)
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

            actual_qty, actual_entry_px = get_open_long_fill(sym)
            if actual_qty > 0:
                qty = actual_qty
            if actual_entry_px > 0:
                entry_px = actual_entry_px

            actual_r_dist = entry_px - signal["sl_price"]
            if actual_r_dist <= 0:
                log(f"[ENTRY] {sym} — r_dist non valido dopo fill reale")
                continue

            # Salva stato e imposta SL
            sl_price = signal["sl_price"]
            sl_pct = actual_r_dist / entry_px * 100
            set_position(sym, {
                "entry_price":       entry_px,
                "sl_price":          sl_price,
                "r_dist":            actual_r_dist,
                "orig_r_dist":       actual_r_dist,   # mai modificato: base per calcolo ratchet
                "qty":               qty,
                "entry_time":        time.time(),
                "trailing_active":   False,
                "breakeven_active":  False,
                "partial_tp_active": False,
            })
            add_open(sym)
            time.sleep(0.3)
            set_position_stoploss_long(sym, sl_price)

            notify_telegram(
                f"📈 ENTRY {sym} — Pullback EMA20(4h)\n"
                f"Entry: {entry_px:.4f} | SL: {sl_price:.4f} ({sl_pct:.1f}%)\n"
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
            log(f"[REJECT] LONG top motivi: {reject_msg}")


# ── AVVIO ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    run_startup_self_checks()
    log("=" * 62)
    log("  TREND FOLLOWING — 4h EMA20 PULLBACK BOT")
    log("=" * 62)
    log(f"  Timeframe : Daily trend + 4h segnale | Scan ogni {SCAN_INTERVAL_SEC//60}min")
    log(f"  Filtri    : EMA50 daily | EMA20(4h) touch | RSI {RSI_MIN_4H}-{RSI_MAX_4H} | "
        f"dist EMA <{MAX_DIST_EMA}%")
    log(f"  Risk      : {RISK_PCT*100:.1f}%/trade | MAX={MAX_OPEN_POSITIONS} pos | "
        f"Leva {DEFAULT_LEVERAGE}× | Ratchet floor fissi")
    first_trigger, first_floor = RATCHET_TABLE[0]
    log(f"  Exits     : Ratchet(≥{first_trigger}%→+{first_floor}% ... ≥150%→+120%) + "
        f"Partial TP {PARTIAL_TP_PCT*100:.0f}%@{PARTIAL_TP_R:.1f}R")
    log(f"  Regime    : BTC daily EMA50 (slope+) + BTC weekly EMA200")
    log("=" * 62)

    equity0 = get_total_equity()
    log(f"[AVVIO] Equity: {equity0:.2f} USDT")

    notify_telegram(
        f"📈 PULLBACK BOT AVVIATO — Trend Following 4h\n"
        f"Segnale: EMA20(4h) pullback + daily uptrend\n"
        f"Regime: BTC daily EMA50 (slope+) + BTC weekly EMA200\n"
        f"Exit: Ratchet floor fissi ≥{first_trigger}%→+{first_floor}% ... ≥150%→+120% | "
        f"Partial 50%@{PARTIAL_TP_R:.1f}R\n"
        f"Scan ogni {SCAN_INTERVAL_SEC//60}min | Leva {DEFAULT_LEVERAGE}× | "
        f"Risk {RISK_PCT*100:.1f}%\n"
        f"Equity: {equity0:.2f} USDT"
    )

    sync_positions_from_wallet()
    _update_btc_filter()

    threading.Thread(target=trailing_worker, daemon=True).start()
    threading.Thread(target=sl_watchdog,     daemon=True).start()

    main_loop()
