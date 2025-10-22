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
DEFAULT_LEVERAGE = int(os.getenv("BYBIT_LEVERAGE", "15"))          # leva usata sul conto (Cross/Isolated)
MARGIN_USE_PCT = float(os.getenv("MARGIN_USE_PCT", "0.35"))         # quota saldo USDT da impegnare come margine max (50%)
TARGET_NOTIONAL_PER_TRADE = float(os.getenv("TARGET_NOTIONAL_PER_TRADE", "200"))  # obiettivo notional per trade (USDT)

INTERVAL_MINUTES = 60  # era 15
ATR_WINDOW = 14
TP_FACTOR = 2.0
SL_FACTOR = 1.5
# Soglie dinamiche consigliate
TP_MIN = 1.5
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.5
TRAILING_MIN = 0.015  # era 0.005, trailing più largo
TRAILING_MAX = 0.05   # era 0.03, trailing più largo
TRAILING_ACTIVATION_THRESHOLD = 0.02  # trailing parte dopo -2%
TRAILING_SL_BUFFER = 0.015            # era 0.007, trailing SL più largo
TRAILING_DISTANCE = 0.04              # era 0.02, trailing SL più largo
ENABLE_TP1 = False       # abilita TP parziale a 1R
TP1_R_MULT = 1.0        # target TP1 a 1R
TP1_CLOSE_PCT = 0.5     # chiudi il 50% a TP1
INITIAL_STOP_LOSS_PCT = 0.03          # era 0.02, SL iniziale più largo
COOLDOWN_MINUTES = 60
cooldown = {}
MAX_LOSS_PCT = -2.5  # perdita massima accettata su SHORT in %, più stretto
ORDER_USDT = 50.0
ENABLE_BREAKOUT_FILTER = False  # rende opzionale il filtro breakout 6h
# --- MTF entry: segnali su 15m, trend su 4h/1h ---
USE_MTF_ENTRY = True
ENTRY_TF_MINUTES = 30
ENTRY_ADX_VOLATILE = 12   # soglia ADX più bassa su 15m per non arrivare tardi
ENTRY_ADX_STABLE = 10
# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
LIQUIDITY_MIN_VOLUME = 1_000_000  # Soglia minima volume 24h USDT (consigliato)
# ---- Logging flags (accensione selettiva via env/Variables di Railway) ----
LOG_DEBUG_ASSETS     = os.getenv("LOG_DEBUG_ASSETS", "0") == "1"
LOG_DEBUG_DECIMALS   = os.getenv("LOG_DEBUG_DECIMALS", "0") == "1"
LOG_DEBUG_SYNC       = os.getenv("LOG_DEBUG_SYNC", "0") == "1"
LOG_DEBUG_STRATEGY   = os.getenv("LOG_DEBUG_STRATEGY", "0") == "1"
LOG_DEBUG_TRAILING   = os.getenv("LOG_DEBUG_TRAILING", "0") == "1"
LOG_DEBUG_PORTFOLIO  = os.getenv("LOG_DEBUG_PORTFOLIO", "0") == "1"
# Large-cap con minQty elevata: abilita auto-bump del notional al minimo
LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}
# --- BLACKLIST STABLECOIN ---
STABLECOIN_BLACKLIST = [
    "USDCUSDT", "USDEUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "EURUSDT", "USDTUSDT"
]
EXCLUSION_LIST = ["FUSDT", "YBUSDT", "ZBTUSDT", "RECALLUSDT", "XPLUSDT", "BRETTUSDT"]
LINEAR_MIN_TURNOVER = 5_000_000  # turnover 24h minimo su futures linear

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
        return df["Close"].iloc[-1] < ema200.iloc[-1] and ema200.iloc[-1] < ema200.iloc[-10]
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
        return df["Close"].iloc[-1] < ema100.iloc[-1] and ema100.iloc[-1] < ema100.iloc[-10]
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
        return df["Close"].iloc[-1] > ema200.iloc[-1] and ema200.iloc[-1] > ema200.iloc[-10]
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
        return df["Close"].iloc[-1] > ema100.iloc[-1] and ema100.iloc[-1] > ema100.iloc[-10]
    except Exception:
        return False

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
            log(f"[BYBIT-RAW][ERRORE] get_open_long_qty {symbol}: {json.dumps(data)}")
            return 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Buy":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        log(f"❌ Errore get_open_long_qty per {symbol}: {e}")
        return 0.0

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
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
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
            # Insufficient available balance (Unified) → log essenziale con throttling
            tlog(f"err_110007:{symbol}", f"[ERROR][{symbol}] 110007: saldo disponibile insufficiente per aprire LONG", 300)
            break
        # Altri errori non gestiti → throttling per evitare spam
        tlog(f"long_err:{symbol}:{ret_code}", f"[ERROR][{symbol}] Errore non gestito: {ret_code}", 300)
        break
    return None

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
            "reduceOnly": "true",
            "positionIdx": 1  # Hedge: chiude il lato LONG
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
    
def analyze_asset(symbol: str):
    # Filtro permissivo ma accurato per LONG
    up_4h = is_trending_up(symbol, "240")
    up_1h = is_trending_up_1h(symbol, "60")
    down_4h = is_trending_down(symbol, "240")
    down_1h = is_trending_down_1h(symbol, "60")
    
    # LONG: 4h in uptrend OR (1h in uptrend AND 4h non in forte downtrend)
    can_long = up_4h or (up_1h and not down_4h)
    
    # Blocco: se downtrend troppo forte (4h+1h entrambi down), non entrare LONG
    if down_4h and down_1h:
        if LOG_DEBUG_STRATEGY:
            log(f"[TREND-FILTER][{symbol}] Forte downtrend 4h+1h, skip LONG.")
        return None, None, None
    
    if not can_long:
        if LOG_DEBUG_STRATEGY:
            log(f"[TREND-FILTER][{symbol}] Condizioni trend insufficienti per LONG.")
        return None, None, None

    if ENABLE_BREAKOUT_FILTER and not is_breaking_weekly_low(symbol):
        # per LONG puoi anche usare un filtro "breakout 6h al rialzo" se lo implementi
        pass

    try:
        df = fetch_history(symbol, interval=ENTRY_TF_MINUTES if USE_MTF_ENTRY else INTERVAL_MINUTES)
        if df is None or len(df) < 3:
            log(f"[ANALYZE] Dati storici insufficienti per {symbol}")
            return None, None, None
        close = find_close_column(df)
        if close is None:
            log(f"[ANALYZE] Colonna close non trovata per {symbol}")
            return None, None, None

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

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = (ENTRY_ADX_VOLATILE if is_volatile else ENTRY_ADX_STABLE)

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])

        # --- ENTRY LONG: condizioni rialziste su 15m ---
        entry_conditions = []
        entry_strategies = []
        
        if prev["ema20"] <= prev["ema50"] and last["ema20"] > last["ema50"]:
            entry_conditions.append(True); entry_strategies.append("Incrocio EMA 20/50 (30m)")
        if (last["macd"] - last["macd_signal"]) > 0 and (prev["macd"] - prev["macd_signal"]) <= 0:
            entry_conditions.append(True); entry_strategies.append("MACD cross up (30m)")

        if is_volatile:
            cond1 = last["Close"] > last["bb_upper"]
            cond2 = last["rsi"] > 55
            if cond1 and cond2:
                entry_conditions.append(True); entry_strategies.append("Breakout BB (30m)")
            cond5 = last["macd"] > last["macd_signal"]
            cond6 = last["adx"] > adx_threshold
            if cond5 and cond6:
                entry_conditions.append(True); entry_strategies.append("MACD bullish + ADX (30m)")
            if len(entry_conditions) >= 2:
                if LOG_DEBUG_STRATEGY:
                    log(f"[STRATEGY][{symbol}] Segnale ENTRY LONG: {entry_strategies}")
                return "entry", ", ".join(entry_strategies), price
        else:
            if last["ema20"] > last["ema50"]:
                entry_conditions.append(True); entry_strategies.append("EMA20>EMA50 (30m)")
            cond3 = last["macd"] > last["macd_signal"]
            cond4 = last["adx"] > adx_threshold
            if cond3 and cond4:
                entry_conditions.append(True); entry_strategies.append("MACD bullish (30m)")
            cond5 = last["rsi"] > 50 and last["ema20"] > last["ema50"]
            if cond5:
                entry_conditions.append(True); entry_strategies.append("Trend EMA+RSI (30m)")
            if len(entry_conditions) >= 2:
                if LOG_DEBUG_STRATEGY:
                    log(f"[STRATEGY][{symbol}] Segnale ENTRY LONG: {entry_strategies}")
                return "entry", ", ".join(entry_strategies), price

        # --- EXIT LONG: segnali ribassisti ---
        cond_exit1 = last["Close"] < last["bb_lower"] and last["rsi"] < 45
        if cond_exit1:
            return "exit", "Breakdown BB + RSI (bearish)", price
        if last["macd"] < last["macd_signal"] and last["adx"] > adx_threshold:
            return "exit", "MACD bearish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT [LONG] AVVIATO - In ascolto per segnali di ingresso/uscita")

TEST_MODE = False  # Acquisti e vendite normali abilitati

MIN_HOLDING_MINUTES = 15  # Tempo minimo in minuti da attendere dopo l'acquisto prima di poter attivare uno stop loss
# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}

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
            sl = price - (atr_val * SL_FACTOR)

            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price
            }
            trovate += 1
            log(f"[SYNC] Posizione LONG trovata: {symbol} qty={qty} entry={entry_price:.4f} SL={sl:.4f} TP={tp:.4f}")
            # Marca come già in posizione per evitare nuovi entry
            open_positions.add(symbol)
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

def trailing_stop_worker():
    log("[DEBUG] Avvio ciclo trailing_stop_worker (LONG)")
    while True:
        for symbol in list(open_positions):
            if symbol not in position_data:
                continue

            qty_live = get_open_long_qty(symbol)
            info = get_instrument_info(symbol)
            min_qty = info.get("min_qty", 0.0)
            if qty_live is None or qty_live < min_qty:
                log(f"[CLEANUP] {symbol}: qty LONG troppo bassa ({qty_live}), rimuovo (polvere)")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue

            entry = position_data[symbol]
            if time.time() - entry.get("entry_time", 0) < MIN_HOLDING_MINUTES * 60:
                if LOG_DEBUG_TRAILING:
                    tlog(f"holding_fast:{symbol}", f"[HOLDING][FAST] {symbol}: attendo ancora {max(0, MIN_HOLDING_MINUTES - (time.time()-entry.get('entry_time',0))/60):.1f} min prima di attivare SL/TSL", 60)
                continue

            current_price = get_last_price(symbol)
            if not current_price:
                continue

            if "mfe" not in entry:
                entry["mfe"] = 0.0
                entry["mae"] = 0.0
            entry_price = entry.get("entry_price", current_price)
            profit_pct = ((current_price - entry_price) / entry_price) * 100.0
            entry["mfe"] = max(entry["mfe"], profit_pct)
            entry["mae"] = min(entry["mae"], profit_pct)

            qty = get_open_long_qty(symbol)

            # MAX LOSS (LONG): se scende oltre soglia, chiudi
            if qty > 0 and entry_price and current_price:
                pnl_pct = ((current_price - entry_price) / entry_price) * 100.0
                if pnl_pct < MAX_LOSS_PCT:
                    log(f"🔴 [MAX LOSS] Chiudo LONG su {symbol} (PnL: {pnl_pct:.2f}%)")
                    notify_telegram(f"🛑 MAX LOSS LONG {symbol} PnL {pnl_pct:.2f}%")
                    resp = market_close_long(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        exit_value = current_price * qty
                        log_trade_to_google(symbol, entry_price, current_price, pnl_pct,
                                            "MAX LOSS LONG", "Forced Exit",
                                            usdt_entry=entry.get("entry_cost", 0),
                                            usdt_exit=exit_value,
                                            holding_time_min=(time.time() - entry.get("entry_time", 0)) / 60,
                                            mfe_r=entry.get('mfe', 0), mae_r=entry.get('mae', 0),
                                            r_multiple=None, market_condition="max_loss_long")
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    continue

            # TP1 a 1R (LONG): chiudi 50% e porta SL a breakeven
            if ENABLE_TP1 and qty and qty > 0 and "pt1_done" not in entry:
                risk_per_unit = max(0.0, entry_price - entry.get("sl", entry_price * 0.99))
                if risk_per_unit > 0:
                    target_1r = entry_price + (risk_per_unit * TP1_R_MULT)
                    if current_price >= target_1r:
                        step_p = info.get("qty_step", 0.01)
                        half_qty = qty * TP1_CLOSE_PCT
                        half_dec = Decimal(str(half_qty))
                        step_dec = Decimal(str(step_p))
                        half_aligned = float((half_dec // step_dec) * step_dec)
                        if half_aligned >= step_p:
                            resp = market_close_long(symbol, half_aligned)
                            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                                entry["pt1_done"] = True
                                entry["sl"] = entry_price  # BE
                                entry["qty"] = max(0.0, qty - half_aligned)
                                entry["entry_cost"] = entry_price * entry["qty"]
                                notify_telegram(f"✅ TP1 (1R) LONG {symbol}: chiuso {TP1_CLOSE_PCT*100:.0f}% e SL a BE.")

            # Trailing attivazione (LONG)
            trailing_threshold = max(TRAILING_ACTIVATION_THRESHOLD, 0.02) if symbol in VOLATILE_ASSETS else max(TRAILING_ACTIVATION_THRESHOLD / 2, 0.008)
            soglia_attivazione = entry["entry_price"] * (1 + trailing_threshold)
            if LOG_DEBUG_TRAILING:
                tlog(
                    f"trail_check:{symbol}",
                    f"[TRAILING CHECK][LONG] {symbol} | entry_price={entry.get('entry_price',0):.4f} | current_price={current_price:.4f} | soglia={soglia_attivazione:.4f} | trailing_active={entry.get('trailing_active', False)}",
                    30
                )
            if not entry["trailing_active"] and current_price >= soglia_attivazione:
                entry["trailing_active"] = True
                notify_telegram(f"🔛🔺 Trailing Stop LONG attivato su {symbol} a {current_price:.4f}")

            if entry["trailing_active"]:
                # Aggiorna massimo
                if current_price > entry.get("p_max", entry["entry_price"]):
                    entry["p_max"] = current_price

                # Trailing TP: chiudi se ritraccia sotto p_max*(1 - buffer)
                tp_trailing_buffer = 0.015 if symbol in VOLATILE_ASSETS else 0.010
                trailing_tp_price = entry.get("p_max", entry["entry_price"]) * (1 - tp_trailing_buffer)
                if current_price <= trailing_tp_price:
                    qty = get_open_long_qty(symbol)
                    if qty > 0:
                        resp = market_close_long(symbol, qty)
                        if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                            pnl = ((current_price - entry["entry_price"]) / entry["entry_price"]) * 100.0
                            log(f"🟢⬇️ Trailing TP LONG chiuso {symbol} PnL {pnl:.2f}%")
                            notify_telegram(f"🟢⬇️ Trailing TP LONG chiuso {symbol} a {current_price:.4f} | PnL {pnl:.2f}%")
                            log_trade_to_google(symbol, entry["entry_price"], current_price, pnl,
                                                "Trailing TP LONG", "TP Triggered",
                                                usdt_entry=entry.get("entry_cost", 0),
                                                usdt_exit=current_price * qty,
                                                holding_time_min=(time.time() - entry.get("entry_time", 0)) / 60,
                                                mfe_r=entry.get('mfe', 0), mae_r=entry.get('mae', 0),
                                                r_multiple=None, market_condition="trailing_tp_long")
                            open_positions.discard(symbol)
                            last_exit_time[symbol] = time.time()
                            position_data.pop(symbol, None)
                    continue

                # Aggiorna SL trailing (LONG): alza lo SL
                new_sl = current_price * (1 - TRAILING_SL_BUFFER)
                if new_sl > entry.get("sl", entry_price * 0.99):
                    entry["sl"] = new_sl

            # Trigger SL (LONG)
            if current_price <= entry.get("sl", entry_price * 0.99):
                qty = get_open_long_qty(symbol)
                if qty is None or qty <= 0:
                    open_positions.discard(symbol); position_data.pop(symbol, None); continue
                resp = market_close_long(symbol, qty)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    pnl = ((current_price - entry["entry_price"]) / entry["entry_price"]) * 100.0
                    notify_telegram(f"🛑 Stop Loss LONG {symbol} a {current_price:.4f} | PnL {pnl:.2f}%")
                    log_trade_to_google(symbol, entry["entry_price"], current_price, pnl,
                                        "Stop Loss LONG", "SL Triggered",
                                        usdt_entry=entry.get("entry_cost", 0),
                                        usdt_exit=current_price * qty,
                                        holding_time_min=(time.time() - entry.get("entry_time", 0)) / 60,
                                        mfe_r=entry.get('mfe', 0), mae_r=entry.get('mae', 0),
                                        r_multiple=None, market_condition="sl_triggered_long")
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
        time.sleep(15)

trailing_thread = threading.Thread(target=trailing_stop_worker, daemon=True)
trailing_thread.start()

while True:
    update_assets()
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

        # ENTRY LONG
        if signal == "entry":
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

            is_volatile = symbol in VOLATILE_ASSETS
            group_budget = volatile_budget if is_volatile else stable_budget
            group_invested = volatile_invested if is_volatile else stable_invested
            group_available = max(0.0, group_budget - group_invested)
            if group_available < ORDER_USDT:
                tlog(f"budget:{symbol}", f"💸 Budget insufficiente per {symbol} (disp: {group_available:.2f})", 300)
                continue

            weights = {
                "Breakout BB (30m)": 1.0,
                "MACD bullish + ADX (30m)": 0.9,
                "Incrocio EMA 20/50 (30m)": 0.75,
                "EMA20>EMA50 (30m)": 0.75,       # usata nel ramo non-volatile
                "MACD cross up (30m)": 0.65,     # usata nel ramo volatile
                "MACD bullish (30m)": 0.65,
                "Trend EMA+RSI (30m)": 0.6
            }
            parts = [p.strip() for p in (strategy or "").split(",") if p.strip()]
            if parts:
                base = max(weights.get(p, 0.5) for p in parts)
                bonus = min(0.1 * (len(parts) - 1), 0.3)  # +0.1 per conferma, max +0.3
                strength = min(1.0, base + bonus)
            else:
                strength = 0.5

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

            if not qty or qty == 0:
                log(f"❌ LONG non aperto per {symbol}")
                continue

            price_now = get_last_price(symbol)
            actual_cost = qty * price_now
            df = fetch_history(symbol)
            if df is None or "Close" not in df.columns:
                continue
            atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
            atr_val = atr.iloc[-1]
            atr_ratio = atr_val / price_now if price_now > 0 else 0
            tp_factor = min(TP_MAX, max(TP_MIN, TP_FACTOR + atr_ratio * 5))
            sl_factor = min(SL_MAX, max(SL_MIN, SL_FACTOR + atr_ratio * 3))

            tp = price_now + (atr_val * tp_factor)
            sl = price_now - (atr_val * sl_factor)
            
            position_data[symbol] = {
                "entry_price": price_now,
                "tp": tp,
                "sl": sl,
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price_now
            }
            open_positions.add(symbol)
            notify_telegram(f"🟢📈 LONG aperto {symbol}\nPrezzo: {price_now:.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f}\nSL: {sl:.4f}\nTP: {tp:.4f}")
            time.sleep(3)

        # EXIT LONG (segnale di uscita strategico)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            if time.time() - entry.get("entry_time", 0) < MIN_HOLDING_MINUTES * 60:
                tlog(f"holding_exit:{symbol}", f"[HOLDING][EXIT] {symbol}: attendo ancora {max(0, MIN_HOLDING_MINUTES - (time.time()-entry.get('entry_time',0))/60):.1f} min prima di poter vendere", 60)
                continue

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
                log_trade_to_google(symbol, entry_price, current_price, pnl, strategy, "Exit Signal",
                                    usdt_entry=entry_cost, usdt_exit=exit_value,
                                    holding_time_min=(time.time() - entry.get("entry_time", 0)) / 60,
                                    mfe_r=entry.get('mfe', 0), mae_r=entry.get('mae', 0),
                                    r_multiple=None, market_condition="exit_signal_long")
                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
                position_data.pop(symbol, None)

    # Cleanup posizioni con qty troppo bassa
    for symbol in list(open_positions):
        saldo = get_open_long_qty(symbol)
        info = get_instrument_info(symbol)
        min_qty = info.get("min_qty", 0.0)
        if saldo is None or saldo < min_qty:
            open_positions.discard(symbol)
            position_data.pop(symbol, None)

    time.sleep(120)