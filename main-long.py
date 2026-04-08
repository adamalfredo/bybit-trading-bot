# STRATEGIA MIGLIORATA NEL TIMEFRAME 5/3/2026 AL 20/3/2026
from typing import Optional
import os
import time
import hmac
import json
import hashlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
import pandas as pd
from ta.volatility import BollingerBands, AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

# Env vars (Railway)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
KEY = BYBIT_API_KEY
SECRET = BYBIT_API_SECRET
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()

# Indici posizione Bybit
LONG_IDX = 1
SHORT_IDX = 2

# --- Sizing per trade (notional) ---
DEFAULT_LEVERAGE = 10          # leva usata sul conto (Cross/Isolated)
MARGIN_USE_PCT = 0.35
TARGET_NOTIONAL_PER_TRADE = 200.0

INTERVAL_MINUTES = 60  # era 15
ATR_WINDOW = 14
TRAILING_MIN = 0.02   # trailing più conservativo
TRAILING_MAX = 0.08   # trailing più conservativo
TP_FACTOR = 2.5                        # TP più ambizioso
SL_FACTOR = 1.2                        # SL più stretto
TP_MIN = 2.0
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.0
# Nuovi parametri per protezione guadagni (stop_floor)
TRIGGER_BY = "LastPrice"    # "LastPrice" o "MarkPrice" per trigger degli stop exchange
RATCHET_TIERS_ROI = [
    (15, 7),    # FIX: era (30,15) → soglie realistiche a leva 10x
    (25, 15),
    (40, 25),
    (60, 40),
    (80, 60)
]
FLOOR_BUFFER_PCT = 0.0015          # 0.15% di prezzo per sicurezza esecuzione
FLOOR_UPDATE_COOLDOWN_SEC = 45     # cooldown più lungo per evitare rumore
FLOOR_TRIGGER_BY = "MarkPrice"     # usa Mark per coerenza con SL

# >>> PATCH: parametri breakeven lock (LONG)
BREAKEVEN_LOCK_PCT = 0.025  # FIX2: era 0.015, attiva BE al +2.5% di prezzo (più respiro prima del lock)
BREAKEVEN_BUFFER   = 0.012  # FIX2: era 0.006, buffer più largo per evitare noise-stop su BE
MAX_LOSS_CAP_PCT = 0.03   # FIX: era 0.015, cap alzato a 3% per non bloccare SL_ATR_MULT=2.0

# >>> NEW: regime + drawdown giornaliero (LONG)
DAILY_DD_CAP_PCT = 0.04
_btc_favorable_long = False    # True se BTC 4h in uptrend → contesto favorevole per LONG
_btc_ctx_ts = 0
_daily_start_equity = None
_trading_paused_until = 0
# BEGIN PATCH: throttle DD (no pausa forzata di default)
ENABLE_DD_PAUSE = os.getenv("ENABLE_DD_PAUSE", "0") == "1"   # se "1" mantiene la pausa forzata
DD_PAUSE_MINUTES = int(os.getenv("DD_PAUSE_MINUTES", "120"))
RISK_THROTTLE_LEVEL = 0  # 0=off, 1=DD > cap, 2=DD > 2*cap
INITIAL_STOP_LOSS_PCT = 0.03          # era 0.02, SL iniziale più largo
ORDER_USDT = 50.0
ENABLE_BREAKOUT_FILTER = False  # FIX2: disabilitato - il breakout obbligatorio causa late-entry dopo il massimo
# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
LIQUIDITY_MIN_VOLUME = 1_000_000  # Soglia minima volume 24h USDT (consigliato)
# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}
last_exit_was_loss = {}  # True se l'ultima uscita su quel simbolo era una perdita
recent_losses = {}          # conteggio loss consecutivi per simbolo
FORCED_WAIT_MIN = 90        # attesa minima (minuti) se il contesto resta sfavorevole
# ---- Logging flags (accensione selettiva via env/Variables di Railway) ----
LOG_DEBUG_ASSETS     = os.getenv("LOG_DEBUG_ASSETS", "0") == "1"
LOG_DEBUG_DECIMALS   = os.getenv("LOG_DEBUG_DECIMALS", "0") == "1"
LOG_DEBUG_SYNC       = os.getenv("LOG_DEBUG_SYNC", "0") == "1"
LOG_DEBUG_STRATEGY   = os.getenv("LOG_DEBUG_STRATEGY", "0") == "1"
LOG_DEBUG_PORTFOLIO  = os.getenv("LOG_DEBUG_PORTFOLIO", "0") == "1"
# --- Loosening via env (ingressi più frequenti) ---
MIN_CONFLUENCE = 2   # FIX: era 1, richiede almeno 2 indicatori allineati
ENTRY_TF_VOLATILE = 60  # FIX2: allineato al loop principale (60m) per evitare segnali stantii
ENTRY_TF_STABLE = 60   # FIX2: allineato al loop principale (60m)
ENTRY_ADX_VOLATILE = 27        # fisso
ENTRY_ADX_STABLE = 24          # fisso
ADX_RELAX_EVENT = 3.0
RSI_LONG_THRESHOLD = 54.0
COOLDOWN_MINUTES = 60          # fisso (non usare os.getenv)
MAX_OPEN_POSITIONS = 3         # massimo posizioni simultanee (leva 10x su ~50 USDT = rischio elevato)
FUNDING_LONG_MAX = 0.0005      # blocca nuovi LONG se funding > +0.05% (longs sovraccarichi = pressione ribassista)
MAX_CONSEC_LOSSES = 2          # fisso
 
LINEAR_MIN_TURNOVER = 10_000_000  # 10M: esclude micro-cap illiquidi e token manipolati
# Large-cap con minQty elevata: abilita auto-bump del notional al minimo
LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}
# --- Nuova gestione rischio e R-multipli ---
RISK_PCT = float(os.getenv("RISK_PCT", "0.0075"))   # 0.75% equity per trade
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "2.0"))   # FIX: era 1.4, SL più largo per ridurre noise-stop
TP1_R = float(os.getenv("TP1_R", "2.5"))             # FIX: era 1.0, R:R almeno 2.5:1
TP1_PARTIAL = float(os.getenv("TP1_PARTIAL", "0.5"))  # 50% posizione al primo TP
BE_AT_R = float(os.getenv("BE_AT_R", "1.0"))
TRAIL_START_R = float(os.getenv("TRAIL_START_R", "0.5"))  # FIX: abbassato da 1.5 a 0.5 per attivazione trailing prima
TRAIL_ATR_MULT = float(os.getenv("TRAIL_ATR_MULT", "1.3"))

# --- Stima fee per expectancy (percentuali lato notional) ---
FEES_TAKER_PCT = float(os.getenv("FEES_TAKER_PCT", "0.0006"))  # ~0.06%
FEES_MAKER_PCT = float(os.getenv("FEES_MAKER_PCT", "0.0001"))  # ~0.01%

# --- Cassaforte in USDT (lock minimo di profitto) ---
# Nota: per tua richiesta, trattiamo questi come default di codice
PNL_TRIGGER_USDT = 3.2   # quando l'Unrealized >= 3.2 USDT
PNL_LOCK_USDT    = 3.0   # fissa uno SL che garantisca ≳ 3.0 USDT
PNL_LOCK_BUFFER_PCT = 0.001  # 0.1% buffer per evitare SL sopra/sotto il prezzo attuale
# --- BLACKLIST STABLECOIN ---
STABLECOIN_BLACKLIST = [
    "USDCUSDT", "USDEUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "EURUSDT", "USDTUSDT"
]
EXCLUSION_LIST = ["FUSDT", "YBUSDT", "ZBTUSDT", "RECALLUSDT", "XPLUSDT", "BRETTUSDT", "STABLEUSDT"]

# Cache leggera prezzo (TTL in secondi)
LAST_PRICE_TTL_SEC = 2
_last_price_cache = {}

# Locks per strutture condivise
_state_lock = threading.RLock()
_instr_lock = threading.RLock()
_price_lock = threading.RLock()

# Helpers atomici per lo stato
def get_position(symbol: str):
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

def is_trending_up(symbol: str, tf: str = "240"):
    """
    True se l'asset è in uptrend su 4h: prezzo sopra EMA200 e EMA200 crescente.
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": tf, "limit": 220}
    try:
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return False
        raw = data["result"]["list"]
        df = pd.DataFrame(raw, columns=["timestamp","Open","High","Low","Close","Volume","turnover"])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 200:
            return False
        ema200 = EMAIndicator(close=df["Close"], window=200).ema_indicator()
        return df["Close"].iloc[-1] > ema200.iloc[-1] and ema200.iloc[-1] >= ema200.iloc[-2]
    except Exception:
        return False

def is_trending_up_1h(symbol: str, tf: str = "60"):
    """
    True se l'asset è in uptrend su 1h: prezzo sopra EMA100 e EMA100 crescente.
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": tf, "limit": 120}
    try:
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return False
        raw = data["result"]["list"]
        df = pd.DataFrame(raw, columns=["timestamp","Open","High","Low","Close","Volume","turnover"])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 100:
            return False
        ema100 = EMAIndicator(close=df["Close"], window=100).ema_indicator()
        return df["Close"].iloc[-1] > ema100.iloc[-1] and ema100.iloc[-1] >= ema100.iloc[-2]
    except Exception:
        return False

def _update_btc_context_long():
    """Aggiorna il contesto BTC ogni 3 min: True se BTC 4h in uptrend (favorevole per LONG)."""
    global _btc_favorable_long, _btc_ctx_ts
    if time.time() - _btc_ctx_ts > 180:
        try:
            _btc_favorable_long = is_trending_up("BTCUSDT", "240")
        except:
            pass
        _btc_ctx_ts = time.time()
        tlog("btc_ctx", f"[CTX] BTC 4h uptrend={_btc_favorable_long}", 180)

def _equity_now():
    total, usdt_balance, coin_values = get_portfolio_value()
    return total

def _update_daily_anchor_and_btc_context():
    """Aggiorna ancora giornaliera di equity e contesto BTC."""
    global _daily_start_equity
    if _daily_start_equity is None or time.strftime("%Y-%m-%d") != time.strftime("%Y-%m-%d", time.gmtime()):
        _daily_start_equity = _equity_now()
    _update_btc_context_long()

def is_breaking_weekly_high(symbol: str):
    """
    True se il prezzo attuale è sopra il massimo delle ultime 6 ore (breakout).
    """
    df = fetch_history(symbol, interval=INTERVAL_MINUTES)
    bars = int(6 * 60 / INTERVAL_MINUTES)
    if df is None or len(df) < bars:
        return False
    last_close = df["Close"].iloc[-1]
    high = df["High"].iloc[-bars:].max()
    return last_close >= high * 1.005  # tolleranza +0.5% sopra il massimo

def update_assets(top_n=12):
    """
    Aggiorna ASSETS, LESS_VOLATILE_ASSETS e VOLATILE_ASSETS.
    Selezione ottimizzata (LONG):
    - Pool: tutti i futures linear USDT con turnover24h >= LINEAR_MIN_TURNOVER (una sola chiamata API)
    - Ranking: 20% volume normalizzato + 55% momentum 24h + 25% forza relativa vs BTC
    - Bias LONG: sweet spot +1%→+12%; premia coin più forti di BTC (forza idiosincratica)
    - VOLATILE: |price24hPcnt| > 5% → ADX threshold 27; altrimenti LESS_VOLATILE → ADX 24
    """
    global ASSETS, LESS_VOLATILE_ASSETS, VOLATILE_ASSETS
    try:
        # Unica chiamata API: futures linear contengono tutto (liquidità + momentum)
        resp = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "linear"}, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"[ASSETS] Errore API linear: {data}")
            return

        tickers = data["result"]["list"]

        # BTC 24h pct per calcolo forza relativa (già presente nella stessa risposta)
        _btc_t = next((t for t in tickers if t["symbol"] == "BTCUSDT"), None)
        btc_pct = float(_btc_t.get("price24hPcnt", 0)) * 100 if _btc_t else 0.0

        # Pool: tutti i linear USDT liquidi, escluse blacklist e funding estremo
        pool = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and float(t.get("turnover24h", 0)) >= LINEAR_MIN_TURNOVER
            and abs(float(t.get("fundingRate", 0))) < 0.003  # esclude asset con funding anomalo (manipolazione)
            and t["symbol"] not in STABLECOIN_BLACKLIST
            and t["symbol"] not in EXCLUSION_LIST
        ]

        if not pool:
            return

        def _momentum_score_long(pct: float) -> float:
            """Punteggio 0-1: favorisce momentum moderatamente positivo, penalizza estremi."""
            if pct > 20 or pct < -15:
                return 0.05  # pump/dump estremo: movimento probabilmente esaurito
            if 1.0 <= pct <= 12.0:
                return 1.0   # sweet spot: trend iniziato ma non esaurito
            if 0.0 <= pct < 1.0:
                return 0.65  # partenza, può ancora svilupparsi
            if 12.0 < pct <= 20.0:
                return 0.35  # esteso, rischio reversal imminente
            return 0.2       # negativo: contro la direzione LONG

        def _relative_score_long(rel: float) -> float:
            """Punteggio 0-1: premia forza relativa vs BTC (rel = coin_pct - btc_pct).
            Coin più forte di BTC = segnale idiosincratico = migliore candidata LONG."""
            if rel >= 4.0:  return 1.0   # molto più forte di BTC: ottimo per LONG
            if rel >= 1.0:  return 0.75  # moderatamente più forte
            if rel >= -1.0: return 0.5   # in linea con BTC
            if rel >= -4.0: return 0.25  # più debole di BTC: scarso per LONG
            return 0.05                   # molto più debole: da evitare

        prev = set(ASSETS)
        candidates = []
        for t in pool:
            sym = t["symbol"]
            vol = float(t.get("turnover24h", 0))
            pct = float(t.get("price24hPcnt", 0)) * 100  # Bybit restituisce valore frazionario
            rel = pct - btc_pct
            mom = _momentum_score_long(pct)
            rel_s = _relative_score_long(rel)
            candidates.append((sym, vol, pct, mom, rel, rel_s))

        # Score finale: 20% volume normalizzato + 55% momentum + 25% forza relativa vs BTC
        max_vol = max(c[1] for c in candidates) or 1.0
        scored = sorted(
            candidates,
            key=lambda c: 0.20 * (c[1] / max_vol) + 0.55 * c[3] + 0.25 * c[5],
            reverse=True
        )

        top = scored[:top_n]
        ASSETS = [c[0] for c in top]
        # VOLATILE = asset che si muovono più del 5% in 24h (in abs) → ADX threshold 27
        # LESS_VOLATILE = gli altri → ADX threshold 24
        VOLATILE_ASSETS      = [c[0] for c in top if abs(c[2]) > 5.0]
        LESS_VOLATILE_ASSETS = [c[0] for c in top if abs(c[2]) <= 5.0]

        changed = set(ASSETS) != prev
        if changed or LOG_DEBUG_ASSETS:
            added   = list(set(ASSETS) - prev)
            removed = list(prev - set(ASSETS))
            if LOG_DEBUG_ASSETS:
                mom_info = {c[0]: f"{c[2]:+.1f}% (rel={c[4]:+.1f}%)" for c in top}
                log(f"[ASSETS] BTC_24h={btc_pct:+.1f}% | Aggiornati: {ASSETS}\nMomento+Rel: {mom_info}\nVolatili(>5%): {VOLATILE_ASSETS}\nStabili(≤5%): {LESS_VOLATILE_ASSETS}")
            else:
                log(f"[ASSETS] Totali={len(ASSETS)} (+{len(added)}/-{len(removed)}) | BTC_24h={btc_pct:+.1f}% | Added={added[:5]} Removed={removed[:5]}")
    except Exception as e:
        log(f"[ASSETS] Errore aggiornamento lista asset: {e}")

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

# Livello log globale: DEBUG/INFO/WARN/ERROR (default INFO)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- HTTP sessione condivisa con retry/backoff ---
RETRY_STRATEGY = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
SESSION = requests.Session()
ADAPTER = HTTPAdapter(max_retries=RETRY_STRATEGY, pool_maxsize=50)
SESSION.mount("https://", ADAPTER)
SESSION.mount("http://", ADAPTER)

# Throttling semplice per log ripetitivi
_last_log_times = {}
def tlog(key: str, msg: str, interval_sec: int = 60):
    now = time.time()
    last = _last_log_times.get(key, 0)
    if now - last >= interval_sec:
        _last_log_times[key] = now
        log(msg)

# --- Logging trade su CSV ---
def _trade_log(event: str, symbol: str, side: str, entry_price: float = 0.0, qty: float = 0.0,
               sl: float = 0.0, tp: float = 0.0, r_dist: float = 0.0, extra: dict | None = None):
    try:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "trades.csv")
        header_needed = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if header_needed:
                f.write("ts,event,symbol,side,entry,qty,sl,tp,r_dist,extra\n")
            jextra = json.dumps(extra or {}, separators=(",", ":"))
            f.write(f"{int(time.time())},{event},{symbol},{side},{entry_price},{qty},{sl},{tp},{r_dist},{jextra}\n")
    except Exception:
        pass

def _expectancy_log(pnl_pct: float, entry_notional: float, exit_notional: float,
                    maker_entry: bool = False, maker_exit: bool = False):
    try:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "expectancy.csv")
        header_needed = not os.path.exists(path)
        fee_entry = entry_notional * (FEES_MAKER_PCT if maker_entry else FEES_TAKER_PCT)
        fee_exit = exit_notional * (FEES_MAKER_PCT if maker_exit else FEES_TAKER_PCT)
        with open(path, "a", encoding="utf-8") as f:
            if header_needed:
                f.write("ts,pnl_pct,entry_notional,exit_notional,fee_entry,fee_exit\n")
            f.write(f"{int(time.time())},{pnl_pct:.6f},{entry_notional:.6f},{exit_notional:.6f},{fee_entry:.6f},{fee_exit:.6f}\n")
    except Exception:
        pass

# --- Helper richieste firmate Bybit (centralizzati) ---
def _bybit_signed_get(path: str, params: dict):
    try:
        from urllib.parse import urlencode
        query_string = urlencode(sorted(params.items()))
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = f"{ts}{KEY}{recv_window}{query_string}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window
        }
        url = f"{BYBIT_BASE_URL}{path}"
        return SESSION.get(url, headers=headers, params=params, timeout=10)
    except Exception as e:
        tlog("signed_get_exc", f"[SIGNED-GET][{path}] exc: {e}", 300)
        raise

def _bybit_signed_post(path: str, body: dict):
    try:
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}{recv_window}{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json"
        }
        url = f"{BYBIT_BASE_URL}{path}"
        return SESSION.post(url, headers=headers, data=body_json, timeout=10)
    except Exception as e:
        tlog("signed_post_exc", f"[SIGNED-POST][{path}] exc: {e}", 300)
        raise

def format_quantity_bybit(qty: float, qty_step: float, precision: Optional[int] = None) -> str:
    """
    Restituisce la quantità formattata secondo i decimali accettati da Bybit per qty_step e basePrecision,
    troncando senza arrotondare e garantendo che sia un multiplo esatto di qty_step.
    """
    from decimal import Decimal, ROUND_DOWN
    def get_decimals(step):
        s = str(step)
        if '.' in s:
            return len(s.split('.')[-1].rstrip('0'))
        return 0
    
    # Usa sempre i decimali del qty_step, ignora precision se incompatibile
    step_decimals = get_decimals(qty_step)
    if precision is None or precision < step_decimals:
        precision = step_decimals
    
    step_dec = Decimal(str(qty_step))
    qty_dec = Decimal(str(qty))
    # Tronca la quantità al multiplo più basso di qty_step
    floored_qty = (qty_dec // step_dec) * step_dec
    # Troncamento ai decimali accettati
    quantize_str = '1.' + '0'*precision if precision > 0 else '1'
    floored_qty = floored_qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)
    # Garantisce che sia multiplo esatto di qty_step
    if (floored_qty / step_dec) % 1 != 0:
        floored_qty = (floored_qty // step_dec) * step_dec
        floored_qty = floored_qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)
    fmt = f"{{0:.{precision}f}}"
    # LOG DIAGNOSTICO
    # LOG DIAGNOSTICO (solo se abilitato)
    if LOG_DEBUG_DECIMALS:
        log(f"[DECIMALI][FORMAT_QTY] qty={qty} | qty_step={qty_step} | precision={precision} | floored_qty={floored_qty} | quantize_str={quantize_str}")
    return fmt.format(floored_qty)

def format_price_bybit(price: float, tick_size: float) -> str:
    step = Decimal(str(tick_size))
    p = Decimal(str(price))
    floored = (p // step) * step  # tronca al tick
    dec = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    return f"{floored:.{dec}f}"

def compute_trailing_distance(symbol: str, atr_val: float) -> float:
    price = get_last_price(symbol) or 0.0
    if price <= 0:
        return max(atr_val * 1.5, 0.0)
    min_abs = price * TRAILING_MIN
    max_abs = price * TRAILING_MAX
    dist = atr_val * 1.5
    return float(max(min_abs, min(max_abs, dist)))

def get_open_long_qty(symbol):
    try:
        params = {"category": "linear", "symbol": symbol}
        resp = _bybit_signed_get("/v5/position/list", params)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_SYNC:
                tlog(f"qty_err_long:{symbol}", f"[BYBIT-RAW][ERRORE] get_open_long_qty {symbol}: {json.dumps(data)}", 300)
            return 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Buy":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        if LOG_DEBUG_SYNC:
            tlog(f"qty_exc_long:{symbol}", f"❌ Errore get_open_long_qty per {symbol}: {e}", 300)
        return 0.0

def get_open_short_qty(symbol):
    try:
        params = {"category": "linear", "symbol": symbol}
        resp = _bybit_signed_get("/v5/position/list", params)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            return 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Sell":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception:
        return 0.0

# --- FUNZIONI DI SUPPORTO BYBIT E TELEGRAM ---
def get_last_price(symbol):
    try:
        now = time.time()
        with _price_lock:
            cached = _last_price_cache.get(symbol)
            if cached and (now - cached.get("ts", 0)) <= LAST_PRICE_TTL_SEC:
                return cached.get("price")
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "linear", "symbol": symbol}  # PATCH: era "spot"
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            item = data["result"]["list"][0]
            price = float(item["lastPrice"])
            bid1 = float(item.get("bid1Price") or price)
            ask1 = float(item.get("ask1Price") or price)
            with _price_lock:
                _last_price_cache[symbol] = {"price": price, "bid1": bid1, "ask1": ask1, "ts": now}
            return price
        else:
            tlog(f"lp_err:{symbol}", f"[BYBIT] Errore get_last_price {symbol}: {data}", 300)
            return None
    except Exception as e:
        tlog(f"lp_exc:{symbol}", f"[BYBIT] Errore get_last_price {symbol}: {e}", 300)
        return None

def get_bid_price(symbol) -> Optional[float]:
    """Ritorna bid1Price dal ticker (cacheato da get_last_price)."""
    get_last_price(symbol)
    with _price_lock:
        c = _last_price_cache.get(symbol, {})
        return c.get("bid1") or c.get("price")

def get_instrument_info(symbol: str) -> dict:
    """
    Info strumento con cache 5m.
    Fallback conservativo: qty_step=0.01, min_order_amt=10 per evitare 170137/170140 ripetitivi.
    """
    now = time.time()
    # Cache semplice (aggiungi queste variabili globali in alto)
    global _instrument_cache
    with _instr_lock:
        if '_instrument_cache' not in globals():
            _instrument_cache = {}
        cached = _instrument_cache.get(symbol)
        if cached and (now - cached["ts"] < 300):
            return cached["data"]

    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "linear", "symbol": symbol}
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            tlog(f"instr_err:{symbol}", f"❌ get_instrument_info retCode {data.get('retCode')} → fallback {symbol}", 600)
            parsed = {
                "min_qty": 0.01,
                "qty_step": 0.01,
                "precision": 4,
                "price_step": 0.01,
                "min_order_amt": 10.0
            }
            with _instr_lock:
                _instrument_cache[symbol] = {"data": parsed, "ts": now}
            return parsed
        
        lst = data.get("result", {}).get("list", [])
        if not lst:
            tlog(f"instr_empty:{symbol}", f"❌ get_instrument_info lista vuota → fallback {symbol}", 600)
            parsed = {
                "min_qty": 0.01,
                "qty_step": 0.01,
                "precision": 4,
                "price_step": 0.01,
                "min_order_amt": 10.0
            }
            with _instr_lock:
                _instrument_cache[symbol] = {"data": parsed, "ts": now}
            return parsed
            
        info = lst[0]
        lot = info.get("lotSizeFilter", {})
        price_filter = info.get("priceFilter", {})
        
        qty_step_raw = lot.get("qtyStep", "0.01") or "0.01"
        try:
            qty_step = float(qty_step_raw)
        except:
            qty_step = 0.01
            
        parsed = {
            "min_qty": float(lot.get("minOrderQty", 0) or 0),
            "qty_step": qty_step,
            "precision": int(info.get("priceScale", 4) or 4),
            "price_step": float(price_filter.get("tickSize", "0.01") or "0.01"),
            "min_order_amt": float(lot.get("minNotionalValue", "5") or "5")
        }
        with _instr_lock:
            _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed
        
    except Exception as e:
        tlog(f"instr_exc:{symbol}", f"❌ Errore get_instrument_info eccezione → fallback {symbol}: {e}", 600)
        parsed = {
            "min_qty": 0.01,
            "qty_step": 0.01,
            "precision": 4,
            "price_step": 0.01,
            "min_order_amt": 10.0
        }
        _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed

def _format_qty_with_step(qty: float, step: float) -> str:
    step_dec = Decimal(str(step))
    q = Decimal(str(qty))
    floored = (q // step_dec) * step_dec
    step_decimals = -step_dec.as_tuple().exponent if step_dec.as_tuple().exponent < 0 else 0
    pattern = Decimal('1.' + '0'*step_decimals) if step_decimals > 0 else Decimal('1')
    floored = floored.quantize(pattern, rounding=ROUND_DOWN)
    if step_decimals > 0:
        return f"{floored:.{step_decimals}f}".rstrip('0').rstrip('.') or "0"
    return f"{int(floored)}"

def get_free_qty(symbol):
    # Normalizza coin
    if symbol.endswith("USDT") and len(symbol) > 4:
        coin = symbol.replace("USDT", "")
    elif symbol == "USDT":
        coin = "USDT"
    else:
        coin = symbol

    params = {"accountType": BYBIT_ACCOUNT_TYPE}

    try:
        resp = _bybit_signed_get("/v5/account/wallet-balance", params)
        data = resp.json()
        if "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_PORTFOLIO:
                log(f"❗ Struttura inattesa da Bybit: {resp.text}")
            return 0.0

        acct = data["result"]["list"][0]
        # Saldo disponibile complessivo (Unified)
        total_avail = float(acct.get("totalAvailableBalance") or 0.0)

        # Per USDT prova a prendere il disponibile della coin; fallback al totale disponibile
        coin_list = acct.get("coin", [])
        if coin == "USDT":
            for c in coin_list:
                if c.get("coin") == "USDT":
                    avail = c.get("availableToWithdraw") or c.get("availableBalance") or c.get("walletBalance") or "0"
                    qty = float(avail) if avail else 0.0
                    # Log minimale e con throttling
                    tlog("balance_usdt", f"📦 Saldo USDT disponibile: {qty}", 600)
                    return qty if qty > 0 else float(total_avail)  # fallback
            # Se non trovata la coin, usa il totale disponibile
            tlog("balance_usdt", f"📦 Saldo USDT disponibile: {total_avail}", 600)
            return float(total_avail)

        # Per altre coin usa quanto disponibile nella coin, altrimenti 0
        for c in coin_list:
            if c.get("coin") == coin:
                avail = c.get("availableToWithdraw") or c.get("availableBalance") or c.get("walletBalance") or "0"
                return float(avail) if avail else 0.0
        return 0.0

    except Exception as e:
        if LOG_DEBUG_PORTFOLIO:
            log(f"❌ Errore nel recupero saldo: {e}")
        return 0.0

def notify_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TELEGRAM] Token o chat_id non configurati")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"[LONG] {msg}"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        log(f"[TELEGRAM] Errore invio messaggio: {e}")

def calculate_quantity(symbol: str, usdt_amount: float) -> Optional[str]:
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    min_order_amt = info.get("min_order_amt", 5)
    min_qty = info.get("min_qty", 0.0)
    precision = info.get("precision", 4)
    
    min_notional = max(float(min_order_amt), float(min_qty) * float(price))
    if usdt_amount < min_notional:
        log(f"❌ Budget {usdt_amount:.2f} USDT insufficiente per notional minimo {min_notional:.2f} su {symbol}")
        return None
    try:
        raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
        if LOG_DEBUG_DECIMALS:
            log(f"[DECIMALI][CALC_QTY] {symbol} | usdt_amount={usdt_amount} | price={price} | raw_qty={raw_qty} | qty_step={qty_step} | precision={precision}")
        qty_str = format_quantity_bybit(float(raw_qty), float(qty_step), precision=precision)
        qty_dec = Decimal(qty_str)
        min_qty_dec = Decimal(str(min_qty))
        if qty_dec < min_qty_dec:
            if LOG_DEBUG_DECIMALS:
                log(f"[DECIMALI][CALC_QTY] {symbol} | qty_dec < min_qty_dec: {qty_dec} < {min_qty_dec}")
            qty_dec = min_qty_dec
            qty_str = format_quantity_bybit(float(qty_dec), float(qty_step), precision=precision)
        order_value = qty_dec * Decimal(str(price))
        if LOG_DEBUG_DECIMALS:
            log(f"[DECIMALI][CALC_QTY] {symbol} | qty_dec={qty_dec} | order_value={order_value}")
        if order_value < Decimal(str(min_order_amt)):
            log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT (minimo richiesto: {min_order_amt})")
            return None
        if qty_dec <= 0:
            log(f"❌ Quantità calcolata troppo piccola per {symbol}")
            return None
        investito_effettivo = float(qty_dec) * float(price)
        if investito_effettivo < 0.95 * usdt_amount:
            log(f"⚠️ Attenzione: valore effettivo investito ({investito_effettivo:.2f} USDT) molto inferiore a quello richiesto ({usdt_amount:.2f} USDT)")
        if LOG_DEBUG_DECIMALS:
            log(f"[DECIMALI][CALC_QTY][RETURN] {symbol} | qty_str={qty_str}")
        return qty_str
    except Exception as e:
        log(f"❌ Errore calcolo quantità per {symbol}: {e}")
        return None

def _try_limit_entry_long(symbol: str, qty_str: str, bid_price_str: str) -> Optional[float]:
    """Tenta ingresso LONG come maker (PostOnly Limit a bid1Price).
    Polling fill max 3 secondi. Cancella e ritorna None se non eseguito (fallback Market)."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Limit",
        "timeInForce": "PostOnly",
        "qty": qty_str,
        "price": bid_price_str,
        "positionIdx": LONG_IDX
    }
    resp = _bybit_signed_post("/v5/order/create", body)
    try:
        data = resp.json()
    except Exception:
        return None
    if data.get("retCode") != 0:
        if LOG_DEBUG_STRATEGY:
            log(f"[LIMIT-ENTRY][LONG][{symbol}] PostOnly rifiutato ({data.get('retCode')}), fallback Market")
        return None
    order_id = data.get("result", {}).get("orderId", "")
    # Polling fill: max 3 secondi (6 × 0.5s)
    for _ in range(6):
        time.sleep(0.5)
        filled_qty = get_open_long_qty(symbol)
        if filled_qty and filled_qty > 0:
            log(f"[LIMIT-ENTRY][LONG][{symbol}] PostOnly @ {bid_price_str} eseguito ✓ (fee maker)")
            return filled_qty
    # Timeout: cancella ordine e segnala fallback
    if order_id:
        try:
            _bybit_signed_post("/v5/order/cancel", {"category": "linear", "symbol": symbol, "orderId": order_id})
        except Exception:
            pass
    if LOG_DEBUG_STRATEGY:
        log(f"[LIMIT-ENTRY][LONG][{symbol}] PostOnly timeout, fallback Market")
    return None


def market_long(symbol: str, usdt_amount: float, qty_exact: Optional[str] = None):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None

    info = get_instrument_info(symbol)
    qty_step = float(info.get("qty_step", 0.01))
    min_qty = float(info.get("min_qty", qty_step))
    min_order_amt = float(info.get("min_order_amt", 10.0))

    # Se è stata fornita una quantità esatta (già conforme ai passi), usala
    step_dec = Decimal(str(qty_step))
    if qty_exact is not None:
        try:
            qty_aligned = Decimal(str(qty_exact))
        except Exception:
            qty_aligned = Decimal("0")
    else:
        safe_usdt_amount = usdt_amount * 0.98
        raw_qty = Decimal(str(safe_usdt_amount)) / Decimal(str(price))
        qty_aligned = (raw_qty // step_dec) * step_dec

    # Guardie: evita qty 0 e rispetta minimi exchange
    if float(qty_aligned) <= 0 or float(qty_aligned) < min_qty:
        qty_aligned = Decimal(str(min_qty))
        tlog(f"qty_guard:{symbol}", f"[QTY-GUARD][{symbol}] qty riallineata a min_qty={float(qty_aligned)}", 600)

    # Rispetta min notional (min_order_amt)
    needed = Decimal(str(min_order_amt)) / Decimal(str(price))
    # ceil al passo
    multiples = (needed / step_dec).quantize(Decimal('1'), rounding=ROUND_UP)
    min_notional_qty = multiples * step_dec
    if qty_aligned * Decimal(str(price)) < Decimal(str(min_order_amt)):
        qty_aligned = max(qty_aligned, min_notional_qty)
        tlog(f"notional_guard:{symbol}", f"[NOTIONAL-GUARD][{symbol}] qty alzata per min_order_amt → {float(qty_aligned)}", 600)

    # NEW: limita il notional all'effettivo margine disponibile adesso
    avail_now = get_usdt_balance() or 0.0
    max_notional_now = avail_now * DEFAULT_LEVERAGE * MARGIN_USE_PCT
    desired_notional = float(qty_aligned) * float(price)
    if desired_notional > max_notional_now:
        # scala qty al tetto consentito dal margine corrente
        qty_aligned = (Decimal(str(max_notional_now)) / Decimal(str(price))) // step_dec * step_dec
        if qty_aligned <= 0:
            return None

    # --- TENTATIVO INGRESSO MAKER (PostOnly Limit a bid1Price) ---
    bid_price = get_bid_price(symbol) or 0.0
    if bid_price > 0:
        price_step = float(info.get("price_step", 0.01))
        bid_str = format_price_bybit(bid_price, price_step)
        qty_str_limit = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str_limit) > 0:
            limit_qty = _try_limit_entry_long(symbol, qty_str_limit, bid_str)
            if limit_qty:
                return limit_qty

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) <= 0:
            log(f"❌ qty_str=0 per {symbol}, skip ordine")
            return None

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": qty_str,
            "positionIdx": LONG_IDX
        }
        response = _bybit_signed_post("/v5/order/create", body)
        if LOG_DEBUG_STRATEGY:
            log(f"[LONG][{symbol}] attempt {attempt}/{max_retries} BODY={json.dumps(body, separators=(',', ':'))}")

        try:
            resp_json = response.json()
        except:
            resp_json = {}
        if LOG_DEBUG_STRATEGY:
            log(f"[LONG][{symbol}] RESP {response.status_code} {resp_json}")

        if resp_json.get("retCode") == 0:
            return float(qty_str)

        ret_code = resp_json.get("retCode")
        if ret_code == 170137:
            tlog(f"retry_137:{symbol}", f"[RETRY][{symbol}] 170137 → refresh instrument e rifloor", 120)
            try:
                with _instr_lock:
                    _instrument_cache.pop(symbol, None)
            except Exception: pass
            info = get_instrument_info(symbol)
            qty_step = float(info.get("qty_step", qty_step))
            step_dec = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            continue
        if ret_code == 170140:
            # Order value exceeded lower limit → riallinea alla qty minima per notional
            info = get_instrument_info(symbol)
            min_qty = float(info.get("min_qty", 0.0))
            min_order_amt = float(info.get("min_order_amt", 10.0))
            needed_qty = max(Decimal(str(min_qty)), Decimal(str(min_order_amt)) / Decimal(str(price)))
            multiples = (needed_qty / step_dec).quantize(Decimal('1'), rounding=ROUND_UP)
            qty_aligned = multiples * step_dec
            tlog(f"retry_140:{symbol}", f"[RETRY][{symbol}] 170140 → qty bump a {float(qty_aligned)} (min_notional)", 120)
            continue
        if ret_code == 170131:
            tlog(f"retry_131:{symbol}", f"[RETRY][{symbol}] 170131 → riduco qty del 10%", 120)
            qty_aligned = (qty_aligned * Decimal("0.9")) // step_dec * step_dec
            if qty_aligned <= 0:
                return None
            continue
        if ret_code == 110007:
            # Insufficient available balance → riduci qty del 20% e ritenta
            scaled = (qty_aligned * Decimal("0.8")) // step_dec * step_dec
            if scaled > 0:
                qty_aligned = scaled
                continue
            # se troppo piccola, log essenziale (throttled) e termina
            tlog(f"err_110007:{symbol}", f"[ERROR][{symbol}] 110007: saldo disponibile insufficiente per aprire LONG", 300)
            break
        # Altri errori non gestiti → throttling per evitare spam
        tlog(f"long_err:{symbol}:{ret_code}", f"[ERROR][{symbol}] Errore non gestito: {ret_code}", 300)
        break
    return None

def place_trailing_stop_long(symbol: str, trailing_dist: float):
    body = {
        "category": "linear",
        "symbol": symbol,
        "trailingStop": str(trailing_dist),
        "positionIdx": LONG_IDX
    }
    resp = _bybit_signed_post("/v5/position/trading-stop", body)
    try:
        data = resp.json()
    except:
        data = {}
    if data.get("retCode") == 0:
        tlog(f"trailing_long:{symbol}", f"[TRAILING-PLACE-LONG] {symbol} trailing={trailing_dist}", 30)
        return True
    tlog(f"trailing_long_err:{symbol}", f"[TRAILING-PLACE-LONG][ERR] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
    return False

def market_close_long(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile chiudere LONG")
        return None

    info = get_instrument_info(symbol)
    qty_step = float(info.get("qty_step", 0.01))
    step_dec = Decimal(str(qty_step))
    qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,          # <--- FIX
            "positionIdx": LONG_IDX
        }
        response = _bybit_signed_post("/v5/order/create", body)
        if LOG_DEBUG_STRATEGY:
            log(f"[CLOSE-LONG][{symbol}] attempt {attempt}/{max_retries} BODY={json.dumps(body, separators=(',', ':'))}")

        try:
            resp_json = response.json()
        except:
            resp_json = {}
        if LOG_DEBUG_STRATEGY:
            log(f"[CLOSE-LONG][{symbol}] RESP {response.status_code} {resp_json}")

        if resp_json.get("retCode") == 0:
            return response

        ret_code = resp_json.get("retCode")
        if ret_code == 170137:
            tlog(f"retry_close_137:{symbol}", f"[RETRY-CLOSE][{symbol}] 170137 → refresh instrument e rifloor", 120)
            try:
                with _instr_lock:
                    _instrument_cache.pop(symbol, None)
            except Exception: pass
            info = get_instrument_info(symbol)
            qty_step = float(info.get("qty_step", qty_step))
            step_dec = Decimal(str(qty_step))
            qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec
            continue

        tlog(f"err_close:{symbol}:{ret_code}", f"[ERROR-CLOSE][{symbol}] Errore non gestito: {ret_code}", 300)
        break
    return None

def cancel_all_orders(symbol: str, order_filter: Optional[str] = None) -> bool:
    body = {"category": "linear", "symbol": symbol}
    if order_filter:
        body["orderFilter"] = order_filter  # es: "StopOrder"
    try:
        resp = _bybit_signed_post("/v5/order/cancel-all", body)
        ok = resp.json().get("retCode") == 0
        if not ok:
            tlog(f"cancel_all_err:{symbol}", f"[CANCEL-ALL] {symbol} resp={resp.text}", 300)
        return ok
    except Exception as e:
        tlog(f"cancel_all_exc:{symbol}", f"[CANCEL-ALL] {symbol} exc: {e}", 300)
        return False

# >>> PATCH: funzioni per impostare lo stopLoss sulla posizione (LONG) e worker BE
def set_position_stoploss_long(symbol: str, sl_price: float) -> bool:
    body = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": f"{sl_price:.8f}",
        "slTriggerBy": "MarkPrice",   # <<< FIX: allinea al conditional
        "positionIdx": LONG_IDX
    }
    try:
        resp = _bybit_signed_post("/v5/position/trading-stop", body)
        data = resp.json()
        ok = data.get("retCode") == 0
        if not ok:
            tlog(f"sl_pos_err:{symbol}", f"[POS-SL][LONG] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
        return ok
    except Exception as e:
        tlog(f"sl_pos_exc:{symbol}", f"[POS-SL][LONG] exc: {e}", 300)
        return False

def breakeven_lock_worker_long():
    # Porta lo stop della POSIZIONE a breakeven e piazza anche uno Stop-Market a BE
    while True:
        for symbol in list(open_positions):
            with _state_lock:
                entry = position_data.get(symbol)
            if not entry:
                continue

            be_locked = entry.get("be_locked", False)
            price_now = get_last_price(symbol)
            if not price_now:
                continue

            entry_price = entry.get("entry_price", price_now)
            # Attiva trailing-stop oltre soglia di R
            try:
                trailing_active = entry.get("trailing_active", False)
                r_dist = entry.get("r_dist")
                if (r_dist is not None) and (not trailing_active) and price_now >= entry_price + (TRAIL_START_R * r_dist):
                    df_hist = fetch_history(symbol, interval=INTERVAL_MINUTES)
                    atr_val = None
                    if df_hist is not None and "Close" in df_hist.columns and len(df_hist) > ATR_WINDOW + 2:
                        atr_series = AverageTrueRange(high=df_hist["High"], low=df_hist["Low"], close=df_hist["Close"], window=ATR_WINDOW).average_true_range()
                        last_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
                        atr_val = last_atr
                    if atr_val is None or atr_val <= 0:
                        atr_val = float(r_dist) / max(1e-9, SL_ATR_MULT)
                    trailing_base = atr_val * TRAIL_ATR_MULT
                    trailing_dist = compute_trailing_distance(symbol, trailing_base)
                    if place_trailing_stop_long(symbol, trailing_dist):
                        entry["trailing_active"] = True
                        with _state_lock:
                            position_data[symbol] = entry
                        tlog(f"trail_on_long:{symbol}", f"[TRAIL-ON][LONG] {symbol} attivo dist={trailing_dist:.6f}", 60)
                        notify_telegram(f"🎯 Trailing attivato LONG {symbol}\nPrezzo: {price_now:.4f}\nDistanza trailing: {trailing_dist:.6f}")
            except Exception as _e:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"trail_on_exc_long:{symbol}", f"[TRAIL-ON-EXC][LONG] {symbol} exc={_e}", 300)
            if be_locked:
                continue

            r_dist = entry.get("r_dist")
            cond_be = (r_dist is not None and price_now >= entry_price + (BE_AT_R * r_dist))
            if r_dist is None:
                cond_be = price_now >= entry_price * (1.0 + BREAKEVEN_LOCK_PCT)
            if cond_be:
                be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)
                qty_live = get_open_long_qty(symbol)
                if qty_live and qty_live > 0:
                    # Piazza sia trading-stop di posizione sia uno stop-market di backup
                    place_conditional_sl_long(symbol, be_price, qty_live, trigger_by="MarkPrice")
                    set_position_stoploss_long(symbol, be_price)

                entry["be_locked"] = True
                entry["be_price"] = be_price
                with _state_lock:
                    position_data[symbol] = entry
                tlog(f"be_lock:{symbol}", f"[BE-LOCK][LONG] {symbol} SL→BE {be_price:.6f}", 60)
        time.sleep(2)

def _pick_floor_roi_long(mfe_roi: float) -> Optional[float]:
    """
    Ritorna il floor ROI da garantire, oppure None se non si è ancora raggiunta
    la prima soglia valida. Ignora floor=0 (non applica nulla).
    """
    if not RATCHET_TIERS_ROI:
        return None
    # Se non hai superato la prima soglia → nessun floor
    first_threshold = RATCHET_TIERS_ROI[0][0]
    if mfe_roi < first_threshold:
        return None
    target = None
    for th, floor in RATCHET_TIERS_ROI:
        if mfe_roi >= th and floor > 0:
            target = floor
    return target

def profit_floor_worker_long():
    """
    Aggiorna lo stopLoss della posizione a scalini di ROI (solo dopo prima soglia).
    Non applica floor=0. Non abbassa mai il floor. Usa trading-stop + Stop-Market backup.
    """
    while True:
        for symbol in list(open_positions):
            entry = position_data.get(symbol) or {}
            entry_price = entry.get("entry_price")
            qty_live = get_open_long_qty(symbol)
            if not entry_price or not qty_live or qty_live <= 0:
                continue

            # Cassaforte in USDT: se trailing non attivo e non già lockato, fissa SL che garantisca ≳ PNL_LOCK_USDT
            trailing_active = entry.get("trailing_active", False)
            usdt_floor_locked = entry.get("usdt_floor_locked", False)
            price_now = get_last_price(symbol)
            if price_now and (not trailing_active) and (not usdt_floor_locked):
                unrealized = (price_now - float(entry_price)) * float(qty_live)
                if unrealized >= PNL_TRIGGER_USDT:
                    try:
                        target_sl = float(entry_price) + (float(PNL_LOCK_USDT) / max(1e-9, float(qty_live)))
                        target_sl = min(target_sl, price_now * (1.0 - PNL_LOCK_BUFFER_PCT))
                        if target_sl > float(entry_price):
                            set_ok = set_position_stoploss_long(symbol, target_sl)
                            entry["usdt_floor_locked"] = True
                            entry["usdt_floor_price"] = target_sl
                            entry["usdt_floor_pnl"] = PNL_LOCK_USDT
                            entry["floor_updated_ts"] = time.time()
                            tlog(f"usdt_floor_long:{symbol}", f"[USDT-FLOOR][LONG] {symbol} SL→{target_sl:.6f} (lock≈{PNL_LOCK_USDT} USDT) set={set_ok}", 30)
                            set_position(symbol, entry)
                            continue
                    except Exception as _e:
                        if LOG_DEBUG_STRATEGY:
                            tlog(f"usdt_floor_exc_long:{symbol}", f"[USDT-FLOOR-EXC][LONG] {symbol} exc={_e}", 180)

            # Se il trailing Bybit è attivo, non alziamo più lo stop manualmente
            if entry.get("trailing_active", False):
                set_position(symbol, entry)
                continue

            price_now = get_last_price(symbol)
            if not price_now:
                continue

            # ROI corrente (LONG): movimento percentuale * leverage
            price_move_pct = ((price_now - entry_price) / entry_price) * 100.0
            roi_now = price_move_pct * DEFAULT_LEVERAGE

            # Aggiorna MFE ROI
            mfe_roi = max(entry.get("mfe_roi", 0.0), roi_now)
            entry["mfe_roi"] = mfe_roi

            # Determina floor ROI (None finché non superi la prima soglia)
            target_floor_roi = _pick_floor_roi_long(mfe_roi)
            prev_floor_roi = entry.get("floor_roi", None)

            # Se ancora nessuna soglia valida → non fare nulla
            if target_floor_roi is None:
                set_position(symbol, entry)
                continue

            # Non aggiornare se il floor non cresce
            if prev_floor_roi is not None and target_floor_roi <= prev_floor_roi:
                set_position(symbol, entry)
                continue

            # Rispetta cooldown
            last_upd = entry.get("floor_updated_ts", 0)
            if time.time() - last_upd < FLOOR_UPDATE_COOLDOWN_SEC:
                continue

            # Calcolo livello di prezzo corrispondente al floor ROI
            # floor ROI = % PnL garantita → converti in % di movimento prezzo
            # movimento prezzo percentuale = floor_roi / leverage
            delta_pct_price = (target_floor_roi / max(1, DEFAULT_LEVERAGE)) / 100.0
            floor_price = entry_price * (1.0 + delta_pct_price)

            # Applica buffer (LONG → leggermente sotto)
            floor_price *= (1.0 - FLOOR_BUFFER_PCT)

            # Non applicare stop che risulti sopra il prezzo attuale (sarebbe inutile)
            if floor_price >= price_now:
                # salva comunque il floor ROI ma non piazza stop
                entry["floor_roi"] = target_floor_roi
                entry["floor_price"] = floor_price
                entry["floor_updated_ts"] = time.time()
                set_position(symbol, entry)
                tlog(f"floor_up_long_skip:{symbol}",
                     f"[FLOOR-UP-SKIP][LONG] {symbol} MFE={mfe_roi:.1f}% targetROI={target_floor_roi:.1f}% floorPrice={floor_price:.6f} ≥ current={price_now:.6f}", 120)
                continue

            # Aggiorna trading-stop (niente Stop-Market backup)
            set_ok = set_position_stoploss_long(symbol, floor_price)

            entry["floor_roi"] = target_floor_roi
            entry["floor_price"] = floor_price
            entry["floor_updated_ts"] = time.time()

            tlog(f"floor_up_long:{symbol}",
                 f"[FLOOR-UP][LONG] {symbol} MFE={mfe_roi:.1f}% → FloorROI={target_floor_roi:.1f}% → SL={floor_price:.6f} set={set_ok}", 30)
            set_position(symbol, entry)

        time.sleep(3)

def place_conditional_sl_long(symbol: str, stop_price: float, qty: float, trigger_by: str = TRIGGER_BY) -> bool:
    """
    Piazza/aggiorna uno stop-market reduceOnly per proteggere la posizione LONG.
    Usa l'endpoint order/create (v5) per un ordine condizionale di chiusura.
    """
    try:
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.01)
        price_step = info.get("price_step", 0.01)
        qty_str = _format_qty_with_step(float(qty), qty_step)
        stop_str = format_price_bybit(stop_price, price_step)
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,
            "positionIdx": LONG_IDX,
            "triggerBy": trigger_by,
            "triggerPrice": stop_str,
            "triggerDirection": 2,
            "closeOnTrigger": True,
            "timeInForce": "GoodTillCancel"
        }
        if LOG_DEBUG_STRATEGY:
            log(f"[SL-DEBUG-BODY][LONG] {json.dumps(body)}")
        resp = _bybit_signed_post("/v5/order/create", body)
        try:
            data = resp.json()
        except:
            data = {}
        if data.get("retCode") == 0:
            return True
        tlog(
            f"sl_create_err:{symbol}",
            f"[SL-PLACE][LONG] retCode={data.get('retCode')} msg={data.get('retMsg')} resp={json.dumps(data)} body={body}",
            300,
        )
        return False
    except Exception as e:
        tlog(f"sl_create_exc:{symbol}", f"[SL-PLACE][LONG] eccezione: {e}", 300)
        return False

def place_takeprofit_long(symbol: str, tp_price: float, qty: float) -> tuple[bool, str]:
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.01)
    min_qty = float(info.get("min_qty", 0.0))
    price_step = info.get("price_step", 0.01)
    qty_f = float(qty)
    # Guard: se la quantità parziale è sotto min_qty, salta TP1
    if qty_f < max(min_qty, float(qty_step)):
        tlog(f"tp_skip_min:{symbol}", f"[TP-SKIP][LONG] qty parziale {qty_f} < min_qty {min_qty} (step {qty_step})", 120)
        return False, ""
    qty_str = _format_qty_with_step(qty_f, qty_step)
    try:
        from decimal import Decimal
        if Decimal(qty_str) <= 0:
            tlog(f"tp_skip_zero:{symbol}", f"[TP-SKIP][LONG] qty_str={qty_str} non valido (≤0)", 120)
            return False, ""
    except Exception:
        pass
    tp_str = format_price_bybit(tp_price, price_step)
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Limit",
        "qty": qty_str,
        "price": tp_str,
        "timeInForce": "PostOnly",
        "reduceOnly": True,
        "positionIdx": LONG_IDX,
    }
    try:
        resp = _bybit_signed_post("/v5/order/create", body)
        data = resp.json()
    except Exception:
        data = {}
    if data.get("retCode") == 0:
        oid = data.get("result", {}).get("orderId", "") or ""
        tlog(
            f"tp_place:{symbol}",
            f"[TP-PLACE] {symbol} tp={tp_price:.6f} qty={qty_str} orderId={oid}",
            30,
        )
        return True, oid
    tlog(
        f"tp_create_err:{symbol}",
        f"[TP-PLACE][LONG] retCode={data.get('retCode')} msg={data.get('retMsg')}",
        300,
    )
    return False, ""

def fetch_history(symbol: str, interval=INTERVAL_MINUTES, limit=400):
    """
    Scarica la cronologia dei prezzi per il simbolo dato da Bybit (linear/futures).
    """
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": str(interval),
            "limit": limit
        }
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 10006:
            tlog(f"fetch_rl:{symbol}", f"[BYBIT] Rate limit su {symbol}, piccolo backoff...", 10)
            time.sleep(1.2)
            return None
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            log(f"[BYBIT] Errore fetch_history {symbol}: {data}")
            return None
        klines = data["result"]["list"]
        # Bybit restituisce i dati dal più vecchio al più recente
        df = pd.DataFrame(klines, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "Turnover"
        ])
        # Conversioni di tipo
        for col in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        log(f"[BYBIT] Errore fetch_history {symbol}: {e}")
        return None

def find_close_column(df):
    """
    Restituisce la colonna 'Close' se presente, altrimenti None.
    """
    for col in df.columns:
        if col.lower() == "close":
            return df[col]
    return None
def is_symbol_linear(symbol):
    """
    Verifica se il simbolo è disponibile su Bybit futures linear.
    """
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "linear", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        return data.get("retCode") == 0 and data["result"]["list"]
    except Exception:
        return False
    
def record_exit(symbol: str, entry_price: float, exit_price: float, side: str):
    # Aggiorna cooldown e contatore di loss
    last_exit_time[symbol] = time.time()
    if side == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:  # SHORT
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
    if pnl_pct < 0:
        recent_losses[symbol] = recent_losses.get(symbol, 0) + 1
        last_exit_was_loss[symbol] = True
    else:
        recent_losses[symbol] = 0
        last_exit_was_loss[symbol] = False
    
def analyze_asset(symbol: str):
    funding_rate = None  # funding rate corrente (da tickers API), usato come filtro
    # Telemetria ingresso (ADX/EMA slopes/RSI/breakout/24h change)
    try:
        resp1 = requests.get(f"{BYBIT_BASE_URL}/v5/market/kline", params={"category":"linear","symbol":symbol,"interval":"60","limit":120}, timeout=10)
        d1 = resp1.json()
        adx1h = None
        ema100_slope = None
        if d1.get("retCode") == 0 and d1.get("result",{}).get("list"):
            raw1 = d1["result"]["list"]
            df1 = pd.DataFrame(raw1, columns=["timestamp","Open","High","Low","Close","Volume","turnover"])
            # Convertiamo numerici per evitare errori nelle librerie ta
            for col in ("Open","High","Low","Close"):
                df1[col] = pd.to_numeric(df1[col], errors="coerce")
            df1.dropna(subset=["Close"], inplace=True)
            if len(df1) >= 100:
                adx_series = ADXIndicator(high=df1["High"].astype(float), low=df1["Low"].astype(float), close=df1["Close"].astype(float), window=14).adx()
                if len(adx_series) > 0:
                    adx1h = float(adx_series.iloc[-1])
                ema100 = EMAIndicator(close=df1["Close"], window=100).ema_indicator()
                ema100_slope = float(ema100.iloc[-1] - ema100.iloc[-2]) if len(ema100) >= 2 else None
        resp4 = requests.get(f"{BYBIT_BASE_URL}/v5/market/kline", params={"category":"linear","symbol":symbol,"interval":"240","limit":220}, timeout=10)
        d4 = resp4.json()
        ema200_slope = None
        if d4.get("retCode") == 0 and d4.get("result",{}).get("list"):
            raw4 = d4["result"]["list"]
            df4 = pd.DataFrame(raw4, columns=["timestamp","Open","High","Low","Close","Volume","turnover"])
            for col in ("Open","High","Low","Close"):
                df4[col] = pd.to_numeric(df4[col], errors="coerce")
            df4.dropna(subset=["Close"], inplace=True)
            if len(df4) >= 200:
                ema200 = EMAIndicator(close=df4["Close"], window=200).ema_indicator()
                ema200_slope = float(ema200.iloc[-1] - ema200.iloc[-2]) if len(ema200) >= 2 else None
        rsi1h = None
        if d1.get("retCode") == 0 and d1.get("result",{}).get("list"):
            try:
                rsi1h = float(RSIIndicator(close=df1["Close"], window=14).rsi().iloc[-1]) if len(df1) >= 15 else None
            except:
                rsi1h = None
        breakout_ok = is_breaking_weekly_high(symbol) if ENABLE_BREAKOUT_FILTER else None
        chg = None
        try:
            tick = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category":"linear","symbol":symbol}, timeout=10).json()
            if tick.get("retCode") == 0 and tick.get("result", {}).get("list"):
                lst = tick["result"]["list"]
                chg = float(lst[0].get("price24hPcnt", 0.0))
                funding_rate = float(lst[0].get("fundingRate") or 0.0)
        except:
            chg = None
        tlog(f"telem_long:{symbol}", f"[TELEM][LONG][{symbol}] adx1h={adx1h} ema100_slope={ema100_slope} ema200_slope={ema200_slope} rsi1h={rsi1h} chg24h={chg} funding={funding_rate}", 300)
    except Exception as e:
        log(f"[TELEM][LONG][{symbol}] errore telemetria: {e}")

    # Trend filter configurabile
    up_4h = is_trending_up(symbol, "240")
    up_1h = is_trending_up_1h(symbol, "60")

    # BTC favorevole (4h uptrend) → accettiamo solo 4h; altrimenti richiediamo 4h+1h
    trend_ok = up_4h if _btc_favorable_long else (up_4h and up_1h)

    # Filtro trend: obbligatorio quando BTC non è in uptrend (contesto sfavorevole)
    if not _btc_favorable_long and not trend_ok:
        tlog(f"trend_long:{symbol}", f"[TREND-FILTER][{symbol}] BTC sfavorevole, trend non idoneo, skip.", 600)
        return None, None, None

    # Breakout filter: permetti fallback se trend è forte anche senza breakout
    if ENABLE_BREAKOUT_FILTER:
        brk = is_breaking_weekly_high(symbol)
        if not brk:
            adx_thresh = ENTRY_ADX_VOLATILE if (symbol in VOLATILE_ASSETS) else ENTRY_ADX_STABLE
            ema_up = (ema200_slope is not None and ema200_slope > 0) or (ema100_slope is not None and ema100_slope > 0)
            strong_trend = trend_ok and (adx1h is not None and adx1h >= adx_thresh) and ema_up
            if not strong_trend:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"breakout_long:{symbol}", f"[BREAKOUT-FILTER][{symbol}] No breakout e fallback non soddisfatto → skip", 600)
                return None, None, None

    try:
        is_volatile = symbol in VOLATILE_ASSETS
        tf_minutes = ENTRY_TF_VOLATILE if is_volatile else ENTRY_TF_STABLE

        df = fetch_history(symbol, interval=tf_minutes)
        if df is None or len(df) < 4:
            # Riduce spam: messaggio ogni 5 minuti per simbolo
            tlog(f"analyze:data:{symbol}", f"[ANALYZE] Dati storici insufficienti per {symbol}", 300)
            return None, None, None

        close = find_close_column(df)
        if close is None:
            # Riduce spam: messaggio ogni 5 minuti per simbolo
            tlog(f"analyze:close:{symbol}", f"[ANALYZE] Colonna close non trovata per {symbol}", 300)
            return None, None, None

        # Indicatori
        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()
        df["ema20"] = EMAIndicator(close=close, window=20).ema_indicator()
        df["ema50"] = EMAIndicator(close=close, window=50).ema_indicator()
        df["ema200"] = EMAIndicator(close=close, window=200).ema_indicator()
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=["bb_upper","bb_lower","rsi","sma20","sma50","ema20","ema50","ema200","macd","macd_signal","adx","atr"], inplace=True)
        if len(df) < 4:
            return None, None, None

        # ADX base: permissivo quando BTC favorevole (uptrend 4h), standard altrimenti
        if _btc_favorable_long:
            adx_threshold = (ENTRY_ADX_VOLATILE - 3) if is_volatile else (ENTRY_ADX_STABLE - 3)  # 24 / 21
        else:
            adx_threshold = ENTRY_ADX_VOLATILE if is_volatile else ENTRY_ADX_STABLE               # 27 / 24

        # Usa SOLO candele chiuse per i segnali (evita repaint)
        last = df.iloc[-2]       # candela appena chiusa
        prev = df.iloc[-3]       # candela chiusa precedente
        price = float(df["Close"].iloc[-1])  # prezzo attuale (candela in corso)
        tf_tag = f"({tf_minutes}m)"

        # Filtro estensione: evita LONG troppo sopra EMA20 + k*ATR
        ema20v = float(last["ema20"]); atrv = float(last["atr"])
        k = 1.8 if symbol in LARGE_CAPS else 1.5
        ext_cap = ema20v + k * atrv
        if float(last["Close"]) > ext_cap:
            if LOG_DEBUG_STRATEGY:
                tlog(f"ext_long:{symbol}", f"[FILTER][{symbol}] Estensione: close {last['Close']:.6f} > ema20 {ema20v:.6f} + {k}*ATR ({ext_cap:.6f})", 600)
            return None, None, None

        # Eventi e stati
        rsi_th = RSI_LONG_THRESHOLD
        ema_bullish_cross = (prev["ema20"] <= prev["ema50"]) and (last["ema20"] > last["ema50"])
        macd_bullish_cross = (prev["macd"] <= prev["macd_signal"]) and (last["macd"] > last["macd_signal"])
        rsi_break = (prev["rsi"] <= rsi_th) and (last["rsi"] > rsi_th)

        ema_state = last["ema20"] > last["ema50"]
        macd_state = last["macd"] > last["macd_signal"]
        rsi_state = last["rsi"] > rsi_th

        event_triggered = ema_bullish_cross or macd_bullish_cross or rsi_break
        conf_count = [ema_state, macd_state, rsi_state].count(True)
        
        # Confluenza richiesta: più alta quando BTC non favorevole
        if not _btc_favorable_long:
            required_confluence = MIN_CONFLUENCE + 1
        else:
            required_confluence = MIN_CONFLUENCE

        # Quando BTC non favorevole, richiedi SEMPRE un evento reale (cross/break)
        if not _btc_favorable_long and not event_triggered:
            return None, None, None

        # ADX richiesto + bonus extra quando BTC non favorevole
        adx_needed = max(0.0, adx_threshold - (ADX_RELAX_EVENT if event_triggered else 0.0))
        if not _btc_favorable_long:
            adx_needed += 1.5

        # >>> PATCH: throttle DD → più conferme, ADX più alto, e richiedi evento se in DD
        required_confluence += RISK_THROTTLE_LEVEL
        adx_needed += 1.5 * RISK_THROTTLE_LEVEL
        if RISK_THROTTLE_LEVEL >= 1 and not event_triggered:
            return None, None, None

        tlog(
            f"entry_chk_long:{symbol}",
            f"[ENTRY-CHECK][LONG] conf={conf_count}/{required_confluence} | ADX={last['adx']:.1f}>{adx_needed:.1f} | event={event_triggered} | btc_fav={_btc_favorable_long} | tf={tf_tag}",
            300
        )

        # Guardrail su loss recenti
        if recent_losses.get(symbol, 0) >= MAX_CONSEC_LOSSES:
            wait_min = (time.time() - last_exit_time.get(symbol, 0)) / 60
            if price < last["ema50"] and wait_min < FORCED_WAIT_MIN:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"loss_guard:{symbol}", f"[LOSS-GUARD] Blocco LONG {symbol} (loss={recent_losses.get(symbol)}) sotto EMA50, wait {wait_min:.1f}m", 300)
                return None, None, None

        # Segnale ingresso LONG: richiede evento fresco E confluenza minima
        # FIX: rimosso "OR event_triggered" che permetteva ingressi con un solo cross
        min_conf_with_event = max(1, required_confluence - 1)  # evento = bonus -1 di confluenza
        entry_condition = (event_triggered and conf_count >= min_conf_with_event) or (conf_count >= required_confluence)
        if (entry_condition and float(last["adx"]) > adx_needed):
            # Filtro volume: segnale deve avere almeno 60% del volume medio (ultimi 20 periodi chiusi)
            vol_series = pd.to_numeric(df["Volume"], errors="coerce")
            vol_avg20 = vol_series.iloc[-22:-2].mean() if len(vol_series) >= 22 else 0.0
            vol_last = float(vol_series.iloc[-2])
            vol_ratio = vol_last / vol_avg20 if vol_avg20 > 0 else 1.0
            if vol_ratio < 0.6:
                tlog(f"vol_low:{symbol}", f"[VOL-FILTER][LONG] {symbol} volume={vol_ratio:.2f}x media, segnale debole, skip", 300)
                return None, None, None
            # Filtro funding: se longs sovraccaricati (funding alto) → pressione ribassista
            if funding_rate is not None and funding_rate > FUNDING_LONG_MAX:
                tlog(f"funding:{symbol}", f"[FUNDING-FILTER][LONG] {symbol} funding={funding_rate:.4%} > max {FUNDING_LONG_MAX:.4%}, skip", 300)
                return None, None, None
            entry_strategies = []
            if ema_state: entry_strategies.append(f"EMA Bullish {tf_tag}")
            if macd_state: entry_strategies.append(f"MACD Bullish {tf_tag}")
            if rsi_state: entry_strategies.append(f"RSI Bullish {tf_tag}")
            entry_strategies.append("ADX Trend")
            if LOG_DEBUG_STRATEGY:
                log(f"[ENTRY-LONG][{symbol}] EVENTO/CONFLUENZA → {entry_strategies}")
            return "entry", ", ".join(entry_strategies), price

        # OVERRIDE: pullback su trend BULL (mean reversion)
        # Attivo solo in BULL con 4h ancora up e RSI 1h fortemente ipervenduto
        # Cattura i ritracciamenti profondi senza aspettare la conferma lagging degli EMA/MACD
        if (_btc_favorable_long
                and ema200_slope is not None and ema200_slope > 0   # 4h ancora in uptrend
                and rsi1h is not None and rsi1h < 32                # RSI 1h ipervenduto
                and adx1h is not None and adx1h > 18                # trend ancora attivo su 1h
                and price > last["ema200"]                          # prezzo sopra EMA200 60m (non in crash)
                and RISK_THROTTLE_LEVEL == 0                        # nessun drawdown attivo
                and (funding_rate is None or funding_rate <= FUNDING_LONG_MAX)):  # no funding estremo
            tlog(f"pullback_long:{symbol}",
                 f"[PULLBACK-OVERRIDE][LONG] {symbol} | rsi1h={rsi1h:.1f} adx1h={adx1h:.1f} ema200_slope={ema200_slope:.4f} → ingresso pullback BULL",
                 300)
            return "entry", f"Pullback BULL RSI{rsi1h:.0f} (1h)", price

        # Segnali uscita
        cond_exit1 = last["Close"] < last["bb_lower"] and last["rsi"] < 45
        def can_exit(symbol, current_price):
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price")
            entry_time = entry.get("entry_time")
            if not entry_price or not entry_time:
                return True
            r = abs(current_price - entry_price) / (entry_price * INITIAL_STOP_LOSS_PCT)
            holding_min = (time.time() - entry_time) / 60
            # FIX2: uscita solo dopo 90 minuti (allineato a candele 60m) o se molto in perdita
            return (r > 0.5) or (holding_min > 90)

        if cond_exit1 and can_exit(symbol, price):
            return "exit", "Breakdown BB + RSI (bearish)", price

        exit_1h = False
        try:
            df_1h = fetch_history(symbol, interval=60)
            if df_1h is not None and len(df_1h) > 2:
                macd_1h = MACD(close=df_1h["Close"])
                df_1h["macd"] = macd_1h.macd()
                df_1h["macd_signal"] = macd_1h.macd_signal()
                df_1h["adx"] = ADXIndicator(high=df_1h["High"], low=df_1h["Low"], close=df_1h["Close"]).adx()
                last_1h = df_1h.iloc[-1]
                if last_1h["macd"] < last_1h["macd_signal"] and last_1h["adx"] > adx_threshold:
                    exit_1h = True
        except Exception:
            exit_1h = False

        if last["macd"] < last["macd_signal"] and last["adx"] > adx_threshold and exit_1h and can_exit(symbol, price):
            return "exit", "MACD bearish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None
# ...existing code...

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT [LONG] AVVIATO - In ascolto per segnali di ingresso/uscita")

TEST_MODE = False  # Acquisti e vendite normali abilitati

def sync_positions_from_wallet():
    log("[SYNC] Avvio scansione posizioni LONG DAL CONTO (tutti i simboli linear)...")
    trovate = 0
    endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
    params = {"category": "linear"}
    from urllib.parse import urlencode
    query_string = urlencode(sorted(params.items()))
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    sign_payload = f"{ts}{KEY}{recv_window}{query_string}"
    sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window
    }
    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        pos_list = data.get("result", {}).get("list", []) if data.get("retCode") == 0 else []
    except Exception:
        pos_list = []

    # Filtra posizioni LONG (side=Buy)
    symbols = {p["symbol"] for p in pos_list if p.get("side") == "Buy" and float(p.get("size", 0) or 0) > 0} or set(ASSETS)
    for symbol in symbols:
        if symbol == "USDT":
            continue
        qty = get_open_long_qty(symbol)
        if LOG_DEBUG_SYNC:
            log(f"[SYNC-DEBUG] {symbol}: qty long trovata = {qty}")
        if qty and qty > 0:
            price = get_last_price(symbol)
            if not price:
                continue
            try:
                pos = next(p for p in pos_list if p.get("symbol") == symbol and p.get("side") == "Buy")
                entry_price = float(pos.get("avgPrice") or price)
            except StopIteration:
                entry_price = price
            entry_cost = qty * entry_price
            # Calcola ATR e parametri coerenti con la nuova gestione (r_dist, tp1)
            df = fetch_history(symbol)
            if df is not None and "Close" in df.columns:
                try:
                    atr_series = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                    atr_val = float(atr_series.iloc[-1])
                except Exception:
                    atr_val = price * 0.02
            else:
                atr_val = price * 0.02

            # Nuovi parametri locali coerenti con R-based: r_dist e tp1
            r_dist = atr_val * SL_ATR_MULT
            tp = entry_price + (TP1_R * r_dist)
            # SL locale informativo; non modifichiamo ordini a sync
            sl_atr = entry_price - r_dist
            sl_cap = entry_price * (1.0 - MAX_LOSS_CAP_PCT)
            final_sl = max(sl_atr, sl_cap)
            set_position(symbol, {
                "entry_price": entry_price,
                "tp": tp,
                "sl": final_sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price,
                "r_dist": r_dist
            })
            trovate += 1
            log(f"[SYNC] Posizione LONG trovata: {symbol} qty={qty} entry={entry_price:.4f} SL={final_sl:.4f} TP={tp:.4f}")
            # Piazza subito stop di posizione + conditional (backup) col CAP
            set_position_stoploss_long(symbol, final_sl)
            place_conditional_sl_long(symbol, final_sl, qty, trigger_by="MarkPrice")
            # Marca come già in posizione per evitare nuovi entry
            add_open(symbol)

            # >>> PATCH: BE-LOCK immediato se già oltre soglia al riavvio
            try:
                if price >= entry_price * (1.0 + BREAKEVEN_LOCK_PCT) and not position_data[symbol].get("be_locked"):
                    be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)
                    qty_live = get_open_long_qty(symbol)
                    if qty_live and qty_live > 0:
                        place_conditional_sl_long(symbol, be_price, qty_live, trigger_by="MarkPrice")
                        set_position_stoploss_long(symbol, be_price)
                        entry_pd = get_position(symbol) or {}
                        entry_pd["be_locked"] = True
                        entry_pd["be_price"] = be_price
                        set_position(symbol, entry_pd)
                        tlog(f"be_lock_sync:{symbol}", f"[BE-LOCK-SYNC][LONG] SL→BE {be_price:.6f}", 300)
            except Exception as e:
                tlog(f"be_lock_sync_exc:{symbol}", f"[BE-LOCK-SYNC][LONG] exc: {e}", 300)
                
    log(f"[SYNC] Totale posizioni LONG recuperate dal wallet: {trovate}")

# --- Esegui sync all'avvio ---

# Aggiorna la lista asset all'avvio
update_assets()
sync_positions_from_wallet()

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

import threading

 

# --- LOGICA 70/30 SU VALORE TOTALE PORTAFOGLIO (USDT + coin) ---
def get_portfolio_value():
    """
    Restituisce (equity totale reale, saldo USDT disponibile, esposizione per simbolo).
    Equity viene presa da /v5/account/wallet-balance per evitare gonfiaggi dovuti al notional.
    """
    # Equity e bilancio
    try:
        resp = _bybit_signed_get("/v5/account/wallet-balance", {"accountType": BYBIT_ACCOUNT_TYPE})
        data = resp.json()
        acct = data.get("result", {}).get("list", [{}])[0]
        total_equity = float(acct.get("totalEquity") or acct.get("totalAvailableBalance") or 0.0)
        usdt_balance = 0.0
        for c in acct.get("coin", []):
            if c.get("coin") == "USDT":
                usdt_balance = float(c.get("availableToWithdraw") or c.get("availableBalance") or c.get("walletBalance") or 0.0)
                break
    except Exception:
        total_equity = get_usdt_balance() or 0.0
        usdt_balance = total_equity

    # Mappa esposizioni (notional), utile solo per bilanciamento 40/60
    coin_values = {}
    symbols = set(ASSETS) | set(open_positions)
    for symbol in symbols:
        if symbol == "USDT":
            continue
        qty = get_open_long_qty(symbol)
        price = get_last_price(symbol)
        if qty and qty > 0 and price:
            coin_values[symbol] = qty * price

    return total_equity, usdt_balance, coin_values

 
# >>> PATCH: avvio worker di breakeven lock (LONG)
be_lock_thread_long = threading.Thread(target=breakeven_lock_worker_long, daemon=True)
be_lock_thread_long.start()
profit_floor_thread_long = threading.Thread(target=profit_floor_worker_long, daemon=True)
profit_floor_thread_long.start()

while True:
    update_assets()

    _update_daily_anchor_and_btc_context()
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()

    # >>> PATCH: throttle DD (più selettivo invece di bloccare, a meno che ENABLE_DD_PAUSE=1)
    if _daily_start_equity:
        dd_pct = (portfolio_value - _daily_start_equity) / max(1e-9, _daily_start_equity)  # negativo se in DD
        if ENABLE_DD_PAUSE and dd_pct < -DAILY_DD_CAP_PCT:
            tlog("dd_cap", f"🛑 DD giornaliero {-dd_pct*100:.2f}% > cap {DAILY_DD_CAP_PCT*100:.1f}%, stop nuovi LONG per {DD_PAUSE_MINUTES}m", 600)
            _trading_paused_until = time.time() + DD_PAUSE_MINUTES * 60
        else:
            draw = -dd_pct  # positivo se in perdita
            RISK_THROTTLE_LEVEL = 2 if draw > DAILY_DD_CAP_PCT * 2 else (1 if draw > DAILY_DD_CAP_PCT else 0)
            if RISK_THROTTLE_LEVEL > 0:
                tlog("dd_throttle", f"[THROTTLE] DD={draw*100:.2f}% → livello={RISK_THROTTLE_LEVEL}", 600)

    portfolio_value, usdt_balance, coin_values = get_portfolio_value()
    volatile_budget = portfolio_value * 0.4
    stable_budget = portfolio_value * 0.6
    volatile_invested = sum(coin_values.get(s, 0) for s in open_positions if s in VOLATILE_ASSETS)
    stable_invested = sum(coin_values.get(s, 0) for s in open_positions if s in LESS_VOLATILE_ASSETS)
    tlog("portfolio_long", f"[PORTAFOGLIO] equity={portfolio_value:.2f} USDT | pos={len(open_positions)} | liberi={usdt_balance:.2f} | btc_fav={_btc_favorable_long}", 900)

    # Analisi in parallelo con prefiltraggio
    eligible_symbols = [s for s in ASSETS if s not in STABLECOIN_BLACKLIST and is_symbol_linear(s)]
    log(f"[CICLO][LONG] simboli={len(eligible_symbols)} | pos_aperte={len(open_positions)} | btc_fav={_btc_favorable_long} | equity={portfolio_value:.2f}")
    results = {}
    if eligible_symbols:
        with ThreadPoolExecutor(max_workers=4) as ex:
            future_map = {ex.submit(analyze_asset, s): s for s in eligible_symbols}
            for fut in as_completed(future_map):
                s = future_map[fut]
                try:
                    results[s] = fut.result()
                except Exception as e:
                    tlog(f"analyze_exc:{s}", f"[ANALYZE-EXC] {s} {e}", 300)
    for symbol in eligible_symbols:
        signal, strategy, price = results.get(symbol, (None, None, None))
        if signal is None or strategy is None or price is None:
            continue
        log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ✅ ENTRATA LONG
        if signal == "entry":
            # >>> GATE: blocca solo le NUOVE APERTURE (non gli exit)
            if ENABLE_DD_PAUSE and time.time() < _trading_paused_until:
                tlog(f"paused:{symbol}", f"[PAUSE] trading sospeso (DD cap), skip LONG {symbol}", 600)
                continue
            # Regime gate rimosso: analyze_asset gestisce già i requisiti più stringenti quando BTC è sfavorevole

            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                cd_min = COOLDOWN_MINUTES * 4 if last_exit_was_loss.get(symbol) else COOLDOWN_MINUTES
                if elapsed < cd_min * 60:
                    if LOG_DEBUG_STRATEGY:
                        tlog(f"cooldown:{symbol}", f"⏳ Cooldown {'post-loss' if last_exit_was_loss.get(symbol) else 'post-win'} attivo per {symbol}, salto ingresso", 300)
                    continue
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                tlog(f"maxpos", f"[MAX-POS] {len(open_positions)}/{MAX_OPEN_POSITIONS} posizioni aperte, skip {symbol}", 300)
                continue
            if symbol in open_positions:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"inpos:{symbol}", f"⏩ Ignoro apertura LONG: già in posizione su {symbol}", 1800)
                continue

            # Se c’è già una posizione SHORT aperta (altro bot), non aprire il LONG sullo stesso simbolo
            if get_open_short_qty(symbol) > 0:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"opp_side:{symbol}", f"[SKIP] {symbol} ha SHORT aperto, salto LONG", 300)
                continue

            is_volatile = symbol in VOLATILE_ASSETS
            group_budget = volatile_budget if is_volatile else stable_budget
            group_invested = volatile_invested if is_volatile else stable_invested
            group_available = max(0.0, group_budget - group_invested)

            weights_no_tf = {
                # Nuovi nomi (confluenza)
                "EMA Bullish": 0.75,
                "MACD Bullish": 0.70,
                "RSI Bullish": 0.60,
                "ADX Trend": 0.85,
                # Vecchi nomi (compatibilità log storici)
                "Breakout BB": 1.00,
                "MACD bullish + ADX": 0.90,
                "Incrocio EMA 20/50": 0.75,
                "EMA20>EMA50": 0.75,
                "MACD cross up": 0.65,
                "MACD bullish": 0.65,
                "Trend EMA+RSI": 0.60
            }
            parts = [p.strip().split(" (")[0] for p in (strategy or "").split(",") if p.strip()]
            if parts:
                base = max(weights_no_tf.get(p, 0.5) for p in parts)
                bonus = min(0.1 * (len(parts) - 1), 0.3)  # +0.1 per conferma, max +0.3
                strength = min(1.0, base + bonus)
            else:
                strength = 0.5
             # >>> PATCH: throttle DD – riduci aggressività
            if RISK_THROTTLE_LEVEL == 1:
                strength *= 0.7
            elif RISK_THROTTLE_LEVEL >= 2:
                strength *= 0.5

            df_hist = fetch_history(symbol)
            if df_hist is not None and "Close" in df_hist.columns:
                try:
                    atr = AverageTrueRange(high=df_hist["High"], low=df_hist["Low"], close=df_hist["Close"], window=ATR_WINDOW).average_true_range()
                    atr_val = atr.iloc[-1]
                    last_price = df_hist["Close"].iloc[-1]
                    atr_ratio = atr_val / last_price if last_price > 0 else 0
                    if atr_ratio > 0.08:
                        strength *= 0.5
                    elif atr_ratio > 0.04:
                        strength *= 0.75
                except Exception:
                    pass

            # --- Sizing basato sul rischio (ATR e R) ---
            price_now_calc = get_last_price(symbol) or price
            df = fetch_history(symbol, interval=INTERVAL_MINUTES)
            if df is None or len(df) < max(ATR_WINDOW+2, 50):
                tlog(f"no_hist:{symbol}", f"[SKIP] Storico insufficiente per sizing ATR su {symbol}", 600)
                continue
            atr_series = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
            atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
            if atr_val <= 0:
                tlog(f"atr_zero:{symbol}", f"[SKIP] ATR nullo per {symbol}", 600)
                continue
            r_dist = atr_val * SL_ATR_MULT
            risk_usdt = max(0.0, float(portfolio_value) * RISK_PCT)
            qty_target = risk_usdt / max(1e-9, r_dist)
            notional_target = qty_target * price_now_calc
            max_notional_by_margin = usdt_balance * DEFAULT_LEVERAGE * MARGIN_USE_PCT
            order_amount = min(notional_target * max(0.5, min(1.0, strength)), group_available, max_notional_by_margin, 1000.0)
            info_i = get_instrument_info(symbol)
            min_order_amt = float(info_i.get("min_order_amt", 5))
            min_qty = float(info_i.get("min_qty", 0.0))
            price_now_chk = price_now_calc
            min_notional = max(min_order_amt, (min_qty or 0.0) * price_now_chk)
            if order_amount < min_notional:
                bump = min_notional * 1.01
                max_by_margin = max_notional_by_margin
                if max_by_margin >= bump:
                    old = order_amount
                    order_amount = min(bump, max_by_margin, 1000.0)
                    tlog(f"bump_notional:{symbol}", f"[BUMP-NOTIONAL][{symbol}] alzato notional da {old:.2f} a {order_amount:.2f} per rispettare min_qty/min_notional", 600)
                else:
                    tlog(f"min_notional:{symbol}", f"❌ Notional richiesto {order_amount:.2f} < minimo {min_notional:.2f} per {symbol} (min_qty={min_qty}, price={price_now_chk})", 300)
                    continue

            if TEST_MODE:
                log(f"[TEST_MODE] LONG inibiti per {symbol}")
                continue

            # Calcola una volta la quantità coerente con i vincoli di strumento
            qty_str = calculate_quantity(symbol, order_amount)
            if not qty_str:
                log(f"❌ Quantità non valida per LONG di {symbol}")
                continue
            qty = market_long(symbol, order_amount, qty_exact=qty_str)
            price_now = get_last_price(symbol)
            if not price_now:
                log(f"❌ Prezzo non disponibile post-ordine per {symbol}")
                continue

            df = fetch_history(symbol)
            if df is not None and "Close" in df.columns:
                atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                atr_val = float(atr.iloc[-1])
            else:
                atr_val = price_now * 0.02

            if not qty or qty == 0:
                if LOG_DEBUG_STRATEGY:
                    log(f"❌ LONG non aperto per {symbol}")
                continue

            # TP1_R regime-aware: in BTC favorevole lascia correre (2.5R), altrimenti prende profitto prima
            _tp1_r = TP1_R if _btc_favorable_long else 1.8
            tp1_price = price_now + (_tp1_r * r_dist)
            qty_tp1 = max(0.0, qty * TP1_PARTIAL)
            tp_oid = None
            if qty_tp1 > 0:
                ok_tp, tp_oid = place_takeprofit_long(symbol, tp1_price, qty_tp1)
                if ok_tp:
                    tlog(f"tp1_long:{symbol}", f"[TP1] {symbol} tp1={tp1_price:.6f} qty={qty_tp1}", 60)

            actual_cost = qty * price_now
            
            # APPPLICA CAP PERDITA: non oltre MAX_LOSS_CAP_PCT sotto l'entry
            sl_cap = price_now * (1.0 - MAX_LOSS_CAP_PCT)
            final_sl = max(price_now - r_dist, sl_cap)
            set_position_stoploss_long(symbol, final_sl)
            # Backup: piazza anche uno Stop-Market reduceOnly
            try:
                place_conditional_sl_long(symbol, final_sl, qty, trigger_by="MarkPrice")
            except Exception:
                pass

            # Niente trailing immediato; sarà attivato sopra 2R
            trail_threshold = price_now + (TRAIL_START_R * r_dist)
            log(f"[ENTRY-DETAIL] {symbol} | Entry: {price_now:.4f} | SL: {final_sl:.4f} | TP1: {tp1_price:.4f} | ATR: {atr_val:.4f} | Trail@≥{trail_threshold:.4f}")
            _trade_log("entry", symbol, "LONG", entry_price=price_now, qty=qty, sl=final_sl, tp=tp1_price, r_dist=r_dist,
                       extra={"tp1_qty": qty_tp1})

            set_position(symbol, {
                "entry_price": price_now,
                "tp": tp1_price,
                "tp_order_id": tp_oid if 'tp_oid' in locals() else None,
                "sl_order_id": None,
                "sl": final_sl,
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price_now,
                "r_dist": r_dist
            })
            add_open(symbol)
            notify_telegram(f"🟢📈 LONG aperto {symbol}\nPrezzo: {price_now:.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f}\nSL: {final_sl:.4f}\nTP1: {tp1_price:.4f}")
            time.sleep(3)

        # EXIT LONG (segnale di uscita strategico)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            
            qty = get_open_long_qty(symbol)
            if not qty or qty <= 0:
                discard_open(symbol)
                last_exit_time[symbol] = time.time()  # cooldown anche se già chiusa dall'exchange
                with _state_lock:
                    position_data.pop(symbol, None)
                continue

            resp = market_close_long(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                current_price = get_last_price(symbol)
                exit_value = current_price * qty
                pnl = ((current_price - entry_price) / entry_price) * 100.0
                log(f"[EXIT-LONG][{symbol}] prezzo={current_price:.4f} entry={entry_price:.4f} pnl={pnl:.2f}% qty={qty:.4f}")
                notify_telegram(f"✅ Exit LONG {symbol} a {current_price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}%")
                record_exit(symbol, entry_price, current_price, "LONG")
                _trade_log("exit", symbol, "LONG", entry_price=entry_price, qty=qty, sl=entry.get("sl", 0.0), tp=entry.get("tp", 0.0), r_dist=entry.get("r_dist", 0.0), extra={"pnl_pct": pnl})
                try:
                    _expectancy_log(pnl, qty * entry_price, exit_value, maker_entry=False, maker_exit=False)
                except Exception:
                    pass
                # (Report Google Sheets rimosso)
                discard_open(symbol)
                last_exit_time[symbol] = time.time()
                with _state_lock:
                    position_data.pop(symbol, None)
                if get_open_short_qty(symbol) == 0:
                    cancel_all_orders(symbol)

    # Cleanup posizioni con qty troppo bassa
    for symbol in list(open_positions):
        saldo = get_open_long_qty(symbol)
        info = get_instrument_info(symbol)
        min_qty = info.get("min_qty", 0.0)
        # cleanup SOLO se lettura qty è valida e < min_qty
        if (saldo is not None) and (saldo < min_qty):
            tlog(f"ext_close:{symbol}", f"[CLEANUP][LONG] {symbol} chiusa lato exchange (qty={saldo}). Cancello TP/SL.", 60)
            discard_open(symbol)
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", get_last_price(symbol) or 0.0)
            exit_price = get_last_price(symbol) or 0.0
            record_exit(symbol, entry_price, exit_price, "LONG")
            with _state_lock:
                position_data.pop(symbol, None)
            if get_open_short_qty(symbol) == 0:
                cancel_all_orders(symbol)
    
    # --- SAFETY: impone il BE se il worker non è riuscito a piazzarlo ---
    for symbol in list(open_positions):
        entry = position_data.get(symbol)
        if not entry or entry.get("be_locked"):
            continue
        price_now = get_last_price(symbol)
        if not price_now:
            continue
        entry_price = entry.get("entry_price", price_now)
        # LONG: trigger BE se prezzo ≥ entry*(1 + 1%)
        if price_now >= entry_price * (1.0 + BREAKEVEN_LOCK_PCT):
            be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)  # sopra entry
            qty_live = get_open_long_qty(symbol)
            if qty_live and qty_live > 0:
                place_conditional_sl_long(symbol, be_price, qty_live, trigger_by="MarkPrice")
                set_position_stoploss_long(symbol, be_price)
                entry["be_locked"] = True
                entry["be_price"] = be_price
                tlog(f"be_lock_safety:{symbol}", f"[BE-LOCK-SAFETY][LONG] SL→BE {be_price:.6f}", 60)

    time.sleep(180)