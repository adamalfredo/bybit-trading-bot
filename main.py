import os
import time
import hmac
import json
import hashlib
from decimal import Decimal
import requests
import pandas as pd
from ta.volatility import BollingerBands, AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
from typing import Optional

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
MIN_BALANCE_USDT = 20.0  # prima 50.0
SAFETY_AVAILABLE_PCT = 0.97       # Usa max 97% del saldo disponibile
MARKET_COST_BUFFER_PCT = 0.0025   # 0.25% buffer (fee + micro slippage) per pre-check MARKET
ALLOW_SUB_MIN_BALANCE_ENTRY = True    # consente ingresso se saldo < MIN_BALANCE_USDT ma >= min_order_amt
SYNC_BACKFILL_HOLDING_EXEMPT = True   # posizioni sincronizzate esentate da holding minimo per exit/trailing
LARGE_ASSETS = {"BTCUSDT", "ETHUSDT", "SOLUSDT"}  # gruppo large cap
EXTENSION_ATR_MULT_BASE = 1.2
EXTENSION_ATR_MULT_LARGE = 1.5  # large cap più permissive
TREND_MIN_RATIO = 0.985        # prima 0.995
SECONDARY_RATIO = 0.970        # prima 0.980
COUNTER_TREND_MIN_RATIO = 0.950
REVERSAL_MIN_RATIO = 0.940
ENABLE_COUNTER_TREND = True
ENABLE_REVERSAL_BB = True

COUNTER_SLOPE_EPS = 0.0005          # tolleranza slope ema20 (già usata prima se la vorrai integrare)
TRAILING_ACTIVATION_R = 1.5         # (modificato: prima 2.0)
TRAILING_LOCK_R = 0.8               # (modificato: prima 1.0)

# Pullback + Giveback nuova logica
ENABLE_PULLBACK_EMA20 = True
PULLBACK_MAX_RATIO = 0.985          # entro area “sana” (sotto il primary)
PULLBACK_MIN_RATIO = COUNTER_TREND_MIN_RATIO
PULLBACK_ATR_PENETRATION = 0.20     # quanto sotto ema20 (Close precedente) consideriamo valido (in ATR)
PULLBACK_LOW_PENETRATION = 0.30     # alternativa via Low precedente
PULLBACK_MIN_RSI = 45               # conferma momentum base

# Giveback exit
ENABLE_GIVEBACK_EXIT = True
GIVEBACK_MIN_MFE_R = 1.2            # attivo solo se ha toccato almeno 1.2R
GIVEBACK_DROP_R = 0.6               # restituisce ≥0.6R dal massimo ⇒ exit
COUNTER_OVERRIDE_RSI = 48           # RSI sopra questa soglia abilita override momentum nel counter-trend
EARLY_EXIT_ENABLE = True
EARLY_EXIT_MIN_R = 0.8          # minimo R raggiunto per considerare uscita anticipata
EARLY_EXIT_RSIFALL = 48         # se RSI scende sotto questa soglia dopo aver superato 55
EARLY_EXIT_REQUIRE_EMA20 = True # richiedi che il prezzo sia < ema20 (altrimenti solo MACD non basta)

STALE_DATA_MAX_HOURS = 2
INVERSION_HEURISTIC_MINUTES = 120  # 2 ore (coerente con staleness)

STRATEGY_STRENGTH = {
    "Breakout Bollinger": 1.0,
    "MACD bullish + ADX": 0.9,
    "Incrocio SMA 20/50": 0.75,
    "Incrocio EMA 20/50": 0.7,
    "MACD bullish (stabile)": 0.65,
    "Reversal BB + RSI": 0.55,
    "Pullback EMA20": 0.7,
    "Trend EMA + RSI": 0.6
}

ASSETS = [
    "WIFUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT",
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]

VOLATILE_ASSETS = [
    "WIFUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT"
]

INTERVAL_MINUTES = 15
ATR_WINDOW = 14
SL_ATR_MULT = 1.0
TP_R_MULT   = 2.5
ATR_MIN_PCT = 0.002
ATR_MAX_PCT = 0.030
MAX_OPEN_POSITIONS = 5
COOLDOWN_MINUTES = 60
TRAIL_LOCK_FACTOR = 1.2
RISK_PCT = 0.01
MIN_HOLDING_MINUTES = 5           # tempo minimo prima di accettare exit/trailing
TRAILING_ENABLED = True           # per disattivare tutta la logica trailing
MIN_SL_PCT = 0.010                # SL minimo = 1% del prezzo (se ATR troppo piccolo)
MAX_NEW_POSITIONS_PER_CYCLE = 2   # massimo ingressi per ciclo di scansione
USE_DYNAMIC_ASSET_LIST = True      # Fase 2: se True sostituirà ASSETS dinamicamente
USE_SAFE_ORDER_BUY   = False        # Fase 3: se True userà safe_market_buy() al posto di market_buy_qty()
LARGE_CAP_MIN_NOTIONAL_MULT = 1.10  # quanto sopra il notional minimo per tentativo large cap
LARGE_CAP_LIMIT_SLIPPAGE    = 0.0015  # 0.15% sopra last price per LIMIT IOC (fill immediato)
ENFORCE_DIVERGENCE_CHECK = False
DIVERGENCE_MAX_PCT = 0.05   # 5% sul testnet
EXCLUDE_LOW_PRICE    = True         # Se True (fase 1) solo DRY-RUN (non modifica ASSETS)
PRICE_MIN_ACTIVE     = 0.01         # Soglia prezzo per esclusione preventiva (dry-run ora)
DYNAMIC_ASSET_MIN_VOLUME = 500000   # Filtro volume quote (USDT)
MAX_DYNAMIC_ASSETS   = 25           # Limite massimo asset dinamici
DYNAMIC_REFRESH_MIN  = 30           # Ogni X minuti (fase 2)

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

def notify_telegram(message: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=data, timeout=10)
        except Exception as e:
            log(f"Errore invio Telegram: {e}")

def get_last_price(symbol: str) -> Optional[float]:
    try:
        resp = requests.get(
            f"{BYBIT_BASE_URL}/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=10
        )
        data = resp.json()

        if data.get("retCode") != 0:
            log(f"❌ get_last_price retCode !=0 per {symbol}: {data.get('retMsg')}")
            return None

        result = data.get("result")
        if not isinstance(result, dict):
            log(f"❌ get_last_price struttura inattesa result (non dict) per {symbol}: {data}")
            return None

        lst = result.get("list")
        if not lst or not isinstance(lst, list):
            log(f"❌ get_last_price lista vuota per {symbol}: {data}")
            return None

        price_raw = lst[0].get("lastPrice")
        if price_raw is None:
            log(f"❌ get_last_price lastPrice mancante per {symbol}: {lst[0]}")
            return None

        return float(price_raw)
    except Exception as e:
        log(f"❌ Errore in get_last_price({symbol}): {e}")
        return None
    
# --- CACHE INFO STRUMENTI (nuovo) ---
_instrument_cache = {}

def get_instrument_info(symbol: str) -> dict:
    """
    Restituisce info strumento con cache 5 minuti per ridurre chiamate ripetute.
    """
    now = time.time()
    cached = _instrument_cache.get(symbol)
    if cached and (now - cached["ts"] < 300):  # 300s = 5 minuti
        return cached["data"]

    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    params = {"category": "spot", "symbol": symbol}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"❌ Errore fetch info strumento {symbol}: {data.get('retMsg')}")
            return {}
        lst = data.get("result", {}).get("list", [])
        if not lst:
            return {}
        info = lst[0]
        lot = info.get("lotSizeFilter", {})
        price_filter = info.get("priceFilter", {})
        tick_size_raw = price_filter.get("tickSize", "0.01") or "0.01"
        try:
            tick_size = float(tick_size_raw)
        except:
            tick_size = 0.01
        parsed = {
            "min_qty": float(lot.get("minOrderQty", 0) or 0),
            "qty_step": float(lot.get("qtyStep", 0.0001) or 0.0001),
            "precision": int(info.get("priceScale", 4) or 4),
            "tick_size": tick_size,
            "min_order_amt": float(info.get("minOrderAmt", 5) or 5),
            "max_order_qty": float(lot.get("maxOrderQty")) if lot.get("maxOrderQty") else None,
            "max_order_amt": float(lot.get("maxOrderAmt")) if lot.get("maxOrderAmt") else None
        }
        _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed
    except Exception as e:
        log(f"❌ Errore get_instrument_info {symbol}: {e}")
        return {}
    
def is_symbol_supported(symbol: str) -> bool:
    info = get_instrument_info(symbol)
    return bool(info)  # se vuoto consideriamo non supportato

def get_free_qty(symbol: str) -> float:
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
            log(f"❗ Struttura inattesa da Bybit per {symbol}: {resp.text}")
            return 0.0

        coin_list = data["result"]["list"][0].get("coin", [])
        for c in coin_list:
            if c.get("coin") == coin:
                raw_wallet = c.get("walletBalance", "0")
                raw_avail = c.get("availableBalance", raw_wallet)
                try:
                    wallet = float(raw_wallet) if raw_wallet else 0.0
                    avail = float(raw_avail) if raw_avail else wallet
                except Exception as e:
                    log(f"⚠️ Errore conversione saldo {coin}: {e}")
                    return 0.0

                if coin == "USDT":
                    if avail < wallet:
                        log(f"📦 USDT wallet={wallet:.4f} available={avail:.4f}")
                    else:
                        log(f"📦 USDT available={avail:.4f}")
                    return avail
                else:
                    if wallet > 0:
                        log(f"📦 Saldo trovato per {coin}: {wallet}")
                    else:
                        log(f"🟡 Nessun saldo disponibile per {coin}")
                    return wallet

        log(f"🔍 Coin {coin} non trovata nel saldo.")
        return 0.0

    except Exception as e:
        log(f"❌ Errore nel recupero saldo per {symbol}: {e}")
        return 0.0

def market_sell(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile vendere")
        return

    order_value = qty * price
    if order_value < 5:
        log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT")
        return

    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    if not qty_step or qty_step <= 0:
        qty_step = 0.0001

    try:
        step = Decimal(str(qty_step))
        dec_qty = Decimal(str(qty))
        floored_qty = (dec_qty // step) * step

        # Deriva decimali dallo step
        step_str = f"{qty_step:.10f}".rstrip('0')
        if '.' in step_str:
            step_decimals = len(step_str.split('.')[1])
        else:
            step_decimals = 0

        if floored_qty <= 0:
            log(f"❌ Quantità troppo piccola per {symbol} (dopo arrotondamento)")
            return

        qty_str = f"{floored_qty:.{step_decimals}f}".rstrip('0').rstrip('.')
        if qty_str == '':
            qty_str = '0'

        log(f"[DEBUG-SELL-QTY] {symbol} req={qty} step={qty_step} send={qty_str}")

    except Exception as e:
        log(f"❌ Errore arrotondamento quantità {symbol}: {e}")
        return

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "qty": qty_str
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
    try:
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        log(f"SELL BODY: {body_json}")
        log(f"RESPONSE: {resp.status_code} {resp.json()}")
        return resp
    except Exception as e:
        log(f"❌ Errore invio ordine SELL: {e}")
        return None

def _align_price_tick(price: float, tick: float, up: bool = False) -> float:
    if tick <= 0:
        return price
    import math
    if up:
        return math.ceil(price / tick) * tick
    return math.floor(price / tick) * tick

def _format_qty(qty: Decimal, step: float) -> (Decimal, str):
    step_dec = Decimal(str(step))
    floored = (qty // step_dec) * step_dec
    step_str = f"{step:.10f}".rstrip('0')
    decs = len(step_str.split('.')[1]) if '.' in step_str else 0
    s = f"{floored:.{decs}f}".rstrip('0').rstrip('.')
    if s == '':
        s = '0'
    return floored, s

def _format_price(price: float, tick: float) -> str:
    tick_str = f"{tick:.10f}".rstrip('0')
    decs = len(tick_str.split('.')[1]) if '.' in tick_str else 0
    return f"{price:.{decs}f}"

LAST_ORDER_RETCODE = None

def execute_buy_order(symbol: str, qty_dec: Decimal, prefer_limit: bool,
                      slippage: float = 0.0015, max_retries: int = 3) -> bool:
    """
    Esegue ordine BUY robusto:
      - Allinea qty a qty_step
      - LIMIT IOC per large cap / prezzo ≥100
      - Pre-check saldo per MARKET (include buffer)
      - Retry su:
          170140 (notional basso) → aumenta qty
          170134 (decimali prezzo) → riduce qty (LIMIT)
          170131 (insufficient balance) → riduce qty (−10%)
    """
    global LAST_ORDER_RETCODE
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    min_order_amt = info.get("min_order_amt", 5)
    tick = info.get("tick_size", 0.01)
    max_order_qty = info.get("max_order_qty")
    step_dec = Decimal(str(qty_step))

    qty_aligned = (qty_dec // step_dec) * step_dec
    if qty_aligned <= 0:
        log(f"❌ execute_buy_order qty iniziale non valida {symbol}")
        return False

    for attempt in range(1, max_retries + 1):
        LAST_ORDER_RETCODE = None
        last_price = get_last_price(symbol)
        if not last_price:
            log(f"❌ Nessun prezzo per {symbol}")
            return False

        if prefer_limit:
            raw_limit = last_price * (1 + slippage)
            limit_price = _align_price_tick(raw_limit, tick, up=True)
            price_str = _format_price(limit_price, tick)
        else:
            limit_price = last_price
            price_str = None  # MARKET

        if max_order_qty and float(qty_aligned) > max_order_qty:
            qty_aligned = (Decimal(str(max_order_qty)) // step_dec) * step_dec

        notional = float(qty_aligned) * limit_price
        if notional < min_order_amt:
            needed = Decimal(str(min_order_amt / limit_price))
            needed = (needed // step_dec) * step_dec
            if needed > qty_aligned:
                qty_aligned = needed
                notional = float(qty_aligned) * limit_price

        # Pre-check saldo solo per MARKET (usa available reale + buffer costo)
        if not prefer_limit:
            avail_usdt = get_usdt_balance()
            est_cost = float(qty_aligned) * limit_price * (1 + MARKET_COST_BUFFER_PCT)
            # Limita a SAFETY_AVAILABLE_PCT
            hard_cap = avail_usdt * SAFETY_AVAILABLE_PCT
            if est_cost > hard_cap:
                reduce_factor = hard_cap / est_cost if est_cost > 0 else 0
                new_qty = (qty_aligned * Decimal(str(reduce_factor))) // step_dec * step_dec
                if new_qty <= 0:
                    log(f"❌ Pre-check saldo: impossibile ridurre qty {symbol}")
                    return False
                log(f"[PRE-CHECK][{symbol}] Riduzione qty {qty_aligned}→{new_qty} (est_cost {est_cost:.4f} > cap {hard_cap:.4f})")
                qty_aligned = new_qty

        # Format final
        _, qty_str = _format_qty(qty_aligned, qty_step)
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit" if prefer_limit else "Market",
            "qty": qty_str
        }
        if prefer_limit:
            body["timeInForce"] = "IOC"
            body["price"] = price_str

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
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        try:
            rj = resp.json()
        except:
            rj = {}
        LAST_ORDER_RETCODE = rj.get("retCode")
        log(f"[EXEC BUY][{symbol}] attempt {attempt}/{max_retries} BODY={body_json}")
        log(f"[EXEC BUY][{symbol}] RESP {resp.status_code} {rj}")

        if resp.status_code == 200 and rj.get("retCode") == 0:
            return True

        rc = rj.get("retCode")
        if attempt == max_retries:
            break

        if rc == 170140:  # notional basso → aumenta
            bump = qty_aligned * Decimal("1.35")
            qty_aligned = (bump // step_dec) * step_dec
            log(f"[RETRY][{symbol}] bump qty per notional basso → {qty_aligned}")
            continue

        if rc == 170134 and prefer_limit:  # decimali prezzo → riduci qty
            qty_aligned = ((qty_aligned - step_dec) // step_dec) * step_dec
            if qty_aligned <= 0:
                break
            log(f"[RETRY][{symbol}] decimali prezzo: qty→{qty_aligned}")
            continue

        if rc == 170131:  # Insufficient balance → riduci qty (−10%)
            reduced = (qty_aligned * Decimal("0.90")) // step_dec * step_dec
            if reduced <= 0 or reduced == qty_aligned:
                log(f"[RETRY][{symbol}] balance insufficiente: impossibile ridurre oltre ({qty_aligned})")
                break
            qty_aligned = reduced
            log(f"[RETRY][{symbol}] insufficiente balance: qty→{qty_aligned}")
            continue

        break
    return False

def fetch_history(symbol: str):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": str(INTERVAL_MINUTES),
        "limit": 400  # WAS 100 → aumentato per calcolare EMA200
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            log(f"[!] Errore Kline per {symbol}: {data}")
            return None
        raw = data["result"]["list"]
        df = pd.DataFrame(raw, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "turnover"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df.set_index("timestamp", inplace=True)
        df.sort_index(inplace=True)  # assicura ordine cronologico crescente
        # Controllo staleness (candela finale troppo vecchia)
        latest_ts = df.index[-1]
        age_sec = time.time() - latest_ts.timestamp()
        if age_sec > STALE_DATA_MAX_HOURS * 3600:
            log(f"[STALE][{symbol}] Ultima candela {latest_ts} vecchia {age_sec/3600:.2f}h (> {STALE_DATA_MAX_HOURS}h)")

        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        log(f"[!] Errore richiesta Kline per {symbol}: {e}")
        return None

def analyze_asset(symbol: str):
    try:
        df = fetch_history(symbol)
        if df is None:
            return None, None, None
        close = df["Close"]
        if close is None:
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

        df.dropna(subset=[
            "bb_upper","bb_lower","rsi","sma20","sma50","ema20","ema50",
            "macd","macd_signal","adx","atr"
        ], inplace=True)

        if len(df) < 50:
            log(f"[DEBUG][{symbol}] STOP: len(df)={len(df)} dopo dropna (storico pulito insufficiente)")
            return None, None, None

        # Controllo EMA200: se ancora NaN (per prime ~200 barre) esco senza errore
        if pd.isna(df.iloc[-1]["ema200"]):
            log(f"[FILTER][{symbol}] ema200 non pronta (ancora <200 barre utili)")
            return None, None, None

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = 20 if is_volatile else 15

        inverted = False
        if ((time.time() - df.index[-1].timestamp()) > INVERSION_HEURISTIC_MINUTES * 60
            and (time.time() - df.index[0].timestamp()) < INVERSION_HEURISTIC_MINUTES * 60):
            inverted = True
            last = df.iloc[0]
            prev = df.iloc[1]
            last_ts_used = df.index[0]
        else:
            last = df.iloc[-1]
            prev = df.iloc[-2]
            last_ts_used = df.index[-1]
        log(f"[BAR][{symbol}] last_ts={last_ts_used} inverted={inverted} age={(time.time()-last_ts_used.timestamp()):.1f}s")
        price = float(last["Close"])
        atr_val = float(last["atr"])
        # Salva cache per riuso nel sizing (evita seconda fetch)
        ANALYSIS_CACHE[symbol] = {
            "atr_val": atr_val,
            "close": price,
            "ts": df.index[-1].timestamp(),
            "ema20": float(last["ema20"]),
            "ema50": float(last["ema50"]),
            "ema200": float(last["ema200"]),
            "macd": float(last["macd"]),
            "macd_signal": float(last["macd_signal"]),
            "rsi": float(last["rsi"]),
            "adx": float(last["adx"])
        }
        atr_pct = atr_val / price if price else 0
        # log(f"[ANALYZE] {symbol} ATR={atr_val:.5f} ({atr_pct:.2%})")

        log(f"[STATE][{symbol}] Close={price:.6f} ATR%={atr_pct:.3%} ema50={last['ema50']:.4f} ema200={last['ema200']:.4f}")
        # FILTRI + DEBUG (MODALITÀ TOLLERANTE)
        ema50v = float(last["ema50"])
        ema200v = float(last["ema200"])
        ema20v = float(last["ema20"])
        ema_ratio = ema50v / ema200v if ema200v else 0.0

        # --- Trend logic modulare (REVISIONE con slope + momentum override) ---
        primary_trend = ema_ratio >= TREND_MIN_RATIO
        transitional_ok = (ema_ratio >= SECONDARY_RATIO) and (ema20v > ema50v)

        # Calcolo slope (tolleranza COUNTER_SLOPE_EPS)
        ema20_prev = float(prev["ema20"])
        ema50_prev = float(prev["ema50"])
        ema20_rising = ema20v >= ema20_prev * (1 - COUNTER_SLOPE_EPS)
        ema50_not_dumping = ema50v >= ema50_prev * 0.998  # evita ema50 in caduta ripida

        # Momentum forte può scavalcare il requisito di ema20_rising
        strong_momentum = (last["macd"] > last["macd_signal"]) and (last["rsi"] >= COUNTER_OVERRIDE_RSI)

        counter_trend_ok = (
            ENABLE_COUNTER_TREND
            and (ema_ratio >= COUNTER_TREND_MIN_RATIO)
            and ema50_not_dumping
            and (ema20_rising or strong_momentum)
            and (last["macd"] > last["macd_signal"])
            and (last["rsi"] > 42)   # soglia base (override usa RSI più alta)
        )

        if not (primary_trend or transitional_ok or counter_trend_ok):
            log(f"[FILTER][{symbol}] Trend KO: ratio={ema_ratio:.4f} "
                f"(need ≥{TREND_MIN_RATIO:.3f} | trans ≥{SECONDARY_RATIO:.3f} | counter ≥{COUNTER_TREND_MIN_RATIO:.3f} "
                f"| slope20={ema20_rising} strongMom={strong_momentum})")
            return None, None, None

        EPS = 0.00005  # tolleranza
        if not (ATR_MIN_PCT - EPS <= atr_pct <= ATR_MAX_PCT + EPS):
            log(f"[FILTER][{symbol}] ATR% {atr_pct:.4%} fuori range tol ({ATR_MIN_PCT:.2%}-{ATR_MAX_PCT:.2%})")
            return None, None, None

        ext_mult = EXTENSION_ATR_MULT_LARGE if symbol in LARGE_ASSETS else EXTENSION_ATR_MULT_BASE
        limit_ext = last["ema20"] + ext_mult * atr_val
        if price > limit_ext:
            log(f"[FILTER][{symbol}] Estensione: price {price:.4f} > ema20 {ema20v:.4f} + {ext_mult}*ATR ({limit_ext:.4f})")
            return None, None, None
        
        # Strategie per asset volatili
        if is_volatile:
            if last["Close"] > last["bb_upper"] and last["rsi"] < 70:
                return "entry", "Breakout Bollinger", price
            elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
                return "entry", "Incrocio SMA 20/50", price
            elif last["macd"] > last["macd_signal"] and last["adx"] > adx_threshold:
                return "entry", "MACD bullish + ADX", price
        # Strategie per asset stabili
        else:
            if prev["ema20"] < prev["ema50"] and last["ema20"] > last["ema50"]:
                return "entry", "Incrocio EMA 20/50", price
            elif last["macd"] > last["macd_signal"] and last["adx"] > adx_threshold:
                return "entry", "MACD bullish (stabile)", price
            elif last["rsi"] > 50 and last["ema20"] > last["ema50"]:
                return "entry", "Trend EMA + RSI", price

        # Pullback EMA20 (rientro controllato)
        if ENABLE_PULLBACK_EMA20:
            # Condizioni di contesto: ratio dentro range, non già primary forte
            if (PULLBACK_MIN_RATIO <= ema_ratio <= PULLBACK_MAX_RATIO
                and ema20v > ema50v
                and last["ema50"] >= last["ema200"] * 0.94  # evita crolli profondi
            ):
                atr_prev = float(prev["atr"])
                ema20_prev = float(prev["ema20"])
                prev_close = float(prev["Close"])
                prev_low = float(prev["Low"])
                penetrated_close = prev_close <= ema20_prev - PULLBACK_ATR_PENETRATION * atr_prev
                penetrated_low = prev_low <= ema20_prev - PULLBACK_LOW_PENETRATION * atr_prev
                regained = price > ema20v
                momentum_ok = (last["macd"] > last["macd_signal"]) or (last["rsi"] >= PULLBACK_MIN_RSI)
                if (regained and momentum_ok and (penetrated_close or penetrated_low)):
                    return "entry", "Pullback EMA20", price

        # Reversal BB (mean reversion controllata)
        if ENABLE_REVERSAL_BB and ema_ratio >= REVERSAL_MIN_RATIO:
            if last["Close"] <= last["bb_lower"] * 1.01 and last["rsi"] < 35 and last["adx"] < 22:
                return "entry", "Reversal BB + RSI", price

        # EXIT comune a tutti
        if last["Close"] < last["bb_lower"] and last["rsi"] > 30:
            return "exit", "Rimbalzo RSI + BB", price
        elif last["macd"] < last["macd_signal"] and last["adx"] > adx_threshold:
            return "exit", "MACD bearish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT AVVIATO - In ascolto per segnali di ingresso/uscita")

log(f"[CONFIG] FEATURES: dyn_assets={USE_DYNAMIC_ASSET_LIST} safe_buy={USE_SAFE_ORDER_BUY} exclude_low_price={EXCLUDE_LOW_PRICE}")
log(f"[GROUPS] Large={list(LARGE_ASSETS)} BaseExt={EXTENSION_ATR_MULT_BASE} LargeExt={EXTENSION_ATR_MULT_LARGE}")

def _dry_run_low_price_exclusions():
    if not EXCLUDE_LOW_PRICE:
        return
    to_exclude = []
    for sym in ASSETS:
        price = get_last_price(sym)
        if price and price < PRICE_MIN_ACTIVE:
            to_exclude.append(f"{sym}({price})")
    if to_exclude:
        log(f"[DRY-RUN][LOW-PRICE] Escluderei (prezzo<{PRICE_MIN_ACTIVE}): {', '.join(to_exclude)}")
    else:
        log(f"[DRY-RUN][LOW-PRICE] Nessun asset sotto {PRICE_MIN_ACTIVE}")

_dry_run_low_price_exclusions()

def apply_low_price_exclusion():
    global ASSETS
    if not EXCLUDE_LOW_PRICE:
        return
    kept = []
    removed = []
    for sym in ASSETS:
        p = get_last_price(sym)
        if p is None:
            kept.append(sym)  # se non recupero prezzo non lo scarto subito
            continue
        if p < PRICE_MIN_ACTIVE:
            removed.append(f"{sym}({p})")
        else:
            kept.append(sym)
    if removed:
        log(f"[LOW-PRICE][APPLIED] Rimossi: {removed}")
    else:
        log("[LOW-PRICE][APPLIED] Nessuna rimozione")
    ASSETS = kept

# Applica rimozione reale (dopo il DRY-RUN per confrontare)
apply_low_price_exclusion()

def update_dynamic_assets():
    """
    Costruisce lista dinamica:
      - Filtra solo coppie USDT
      - Esclude stablecoin note
      - Filtro volume minimo e prezzo minimo
      - Ordina per turnover24h desc
    Ritorna (assets, volatile_assets)
    """
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "spot"}
    try:
        resp = requests.get(url, params=params, timeout=15)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"❌ dynamic assets retCode !=0: {data.get('retMsg')}")
            return ASSETS, VOLATILE_ASSETS

        raw_list = data.get("result", {}).get("list", [])
        filtered = []
        for r in raw_list:
            sym = r.get("symbol")
            if not sym or not sym.endswith("USDT"):
                continue
            if sym in ("USDCUSDT","DAIUSDT","BUSDUSDT","USDTUSDT","FDUSDUSDT","TUSDUSDT","USDEUSDT"):
                continue
            try:
                lastp = float(r.get("lastPrice", 0) or 0)
                vol_quote = float(r.get("turnover24h", 0) or 0)
            except:
                continue
            if EXCLUDE_LOW_PRICE and lastp < PRICE_MIN_ACTIVE:
                continue
            if vol_quote < DYNAMIC_ASSET_MIN_VOLUME:
                continue
            filtered.append((sym, vol_quote, lastp))

        filtered.sort(key=lambda x: x[1], reverse=True)
        top = filtered[:MAX_DYNAMIC_ASSETS]

        # Classifica "volatili" in base al prezzo (più basso) o coda di volume
        assets_new = [t[0] for t in top]
        # euristica: volatili = ultime 60% per volume + coin sotto prezzo 5 USDT
        cut = int(len(assets_new) * 0.4)
        high_volume = set(assets_new[:cut])
        volatile = []
        for sym, vol, p in top:
            if sym not in high_volume and p < 15:
                volatile.append(sym)
            elif p < 5:
                volatile.append(sym)
        # fallback se troppo poche
        if len(volatile) < max(2, len(assets_new)//5):
            volatile = [a for a in assets_new[cut:]]

        log(f"[DYN][REFRESH] assets={len(assets_new)} volatile={len(volatile)}")
        return assets_new, volatile
    except Exception as e:
        log(f"❌ update_dynamic_assets errore: {e}")
        return ASSETS, VOLATILE_ASSETS

if USE_DYNAMIC_ASSET_LIST:
    dyn_list = update_dynamic_assets()
    log(f"[DYN] Lista dinamica ATTIVA: {dyn_list}")
    # NOTA: in FASE 1 non sostituiamo ASSETS. In FASE 2 potrai fare: ASSETS = dyn_list
else:
    log("[DYN] Disattivato (fase 1)")

TEST_MODE = False  # Acquisti e vendite normali abilitati


# Inizializza struttura base
open_positions = set()
position_data = {}
last_exit_time = {}

ANALYSIS_CACHE = {}   # <— aggiunto: cache indicatori per sizing / trailing

LAST_BAR_SLOT = None         # id dell’ultima candela analizzata
SCAN_THIS_CYCLE = True       # flag se eseguiamo analisi completa
LAST_DYNAMIC_REFRESH = 0

def _compute_atr_and_risk(symbol: str, price: float):
    df_hist = fetch_history(symbol)
    if df_hist is None or "Close" not in df_hist.columns:
        return None
    try:
        atr_series = AverageTrueRange(
            high=df_hist["High"], low=df_hist["Low"], close=df_hist["Close"], window=ATR_WINDOW
        ).average_true_range()
        atr_val = float(atr_series.iloc[-1])
        if atr_val <= 0:
            return None
    except Exception:
        return None
    risk_per_unit = max(atr_val * SL_ATR_MULT, price * MIN_SL_PCT)
    sl = price - risk_per_unit
    tp = price + risk_per_unit * TP_R_MULT
    return {
        "risk_per_unit": risk_per_unit,
        "sl": sl,
        "initial_sl": sl,
        "tp": tp
    }

def sync_positions_from_wallet():
    """
    Sincronizza saldi già presenti e assegna subito risk/SL/TP.
    """
    synced = []
    for sym in ASSETS:
        if sym == "USDT":
            continue
        qty = get_free_qty(sym)
        if qty <= 0:
            continue
        price = get_last_price(sym)
        if not price:
            continue
        pack = _compute_atr_and_risk(sym, price)
        if not pack:
            # fallback 1% se ATR non disponibile
            fallback_risk = price * MIN_SL_PCT
            pack = {
                "risk_per_unit": fallback_risk,
                "sl": price - fallback_risk,
                "initial_sl": price - fallback_risk,
                "tp": price + fallback_risk * TP_R_MULT
            }
        position_data[sym] = {
            "entry_price": price,
            "tp": pack["tp"],
            "sl": pack["sl"],
            "initial_sl": pack["initial_sl"],
            "risk_per_unit": pack["risk_per_unit"],
            "entry_cost": qty * price,
            "qty": qty,
            "entry_time": time.time(),
            "trailing_active": False,
            "p_max": price,
            "mfe": 0.0,
            "mae": 0.0,
            "used_risk": pack["risk_per_unit"] * qty,
            "synced": True
        }
        open_positions.add(sym)
        synced.append(f"{sym}:{qty}")
    if synced:
        log(f"[SYNC] Posizioni iniziali registrate (con SL/TP): {', '.join(synced)}")
    else:
        log("[SYNC] Nessuna posizione iniziale da registrare")

def retrofit_missing_risk():
    updated = []
    for sym, data in list(position_data.items()):
        if not data.get("risk_per_unit"):
            price = get_last_price(sym) or data.get("entry_price")
            if not price:
                continue
            pack = _compute_atr_and_risk(sym, price)
            if not pack:
                continue
            data["risk_per_unit"] = pack["risk_per_unit"]
            data["sl"] = pack["sl"]
            data["initial_sl"] = pack["initial_sl"]
            data["tp"] = pack["tp"]
            data["p_max"] = price
            data["used_risk"] = pack["risk_per_unit"] * data.get("qty", 0)
            data["synced"] = True
            updated.append(sym)
    if updated:
        log(f"[RETROFIT] Aggiornate posizioni senza risk: {updated}")

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

import gspread
from google.oauth2.service_account import Credentials

# Config
SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"  # copia da URL: https://docs.google.com/spreadsheets/d/<QUESTO>/edit
SHEET_NAME = "Foglio1"  # o quello che hai scelto

# Setup una sola volta
def setup_gspread():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("gspread-creds.json", scopes=scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)

# Salva una riga nel foglio
def log_trade_to_google(symbol, entry, exit, pnl_pct, strategy, result_type):
    try:
        import base64

        SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"
        SHEET_NAME = "Foglio1"

        # Decodifica la variabile base64 in file temporaneo
        encoded = os.getenv("GSPREAD_CREDS_B64")
        if not encoded:
            log("❌ Variabile GSPREAD_CREDS_B64 non trovata")
            return

        creds_path = "/tmp/gspread-creds.json"
        with open(creds_path, "wb") as f:
            f.write(base64.b64decode(encoded))

        scope = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
        sheet.append_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            round(entry, 6),
            round(exit, 6),
            f"{pnl_pct:.2f}%",
            strategy,
            result_type
        ])
    except Exception as e:
        log(f"❌ Errore log su Google Sheets: {e}")

sync_positions_from_wallet()

while True:
    retrofit_missing_risk()
    # Refresh dinamico lista asset
    if USE_DYNAMIC_ASSET_LIST:
        now = time.time()
        if now - LAST_DYNAMIC_REFRESH >= DYNAMIC_REFRESH_MIN * 60:
            prev_set = set(ASSETS)
            dyn_assets, dyn_volatile = update_dynamic_assets()
            if dyn_assets:
                preserve = [s for s in open_positions if s not in dyn_assets]
                if preserve:
                    log(f"[DYN][PRESERVE] Mantengo asset con posizioni aperte: {preserve}")
                ASSETS = dyn_assets + preserve
                added = set(dyn_assets) - prev_set
                removed = prev_set - set(dyn_assets)
                if added:
                    log(f"[DYN][ADDED] {list(added)[:8]}")
                if removed:
                    log(f"[DYN][REMOVED] {list(removed)[:8]}")
                VOLATILE_ASSETS = dyn_volatile
                LAST_DYNAMIC_REFRESH = now
                log(f"[DYN][ACTIVE] ASSETS={len(ASSETS)} VOLATILE={len(VOLATILE_ASSETS)}")
                
    # Determina slot candela corrente (inizio minuto relativo al frame 15m)
    slot = int(time.time() // (INTERVAL_MINUTES * 60))
    if LAST_BAR_SLOT is None or slot > LAST_BAR_SLOT:
        SCAN_THIS_CYCLE = True
        LAST_BAR_SLOT = slot
        log(f"[BAR-NEW] Nuova candela 15m slot={slot}")
    else:
        SCAN_THIS_CYCLE = False

    _support_cache = {}
    new_positions_this_cycle = 0

    for symbol in ASSETS:
        # Skip simboli non supportati (testnet issue)
        if symbol not in _support_cache:
            _support_cache[symbol] = is_symbol_supported(symbol)
        if not _support_cache[symbol]:
            log(f"[SKIP][{symbol}] Non supportato (testnet)")
            continue

        if new_positions_this_cycle >= MAX_NEW_POSITIONS_PER_CYCLE:
            break
        if SCAN_THIS_CYCLE:
            signal, strategy, price = analyze_asset(symbol)
        else:
            signal, strategy, price = (None, None, None)
        if SCAN_THIS_CYCLE:
            log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ❌ Filtra segnali nulli
        if signal is None or strategy is None or price is None:
            continue

        # ✅ ENTRATA
        if signal == "entry":
            # Se esiste saldo non registrato → registra e salta nuovo acquisto
            existing_qty = get_free_qty(symbol)
            if existing_qty > 0 and symbol not in open_positions:
                log(f"[HAVE_BALANCE][{symbol}] Saldo già presente ({existing_qty}) → registro posizione senza comprare.")
                position_data[symbol] = {
                    "entry_price": price,
                    "tp": None,
                    "sl": None,
                    "initial_sl": None,
                    "risk_per_unit": None,
                    "entry_cost": existing_qty * price,
                    "qty": existing_qty,
                    "entry_time": time.time(),
                    "trailing_active": False,
                    "p_max": price,
                    "mfe": 0.0,
                    "mae": 0.0,
                    "used_risk": 0.0
                }
                open_positions.add(symbol)
                continue

            # Blocca piramidazione: se già in open_positions skip
            if symbol in open_positions:
                log(f"[PYRAMID BLOCK][{symbol}] Già in posizione → skip nuovo ingresso.")
                continue

            # Cooldown
            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if elapsed < COOLDOWN_MINUTES * 60:
                    log(f"⏳ Cooldown attivo per {symbol} ({elapsed:.0f}s), salto ingresso")
                    continue

            usdt_balance = get_usdt_balance()
            if usdt_balance < MIN_BALANCE_USDT:
                if not ALLOW_SUB_MIN_BALANCE_ENTRY:
                    log(f"💸 Saldo USDT insufficiente ({usdt_balance:.2f} < {MIN_BALANCE_USDT}) per {symbol}")
                    continue
                info_tmp = get_instrument_info(symbol)
                min_amt = info_tmp.get("min_order_amt", 5)
                if usdt_balance < min_amt:
                    log(f"💸 Saldo insufficiente (< min_order_amt {min_amt}) per {symbol}")
                    continue
                log(f"[SUB-MIN BAL] Ingresso ridotto consentito {symbol} saldo={usdt_balance:.2f} min_amt={min_amt}")

            # Limite posizioni aperte
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                log(f"🚫 Limite posizioni raggiunto ({MAX_OPEN_POSITIONS}), salto {symbol}")
                continue

            # === POSITION SIZING A RISCHIO FISSO (con gestione large cap) ===
            cache = ANALYSIS_CACHE.get(symbol)
            if not cache:
                log(f"❌ Cache analisi mancante per {symbol} (no ATR), salto")
                continue
            atr_val = cache["atr_val"]
            if atr_val <= 0:
                log(f"❌ ATR cache ≤0 per {symbol}")
                continue
            if time.time() - cache["ts"] > 120:
                log(f"[STALE-CACHE][{symbol}] Dati analisi vecchi >120s, salto")
                continue
            
            live_price = get_last_price(symbol)
            if live_price and ENFORCE_DIVERGENCE_CHECK:
                divergence = abs(live_price - price) / price if price else 0
                if divergence > DIVERGENCE_MAX_PCT:
                    log(f"[DIVERGENZA][{symbol}] Close={price:.6f} Ticker={live_price:.6f} Δ={divergence:.2%} → salto ingresso")
                    continue
            if not live_price:
                log(f"❌ Prezzo non disponibile per sizing {symbol}")
                continue

            risk_per_unit = atr_val * SL_ATR_MULT
            min_risk_abs = live_price * MIN_SL_PCT
            if risk_per_unit < min_risk_abs:
                log(f"[RISK][{symbol}] risk_per_unit {risk_per_unit:.6f} troppo basso → forzato a {min_risk_abs:.6f}")
                risk_per_unit = min_risk_abs

            equity = get_usdt_balance()
            risk_capital = equity * RISK_PCT
            qty_risk = risk_capital / risk_per_unit

            info = get_instrument_info(symbol)
            qty_step = info.get("qty_step", 0.0001)
            min_qty = info.get("min_qty", 0.0)
            min_order_amt = info.get("min_order_amt", 5)

            step_dec = Decimal(str(qty_step))
            qty_dec = Decimal(str(qty_risk))
            qty_adj = (qty_dec // step_dec) * step_dec
            if qty_adj < Decimal(str(min_qty)):
                qty_adj = Decimal(str(min_qty))

            order_amount = float(qty_adj) * live_price

            # Limite sicurezza: non usare oltre SAFETY_AVAILABLE_PCT del balance disponibile
            avail_cap = get_usdt_balance() * SAFETY_AVAILABLE_PCT
            if order_amount > avail_cap:
                safe_qty = Decimal(str(avail_cap / live_price))
                safe_qty = (safe_qty // step_dec) * step_dec
                if safe_qty > 0 and safe_qty < qty_adj:
                    log(f"[SAFETY-SIZE][{symbol}] Ridimensiono notional {order_amount:.2f}→{float(safe_qty)*live_price:.2f}")
                    qty_adj = safe_qty
                    order_amount = float(qty_adj) * live_price

            # Notional minimo reale (considera min_qty*price)
            min_notional_required = max(min_order_amt, min_qty * live_price)

            # CAP notional (strength & globale)
            strength = STRATEGY_STRENGTH.get(strategy, 0.5)
            cap_strength = equity * strength
            cap_global = 250.0
            max_notional = min(cap_strength, cap_global, equity)
            if order_amount > max_notional:
                qty_adj = Decimal(str(max_notional / live_price))
                qty_adj = (qty_adj // step_dec) * step_dec
                order_amount = float(qty_adj) * live_price

            # Adeguamento al notional minimo (prima pass)
            if order_amount < min_notional_required:
                needed_qty = Decimal(str(min_notional_required / live_price))
                needed_qty = (needed_qty // step_dec) * step_dec
                if needed_qty > qty_adj:
                    qty_adj = needed_qty
                    order_amount = float(qty_adj) * live_price
                    risk_capital = float(qty_adj) * risk_per_unit
                    log(f"⚠️ Adeguo {symbol} a min_notional {order_amount:.2f} (risk_cap {risk_capital:.2f})")

            # Large cap: se ancora vicino al limite spingo a un 10% sopra
            if symbol in LARGE_ASSETS and order_amount < min_notional_required * LARGE_CAP_MIN_NOTIONAL_MULT:
                bump_notional = min_notional_required * LARGE_CAP_MIN_NOTIONAL_MULT
                bump_qty = Decimal(str(bump_notional / live_price))
                bump_qty = (bump_qty // step_dec) * step_dec
                if bump_qty > qty_adj:
                    qty_adj = bump_qty
                    order_amount = float(qty_adj) * live_price
                    risk_capital = float(qty_adj) * risk_per_unit
                    log(f"[LARGE][{symbol}] Bump notional → {order_amount:.2f} (min_req {min_notional_required:.2f})")

            if order_amount < min_notional_required:
                log(f"❌ Notional < required ({order_amount:.2f} < {min_notional_required:.2f}) {symbol}")
                continue
            if float(qty_adj) <= 0:
                log(f"❌ Qty finale nulla per {symbol}")
                continue

            if TEST_MODE:
                log(f"[TEST_MODE] (NO BUY) {symbol} qty={qty_adj} notional={order_amount:.2f}")
                continue

            # Scelta + esecuzione unificata (robusta)
            prefer_limit = (symbol in LARGE_ASSETS) or (live_price >= 100)
            log(f"[SIZE][{symbol}] qty_adj={qty_adj} order_amount={order_amount:.2f} min_req={min_notional_required:.2f} prefer_limit={prefer_limit}")
            pre_qty = get_free_qty(symbol)
            buy_ok = execute_buy_order(symbol, qty_adj, prefer_limit=prefer_limit, slippage=LARGE_CAP_LIMIT_SLIPPAGE)

            if not buy_ok:
                log(f"❌ Acquisto fallito per {symbol} (dopo retry interno)")
                continue

            time.sleep(2)
            post_qty = get_free_qty(symbol)
            qty_filled = max(0.0, post_qty - pre_qty)
            theoretical = float(qty_adj)
            if qty_filled <= 0:
                qty_filled = theoretical
                log(f"[FILL][{symbol}] Delta saldo zero → uso qty teorica {theoretical}")
            fill_ratio = qty_filled / theoretical if theoretical > 0 else 0
            if fill_ratio < 0.5:
                log(f"[WARN][{symbol}] Partial fill stimato {fill_ratio:.0%} → forza qty teorica")
                qty_filled = theoretical

            if qty_filled <= 0:
                log(f"❌ Nessuna quantità risultante per {symbol}")
                continue

            entry_price = live_price
            sl = entry_price - risk_per_unit
            tp = entry_price + (risk_per_unit * TP_R_MULT)
            actual_cost = entry_price * qty_filled
            used_risk = risk_per_unit * qty_filled
            if equity and (used_risk / equity) > (RISK_PCT * 1.25):
                log(f"[RISK-WARN][{symbol}] UsedRisk {used_risk:.2f} supera {RISK_PCT*100:.2f}% equity (adeguamento min_notional)")

            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "initial_sl": sl,
                "risk_per_unit": risk_per_unit,
                "entry_cost": actual_cost,
                "qty": qty_filled,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": entry_price,
                "mfe": 0.0,
                "mae": 0.0,
                "used_risk": used_risk
            }
            open_positions.add(symbol)
            new_positions_this_cycle += 1
            risk_pct_eff = (used_risk / equity) * 100 if equity else 0
            log(f"🟢 Acquisto {symbol} | Qty {qty_filled:.8f} | Entry {entry_price:.6f} | SL {sl:.6f} | TP {tp:.6f} | R/unit {risk_per_unit:.6f} | Notional {actual_cost:.2f} | UsedRisk {used_risk:.2f} ({risk_pct_eff:.2f}%)")
            notify_telegram(f"🟢📈 Acquisto {symbol}\nQty: {qty_filled:.6f}\nPrezzo: {entry_price:.6f}\nStrategia: {strategy}\nSL: {sl:.6f}\nTP: {tp:.6f}")

        # 🔴 USCITA (EXIT)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            qty = entry.get("qty", get_free_qty(symbol))
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", entry_price * qty)
            risk_per_unit = entry.get("risk_per_unit", None)
            mfe = entry.get("mfe", 0.0)
            mae = entry.get("mae", 0.0)

            # Tempo minimo in posizione (esenta posizioni sincronizzate se flag attivo)
            holding_sec = time.time() - entry.get("entry_time", 0)
            if not (entry.get("synced") and SYNC_BACKFILL_HOLDING_EXEMPT):
                if holding_sec < MIN_HOLDING_MINUTES * 60:
                    remain = (MIN_HOLDING_MINUTES * 60 - holding_sec) / 60
                    log(f"[HOLD][{symbol}] Exit ignorata (holding {holding_sec/60:.1f}m < {MIN_HOLDING_MINUTES}m, restano {remain:.1f}m)")
                    continue

            latest_before = get_last_price(symbol)
            if latest_before:
                price = round(latest_before, 6)

            resp = market_sell(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                latest_after = get_last_price(symbol)
                if latest_after:
                    price = round(latest_after, 6)

                exit_value = price * qty
                delta = exit_value - entry_cost
                pnl = (delta / entry_cost) * 100
                r_multiple = (price - entry_price) / risk_per_unit if risk_per_unit else 0

                log(f"📊 EXIT {symbol} PnL: {pnl:.2f}% | R={r_multiple:.2f} | MFE={mfe:.2f}R | MAE={mae:.2f}R")
                notify_telegram(
                    f"🔴📉 Vendita {symbol} @ {price:.6f}\nPnL: {pnl:.2f}% | R={r_multiple:.2f}\nMFE={mfe:.2f}R MAE={mae:.2f}R"
                )
                log_trade_to_google(
                    symbol,
                    entry_price,
                    price,
                    pnl,
                    f"{strategy} | R={r_multiple:.2f} | MFE={mfe:.2f} | MAE={mae:.2f}",
                    "Exit Signal"
                )
                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
                position_data.pop(symbol, None)
            else:
                log(f"❌ Vendita fallita per {symbol}")

    time.sleep(1)

    # 🔁 Controllo Trailing Stop per le posizioni aperte
    for symbol in list(open_positions):
        if symbol not in position_data:
            continue

        entry = position_data[symbol]
        current_price = get_last_price(symbol)
        if not current_price:
            continue

        risk = entry.get("risk_per_unit")
        if not risk or risk <= 0:
            continue

        entry_price = entry["entry_price"]

        # Aggiorna MFE / MAE (in R)
        r_current = (current_price - entry_price) / risk
        if r_current > entry["mfe"]:
            entry["mfe"] = r_current
        if r_current < entry["mae"]:
            entry["mae"] = r_current
        
        # Early Exit (momentum deteriora prima di trailing/giveback)
        if EARLY_EXIT_ENABLE and entry["mfe"] >= EARLY_EXIT_MIN_R and r_current > 0:
            cache_ind = ANALYSIS_CACHE.get(symbol)
            if cache_ind and (time.time() - cache_ind["ts"] < INTERVAL_MINUTES * 60 + 30):
                macd_val = cache_ind["macd"]
                macd_sig = cache_ind["macd_signal"]
                rsi_val = cache_ind["rsi"]
                ema20_val = cache_ind["ema20"]
                hist = macd_val - macd_sig
                cond_macd_flip = hist <= 0
                cond_rsi_drop = (rsi_val < EARLY_EXIT_RSIFALL) and (entry["mfe"] >= 1.0)
                cond_ema_fail = (current_price < ema20_val) if EARLY_EXIT_REQUIRE_EMA20 else True
                if (cond_macd_flip and cond_ema_fail) or (cond_rsi_drop and cond_macd_flip):
                    qty = get_free_qty(symbol)
                    if qty > 0:
                        resp = market_sell(symbol, qty)
                        if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                            fill_price = get_last_price(symbol) or current_price
                            pnl_val = (fill_price - entry_price) * qty
                            pnl_pct = (pnl_val / entry["entry_cost"]) * 100
                            r_mult = (fill_price - entry_price) / risk
                            log(f"⚡ Early Exit {symbol} @ {fill_price:.6f} | R={r_mult:.2f} | MFE={entry['mfe']:.2f}R | MACD flip | RSI={rsi_val:.1f}")
                            notify_telegram(f"⚡ Early Exit {symbol} @ {fill_price:.6f}\nR={r_mult:.2f} MFE={entry['mfe']:.2f}R\nRSI={rsi_val:.1f} MACD flip")
                            log_trade_to_google(symbol, entry_price, fill_price, pnl_pct,
                                                f"EarlyExit | MFE={entry['mfe']:.2f}R | MACDflip",
                                                "Early Exit")
                            open_positions.discard(symbol)
                            last_exit_time[symbol] = time.time()
                            position_data.pop(symbol, None)
                            continue

        # Giveback exit: se forte ritracciamento dal massimo favorevole
        if ENABLE_GIVEBACK_EXIT:
            if entry["mfe"] >= GIVEBACK_MIN_MFE_R:
                giveback = entry["mfe"] - r_current
                if giveback >= GIVEBACK_DROP_R and r_current > 0:
                    qty = get_free_qty(symbol)
                    if qty > 0:
                        resp = market_sell(symbol, qty)
                        if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                            fill_price = get_last_price(symbol) or current_price
                            pnl_val = (fill_price - entry_price) * qty
                            pnl_pct = (pnl_val / entry["entry_cost"]) * 100
                            r_mult = (fill_price - entry_price) / risk
                            log(f"↩️ Giveback Exit {symbol} @ {fill_price:.6f} | Drop {giveback:.2f}R | R={r_mult:.2f} | MFE={entry['mfe']:.2f}R")
                            notify_telegram(f"↩️ Giveback Exit {symbol} @ {fill_price:.6f}\nDrop {giveback:.2f}R | R={r_mult:.2f}\nMFE={entry['mfe']:.2f}R")
                            log_trade_to_google(symbol, entry_price, fill_price, pnl_pct,
                                                f"Giveback | MFE={entry['mfe']:.2f}R Drop={giveback:.2f}R",
                                                "Giveback Exit")
                            open_positions.discard(symbol)
                            last_exit_time[symbol] = time.time()
                            position_data.pop(symbol, None)
                            continue

        # TP hard
        if current_price >= entry["tp"]:
            qty = get_free_qty(symbol)
            if qty > 0:
                resp = market_sell(symbol, qty)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    fill_price = get_last_price(symbol) or current_price
                    pnl_val = (fill_price - entry_price) * qty
                    pnl_pct = (pnl_val / entry["entry_cost"]) * 100
                    r_mult = (fill_price - entry_price) / risk
                    log(f"🎯 TP {symbol} @ {fill_price:.6f} | PnL {pnl_pct:.2f}% | R={r_mult:.2f} | MFE={entry['mfe']:.2f}R | MAE={entry['mae']:.2f}R")
                    notify_telegram(f"🎯 TP {symbol} @ {fill_price:.6f}\nPnL: {pnl_pct:.2f}% | R={r_mult:.2f}\nMFE={entry['mfe']:.2f}R MAE={entry['mae']:.2f}R")
                    log_trade_to_google(symbol, entry_price, fill_price, pnl_pct, f"TP | R={r_mult:.2f} | MFE={entry['mfe']:.2f} | MAE={entry['mae']:.2f}", "TP Hit")
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
                    continue
                else:
                    log(f"❌ Vendita TP fallita per {symbol}")

        # Gestione trailing
        if not TRAILING_ENABLED:
            continue

        holding_sec = time.time() - entry.get("entry_time", 0)

        # Attiva trailing solo dopo condizioni:
        # - profit ≥ TRAILING_ACTIVATION_R
        # - tempo minimo rispettato
        if (
            not entry["trailing_active"]
            and r_current >= TRAILING_ACTIVATION_R
            and (
                (entry.get("synced") and SYNC_BACKFILL_HOLDING_EXEMPT)
                or (time.time() - entry.get("entry_time", 0)) >= MIN_HOLDING_MINUTES * 60
            )
        ):
            entry["trailing_active"] = True
            locked_sl = entry_price + (risk * TRAILING_LOCK_R)
            if locked_sl > entry["sl"]:
                entry["sl"] = locked_sl
            log(f"🔛 Trailing attivo {symbol} (≥{TRAILING_ACTIVATION_R}R & hold OK) | SL lock {entry['sl']:.6f}")
            notify_telegram(f"🔛 Trailing attivo {symbol}\nSL lock {entry['sl']:.6f}")

        if entry["trailing_active"]:
            # aggiorna massimo
            if current_price > entry["p_max"]:
                entry["p_max"] = current_price

            # propone nuovo SL seguendo (TRAIL_LOCK_FACTOR * risk) sotto il massimo
            target_sl = entry["p_max"] - (risk * TRAIL_LOCK_FACTOR)
            # mai scendere sotto lock iniziale (entry + TRAILING_LOCK_R*R)
            min_lock = entry_price + (risk * TRAILING_LOCK_R)
            if target_sl < min_lock:
                target_sl = min_lock

            if target_sl > entry["sl"]:
                log(f"📉 SL trail {symbol}: {entry['sl']:.6f} → {target_sl:.6f}")
                entry["sl"] = target_sl

            # Stop colpito
            if current_price <= entry["sl"]:
                qty = get_free_qty(symbol)
                if qty > 0:
                    resp = market_sell(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        fill_price = get_last_price(symbol) or current_price
                        pnl_val = (fill_price - entry_price) * qty
                        pnl_pct = (pnl_val / entry["entry_cost"]) * 100
                        r_mult = (fill_price - entry_price) / risk
                        log(f"🔻 Trailing Stop {symbol} @ {fill_price:.6f} | PnL {pnl_pct:.2f}% | R={r_mult:.2f} | MFE={entry['mfe']:.2f}R | MAE={entry['mae']:.2f}R")
                        notify_telegram(f"🔻 Trailing Stop {symbol} @ {fill_price:.6f}\nPnL: {pnl_pct:.2f}% | R={r_mult:.2f}\nMFE={entry['mfe']:.2f}R MAE={entry['mae']:.2f}R")
                        log_trade_to_google(symbol, entry_price, fill_price, pnl_pct, f"Trailing | R={r_mult:.2f} | MFE={entry['mfe']:.2f} | MAE={entry['mae']:.2f}", "SL Triggered")
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    else:
                        log(f"❌ Vendita fallita Trailing {symbol}")

    # Sicurezza: attesa tra i cicli principali
    # Aggiungi pausa di sicurezza per evitare ciclo troppo veloce se tutto salta
    log(f"[CYCLE] Completato ciclo. Posizioni aperte: {len(open_positions)}")
    time.sleep(60)  # ciclo ogni 60s; segnali su base 15m restano validi