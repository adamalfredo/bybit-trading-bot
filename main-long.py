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
ENTRY_TF_MINUTES = 60  # era 30
ATR_WINDOW = 14
TRAILING_MIN = 0.015  # trailing più largo
TRAILING_MAX = 0.05   # trailing più largo
TRAILING_ACTIVATION_THRESHOLD = 0.015  # trailing parte dopo +1.5%
TRAILING_SL_BUFFER = 0.015             # trailing SL più largo
TRAILING_DISTANCE = 0.04               # trailing SL più largo
TP_FACTOR = 2.5                        # TP più ambizioso
SL_FACTOR = 1.2                        # SL più stretto
TP_MIN = 2.0
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.0
# Nuovi parametri per protezione guadagni (stop_floor)
MIN_PROTECT_PCT = 1.0       # soglia minima protezione (1%)  // <<< PATCH commento corretto
TRAILING_PROTECT_TIERS = [  # (p_max_pct_threshold, margin_pct)
    (2, 2.0),   # se p_max_pct <5 e >=2 -> margin 2%
    (5, 3.0),   # se p_max_pct <10 -> margin 3%
    (10, 4.0),  # se p_max_pct <20 -> margin 4%
    (20, 5.0)   # >=20 -> margin 5%
]
TRIGGER_BY = "LastPrice"    # "LastPrice" o "MarkPrice" per trigger degli stop exchange
TRAILING_POLL_SEC = 5       # frequenza worker trailing in secondi (default 5s)

RATCHET_TIERS_ROI = [
    (30, 15),   # superato 30% ROI → garantisci 15%
    (45, 30),
    (60, 45),
    (80, 60),
    (100, 75)
]
FLOOR_BUFFER_PCT = 0.0015          # 0.15% di prezzo per sicurezza esecuzione
FLOOR_UPDATE_COOLDOWN_SEC = 8      # evita update troppo frequenti
FLOOR_TRIGGER_BY = "MarkPrice"     # usa Mark per coerenza con SL

# >>> PATCH: parametri breakeven lock (LONG)
BREAKEVEN_LOCK_PCT = 0.01   # attiva BE al +1% di prezzo (~+10% PnL con leva 10x)
BREAKEVEN_BUFFER   = 0.0015  # stop a BE + 0.15% per evitare micro-slippage
MAX_LOSS_CAP_PCT = 0.015  # CAP perdita sul prezzo: 1.5% sotto l'entry (SL non oltre questo)

# >>> NEW: regime + drawdown giornaliero (LONG)
DAILY_DD_CAP_PCT = 0.04
REGIME_REFRESH_SEC = 180
CURRENT_REGIME = "MIXED"   # BULL / BEAR / MIXED
_last_regime_ts = 0
_daily_start_equity = None
_trading_paused_until = 0
# BEGIN PATCH: throttle DD (no pausa forzata di default)
ENABLE_DD_PAUSE = os.getenv("ENABLE_DD_PAUSE", "0") == "1"   # se "1" mantiene la pausa forzata
DD_PAUSE_MINUTES = int(os.getenv("DD_PAUSE_MINUTES", "120"))
RISK_THROTTLE_LEVEL = 0  # 0=off, 1=DD > cap, 2=DD > 2*cap
ENABLE_TP1 = False       # abilita TP parziale a 1R
TP1_R_MULT = 1.0        # target TP1 a 1R
TP1_CLOSE_PCT = 0.5     # chiudi il 50% a TP1
INITIAL_STOP_LOSS_PCT = 0.03          # era 0.02, SL iniziale più largo
COOLDOWN_MINUTES = 60
cooldown = {}
ORDER_USDT = 50.0
ENABLE_BREAKOUT_FILTER = False  # rende opzionale il filtro breakout 6h
# --- MTF entry: segnali su 15m, trend su 4h/1h ---
USE_MTF_ENTRY = True
# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
LIQUIDITY_MIN_VOLUME = 1_000_000  # Soglia minima volume 24h USDT (consigliato)
# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}
recent_losses = {}          # conteggio loss consecutivi per simbolo
last_entry_side = {}        # "LONG" o "SHORT"
MAX_CONSEC_LOSSES = 2       # dopo 2 loss consecutivi blocca nuovi ingressi
FORCED_WAIT_MIN = 90        # attesa minima (minuti) se il contesto resta sfavorevole
# ---- Logging flags (accensione selettiva via env/Variables di Railway) ----
LOG_DEBUG_ASSETS     = os.getenv("LOG_DEBUG_ASSETS", "0") == "1"
LOG_DEBUG_DECIMALS   = os.getenv("LOG_DEBUG_DECIMALS", "0") == "1"
LOG_DEBUG_SYNC       = os.getenv("LOG_DEBUG_SYNC", "0") == "1"
LOG_DEBUG_STRATEGY   = os.getenv("LOG_DEBUG_STRATEGY", "0") == "1"
LOG_DEBUG_TRAILING   = os.getenv("LOG_DEBUG_TRAILING", "0") == "1"
LOG_DEBUG_PORTFOLIO  = os.getenv("LOG_DEBUG_PORTFOLIO", "0") == "1"
# --- Loosening via env (ingressi più frequenti) ---
MIN_CONFLUENCE = 1
TREND_MODE = "LOOSE_4H"
ENTRY_TF_VOLATILE = 30
ENTRY_TF_STABLE = 30
ENTRY_ADX_VOLATILE = 22        # fisso
ENTRY_ADX_STABLE = 20          # fisso
ADX_RELAX_EVENT = 3.0
RSI_LONG_THRESHOLD = 52.0
COOLDOWN_MINUTES = 60          # fisso (non usare os.getenv)
MAX_CONSEC_LOSSES = 2          # fisso
FORCED_WAIT_MIN = 90           # fisso
LIQUIDITY_MIN_VOLUME = 1_000_000
LINEAR_MIN_TURNOVER = 5_000_000
# Large-cap con minQty elevata: abilita auto-bump del notional al minimo
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
    - BULL: BTC/ETH in uptrend su 4h e breadth rossa < 0.4
    - BEAR: BTC/ETH NON in uptrend e breadth rossa > 0.6
    - MIXED: altrimenti
    """
    try:
        btc_up = is_trending_up("BTCUSDT", "240")
        eth_up = is_trending_up("ETHUSDT", "240")
        breadth_red = _get_market_breadth()
        if btc_up and eth_up and breadth_red < 0.4:
            return "BULL"
        if (not btc_up) and (not eth_up) and breadth_red > 0.6:
            return "BEAR"
        return "MIXED"
    except:
        return "MIXED"

def _equity_now():
    total, usdt_balance, coin_values = get_portfolio_value()
    return total

def _update_daily_anchor_and_regime():
    global _daily_start_equity, CURRENT_REGIME, _last_regime_ts
    if _daily_start_equity is None or time.strftime("%Y-%m-%d") != time.strftime("%Y-%m-%d", time.localtime(_last_regime_ts or time.time())):
        _daily_start_equity = _equity_now()
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
        # Ranking spot
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

        # Liquidità futures linear
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

        # Intersezione + esclusioni
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
        endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
        params = {"category": "linear", "symbol": symbol}
        from urllib.parse import urlencode
        query_string = urlencode(sorted(params.items()))
        ts = str(int(time.time() * 1000))
        recv_window = "5000"
        sign_payload = f"{ts}{KEY}{recv_window}{query_string}"
        sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv_window}
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_SYNC:
                tlog(f"qty_err_long:{symbol}", f"[BYBIT-RAW][ERRORE] get_open_long_qty {symbol}: {json.dumps(data)}", 300)
            return None  # <<< PRIMA log+0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Buy":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        if LOG_DEBUG_SYNC:
            tlog(f"qty_exc_long:{symbol}", f"❌ Errore get_open_long_qty per {symbol}: {e}", 300)
        return None  # <<< PRIMA 0.0

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
        headers = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": recv_window}
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            return None  # <<< PRIMA 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Sell":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception:
        return None  # <<< PRIMA 0.0

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

def market_long(symbol: str, usdt_amount: float):
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
        log(f"[QTY-GUARD][{symbol}] qty riallineata a min_qty={float(qty_aligned)}")

    # Rispetta min notional (min_order_amt)
    needed = Decimal(str(min_order_amt)) / Decimal(str(price))
    # ceil al passo
    multiples = (needed / step_dec).quantize(Decimal('1'), rounding=ROUND_UP)
    min_notional_qty = multiples * step_dec
    if qty_aligned * Decimal(str(price)) < Decimal(str(min_order_amt)):
        qty_aligned = max(qty_aligned, min_notional_qty)
        log(f"[NOTIONAL-GUARD][{symbol}] qty alzata per min_order_amt → {float(qty_aligned)}")

    # NEW: limita il notional all'effettivo margine disponibile adesso
    avail_now = get_usdt_balance() or 0.0
    max_notional_now = avail_now * DEFAULT_LEVERAGE * MARGIN_USE_PCT
    desired_notional = float(qty_aligned) * float(price)
    if desired_notional > max_notional_now:
        # scala qty al tetto consentito dal margine corrente
        qty_aligned = (Decimal(str(max_notional_now)) / Decimal(str(price))) // step_dec * step_dec
        if qty_aligned <= 0:
            return None

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
            "positionIdx": 1
        }
        ts = str(int(time.time() * 1000))
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}5000{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json"}

        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        if LOG_DEBUG_STRATEGY:
            log(f"[LONG][{symbol}] attempt {attempt}/{max_retries} BODY={body_json}")

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
            log(f"[RETRY][{symbol}] 170137 → refresh instrument e rifloor")
            try: _instrument_cache.pop(symbol, None)
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
            log(f"[RETRY][{symbol}] 170140 → qty bump a {float(qty_aligned)} (min_notional)")
            continue
        if ret_code == 170131:
            log(f"[RETRY][{symbol}] 170131 → riduco qty del 10%")
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
        "positionIdx": 1
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
            "positionIdx": 1
        }
        ts = str(int(time.time() * 1000))
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}5000{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {"X-BAPI-API-KEY": KEY, "X-BAPI-SIGN": sign, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": "5000", "X-BAPI-SIGN-TYPE": "2", "Content-Type": "application/json"}

        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        if LOG_DEBUG_STRATEGY:
            log(f"[CLOSE-LONG][{symbol}] attempt {attempt}/{max_retries} BODY={body_json}")

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
            log(f"[RETRY-CLOSE][{symbol}] 170137 → refresh instrument e rifloor")
            try: _instrument_cache.pop(symbol, None)
            except Exception: pass
            info = get_instrument_info(symbol)
            qty_step = float(info.get("qty_step", qty_step))
            step_dec = Decimal(str(qty_step))
            qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec
            continue

        log(f"[ERROR-CLOSE][{symbol}] Errore non gestito: {ret_code}")
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

# >>> PATCH: funzioni per impostare lo stopLoss sulla posizione (LONG) e worker BE
def set_position_stoploss_long(symbol: str, sl_price: float) -> bool:
    body = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": f"{sl_price:.8f}",
        "slTriggerBy": "MarkPrice",   # <<< FIX: allinea al conditional
        "positionIdx": 1
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
            tlog(f"sl_pos_err:{symbol}", f"[POS-SL][LONG] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
        return ok
    except Exception as e:
        tlog(f"sl_pos_exc:{symbol}", f"[POS-SL][LONG] exc: {e}", 300)
        return False

def breakeven_lock_worker_long():
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
            # Trigger BE quando il prezzo è salito almeno dell’1% (≈ +10% PnL a 10x)
            if price_now >= entry_price * (1.0 + BREAKEVEN_LOCK_PCT):
                # Long: copertura a BE con piccolo buffer di profitto
                be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)

                # 2) piazza Stop-Market reduceOnly a BE (sul book)
                qty_live = get_open_long_qty(symbol)
                if qty_live and qty_live > 0:
                    place_conditional_sl_long(symbol, be_price, qty_live, trigger_by="MarkPrice")

                # 3) aggiorna anche lo stopLoss della POSIZIONE (trading-stop)
                set_position_stoploss_long(symbol, be_price)

                entry["be_locked"] = True
                entry["be_price"] = be_price
                tlog(f"be_lock:{symbol}", f"[BE-LOCK][LONG] {symbol} SL→BE {be_price:.6f}", 60)
        time.sleep(5)

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
                position_data[symbol] = entry
                continue

            # Non aggiornare se il floor non cresce
            if prev_floor_roi is not None and target_floor_roi <= prev_floor_roi:
                position_data[symbol] = entry
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
                position_data[symbol] = entry
                tlog(f"floor_up_long_skip:{symbol}",
                     f"[FLOOR-UP-SKIP][LONG] {symbol} MFE={mfe_roi:.1f}% targetROI={target_floor_roi:.1f}% floorPrice={floor_price:.6f} ≥ current={price_now:.6f}", 120)
                continue

            # Aggiorna trading-stop
            set_ok = set_position_stoploss_long(symbol, floor_price)
            # Stop-Market backup
            place_conditional_sl_long(symbol, floor_price, qty_live, trigger_by=FLOOR_TRIGGER_BY)

            entry["floor_roi"] = target_floor_roi
            entry["floor_price"] = floor_price
            entry["floor_updated_ts"] = time.time()

            tlog(f"floor_up_long:{symbol}",
                 f"[FLOOR-UP][LONG] {symbol} MFE={mfe_roi:.1f}% → FloorROI={target_floor_roi:.1f}% → SL={floor_price:.6f} set={set_ok}", 30)
            position_data[symbol] = entry

        time.sleep(3)

def place_conditional_sl_long(symbol: str, stop_price: float, qty: float, trigger_by: str = TRIGGER_BY) -> bool:
    """
    Piazza/aggiorna uno stop-market reduceOnly per proteggere la posizione LONG.
    ReduceOnly=True, orderType=Market, triggerPrice = stop_price, triggerBy = trigger_by.
    Restituisce True se la chiamata API ha retCode==0.
    """
    try:
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.01)
        price_step = info.get("price_step", 0.01)  # <-- aggiungi
        qty_str = _format_qty_with_step(float(qty), qty_step)
        stop_str = format_price_bybit(stop_price, price_step)  # <-- aggiungi
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,
            "positionIdx": 1,
            "triggerBy": trigger_by,
            "triggerPrice": stop_str,  # <-- sostituisci
            "triggerDirection": 2,
            "closeOnTrigger": True
        }
        # DEBUG: log body json (temporaneo)
        if LOG_DEBUG_STRATEGY:
            log(f"[SL-DEBUG-BODY][LONG] {json.dumps(body)}")
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
        # Log completo (response + body) per debug
        tlog(f"sl_create_err:{symbol}", f"[SL-PLACE][LONG] retCode={data.get('retCode')} msg={data.get('retMsg')} resp={json.dumps(data)} body={body}", 300)
        return False
    except Exception as e:
        tlog(f"sl_create_exc:{symbol}", f"[SL-PLACE][LONG] eccezione: {e}", 300)
        return False

def place_takeprofit_long(symbol: str, tp_price: float, qty: float) -> (bool, str):
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.01)
    price_step = info.get("price_step", 0.01)
    qty_str = _format_qty_with_step(float(qty), qty_step)
    tp_str = format_price_bybit(tp_price, price_step)   # <<< allinea al tick
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Limit",
        "qty": qty_str,
        "price": tp_str,                                  # <<< usa prezzo allineato
        "timeInForce": "PostOnly",
        "reduceOnly": True,
        "positionIdx": 1
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
        tlog(f"tp_place:{symbol}", f"[TP-PLACE] {symbol} tp={tp_price:.6f} qty={qty_str} orderId={oid}", 30)
        return True, oid
    tlog(f"tp_create_err:{symbol}", f"[TP-PLACE][LONG] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
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
        resp = requests.get(endpoint, params=params, timeout=10)
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
    else:
        recent_losses[symbol] = 0
    last_entry_side[symbol] = side
    
def analyze_asset(symbol: str):
    # Trend filter configurabile
    up_4h = is_trending_up(symbol, "240")
    up_1h = is_trending_up_1h(symbol, "60")

    if TREND_MODE == "STRICT":
        trend_ok = up_4h and up_1h
    elif TREND_MODE == "LOOSE_4H":
        trend_ok = up_4h
    else:  # ANY
        trend_ok = up_4h or up_1h

    # Filtro trend:
    # - BULL: permissivo (non richiede trend_ok)
    # - BEAR: obbligatorio (long solo se asset in uptrend)
    # - MIXED: obbligatorio
    if CURRENT_REGIME in ("BEAR", "MIXED") and not trend_ok:
        if LOG_DEBUG_STRATEGY:
            tlog(f"trend_long:{symbol}", f"[TREND-FILTER][{symbol}] Regime={CURRENT_REGIME}, trend non idoneo (mode={TREND_MODE}), skip.", 600)
        return None, None, None

    # Non blocca: solo log informativo
    if ENABLE_BREAKOUT_FILTER and not is_breaking_weekly_low(symbol):
        if LOG_DEBUG_STRATEGY:
            tlog(f"breakout_long:{symbol}", f"[BREAKOUT-FILTER][{symbol}] Non in breakout 6h (info).", 1800)

    try:
        is_volatile = symbol in VOLATILE_ASSETS
        tf_minutes = ENTRY_TF_VOLATILE if (USE_MTF_ENTRY and is_volatile) else ENTRY_TF_STABLE

        df = fetch_history(symbol, interval=tf_minutes)
        if df is None or len(df) < 3:
            log(f"[ANALYZE] Dati storici insufficienti per {symbol}")
            return None, None, None

        close = find_close_column(df)
        if close is None:
            log(f"[ANALYZE] Colonna close non trovata per {symbol}")
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
        if len(df) < 3:
            return None, None, None

        adx_threshold = ENTRY_ADX_VOLATILE if is_volatile else ENTRY_ADX_STABLE

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])
        tf_tag = f"({tf_minutes}m)"

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
        
        # Confluenza richiesta per regime
        if CURRENT_REGIME in ("BEAR", "MIXED"):
            required_confluence = MIN_CONFLUENCE + 1
        else:  # BULL
            required_confluence = MIN_CONFLUENCE

        # ADX richiesto + bonus per regime
        adx_needed = max(0.0, adx_threshold - (ADX_RELAX_EVENT if event_triggered else 0.0))
        if CURRENT_REGIME == "MIXED":
            adx_needed += 1.5
        elif CURRENT_REGIME == "BEAR":
            adx_needed += 2.0

        # >>> PATCH: throttle DD → più conferme, ADX più alto, e richiedi evento se in DD
        required_confluence += RISK_THROTTLE_LEVEL
        adx_needed += 1.5 * RISK_THROTTLE_LEVEL
        if RISK_THROTTLE_LEVEL >= 1 and not event_triggered:
            return None, None, None

        if LOG_DEBUG_STRATEGY:
            tlog(
                f"entry_chk_long:{symbol}",
                f"[ENTRY-CHECK][LONG] conf={conf_count}/{required_confluence} | ADX={last['adx']:.1f}>{adx_needed:.1f} | event={event_triggered} | regime={CURRENT_REGIME} | tf={tf_tag}",
                300
            )

        # Guardrail su loss recenti
        if recent_losses.get(symbol, 0) >= MAX_CONSEC_LOSSES:
            wait_min = (time.time() - last_exit_time.get(symbol, 0)) / 60
            if price < last["ema50"] and wait_min < FORCED_WAIT_MIN:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"loss_guard:{symbol}", f"[LOSS-GUARD] Blocco LONG {symbol} (loss={recent_losses.get(symbol)}) sotto EMA50, wait {wait_min:.1f}m", 300)
                return None, None, None

        # Segnale ingresso
        if (((conf_count >= required_confluence) or event_triggered) and float(last["adx"]) > adx_needed):
            entry_strategies = []
            if ema_state: entry_strategies.append(f"EMA Bullish {tf_tag}")
            if macd_state: entry_strategies.append(f"MACD Bullish {tf_tag}")
            if rsi_state: entry_strategies.append(f"RSI Bullish {tf_tag}")
            entry_strategies.append("ADX Trend")
            if LOG_DEBUG_STRATEGY:
                log(f"[ENTRY-LONG][{symbol}] EVENTO/CONFLUENZA → {entry_strategies}")
            return "entry", ", ".join(entry_strategies), price

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
            return (current_price > entry_price and r > 0.5) or holding_min > 60

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
            # Calcola ATR e SL/TP corretti per LONG
            df = fetch_history(symbol)
            if df is not None and "Close" in df.columns:
                try:
                    atr_series = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                    atr_val = float(atr_series.iloc[-1])
                except Exception:
                    atr_val = price * 0.02
            else:
                atr_val = price * 0.02

            tp = price + (atr_val * TP_FACTOR)
            sl_atr = entry_price - (atr_val * SL_FACTOR)      # usa entry_price come riferimento
            sl_cap = entry_price * (1.0 - MAX_LOSS_CAP_PCT)   # cap 3% sotto entry
            final_sl = max(sl_atr, sl_cap)
            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": final_sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price
            }
            trovate += 1
            log(f"[SYNC] Posizione LONG trovata: {symbol} qty={qty} entry={entry_price:.4f} SL={final_sl:.4f} TP={tp:.4f}")
            # Piazza subito stop di posizione + conditional (backup) col CAP
            set_position_stoploss_long(symbol, final_sl)
            place_conditional_sl_long(symbol, final_sl, qty, trigger_by="MarkPrice")
            # Marca come già in posizione per evitare nuovi entry
            open_positions.add(symbol)

            # >>> PATCH: BE-LOCK immediato se già oltre soglia al riavvio
            try:
                if price >= entry_price * (1.0 + BREAKEVEN_LOCK_PCT) and not position_data[symbol].get("be_locked"):
                    be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)
                    qty_live = get_open_long_qty(symbol)
                    if qty_live and qty_live > 0:
                        place_conditional_sl_long(symbol, be_price, qty_live, trigger_by="MarkPrice")
                        set_position_stoploss_long(symbol, be_price)
                        position_data[symbol]["be_locked"] = True
                        position_data[symbol]["be_price"] = be_price
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

def calculate_stop_loss(entry_price, current_price, p_max, trailing_active):
    # LONG: SL iniziale sotto l’entry; in trailing segue p_max
    if not trailing_active:
        return entry_price * (1 - INITIAL_STOP_LOSS_PCT)
    else:
        return p_max * (1 - TRAILING_DISTANCE)

import threading
import gspread
from google.oauth2.service_account import Credentials

# Config
SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"  # copia da URL: https://docs.google.com/spreadsheets/d/<QUESTO>/edit
SHEET_NAME = "Long"  # o quello che hai scelto

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
        SHEET_NAME = "Long"

        encoded = os.getenv("GSPREAD_CREDS_B64")
        if not encoded:
            log("❌ Variabile GSPREAD_CREDS_B64 non trovata")
            return

        # Path portabile anche su Windows
        creds_path = os.path.join(os.getcwd(), "gspread-creds-runtime.json")
        with open(creds_path, "wb") as f:
            f.write(base64.b64decode(encoded))

        scope = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)

        # Se non forniti li calcoliamo come fallback
        if usdt_entry is None:
            usdt_entry = entry_price
        if usdt_exit is None:
            usdt_exit = exit_price
        delta_usd = usdt_exit - usdt_entry

        # Append row (15 colonne ora)
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
    symbols = set(ASSETS) | set(open_positions)
    for symbol in symbols:
        if symbol == "USDT":
            continue
        qty = get_open_long_qty(symbol)
        if qty and qty > 0:
            price = get_last_price(symbol)
            if price:
                value = qty * price
                coin_values[symbol] = value
                total += value
    return total, usdt_balance, coin_values

low_balance_alerted = False  # Deve essere fuori dal ciclo per persistere tra i cicli
# >>> PATCH: avvio worker di breakeven lock (LONG)
be_lock_thread_long = threading.Thread(target=breakeven_lock_worker_long, daemon=True)
be_lock_thread_long.start()
profit_floor_thread_long = threading.Thread(target=profit_floor_worker_long, daemon=True)
profit_floor_thread_long.start()

while True:
    update_assets()

    _update_daily_anchor_and_regime()
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
    if LOG_DEBUG_PORTFOLIO:
        tlog("portfolio", f"[PORTAFOGLIO] Totale: {portfolio_value:.2f} | Volatili: {volatile_invested:.2f} | Meno volatili: {stable_invested:.2f} | USDT: {usdt_balance:.2f}", 120)

    for symbol in ASSETS:
        if symbol in STABLECOIN_BLACKLIST:
            continue
        if not is_symbol_linear(symbol):
            continue

        signal, strategy, price = analyze_asset(symbol)
        if signal is not None and strategy is not None and price is not None:
            if LOG_DEBUG_STRATEGY:
                log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")
        else:
            # nessun segnale → skip
            continue

        # ✅ ENTRATA LONG
        if signal == "entry":
            # >>> GATE: blocca solo le NUOVE APERTURE (non gli exit)
            if ENABLE_DD_PAUSE and time.time() < _trading_paused_until:
                tlog(f"paused:{symbol}", f"[PAUSE] trading sospeso (DD cap), skip LONG {symbol}", 600)
                continue
            # if CURRENT_REGIME == "BEAR":
            #     tlog(f"reg_gate:{symbol}", f"[REGIME-GATE] BEAR → skip LONG {symbol}", 600)
            #     continue
            #     Nessun blocco regime: in BEAR i filtri sono irrigiditi in analyze_asset.

            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if elapsed < COOLDOWN_MINUTES * 60:
                    if LOG_DEBUG_STRATEGY:
                        tlog(f"cooldown:{symbol}", f"⏳ Cooldown attivo per {symbol}, salto ingresso", 300)
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

            max_notional_by_margin = usdt_balance * DEFAULT_LEVERAGE * MARGIN_USE_PCT
            base_target = min(TARGET_NOTIONAL_PER_TRADE, group_available, max_notional_by_margin)
            order_amount = min(max(0.0, base_target * strength), group_available, max_notional_by_margin, 1000.0)
            info_i = get_instrument_info(symbol)
            min_order_amt = float(info_i.get("min_order_amt", 5))
            min_qty = float(info_i.get("min_qty", 0.0))
            price_now_chk = get_last_price(symbol) or 0.0
            min_notional = max(min_order_amt, (min_qty or 0.0) * price_now_chk)
            # PATCH: se order_amount < min_notional, prova ad alzarlo al minimo richiesto
            if order_amount < min_notional:
                bump = min_notional * 1.01  # +1% cuscinetto
                max_by_margin = max_notional_by_margin
                if symbol in LARGE_CAPS and max_by_margin >= bump:
                    # Consenti override del budget di gruppo per rispettare minQty/minNotional
                    old = order_amount
                    order_amount = min(bump, max_by_margin, 1000.0)
                    log(f"[BUMP-NOTIONAL][{symbol}] alzato notional da {old:.2f} a {order_amount:.2f} per rispettare min_qty/min_notional")
                else:
                    tlog(f"min_notional:{symbol}", f"❌ Notional richiesto {order_amount:.2f} < minimo {min_notional:.2f} per {symbol} (min_qty={min_qty}, price={price_now_chk})", 300)
                    continue

            if TEST_MODE:
                log(f"[TEST_MODE] LONG inibiti per {symbol}")
                continue

            qty = market_long(symbol, order_amount)
            price_now = get_last_price(symbol)
            if not price_now:
                log(f"❌ Prezzo non disponibile post-ordine per {symbol}")
                continue

            df = fetch_history(symbol)
            if df is not None and "Close" in df.columns:
                atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                atr_val = atr.iloc[-1]
            else:
                atr_val = price_now * 0.02

            if not qty or qty == 0:
                if LOG_DEBUG_STRATEGY:
                    log(f"❌ LONG non aperto per {symbol}")
                continue

            tp_price = price_now + (atr_val * TP_FACTOR)
            ok_tp, tp_oid = place_takeprofit_long(symbol, tp_price, qty)
            if ok_tp:
                tlog(f"tp_init_ok:{symbol}", f"[TP-PLACE-INIT] {symbol} tp={tp_price:.6f} orderId={tp_oid}", 30)
            else:
                tlog(f"tp_init_fail:{symbol}", f"[TP-PLACE-INIT] {symbol} tp={tp_price:.6f} ok=False", 30)

            actual_cost = qty * price_now
            
            atr_ratio = atr_val / price_now if price_now > 0 else 0
            tp_factor = min(TP_MAX, max(TP_MIN, TP_FACTOR + atr_ratio * 5))
            sl_factor = min(SL_MAX, max(SL_MIN, SL_FACTOR + atr_ratio * 3))
            
            tp = price_now + (atr_val * tp_factor)
            sl_atr = price_now - (atr_val * sl_factor)
            # CAP di perdita: non permettere SL oltre MAX_LOSS_CAP_PCT
            sl_cap = price_now * (1.0 - MAX_LOSS_CAP_PCT)
            final_sl = max(sl_atr, sl_cap)  # LONG: più vicino all'entry = max tra due prezzi sotto l'entry
            
            # >>> PIAZZA SUBITO LO STOP LOSS CONDITIONAL (reduceOnly) ALL'APERTURA
            sl_order_id = None
            try:
                qty_for_sl = qty
                # prima prova con MarkPrice (più robusto), fallback su LastPrice se retCode==10001
                ok_sl = place_conditional_sl_long(symbol, final_sl, qty_for_sl, trigger_by="MarkPrice")
                if ok_sl:
                    sl_order_id = "placed_mark"
                else:
                    # retry con LastPrice
                    ok_sl2 = place_conditional_sl_long(symbol, final_sl, qty_for_sl, trigger_by="LastPrice")
                    if ok_sl2:
                        sl_order_id = "placed_last"
                    else:
                        tlog(f"sl_init_fail:{symbol}", f"[SL-INIT-FAIL] {symbol} sl={final_sl:.6f} qty={qty_for_sl} (Mark/Last failed)", 30)
                # Backup: imposta anche lo stopLoss della POSIZIONE (lato exchange)
                set_position_stoploss_long(symbol, final_sl)
            except Exception as e:
                tlog(f"sl_init_exc:{symbol}", f"[SL-INIT-EXC] {symbol} exc: {e}", 300)

            try:
                trailing_dist = compute_trailing_distance(symbol, float(atr_val))  # <<< clamp dinamico
                ok_trailing = place_trailing_stop_long(symbol, trailing_dist)
                if ok_trailing:
                    tlog(f"trailing_init_ok:{symbol}", f"[TRAILING-INIT-LONG] {symbol} trailing={trailing_dist:.6f}", 30)
                else:
                    tlog(f"trailing_init_fail:{symbol}", f"[TRAILING-INIT-LONG] {symbol} trailing={trailing_dist:.6f} FAILED", 30)
            except Exception as e:
                tlog(f"trailing_init_exc:{symbol}", f"[TRAILING-INIT-EXC-LONG] {symbol} exc: {e}", 300)

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
                "p_max": price_now
            }
            open_positions.add(symbol)
            notify_telegram(f"🟢📈 LONG aperto {symbol}\nPrezzo: {price_now:.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f}\nSL: {final_sl:.4f}\nTP: {tp:.4f}")
            time.sleep(3)

        # EXIT LONG (segnale di uscita strategico)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            
            qty = get_open_long_qty(symbol)
            log(f"[EXIT-SIGNAL LONG][{symbol}] qty={qty} | entry={entry_price} | now={price}")
            if not qty or qty <= 0:
                open_positions.discard(symbol); position_data.pop(symbol, None); continue

            resp = market_close_long(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                current_price = get_last_price(symbol)
                exit_value = current_price * qty
                pnl = ((current_price - entry_price) / entry_price) * 100.0
                notify_telegram(f"✅ Exit LONG {symbol} a {current_price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}%")
                record_exit(symbol, entry_price, current_price, "LONG")
                log_trade_to_google(symbol, entry_price, current_price, pnl, strategy, "Exit Signal",
                                    usdt_entry=entry_cost, usdt_exit=exit_value,
                                    holding_time_min=(time.time() - entry.get("entry_time", 0)) / 60,
                                    mfe_r=entry.get('mfe', 0), mae_r=entry.get('mae', 0),
                                    r_multiple=None, market_condition="exit_signal_long")
                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
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
            open_positions.discard(symbol)
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", get_last_price(symbol) or 0.0)
            exit_price = get_last_price(symbol) or 0.0
            record_exit(symbol, entry_price, exit_price, "LONG")
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

    time.sleep(120)