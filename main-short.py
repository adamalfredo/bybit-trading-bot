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

# --- Sizing per trade (notional) ---
DEFAULT_LEVERAGE = 10          # leva usata sul conto (Cross/Isolated)
MARGIN_USE_PCT = 0.35
TARGET_NOTIONAL_PER_TRADE = 200.0

INTERVAL_MINUTES = 60  # era 15
ATR_WINDOW = 14
TP_FACTOR = 2.5
SL_FACTOR = 1.2
# Soglie dinamiche consigliate
TP_MIN = 2.0
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.0
TRAILING_MIN = 0.015  # era 0.005, trailing più largo
TRAILING_MAX = 0.05   # era 0.03, trailing più largo
TRAILING_ACTIVATION_THRESHOLD = 0.015  # trailing parte dopo -1.5%
TRAILING_SL_BUFFER = 0.015            # era 0.007, trailing SL più largo
TRAILING_DISTANCE = 0.04              # era 0.02, trailing SL più largo
ENABLE_TP1 = False       # abilita TP parziale a 1R
TP1_R_MULT = 1.0        # target TP1 a 1R
TP1_CLOSE_PCT = 0.5     # chiudi il 50% a TP1
INITIAL_STOP_LOSS_PCT = 0.03          # era 0.02, SL iniziale più largo
COOLDOWN_MINUTES = 60
# Nuovi parametri protezione guadagni (SHORT)
MIN_PROTECT_PCT = 1.0       # soglia minima protezione (1%)  // <<< PATCH commento corretto
TRAILING_PROTECT_TIERS = [
    (2, 2.0),
    (5, 3.0),
    (10, 4.0),
    (20, 5.0)
]
TRIGGER_BY = "LastPrice"
TRAILING_POLL_SEC = 5

RATCHET_TIERS_ROI = [
    (30, 15),
    (45, 30),
    (60, 45),
    (80, 60),
    (100, 75)
]
FLOOR_BUFFER_PCT = 0.0015          # 0.15% di prezzo per sicurezza esecuzione
FLOOR_UPDATE_COOLDOWN_SEC = 8      # evita update troppo frequenti
FLOOR_TRIGGER_BY = "MarkPrice"     # usa Mark per coerenza con SL

# >>> PATCH: parametri breakeven lock (SHORT)
BREAKEVEN_LOCK_PCT = 0.01     # -1% di prezzo ≈ +10% PnL a 10x
BREAKEVEN_BUFFER   = -0.0015  # buffer SOTTO l’entry (chiusura sempre ≥ BE)
MAX_LOSS_CAP_PCT = 0.015  # CAP perdita sul prezzo: 1.5% sopra l'entry

# >>> NEW: regime + drawdown giornaliero (SHORT)
DAILY_DD_CAP_PCT = 0.04         # blocca nuovi ingressi se equity < -4% dal livello di inizio giorno
REGIME_REFRESH_SEC = 180        # aggiorna regime ogni 3 minuti
CURRENT_REGIME = "MIXED"        # BULL / BEAR / MIXED
_last_regime_ts = 0
_daily_start_equity = None
_trading_paused_until = 0

cooldown = {}
ORDER_USDT = 50.0
ENABLE_BREAKOUT_FILTER = False  # rende opzionale il filtro breakout 6h
# --- MTF entry: segnali su 15m, trend su 4h/1h ---
USE_MTF_ENTRY = True
ENTRY_TF_MINUTES = 60
# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}
recent_losses = {}          # conteggio loss consecutivi per simbolo
last_entry_side = {}        # "LONG" o "SHORT"
MAX_CONSEC_LOSSES = 2       # dopo 2 loss consecutivi blocca nuovi ingressi
FORCED_WAIT_MIN = 90        # attesa minima (minuti) se il contesto resta sfavorevole
# ---- Logging flags (accensione selettiva via env) ----
LOG_DEBUG_ASSETS     = os.getenv("LOG_DEBUG_ASSETS", "0") == "1"
LOG_DEBUG_DECIMALS   = os.getenv("LOG_DEBUG_DECIMALS", "0") == "1"
LOG_DEBUG_SYNC       = os.getenv("LOG_DEBUG_SYNC", "0") == "1"
LOG_DEBUG_STRATEGY   = os.getenv("LOG_DEBUG_STRATEGY", "0") == "1"
LOG_DEBUG_TRAILING   = os.getenv("LOG_DEBUG_TRAILING", "0") == "1"
LOG_DEBUG_PORTFOLIO  = os.getenv("LOG_DEBUG_PORTFOLIO", "0") == "1"
# --- Loosening via env ---
MIN_CONFLUENCE = 1
TREND_MODE = "LOOSE_4H"
ENTRY_TF_VOLATILE = 30
ENTRY_TF_STABLE = 30
ENTRY_ADX_VOLATILE = 22       # fisso
ENTRY_ADX_STABLE = 20         # fisso
ADX_RELAX_EVENT = 3.0
RSI_SHORT_THRESHOLD = 48.0
LIQUIDITY_MIN_VOLUME = 1_000_000
LINEAR_MIN_TURNOVER = 5_000_000
# Large-cap con minQty elevata: abilita auto-bump del notional al minimo (come nel LONG)
LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}

# --- BLACKLIST STABLECOIN ---
STABLECOIN_BLACKLIST = [
    "USDCUSDT", "USDEUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "EURUSDT", "USDTUSDT"
]
EXCLUSION_LIST = ["FUSDT", "YBUSDT", "ZBTUSDT", "RECALLUSDT", "XPLUSDT", "BRETTUSDT"]

def is_trending_down(symbol: str, tf: str = "240"):
    """
    Ritorna True se l'asset è in downtrend su timeframe superiore (default 4h).
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": tf,
        "limit": 220  # almeno 200 barre per EMA200
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return False
        raw = data["result"]["list"]
        df = pd.DataFrame(raw, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "turnover"
        ])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 200:
            return False
        ema200 = EMAIndicator(close=df["Close"], window=200).ema_indicator()
        # Downtrend se EMA200 decrescente e prezzo sotto EMA200
        return df["Close"].iloc[-1] < ema200.iloc[-1] and ema200.iloc[-1] <= ema200.iloc[-2]
    except Exception:
        return False

def is_trending_down_1h(symbol: str, tf: str = "60"):
    """
    Ritorna True se l'asset è in downtrend su timeframe 1h.
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": tf,
        "limit": 120  # almeno 100 barre per EMA100
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return False
        raw = data["result"]["list"]
        df = pd.DataFrame(raw, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "turnover"
        ])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 100:
            return False
        ema100 = EMAIndicator(close=df["Close"], window=100).ema_indicator()
        # Downtrend se EMA100 decrescente e prezzo sotto EMA100
        return df["Close"].iloc[-1] < ema100.iloc[-1] and ema100.iloc[-1] <= ema100.iloc[-2]
    except Exception:
        return False

def is_trending_up(symbol: str, tf: str = "240"):
    """
    True se l'asset è in uptrend su 4h: prezzo sopra EMA200 e EMA200 crescente.
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {"category": "linear", "symbol": symbol, "interval": tf, "limit": 220}
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
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
        resp = requests.get(endpoint, params=params, timeout=10)
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

def _get_market_breadth():
    """Breadth futures linear: quota di simboli con price24hPcnt < 0."""
    try:
        resp = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "linear"}, timeout=10)
        data = resp.json()
        lst = data.get("result", {}).get("list", [])
        if not lst:
            return 0.5
        changes = []
        for t in lst:
            try:
                changes.append(float(t.get("price24hPcnt", 0.0)))
            except:
                pass
        red = sum(1 for c in changes if c < 0)
        return red / max(1, len(changes))
    except:
        return 0.5

def _detect_market_regime():
    """
    Regole:
    - BEAR: BTC/ETH in downtrend su 4h e breadth rossa > 0.6
    - BULL: BTC/ETH NON in downtrend e breadth rossa < 0.4
    - MIXED: altrimenti
    """
    try:
        btc_down = is_trending_down("BTCUSDT", "240")
        eth_down = is_trending_down("ETHUSDT", "240")
        breadth_red = _get_market_breadth()
        if btc_down and eth_down and breadth_red > 0.6:
            return "BEAR"
        if (not btc_down) and (not eth_down) and breadth_red < 0.4:
            return "BULL"
        return "MIXED"
    except:
        return "MIXED"

def _equity_now():
    total, usdt_balance, coin_values = get_portfolio_value()
    return total

def _update_daily_anchor_and_regime():
    """Aggiorna ancora giornaliera di equity e regime di mercato con throttling."""
    global _daily_start_equity, CURRENT_REGIME, _last_regime_ts
    # reset ancora se cambia il giorno
    if _daily_start_equity is None or time.strftime("%Y-%m-%d") != time.strftime("%Y-%m-%d", time.localtime(_last_regime_ts or time.time())):
        _daily_start_equity = _equity_now()
    # refresh regime
    if time.time() - _last_regime_ts > REGIME_REFRESH_SEC:
        CURRENT_REGIME = _detect_market_regime()
        _last_regime_ts = time.time()
        tlog("regime", f"[REGIME] mercato={CURRENT_REGIME}", 180)

def is_breaking_weekly_low(symbol: str):
    """
    True se il prezzo attuale è sotto il minimo delle ultime 6 ore.
    """
    df = fetch_history(symbol, interval=INTERVAL_MINUTES)
    bars = int(6 * 60 / INTERVAL_MINUTES)
    if df is None or len(df) < bars:
        return False
    last_close = df["Close"].iloc[-1]
    low = df["Low"].iloc[-bars:].min()
    return last_close <= low * 0.995  # tolleranza 0.5% sotto il minimo

def update_assets(top_n=18, n_stable=7):
    """
    Aggiorna ASSETS, LESS_VOLATILE_ASSETS e VOLATILE_ASSETS:
    - Top N per volume 24h su spot (USDT)
    - Intersezione con futures linear con turnover24h >= LINEAR_MIN_TURNOVER
    - Esclude STABLECOIN_BLACKLIST e EXCLUSION_LIST
    """
    global ASSETS, LESS_VOLATILE_ASSETS, VOLATILE_ASSETS
    try:
        resp_spot = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "spot"}, timeout=10)
        data_spot = resp_spot.json()
        if data_spot.get("retCode") != 0:
            log(f"[ASSETS] Errore API spot: {data_spot}")
            return
        spot = data_spot["result"]["list"]
        spot_usdt = [
            t for t in spot
            if t["symbol"].endswith("USDT")
            and float(t.get("turnover24h", 0)) >= LIQUIDITY_MIN_VOLUME
            and t["symbol"] not in STABLECOIN_BLACKLIST
            and t["symbol"] not in EXCLUSION_LIST
        ]
        spot_usdt.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        top = spot_usdt[:top_n]
        pre = [t["symbol"] for t in top]

        resp_lin = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "linear"}, timeout=10)
        data_lin = resp_lin.json()
        if data_lin.get("retCode") != 0:
            log(f"[ASSETS] Errore API linear: {data_lin}")
            return
        linear = data_lin["result"]["list"]
        linear_liquid = {
            t["symbol"] for t in linear
            if float(t.get("turnover24h", 0)) >= LINEAR_MIN_TURNOVER
        }

        # snapshot lista precedente per log differenziali
        prev = set(ASSETS)

        filtered = [s for s in pre if s in linear_liquid and s not in EXCLUSION_LIST]
        ASSETS = filtered
        LESS_VOLATILE_ASSETS = filtered[:n_stable]
        VOLATILE_ASSETS = [s for s in filtered if s not in LESS_VOLATILE_ASSETS]

        changed = set(ASSETS) != prev
        if changed or LOG_DEBUG_ASSETS:
            added = list(set(ASSETS) - prev)
            removed = list(prev - set(ASSETS))
            if LOG_DEBUG_ASSETS:
                log(f"[ASSETS] Aggiornati: {ASSETS}\nMeno volatili: {LESS_VOLATILE_ASSETS}\nVolatili: {VOLATILE_ASSETS}")
            else:
                log(f"[ASSETS] Totali={len(ASSETS)} (+{len(added)}/-{len(removed)}) | Added={added[:5]} Removed={removed[:5]}")
    except Exception as e:
        log(f"[ASSETS] Errore aggiornamento lista asset: {e}")

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

# Throttling semplice per log ripetitivi
_last_log_times = {}
def tlog(key: str, msg: str, interval_sec: int = 60):
    now = time.time()
    last = _last_log_times.get(key, 0)
    if now - last >= interval_sec:
        _last_log_times[key] = now
        log(msg)

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
    if hasattr(qty_step, '__precision_override__'):
        precision = qty_step.__precision_override__
    if precision is None:
        precision = get_decimals(qty_step)
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
    if LOG_DEBUG_DECIMALS:
        log(f"[DECIMALI][FORMAT_QTY] qty={qty} | qty_step={qty_step} | precision={precision} | floored_qty={floored_qty} | quantize_str={quantize_str}")
    return fmt.format(floored_qty)

def format_price_bybit(price: float, tick_size: float) -> str:
    step = Decimal(str(tick_size))
    p = Decimal(str(price))
    floored = (p // step) * step
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

def get_open_short_qty(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
        params = {"category": "linear", "symbol": symbol}
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
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_SYNC:
                tlog(f"qty_err:{symbol}", f"[BYBIT-RAW][ERRORE] get_open_short_qty {symbol}: {json.dumps(data)}", 300)
            return None  # <<< PRIMA era 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Sell":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        if LOG_DEBUG_SYNC:
            tlog(f"qty_exc:{symbol}", f"❌ Errore get_open_short_qty per {symbol}: {e}", 300)
        return None  # <<< PRIMA era 0.0

def get_open_long_qty(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
        params = {"category": "linear", "symbol": symbol}
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
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_SYNC:
                tlog(f"qty_err_long:{symbol}", f"[BYBIT-RAW][ERRORE] get_open_long_qty {symbol}: {json.dumps(data)}", 300)
            return None  # <<< PRIMA era 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Buy":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        if LOG_DEBUG_SYNC:
            tlog(f"qty_exc_long:{symbol}", f"❌ Errore get_open_long_qty per {symbol}: {e}", 300)
        return None  # <<< PRIMA era 0.0

# --- FUNZIONI DI SUPPORTO BYBIT E TELEGRAM ---
def get_last_price(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "linear", "symbol": symbol}  # PATCH: era "spot"
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            price = data["result"]["list"][0]["lastPrice"]
            return float(price)
        else:
            log(f"[BYBIT] Errore get_last_price {symbol}: {data}")
            return None
    except Exception as e:
        log(f"[BYBIT] Errore get_last_price {symbol}: {e}")
        return None

def get_instrument_info(symbol: str) -> dict:
    """
    Info strumento con cache 5m.
    Fallback conservativo: qty_step=0.01, min_order_amt=10 per evitare 170137/170140 ripetitivi.
    """
    now = time.time()
    # Cache semplice (aggiungi queste variabili globali in alto)
    global _instrument_cache
    if '_instrument_cache' not in globals():
        _instrument_cache = {}
    
    cached = _instrument_cache.get(symbol)
    if cached and (now - cached["ts"] < 300):
        return cached["data"]

    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "linear", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"❌ get_instrument_info retCode {data.get('retCode')} → fallback {symbol}")
            parsed = {
                "min_qty": 0.01,
                "qty_step": 0.01,
                "precision": 4,
                "price_step": 0.01,
                "min_order_amt": 10.0
            }
            _instrument_cache[symbol] = {"data": parsed, "ts": now}
            return parsed
        
        lst = data.get("result", {}).get("list", [])
        if not lst:
            log(f"❌ get_instrument_info lista vuota → fallback {symbol}")
            parsed = {
                "min_qty": 0.01,
                "qty_step": 0.01,
                "precision": 4,
                "price_step": 0.01,
                "min_order_amt": 10.0
            }
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
            "min_order_amt": float(info.get("minOrderAmt", 10) or 10)
        }
        _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed
        
    except Exception as e:
        log(f"❌ Errore get_instrument_info eccezione → fallback {symbol}: {e}")
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

    url = f"{BYBIT_BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": BYBIT_ACCOUNT_TYPE}

    from urllib.parse import urlencode
    query_string = urlencode(params)
    timestamp = str(int(time.time() * 1000))
    sign_payload = f"{timestamp}{KEY}5000{query_string}"
    sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000"
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=10)
        data = resp.json()

        if "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_PORTFOLIO:
                log(f"❗ Struttura inattesa da Bybit per {symbol}: {resp.text}")
            return 0.0

        coin_list = data["result"]["list"][0].get("coin", [])
        for c in coin_list:
            if c["coin"] == coin:
                raw = c.get("walletBalance", "0")
                try:
                    qty = float(raw) if raw else 0.0
                    # Log SOLO per USDT e con throttling
                    if coin == "USDT":
                        tlog("balance_usdt", f"📦 Saldo USDT: {qty}", 1800)  # max 1 riga/10min
                    else:
                        if LOG_DEBUG_PORTFOLIO:
                            log(f"[BALANCE] {coin}: {qty}")
                    return qty
                except Exception as e:
                    if LOG_DEBUG_PORTFOLIO:
                        log(f"⚠️ Errore conversione quantità {coin}: {e}")
                    return 0.0

        if LOG_DEBUG_PORTFOLIO:
            log(f"🔍 Coin {coin} non trovata nel saldo.")
        return 0.0

    except Exception as e:
        if LOG_DEBUG_PORTFOLIO:
            log(f"❌ Errore nel recupero saldo per {symbol}: {e}")
        return 0.0

def notify_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TELEGRAM] Token o chat_id non configurati")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"[SHORT] {msg}"}
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

def market_short(symbol: str, usdt_amount: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None
    
    info = get_instrument_info(symbol)
    qty_step = float(info.get("qty_step", 0.01))
    min_qty = float(info.get("min_qty", qty_step))
    min_order_amt = float(info.get("min_order_amt", 10.0))

    safe_usdt_amount = usdt_amount * 0.98
    raw_qty = Decimal(str(safe_usdt_amount)) / Decimal(str(price))
    step_dec = Decimal(str(qty_step))
    qty_aligned = (raw_qty // step_dec) * step_dec

    # Guardie: evita qty 0 e rispetta minimi exchange
    if float(qty_aligned) <= 0 or float(qty_aligned) < min_qty:
        qty_aligned = Decimal(str(min_qty))
        if LOG_DEBUG_STRATEGY:
            log(f"[QTY-GUARD][{symbol}] qty riallineata a min_qty={float(qty_aligned)}")

    needed = Decimal(str(min_order_amt)) / Decimal(str(price))
    multiples = (needed / step_dec).quantize(Decimal('1'), rounding=ROUND_UP)
    min_notional_qty = multiples * step_dec
    if qty_aligned * Decimal(str(price)) < Decimal(str(min_order_amt)):
        qty_aligned = max(qty_aligned, min_notional_qty)
        if LOG_DEBUG_STRATEGY:
            log(f"[NOTIONAL-GUARD][{symbol}] qty alzata per min_order_amt → {float(qty_aligned)}")

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) <= 0:
            log(f"❌ qty_str=0 per {symbol}, skip ordine")
            return None

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty_str,
            "positionIdx": 2
        }
        ts = str(int(time.time() * 1000))
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}5000{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json"
        }

        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        if LOG_DEBUG_STRATEGY:
            log(f"[SHORT][{symbol}] attempt {attempt}/{max_retries} BODY={body_json}")

        try:
            resp_json = response.json()
        except:
            resp_json = {}
        if LOG_DEBUG_STRATEGY:
            log(f"[SHORT][{symbol}] RESP {response.status_code} {resp_json}")

        if resp_json.get("retCode") == 0:
            return float(qty_str)

        ret_code = resp_json.get("retCode")
        if ret_code == 170137:
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY][{symbol}] 170137 → refresh instrument e rifloor")
            try: _instrument_cache.pop(symbol, None)
            except Exception: pass
            info = get_instrument_info(symbol)
            qty_step = float(info.get("qty_step", qty_step))
            step_dec = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            continue
        elif ret_code == 170131:
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY][{symbol}] 170131 → riduco qty del 10%")
            qty_aligned = (qty_aligned * Decimal("0.9")) // step_dec * step_dec
            if qty_aligned <= 0:
                return None
            continue
        else:
            tlog(f"short_err:{symbol}:{ret_code}", f"[ERROR][{symbol}] Errore non gestito: {ret_code}", 300)
            break

    return None

def market_cover(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile ricoprire")
        return None
    
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.01)  # Fallback conservativo
    
    # Allinea quantità al passo
    step_dec = Decimal(str(qty_step))
    qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",  # Chiusura short = Buy
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,          # <--- FIX
            "positionIdx": 2
        }
        
        ts = str(int(time.time() * 1000))
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}5000{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json"
        }
        
        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        if LOG_DEBUG_STRATEGY:
            log(f"[COVER][{symbol}] attempt {attempt}/{max_retries} BODY={body_json}")
        
        try:
            resp_json = response.json()
        except:
            resp_json = {}
        
        if LOG_DEBUG_STRATEGY:
            log(f"[COVER][{symbol}] RESP {response.status_code} {resp_json}")
        
        if resp_json.get("retCode") == 0:
            return response
            
        # Gestione errori con escalation
        ret_code = resp_json.get("retCode")
        if ret_code == 170137:  # Too many decimals
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY-COVER][{symbol}] 170137 → escalation passo")
            if qty_step < 0.1:
                qty_step = 0.1
            elif qty_step < 1.0:
                qty_step = 1.0
            else:
                qty_step = 10.0
            
            step_dec = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY-COVER][{symbol}] nuovo passo {qty_step}, qty→{qty_aligned}")
            continue
            
        elif ret_code == 170131:  # Insufficient balance (non dovrebbe accadere per cover)
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY-COVER][{symbol}] 170131 → problema inaspettato")
            break
            
        else:
            tlog(f"cover_err:{symbol}:{ret_code}", f"[ERROR-COVER][{symbol}] Errore non gestito: {ret_code}", 300)
            break
    
    return None

def cancel_all_orders(symbol: str, order_filter: Optional[str] = None) -> bool:
    body = {"category": "linear", "symbol": symbol}
    if order_filter:
        body["orderFilter"] = order_filter  # es: "StopOrder"
    ts = str(int(time.time() * 1000))
    body_json = json.dumps(body, separators=(",", ":"))
    payload = f"{ts}{KEY}5000{body_json}"
    sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/cancel-all", headers=headers, data=body_json, timeout=10)
        ok = resp.json().get("retCode") == 0
        if not ok:
            tlog(f"cancel_all_err:{symbol}", f"[CANCEL-ALL] {symbol} resp={resp.text}", 300)
        return ok
    except Exception as e:
        tlog(f"cancel_all_exc:{symbol}", f"[CANCEL-ALL] {symbol} exc: {e}", 300)
        return False

# >>> PATCH: funzioni per impostare lo stopLoss sulla posizione (SHORT) e worker BE
def set_position_stoploss_short(symbol: str, sl_price: float) -> bool:
    body = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": f"{sl_price:.8f}",
        "slTriggerBy": "MarkPrice",   # <<< FIX: allinea al conditional
        "positionIdx": 2
    }
    ts = str(int(time.time() * 1000))
    body_json = json.dumps(body, separators=(",", ":"))
    payload = f"{ts}{KEY}5000{body_json}"
    sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign, "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json"
    }
    try:
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/position/trading-stop", headers=headers, data=body_json, timeout=10)
        data = resp.json()
        ok = data.get("retCode") == 0
        if not ok:
            tlog(f"sl_pos_err:{symbol}", f"[POS-SL][SHORT] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
        return ok
    except Exception as e:
        tlog(f"sl_pos_exc:{symbol}", f"[POS-SL][SHORT] exc: {e}", 300)
        return False

def breakeven_lock_worker_short():
    # Porta lo stop della POSIZIONE a breakeven e piazza anche uno Stop-Market a BE
    while True:
        for symbol in list(open_positions):
            entry = position_data.get(symbol)
            if not entry or entry.get("be_locked"):
                continue

            price_now = get_last_price(symbol)
            if not price_now:
                continue

            entry_price = entry.get("entry_price", price_now)
            # Trigger BE quando il prezzo è sceso almeno dell’1% (≈ +10% PnL a 10x)
            if price_now <= entry_price * (1.0 - BREAKEVEN_LOCK_PCT):
                # Short: copertura a BE con piccolo buffer di profitto
                be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)

                # 2) piazza Stop-Market reduceOnly a BE (sul book)
                qty_live = get_open_short_qty(symbol)
                if qty_live and qty_live > 0:
                    place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")

                # 3) aggiorna anche lo stopLoss della POSIZIONE (trading-stop)
                set_position_stoploss_short(symbol, be_price)

                entry["be_locked"] = True
                entry["be_price"] = be_price
                tlog(f"be_lock:{symbol}", f"[BE-LOCK][SHORT] {symbol} SL→BE {be_price:.6f}", 60)
        time.sleep(5)

def _pick_floor_roi_short(mfe_roi: float) -> Optional[float]:
    """
    Ritorna il floor ROI (SHORT) oppure None se non superata la prima soglia.
    Ignora floor=0 (non applica nulla).
    """
    if not RATCHET_TIERS_ROI:
        return None
    first_threshold = RATCHET_TIERS_ROI[0][0]
    if mfe_roi < first_threshold:
        return None
    target = None
    for th, floor in RATCHET_TIERS_ROI:
        if mfe_roi >= th and floor > 0:
            target = floor
    return target

def profit_floor_worker_short():
    """
    Aggiorna stopLoss posizione (SHORT) a scalini di ROI solo dopo prima soglia.
    Salta floor=0. Non abbassa mai il floor. Usa trading-stop + Stop-Market backup.
    """
    while True:
        for symbol in list(open_positions):
            entry = position_data.get(symbol) or {}
            entry_price = entry.get("entry_price")
            qty_live = get_open_short_qty(symbol)
            if not entry_price or not qty_live or qty_live <= 0:
                continue

            price_now = get_last_price(symbol)
            if not price_now:
                continue

            # ROI (SHORT): prezzo scende → ROI positivo
            price_move_pct = ((entry_price - price_now) / entry_price) * 100.0
            roi_now = price_move_pct * DEFAULT_LEVERAGE

            mfe_roi = max(entry.get("mfe_roi", 0.0), roi_now)
            entry["mfe_roi"] = mfe_roi

            target_floor_roi = _pick_floor_roi_short(mfe_roi)
            prev_floor_roi = entry.get("floor_roi", None)

            if target_floor_roi is None:
                position_data[symbol] = entry
                continue

            if prev_floor_roi is not None and target_floor_roi <= prev_floor_roi:
                position_data[symbol] = entry
                continue

            last_upd = entry.get("floor_updated_ts", 0)
            if time.time() - last_upd < FLOOR_UPDATE_COOLDOWN_SEC:
                continue

            # Converti floor ROI in prezzo (SHORT: entry * (1 - delta))
            delta_pct_price = (target_floor_roi / max(1, DEFAULT_LEVERAGE)) / 100.0
            floor_price = entry_price * (1.0 - delta_pct_price)

            # Buffer (SHORT → leggermente sopra il floor per attivarsi prima se risale)
            floor_price *= (1.0 + FLOOR_BUFFER_PCT)

            # Se floor_price ≤ prezzo attuale, lo stop sarebbe sotto → inutile (non protegge profitto)
            if floor_price <= price_now:
                entry["floor_roi"] = target_floor_roi
                entry["floor_price"] = floor_price
                entry["floor_updated_ts"] = time.time()
                position_data[symbol] = entry
                tlog(f"floor_up_short_skip:{symbol}",
                     f"[FLOOR-UP-SKIP][SHORT] {symbol} MFE={mfe_roi:.1f}% targetROI={target_floor_roi:.1f}% floorPrice={floor_price:.6f} ≤ current={price_now:.6f}", 120)
                continue

            set_ok = set_position_stoploss_short(symbol, floor_price)
            place_conditional_sl_short(symbol, floor_price, qty_live, trigger_by=FLOOR_TRIGGER_BY)

            entry["floor_roi"] = target_floor_roi
            entry["floor_price"] = floor_price
            entry["floor_updated_ts"] = time.time()

            tlog(f"floor_up_short:{symbol}",
                 f"[FLOOR-UP][SHORT] {symbol} MFE={mfe_roi:.1f}% → FloorROI={target_floor_roi:.1f}% → SL={floor_price:.6f} set={set_ok}", 30)
            position_data[symbol] = entry

        time.sleep(3)

def place_conditional_sl_short(symbol: str, stop_price: float, qty: float, trigger_by: str = TRIGGER_BY) -> bool:
    """
    Piazza/aggiorna uno stop-market reduceOnly per proteggere la posizione SHORT.
    Side=Buy (cover), reduceOnly=true, triggerPrice = stop_price.
    """
    try:
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.01)
        price_step = info.get("price_step", 0.01)                 # <<< aggiunto
        qty_str = _format_qty_with_step(float(qty), qty_step)
        stop_str = format_price_bybit(stop_price, price_step)     # <<< aggiunto

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,
            "positionIdx": 2,
            "triggerBy": trigger_by,
            "triggerPrice": stop_str,                              # <<< sostituito
            "triggerDirection": 1,
            "closeOnTrigger": True
        }
        if LOG_DEBUG_STRATEGY:
            log(f"[SL-DEBUG-BODY][SHORT] {json.dumps(body)}")
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
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json, timeout=10)
        try:
            data = resp.json()
        except:
            data = {}
        if data.get("retCode") == 0:
            return True
        tlog(f"sl_create_err:{symbol}", f"[SL-PLACE][SHORT] retCode={data.get('retCode')} msg={data.get('retMsg')} resp={json.dumps(data)} body={body}", 300)
        return False
    except Exception as e:
        tlog(f"sl_create_exc:{symbol}", f"[SL-PLACE][SHORT] eccezione: {e}", 300)
        return False

def place_takeprofit_short(symbol: str, tp_price: float, qty: float) -> (bool, str):
    try:
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.01)
        price_step = info.get("price_step", 0.01)              # <<< aggiunto
        qty_str = _format_qty_with_step(float(qty), qty_step)
        tp_str = format_price_bybit(tp_price, price_step)      # <<< aggiunto

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": qty_str,
            "price": tp_str,                                    # <<< sostituito
            "timeInForce": "PostOnly",
            "reduceOnly": True,
            "positionIdx": 2
        }
        ts = str(int(time.time() * 1000))
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}5000{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json"
        }
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json, timeout=10)
        try:
            data = resp.json()
        except:
            data = {}
        if data.get("retCode") == 0:
            oid = data.get("result", {}).get("orderId", "") or ""
            tlog(f"tp_place_short:{symbol}", f"[TP-PLACE] {symbol} tp={tp_price:.6f} qty={qty_str} orderId={oid}", 30)
            return True, oid
        tlog(f"tp_create_err_short:{symbol}", f"[TP-PLACE][SHORT] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
        return False, ""
    except Exception as e:
        tlog(f"tp_create_exc_short:{symbol}", f"[TP-PLACE][SHORT] exc: {e}", 300)
        return False, ""

def place_trailing_stop_short(symbol: str, trailing_dist: float):
    body = {
        "category": "linear",
        "symbol": symbol,
        "trailingStop": str(trailing_dist),
        "positionIdx": 2
    }
    ts = str(int(time.time() * 1000))
    body_json = json.dumps(body, separators=(",", ":"))
    payload = f"{ts}{KEY}5000{body_json}"
    sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }
    resp = requests.post(f"{BYBIT_BASE_URL}/v5/position/trading-stop", headers=headers, data=body_json, timeout=10)
    try:
        data = resp.json()
    except:
        data = {}
    if data.get("retCode") == 0:
        tlog(f"trailing_short:{symbol}", f"[TRAILING-PLACE-SHORT] {symbol} trailing={trailing_dist}", 30)
        return True
    tlog(f"trailing_short_err:{symbol}", f"[TRAILING-PLACE-SHORT][ERR] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
    return False

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
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 10006:
            tlog(f"fetch_rl:{symbol}", f"[BYBIT] Rate limit su {symbol}, piccolo backoff...", 10)
            time.sleep(1.2)
            return None
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            tlog(f"fetch_err:{symbol}", f"[BYBIT] Errore fetch_history {symbol}: {data}", 600)
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
        tlog(f"fetch_exc:{symbol}", f"[BYBIT] Errore fetch_history {symbol}: {e}", 600)
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
    last_exit_time[symbol] = time.time()
    if side == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:  # SHORT
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
    if pnl_pct < 0:
        recent_losses[symbol] = recent_losses.get(symbol, 0) + 1
    else:
        recent_losses[symbol] = 0
    last_entry_side[symbol] = side

def analyze_asset(symbol: str):
    # Filtro trend configurabile (SHORT)
    down_4h = is_trending_down(symbol, "240")
    down_1h = is_trending_down_1h(symbol, "60")

    if TREND_MODE == "STRICT":
        trend_ok = down_4h and down_1h
    elif TREND_MODE == "LOOSE_4H":
        trend_ok = down_4h
    else:  # ANY
        trend_ok = down_4h or down_1h

    # Filtro trend attivo SOLO in regime MIXED per evitare short “deboli” in range
    if CURRENT_REGIME == "MIXED" and not trend_ok:
        if LOG_DEBUG_STRATEGY:
            tlog(f"trend_short:{symbol}", f"[TREND-FILTER][{symbol}] Regime=MIXED, trend non idoneo (mode={TREND_MODE})", 600)
        return None, None, None

    # Breakout (solo log informativo, non blocca)
    if ENABLE_BREAKOUT_FILTER and not is_breaking_weekly_low(symbol):
        if LOG_DEBUG_STRATEGY:
            tlog(f"breakout_short:{symbol}", f"[BREAKOUT-FILTER][{symbol}] Non in breakdown 6h (info)", 1800)

    try:
        is_volatile = symbol in VOLATILE_ASSETS
        tf_minutes = ENTRY_TF_VOLATILE if (USE_MTF_ENTRY and is_volatile) else ENTRY_TF_STABLE

        df = fetch_history(symbol, interval=tf_minutes)
        if df is None or len(df) < 3:
            if LOG_DEBUG_STRATEGY:
                log(f"[ANALYZE][{symbol}] Dati insufficienti ({tf_minutes}m)")
            return None, None, None

        close = find_close_column(df)
        if close is None:
            log(f"[ANALYZE][{symbol}] Colonna Close assente")
            return None, None, None

        # Indicatori
        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["ema20"] = EMAIndicator(close=close, window=20).ema_indicator()
        df["ema50"] = EMAIndicator(close=close, window=50).ema_indicator()
        df["ema200"] = EMAIndicator(close=close, window=200).ema_indicator()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=["bb_upper","bb_lower","rsi","ema20","ema50","ema200","macd","macd_signal","adx","atr"], inplace=True)
        if len(df) < 3:
            return None, None, None

        adx_threshold = ENTRY_ADX_VOLATILE if is_volatile else ENTRY_ADX_STABLE
        last = df.iloc[-1]; prev = df.iloc[-2]
        price = float(last["Close"])
        tf_tag = f"({tf_minutes}m)"

        # Eventi (trigger anticipato)
        rsi_th = RSI_SHORT_THRESHOLD
        ema_bearish_cross = (prev["ema20"] >= prev["ema50"]) and (last["ema20"] < last["ema50"])
        macd_bearish_cross = (prev["macd"] >= prev["macd_signal"]) and (last["macd"] < last["macd_signal"])
        rsi_break = (prev["rsi"] >= rsi_th) and (last["rsi"] < rsi_th)

        # Stati
        ema_state = last["ema20"] < last["ema50"]
        macd_state = last["macd"] < last["macd_signal"]
        rsi_state = last["rsi"] < rsi_th

        event_triggered = ema_bearish_cross or macd_bearish_cross or rsi_break
        conf_count = [ema_state, macd_state, rsi_state].count(True)
        required_confluence = MIN_CONFLUENCE + (1 if CURRENT_REGIME == "MIXED" else 0)

        adx_needed = max(0.0, adx_threshold - (ADX_RELAX_EVENT if event_triggered else 0.0))
        if CURRENT_REGIME == "MIXED":
            adx_needed += 1.5

        if LOG_DEBUG_STRATEGY:
            tlog(
                f"entry_chk_short:{symbol}",
                f"[ENTRY-CHECK][SHORT] conf={conf_count}/{required_confluence} | ADX={last['adx']:.1f}>{adx_needed:.1f} | event={event_triggered} | regime={CURRENT_REGIME} | tf={tf_tag}",
                300
            )

        # Guardrail loss consecutivi (SHORT): se troppe perdite recenti e prezzo sopra ema50 → aspetta
        if recent_losses.get(symbol, 0) >= MAX_CONSEC_LOSSES:
            wait_min = (time.time() - last_exit_time.get(symbol, 0)) / 60
            if price > last["ema50"] and wait_min < FORCED_WAIT_MIN:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"loss_guard_short:{symbol}",
                         f"[LOSS-GUARD][SHORT] Blocco {symbol} (loss={recent_losses.get(symbol)}) sopra EMA50 wait={wait_min:.1f}m",
                         300)
                return None, None, None

        # Segnale ingresso SHORT
        if (((conf_count >= required_confluence) or event_triggered) and float(last["adx"]) > adx_needed):
            entry_strategies = []
            if ema_state: entry_strategies.append(f"EMA Bearish {tf_tag}")
            if macd_state: entry_strategies.append(f"MACD Bearish {tf_tag}")
            if rsi_state: entry_strategies.append(f"RSI Bearish {tf_tag}")
            entry_strategies.append("ADX Trend")
            if LOG_DEBUG_STRATEGY:
                log(f"[ENTRY-SHORT][{symbol}] EVENTO/CONFLUENZA → {entry_strategies}")
            return "entry", ", ".join(entry_strategies), price

        # Segnale uscita (chiudi SHORT se eccesso di compressione)
        cond_exit1 = last["Close"] > last["bb_upper"] and last["rsi"] > 55
        def can_exit(symbol, current_price):
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price")
            entry_time = entry.get("entry_time")
            if not entry_price or not entry_time:
                return True
            r = abs(current_price - entry_price) / (entry_price * INITIAL_STOP_LOSS_PCT)
            holding_min = (time.time() - entry_time) / 60
            # SHORT: se risalito sopra entry con >0.5R contro e poco tempo → chiudi
            return (current_price > entry_price and r > 0.5) or holding_min > 60

        if cond_exit1 and can_exit(symbol, price):
            return "exit", "Breakout BB + RSI (bullish)", price

        exit_1h = False
        try:
            df_1h = fetch_history(symbol, interval=60)
            if df_1h is not None and len(df_1h) > 2:
                macd_1h = MACD(close=df_1h["Close"])
                df_1h["macd"] = macd_1h.macd()
                df_1h["macd_signal"] = macd_1h.macd_signal()
                df_1h["adx"] = ADXIndicator(high=df_1h["High"], low=df_1h["Low"], close=df_1h["Close"]).adx()
                last_1h = df_1h.iloc[-1]
                if last_1h["macd"] > last_1h["macd_signal"] and last_1h["adx"] > adx_threshold:
                    exit_1h = True
        except Exception:
            exit_1h = False

        if last["macd"] > last["macd_signal"] and last["adx"] > adx_threshold and exit_1h and can_exit(symbol, price):
            return "exit", "MACD bullish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi SHORT {symbol}: {e}")
        return None, None, None

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT [SHORT] AVVIATO - In ascolto per segnali di ingresso/uscita")

TEST_MODE = False  # Acquisti e vendite normali abilitati

def sync_positions_from_wallet():
    log("[SYNC] Avvio scansione posizioni short DAL CONTO (tutti i simboli linear)...")
    trovate = 0
    # Legge l'elenco completo posizioni aperte
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

    # Per ciascuna posizione short aperta
    symbols = {p["symbol"] for p in pos_list if p.get("side") == "Sell" and float(p.get("size", 0) or 0) > 0} or set(ASSETS)
    for symbol in symbols:
        if symbol == "USDT":
            continue
        # PATCH: log dettagliato per ogni asset
        qty = get_open_short_qty(symbol)
        if LOG_DEBUG_SYNC:
            log(f"[SYNC-DEBUG] {symbol}: qty short trovata = {qty}")
        if qty and qty > 0:
            price = get_last_price(symbol)
            if not price:
                continue
            open_positions.add(symbol)
            # dentro sync_positions_from_wallet(), prima di calcolare tp/sl:
            try:
                pos = next(p for p in pos_list if p.get("symbol") == symbol and p.get("side") == "Sell")
                entry_price = float(pos.get("avgPrice") or price)
            except StopIteration:
                entry_price = price
            entry_cost = qty * price
            # Calcola ATR e SL/TP di default
            df = fetch_history(symbol)
            if df is not None and "Close" in df.columns:
                try:
                    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                    last = df.iloc[-1]
                    atr_val = last["atr"] if "atr" in last else atr.iloc[-1]
                except Exception:
                    atr_val = price * 0.02
            else:
                atr_val = price * 0.02
            tp = price - (atr_val * TP_FACTOR)
            sl_atr = entry_price + (atr_val * SL_FACTOR)       # riferimento entry
            sl_cap = entry_price * (1.0 + MAX_LOSS_CAP_PCT)    # cap 3% sopra entry
            final_sl = min(sl_atr, sl_cap)
            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": final_sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_min": price
            }
            trovate += 1
            log(f"[SYNC] Posizione trovata: {symbol} qty={qty} entry={entry_price:.4f} SL={final_sl:.4f} TP={tp:.4f}")
            set_position_stoploss_short(symbol, final_sl)
            place_conditional_sl_short(symbol, final_sl, qty, trigger_by="MarkPrice")

            # >>> PATCH: BE-LOCK immediato se già oltre soglia al riavvio (SHORT)
            try:
                if price <= entry_price * (1.0 - BREAKEVEN_LOCK_PCT) and not position_data[symbol].get("be_locked"):
                    be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)  # buffer negativo
                    qty_live = get_open_short_qty(symbol)
                    if qty_live and qty_live > 0:
                        place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                        set_position_stoploss_short(symbol, be_price)
                        position_data[symbol]["be_locked"] = True
                        position_data[symbol]["be_price"] = be_price
                        tlog(f"be_lock_sync:{symbol}", f"[BE-LOCK-SYNC][SHORT] SL→BE {be_price:.6f}", 300)
            except Exception as e:
                tlog(f"be_lock_sync_exc:{symbol}", f"[BE-LOCK-SYNC][SHORT] exc: {e}", 300)

    log(f"[SYNC] Totale posizioni short recuperate dal wallet: {trovate}")

# --- Esegui sync all'avvio ---

# Aggiorna la lista asset all'avvio
update_assets()
sync_positions_from_wallet()

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

def calculate_stop_loss(entry_price, current_price, p_min, trailing_active):
    # SHORT: SL iniziale sopra l’entry; in trailing usa p_min
    if not trailing_active:
        return entry_price * (1 + INITIAL_STOP_LOSS_PCT)
    else:
        return p_min * (1 + TRAILING_DISTANCE)


import threading
import gspread
from google.oauth2.service_account import Credentials

# Config
SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"  # copia da URL: https://docs.google.com/spreadsheets/d/<QUESTO>/edit
SHEET_NAME = "Short"  # o quello che hai scelto

# Setup una sola volta
def setup_gspread():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("gspread-creds.json", scopes=scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)

# Salva una riga nel foglio
def log_trade_to_google(symbol, entry_price, exit_price, pnl_pct, strategy, result_type,
                        usdt_entry=None, usdt_exit=None, holding_time_min=None, 
                        mfe_r=None, mae_r=None, r_multiple=None, market_condition=None):
    """
    Registra trade sul foglio Google.
    Colonne: Timestamp | Symbol | Entry | Exit | PnL % | Strategia | Tipo | USDT Enter | USDT Exit | 
             Delta USD | Holding Min | MFE R | MAE R | R Multiple | Market Condition
    """
    try:
        import base64

        SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"
        SHEET_NAME = "Short"

        encoded = os.getenv("GSPREAD_CREDS_B64")
        if not encoded:
            log("❌ Variabile GSPREAD_CREDS_B64 non trovata")
            return

        creds_path = os.path.join(os.getcwd(), "gspread-creds-runtime.json")
        with open(creds_path, "wb") as f:
            f.write(base64.b64decode(encoded))

        scope = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)

        if usdt_entry is None:
            usdt_entry = entry_price
        if usdt_exit is None:
            usdt_exit = exit_price

        # SHORT: Delta = ricevuto (entry) - pagato (exit)
        delta_usd = usdt_entry - usdt_exit

        sheet.append_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            round(entry_price, 6),
            round(exit_price, 6),
            f"{pnl_pct:.2f}%",
            strategy,
            result_type,
            f"{usdt_entry:.2f}",
            f"{usdt_exit:.2f}",
            f"{delta_usd:.2f}",
            f"{holding_time_min:.1f}" if holding_time_min else "",
            f"{mfe_r:.2f}" if mfe_r else "",
            f"{mae_r:.2f}" if mae_r else "",
            f"{r_multiple:.2f}" if r_multiple else "",
            market_condition or ""
        ])
    except Exception as e:
        log(f"❌ Errore log su Google Sheets: {e}")

# --- LOGICA 70/30 SU VALORE TOTALE PORTAFOGLIO (USDT + coin) ---
def get_portfolio_value():
    usdt_balance = get_usdt_balance()
    total = usdt_balance
    coin_values = {}
    symbols = set(ASSETS) | set(open_positions)  # includi sempre gli short aperti
    for symbol in symbols:
        if symbol == "USDT":
            continue
        qty = get_open_short_qty(symbol)
        if qty and qty > 0:
            price = get_last_price(symbol)
            if price:
                value = qty * price
                coin_values[symbol] = value
                total += value
    return total, usdt_balance, coin_values

low_balance_alerted = False  # Deve essere fuori dal ciclo per persistere tra i cicli

# >>> PATCH: avvio worker di breakeven lock (SHORT)
be_lock_thread_short = threading.Thread(target=breakeven_lock_worker_short, daemon=True)
be_lock_thread_short.start()
profit_floor_thread_short = threading.Thread(target=profit_floor_worker_short, daemon=True)
profit_floor_thread_short.start()

while True:
    # Aggiorna la lista asset dinamicamente ogni ciclo
    update_assets()
    _update_daily_anchor_and_regime()
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()

    # >>> NEW: daily drawdown cap (pausa nuovi ingressi per 2h se superato)
    if _daily_start_equity:
        dd_pct = (portfolio_value - _daily_start_equity) / max(1e-9, _daily_start_equity)
        if dd_pct < -DAILY_DD_CAP_PCT:
            tlog("dd_cap", f"🛑 DD giornaliero {-dd_pct*100:.2f}% > cap {DAILY_DD_CAP_PCT*100:.1f}%, stop nuovi SHORT per 2h", 600)
            _trading_paused_until = time.time() + 2*3600

    # sync_positions_from_wallet()  # evita di resettare position_data/trailing ad ogni ciclo
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()
    # SHORT: più conservativo sui volatili, più aggressivo su large cap
    volatile_budget = portfolio_value * 0.4  # Era 0.7
    stable_budget = portfolio_value * 0.6    # Era 0.3
    volatile_invested = sum(
        coin_values.get(s, 0) for s in open_positions if s in VOLATILE_ASSETS
    )
    stable_invested = sum(
        coin_values.get(s, 0) for s in open_positions if s in LESS_VOLATILE_ASSETS
    )
    # Log dettagliato bilanciamento
    tot_invested = volatile_invested + stable_invested
    perc_volatile = (volatile_invested / portfolio_value * 100) if portfolio_value > 0 else 0
    perc_stable = (stable_invested / portfolio_value * 100) if portfolio_value > 0 else 0
    if LOG_DEBUG_PORTFOLIO:
        tlog("portfolio", f"[PORTAFOGLIO] Totale: {portfolio_value:.2f} USDT | Volatili: {volatile_invested:.2f} ({perc_volatile:.1f}%) | Meno volatili: {stable_invested:.2f} ({perc_stable:.1f}%) | USDT: {usdt_balance:.2f}", 900)

    # --- Avviso saldo basso: invia solo una volta finché non torna sopra soglia ---
    # low_balance_alerted ora è globale rispetto al ciclo

    for symbol in ASSETS:
        if symbol in STABLECOIN_BLACKLIST:
            continue
        if not is_symbol_linear(symbol):
            tlog(f"skip_linear:{symbol}", f"[SKIP] {symbol} non disponibile su futures linear, salto.", 1800)
            continue

        signal, strategy, price = analyze_asset(symbol)
        # Skip se non ci sono segnali
        if signal is None or strategy is None or price is None:
            continue
        # Log analisi: verboso solo in debug
        if LOG_DEBUG_STRATEGY:
            log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ✅ ENTRATA SHORT
        if signal == "entry":
            # GATE: blocca solo le NUOVE APERTURE (non le uscite)
            if time.time() < _trading_paused_until:
                tlog(f"paused:{symbol}", f"[PAUSE] trading sospeso (DD cap), skip SHORT {symbol}", 600)
                continue
            if CURRENT_REGIME == "BULL":
                tlog(f"reg_gate:{symbol}", f"[REGIME-GATE] BULL → skip SHORT {symbol}", 600)
                continue

            # Cooldown
            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if elapsed < COOLDOWN_MINUTES * 60:
                    tlog(f"cooldown:{symbol}", f"⏳ Cooldown attivo per {symbol} ({elapsed:.0f}s), salto ingresso", 300)
                    continue

            if symbol in open_positions:
                tlog(f"inpos:{symbol}", f"⏩ Ignoro apertura short: già in posizione su {symbol}", 600)
                continue

            # Se c’è già una posizione LONG aperta (altro bot), non aprire lo SHORT sullo stesso simbolo
            if get_open_long_qty(symbol) > 0:
                tlog(f"opp_side:{symbol}", f"[SKIP] {symbol} ha LONG aperto, salto SHORT", 300)
                continue
    
            # --- LOGICA 70/30: verifica budget disponibile ---
            is_volatile = symbol in VOLATILE_ASSETS
            if is_volatile:
                group_budget = volatile_budget
                group_invested = volatile_invested
                group_label = "VOLATILE"
            else:
                group_budget = stable_budget
                group_invested = stable_invested
                group_label = "MENO VOLATILE"

            group_available = max(0.0, group_budget - group_invested)
            tlog(f"budget_detail:{symbol}", f"[BUDGET] {symbol} ({group_label}) - Budget: {group_budget:.2f} | Investito: {group_invested:.2f} | Disp: {group_available:.2f}", 300)

            # 📊 Valuta la forza del segnale in base alla strategia
            weights_no_tf = {
                # Nuovi nomi (confluenza)
                "EMA Bearish": 0.75,
                "MACD Bearish": 0.70,
                "RSI Bearish": 0.60,
                "ADX Trend": 0.85,
                # Vecchi nomi (compatibilità)
                "Breakdown BB": 1.00,
                "MACD bearish + ADX": 0.90,
                "Incrocio EMA 20/50": 0.75,
                "EMA20<EMA50": 0.70,
                "MACD bearish": 0.65,
                "Trend EMA+RSI": 0.60
            }
            parts = [p.strip().split(" (")[0] for p in (strategy or "").split(",") if p.strip()]
            if parts:
                base = max(weights_no_tf.get(p, 0.5) for p in parts)
                bonus = min(0.1 * (len(parts) - 1), 0.3)
                strength = min(1.0, base + bonus)
            else:
                strength = 0.5

            # --- Adatta la size ordine in base alla volatilità (ATR/Prezzo) ---
            df_hist = fetch_history(symbol)
            if df_hist is not None and "atr" in df_hist.columns and "Close" in df_hist.columns:
                last_hist = df_hist.iloc[-1]
                atr_val = last_hist["atr"]
                last_price = last_hist["Close"]
                atr_ratio = atr_val / last_price if last_price > 0 else 0
                # Se la volatilità è molto alta, riduci la size ordine
                if atr_ratio > 0.08:
                    strength *= 0.5
                    if LOG_DEBUG_STRATEGY:
                        log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo molto alto ({atr_ratio:.2%}), size dimezzata.")
                elif atr_ratio > 0.04:
                    strength *= 0.75
                    if LOG_DEBUG_STRATEGY:
                        log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo elevato ({atr_ratio:.2%}), size -25%.")

            # Notional massimo consentito dal margine disponibile con leva
            max_notional_by_margin = usdt_balance * DEFAULT_LEVERAGE * MARGIN_USE_PCT
            # Target di base: obiettivo per trade, limitato da budget e margine
            base_target = min(TARGET_NOTIONAL_PER_TRADE, group_available, max_notional_by_margin)
            # Adatta al "peso" del segnale
            order_amount = max(0.0, base_target * strength)
            # Cap opzionale più ampio (se vuoi): 1000 USDT
            order_amount = min(order_amount, group_available, max_notional_by_margin, 1000)
            tlog(f"strength:{symbol}", f"[FORZA] {symbol} - Strategia: {strategy}, Strength: {strength:.2f}, Notional: {order_amount:.2f} USDT", 300)

            # BLOCCO: non tentare short se order_amount < min_order_amt
            info_i = get_instrument_info(symbol)
            min_order_amt = float(info_i.get("min_order_amt", 5))
            min_qty = float(info_i.get("min_qty", 0.0))
            price_now_chk = get_last_price(symbol) or 0.0
            min_notional = max(min_order_amt, (min_qty or 0.0) * price_now_chk)
            if order_amount < min_notional:
                bump = min_notional * 1.01  # +1% cuscinetto
                max_by_margin = max_notional_by_margin
                if symbol in LARGE_CAPS and max_by_margin >= bump:
                    old = order_amount
                    order_amount = min(bump, max_by_margin, 1000.0)
                    log(f"[BUMP-NOTIONAL][{symbol}] alzato notional da {old:.2f} a {order_amount:.2f} per rispettare min_qty/min_notional")
                else:
                    tlog(f"min_notional:{symbol}", f"❌ Notional richiesto {order_amount:.2f} < minimo {min_notional:.2f} per {symbol} (min_qty={min_qty}, price={price_now_chk})", 300)
                    continue
            else:
                low_balance_alerted = False

            # Logga la quantità calcolata PRIMA dell'apertura short
            qty_str = calculate_quantity(symbol, order_amount)
            if LOG_DEBUG_STRATEGY:
                log(f"[DEBUG-ENTRY] Quantità calcolata per {symbol} con {order_amount:.2f} USDT: {qty_str}")
            if not qty_str:
                log(f"❌ Quantità non valida per short di {symbol}")
                continue

            if TEST_MODE:
                log(f"[TEST_MODE] SHORT inibiti per {symbol}")
                continue

            # APERTURA SHORT
            qty = market_short(symbol, order_amount)
            if not qty or qty == 0:
                log(f"❌ Nessuna quantità shortata per {symbol}. Non registro la posizione.")
                continue
            # >>> PIAZZA TAKE-PROFIT (SHORT) immediatamente
            try:
                price_now = get_last_price(symbol)
                df = fetch_history(symbol)
                if df is not None and "Close" in df.columns:
                    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                    atr_val = atr.iloc[-1]
                else:
                    atr_val = price_now * 0.02
                tp_price = price_now - (atr_val * TP_FACTOR)
                ok_tp, tp_oid = place_takeprofit_short(symbol, tp_price, qty)
                if ok_tp:
                    tlog(f"tp_init_short:{symbol}", f"[TP-PLACE-INIT-SHORT] {symbol} tp={tp_price:.6f} orderId={tp_oid}", 30)
                else:
                    tlog(f"tp_init_short_fail:{symbol}", f"[TP-PLACE-INIT-SHORT] {symbol} tp={tp_price:.6f} ok=False", 30)
            except Exception:
                tp_oid = None
            actual_cost = qty * price_now
            log(f"🟢 SHORT aperto per {symbol}. Investito effettivo: {actual_cost:.2f} USDT")

            # Calcolo ATR, SL, TP per SHORT
            atr_ratio = atr_val / price_now if price_now > 0 else 0
            tp_factor = min(TP_MAX, max(TP_MIN, TP_FACTOR + atr_ratio * 5))
            sl_factor = min(SL_MAX, max(SL_MIN, SL_FACTOR + atr_ratio * 3))
            
            tp = price_now - (atr_val * tp_factor)  # TP sotto l’entry (short)
            sl_atr = price_now + (atr_val * sl_factor)  # SL sopra l’entry
            # CAP di perdita: non permettere SL oltre MAX_LOSS_CAP_PCT
            sl_cap = price_now * (1.0 + MAX_LOSS_CAP_PCT)
            final_sl = min(sl_atr, sl_cap)  # SHORT: più vicino all'entry = min tra due prezzi sopra l'entry

            # >>> PIAZZA SUBITO LO STOP LOSS CONDITIONAL (reduceOnly) ALL'APERTURA (SHORT)
            sl_order_id = None
            try:
                qty_for_sl = qty
                ok_sl = place_conditional_sl_short(symbol, final_sl, qty_for_sl, trigger_by="MarkPrice")
                if ok_sl:
                    sl_order_id = "placed_mark"
                else:
                    ok_sl2 = place_conditional_sl_short(symbol, final_sl, qty_for_sl, trigger_by="LastPrice")
                    if ok_sl2:
                        sl_order_id = "placed_last"
                    else:
                        tlog(f"sl_init_fail:{symbol}", f"[SL-INIT-FAIL-SHORT] {symbol} sl={final_sl:.6f} qty={qty_for_sl} (Mark/Last failed)", 30)
                # Backup: imposta anche lo stopLoss della POSIZIONE (lato exchange)
                set_position_stoploss_short(symbol, final_sl)
            except Exception as e:
                tlog(f"sl_init_exc:{symbol}", f"[SL-INIT-EXC-SHORT] {symbol} exc: {e}", 300)

            try:
                trailing_dist = compute_trailing_distance(symbol, float(atr_val))  # <<< clamp dinamico
                ok_trailing = place_trailing_stop_short(symbol, trailing_dist)
                if ok_trailing:
                    tlog(f"trailing_init_ok:{symbol}", f"[TRAILING-INIT-SHORT] {symbol} trailing={trailing_dist:.6f}", 30)
                else:
                    tlog(f"trailing_init_fail:{symbol}", f"[TRAILING-INIT-SHORT] {symbol} trailing={trailing_dist:.6f} FAILED", 30)
            except Exception as e:
                tlog(f"trailing_init_exc:{symbol}", f"[TRAILING-INIT-EXC-SHORT] {symbol} exc: {e}", 300)

            log(f"[ENTRY-DETAIL] {symbol} | Entry: {price_now:.4f} | SL: {final_sl:.4f} | TP: {tp:.4f} | ATR: {atr_val:.4f}")
            
            position_data[symbol] = {
                "entry_price": price_now,
                "tp": tp,
                "tp_order_id": tp_oid if 'tp_oid' in locals() else None,
                "sl_order_id": sl_order_id,
                "sl": final_sl,
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_min": price_now
            }
            open_positions.add(symbol)
            notify_telegram(f"🟢📉 SHORT aperto per {symbol}\nPrezzo: {price_now:.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f} USDT\nSL: {final_sl:.4f}\nTP: {tp:.4f}")
            time.sleep(3)

        # 🔴 USCITA SHORT (EXIT) - INSERISCI QUI
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            
            # Verifica holding time minimo
            
            qty = get_open_short_qty(symbol)
            log(f"[EXIT-SIGNAL][{symbol}] qty effettiva: {qty} | entry_price: {entry_price} | current_price: {price}")
            
            info = get_instrument_info(symbol)
            min_qty = info.get("min_qty", 0.0)
            qty_step = info.get("qty_step", 0.0)
            
            if qty is None or qty < min_qty or qty < qty_step:
                log(f"[CLEANUP][EXIT] {symbol}: quantità troppo piccola per ricopertura ({qty} < min_qty {min_qty})")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue
            
            if qty <= 0:
                log(f"[EXIT-FAIL] Nessuna quantità short effettiva da ricoprire per {symbol}")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue
            
            # Esegui chiusura
            resp = market_cover(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                current_price = get_last_price(symbol)
                exit_value = current_price * qty
                pnl = ((entry_price - current_price) / entry_price) * 100  # PnL SHORT corretto
                
                log(f"[EXIT-OK] Ricopertura completata per {symbol} | PnL: {pnl:.2f}%")
                notify_telegram(f"✅ Exit Signal: ricopertura SHORT per {symbol} a {current_price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}%")
                record_exit(symbol, entry_price, current_price, "SHORT")
                log_trade_to_google(
                    symbol, 
                    entry_price, 
                    current_price, 
                    pnl, 
                    strategy, 
                    "Exit Signal",
                    usdt_entry=entry_cost,
                    usdt_exit=exit_value,
                    holding_time_min=(time.time() - entry.get("entry_time", 0)) / 60,
                    mfe_r=entry.get('mfe', 0),
                    mae_r=entry.get('mae', 0),
                    r_multiple=None,
                    market_condition="exit_signal"
                )
                
                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
                position_data.pop(symbol, None)
                if get_open_long_qty(symbol) == 0:
                    cancel_all_orders(symbol)
            else:
                log(f"[EXIT-FAIL] Ricopertura fallita per {symbol}")
                try:
                    log(f"[BYBIT ERROR] status={resp.status_code} resp={resp.json()}")
                except:
                    log(f"[BYBIT ERROR] status={resp.status_code} resp=non-json")

    # PATCH: rimuovi posizioni con saldo < 1 (polvere) anche nel ciclo principale
    for symbol in list(open_positions):
        saldo = get_open_short_qty(symbol)
        info = get_instrument_info(symbol)
        min_qty = info.get("min_qty", 0.0)
        # cleanup SOLO se lettura qty è valida e < min_qty
        if (saldo is not None) and (saldo < min_qty):
            tlog(f"ext_close:{symbol}", f"[CLEANUP][SHORT] {symbol} chiusa lato exchange (qty={saldo}). Cancello TP/SL.", 60)
            open_positions.discard(symbol)
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", get_last_price(symbol) or 0.0)
            exit_price = get_last_price(symbol) or 0.0
            record_exit(symbol, entry_price, exit_price, "SHORT")
            position_data.pop(symbol, None)
            if get_open_long_qty(symbol) == 0:
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
        # SHORT: trigger BE se prezzo ≤ entry*(1 - 1%)
        if price_now <= entry_price * (1.0 - BREAKEVEN_LOCK_PCT):
            be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)  # buffer negativo → sotto entry
            qty_live = get_open_short_qty(symbol)
            if qty_live and qty_live > 0:
                place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                set_position_stoploss_short(symbol, be_price)
                entry["be_locked"] = True
                entry["be_price"] = be_price
                tlog(f"be_lock_safety:{symbol}", f"[BE-LOCK-SAFETY][SHORT] SL→BE {be_price:.6f}", 60)

    # Sicurezza: attesa tra i cicli principali
    # time.sleep(INTERVAL_MINUTES * 60)
    time.sleep(120)  # analizza ogni 2 minuti (più ingressi)