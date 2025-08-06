from typing import Optional
import os
import time
import hmac
import json
import hashlib
from decimal import Decimal, ROUND_DOWN
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

ORDER_USDT = 50.0

# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
LIQUIDITY_MIN_VOLUME = 1_000_000  # Soglia minima volume 24h USDT (consigliato)

# --- BLACKLIST STABLECOIN ---
STABLECOIN_BLACKLIST = [
    "USDCUSDT", "USDEUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "EURUSDT", "USDTUSDT"
]

def update_assets(top_n=18, n_stable=7):
    """
    Aggiorna ASSETS, LESS_VOLATILE_ASSETS e VOLATILE_ASSETS:
    - Prende i top N asset spot per volume 24h USDT
    - I primi n_stable per market cap (BTC, ETH, ... se presenti) sono i meno volatili
    - Gli altri sono considerati volatili
    """
    global ASSETS, LESS_VOLATILE_ASSETS, VOLATILE_ASSETS
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "spot"}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"[ASSETS] Errore API tickers: {data}")
            return
        tickers = data["result"]["list"]
        # Filtra solo coppie USDT, con volume sufficiente, ed esclude le stablecoin
        usdt_tickers = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and float(t.get("turnover24h", 0)) >= LIQUIDITY_MIN_VOLUME
            and t["symbol"] not in STABLECOIN_BLACKLIST
        ]
        # Ordina per volume 24h (turnover24h)
        usdt_tickers.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        # Prendi i top N
        top = usdt_tickers[:top_n]
        ASSETS = [t["symbol"] for t in top]
        # Ordina per market cap stimata (lastPrice * totalVolume)
        top.sort(key=lambda x: float(x.get("lastPrice", 0)) * float(x.get("totalVolume", 0)), reverse=True)
        LESS_VOLATILE_ASSETS = [t["symbol"] for t in top[:n_stable]]
        VOLATILE_ASSETS = [s for s in ASSETS if s not in LESS_VOLATILE_ASSETS]
        log(f"[ASSETS] Aggiornati: {ASSETS}\nMeno volatili: {LESS_VOLATILE_ASSETS}\nVolatili: {VOLATILE_ASSETS}")
    except Exception as e:
        log(f"[ASSETS] Errore aggiornamento lista asset: {e}")

INTERVAL_MINUTES = 15
ATR_WINDOW = 14
TP_FACTOR = 2.0
SL_FACTOR = 1.5
# Soglie dinamiche consigliate
TP_MIN = 1.5
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.5
TRAILING_MIN = 0.005  # 0.5%
TRAILING_MAX = 0.03   # 3%
TRAILING_ACTIVATION_THRESHOLD = 0.02
TRAILING_SL_BUFFER = 0.007
TRAILING_DISTANCE = 0.02
INITIAL_STOP_LOSS_PCT = 0.02
COOLDOWN_MINUTES = 60
cooldown = {}

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

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
    log(f"[DECIMALI][FORMAT_QTY] qty={qty} | qty_step={qty_step} | precision={precision} | floored_qty={floored_qty} | quantize_str={quantize_str}")
    return fmt.format(floored_qty)

def get_open_short_qty(symbol):
    """
    Restituisce la quantità short aperta su Bybit futures per il simbolo dato.
    Se la posizione è short (side=Sell), restituisce la quantità assoluta (>0), altrimenti 0.
    """
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
        params = {"category": "linear", "symbol": symbol}
        ts = str(int(time.time() * 1000))
        sign_payload = f"{ts}{KEY}5000"
        sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000"
        }
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            return 0.0
        for pos in data["result"]["list"]:
            # Su Bybit, una posizione short ha "side": "Sell" e "size" > 0
            if pos.get("side") == "Sell":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        log(f"❌ Errore get_open_short_qty per {symbol}: {e}")
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

def get_instrument_info(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "linear", "symbol": symbol}  # PATCH: era "spot"
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            info = data["result"]["list"][0]
            qty_step = float(info.get("lotSizeFilter", {}).get("qtyStep", 0.0001))
            price_step = float(info.get("priceFilter", {}).get("tickSize", 0.0001))
            precision = int(info.get("basePrecision", 4))
            min_order_amt = float(info.get("minOrderAmt", 5))
            min_qty = float(info.get("lotSizeFilter", {}).get("minOrderQty", 0.0))
            return {
                "qty_step": qty_step,
                "price_step": price_step,
                "precision": precision,
                "min_order_amt": min_order_amt,
                "min_qty": min_qty
            }
        else:
            log(f"[BYBIT] Errore get_instrument_info {symbol}: {data}")
            return {"qty_step": 0.0001, "precision": 4, "min_order_amt": 5, "min_qty": 0.0}
    except Exception as e:
        log(f"[BYBIT] Errore get_instrument_info {symbol}: {e}")
        return {"qty_step": 0.0001, "precision": 4, "min_order_amt": 5, "min_qty": 0.0}

def get_free_qty(symbol):
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
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()

        if "result" not in data or "list" not in data["result"]:
            log(f"❗ Struttura inattesa da Bybit per {symbol}: {resp.text}")
            return 0.0

        coin_list = data["result"]["list"][0].get("coin", [])
        for c in coin_list:
            if c["coin"] == coin:
                raw = c.get("walletBalance", "0")
                try:
                    qty = float(raw) if raw else 0.0
                    if qty > 0:
                        log(f"📦 Saldo trovato per {coin}: {qty}")
                    else:
                        log(f"🟡 Nessun saldo disponibile per {coin}")
                    return qty
                except Exception as e:
                    log(f"⚠️ Errore conversione quantità {coin}: {e}")
                    return 0.0

        log(f"🔍 Coin {coin} non trovata nel saldo.")
        return 0.0

    except Exception as e:
        log(f"❌ Errore nel recupero saldo per {symbol}: {e}")
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
    if usdt_amount < min_order_amt:
        log(f"❌ Budget troppo basso per {symbol}: {usdt_amount:.2f} < min_order_amt {min_order_amt}")
        return None
    try:
        raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
        log(f"[DECIMALI][CALC_QTY] {symbol} | usdt_amount={usdt_amount} | price={price} | raw_qty={raw_qty} | qty_step={qty_step} | precision={precision}")
        qty_str = format_quantity_bybit(float(raw_qty), float(qty_step), precision=precision)
        qty_dec = Decimal(qty_str)
        min_qty_dec = Decimal(str(min_qty))
        if qty_dec < min_qty_dec:
            log(f"[DECIMALI][CALC_QTY] {symbol} | qty_dec < min_qty_dec: {qty_dec} < {min_qty_dec}")
            qty_dec = min_qty_dec
            qty_str = format_quantity_bybit(float(qty_dec), float(qty_step), precision=precision)
        order_value = qty_dec * Decimal(str(price))
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
    safe_usdt_amount = usdt_amount * 0.98
    qty_str = calculate_quantity(symbol, safe_usdt_amount)
    if not qty_str:
        log(f"❌ Quantità non valida per short di {symbol}")
        return None
    info = get_instrument_info(symbol)
    min_qty = info.get("min_qty", 0.0)
    min_order_amt = info.get("min_order_amt", 5)
    precision = info.get("precision", 4)
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell",  # PATCH: apertura short
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
    response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
    log(f"SHORT BODY: {body_json}")
    try:
        resp_json = response.json()
    except Exception:
        resp_json = {}
    log(f"RESPONSE: {response.status_code} {resp_json}")
    # PATCH: restituisci la quantità effettiva shortata se l'ordine è OK
    if resp_json.get("retCode") == 0:
        # Bybit non restituisce sempre la qty eseguita, quindi usa qty_str come fallback
        return float(qty_str)
    else:
        return None

def market_cover(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile ricoprire")
        return
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    precision = info.get("precision", 4)
    qty_str = format_quantity_bybit(float(qty), float(qty_step), precision=precision)
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Buy",  # PATCH: chiusura short
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
    response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
    log(f"COVER BODY: {body_json}")
    try:
        resp_json = response.json()
    except Exception:
        resp_json = {}
    log(f"RESPONSE: {response.status_code} {resp_json}")
    return response

def fetch_history(symbol: str, interval=15, limit=300):  # <-- aumenta il limit
    """
    Scarica la cronologia dei prezzi per il simbolo dato da Bybit (linear/futures).
    """
    try:# ✅ ENTRATA SHORT
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
    
# 4. Inverti la logica di ingresso/uscita in analyze_asset
def analyze_asset(symbol: str):
    try:
        df = fetch_history(symbol)
        if df is None or len(df) < 3:
            log(f"[ANALYZE] Dati storici insufficienti per {symbol} (df is None o len < 3)")
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

        df.dropna(subset=[
            "bb_upper", "bb_lower", "rsi", "sma20", "sma50", "ema20", "ema50", "ema200",
            "macd", "macd_signal", "adx", "atr"
        ], inplace=True)
        log(f"[ANALYZE-DF] {symbol} | Dopo dropna, len={len(df)}")

        if len(df) < 3:
            log(f"[ANALYZE] Dati storici insufficienti dopo dropna per {symbol} (len < 3)")
            return None, None, None

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = 15 if is_volatile else 10

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])

        # PATCH: filtro trend più rigoroso (solo se EMA50 < EMA200)
        if last["ema50"] >= last["ema200"]:
            log(f"[STRATEGY][{symbol}] Filtro trend NON superato (EMA50 >= EMA200): ema50={last['ema50']:.4f} >= ema200={last['ema200']:.4f}")
            return None, None, None

        # --- SHORT: almeno 2 condizioni ribassiste devono essere vere ---
        entry_conditions = []
        entry_strategies = []
        if is_volatile:
            # SHORT: breakdown BB, incrocio SMA ribassista, MACD bearish
            cond1 = last["Close"] < last["bb_lower"]
            cond2 = last["rsi"] < 50  # PATCH: RSI sotto 50, più ribassista
            if cond1 and cond2:
                entry_conditions.append(True)
                entry_strategies.append("Breakdown Bollinger")
            cond3 = prev["sma20"] > prev["sma50"]
            cond4 = last["sma20"] < last["sma50"]
            if cond3 and cond4:
                entry_conditions.append(True)
                entry_strategies.append("Incrocio SMA 20/50 (bearish)")
            cond5 = last["macd"] < last["macd_signal"]
            cond6 = last["adx"] > adx_threshold
            if cond5 and cond6:
                entry_conditions.append(True)
                entry_strategies.append("MACD bearish + ADX")
        else:
            # SHORT: incrocio EMA ribassista, MACD bearish, trend EMA+RSI ribassista
            cond1 = prev["ema20"] > prev["ema50"]
            cond2 = last["ema20"] < last["ema50"]
            if cond1 and cond2:
                entry_conditions.append(True)
                entry_strategies.append("Incrocio EMA 20/50 (bearish)")
            cond3 = last["macd"] < last["macd_signal"]
            cond4 = last["adx"] > adx_threshold
            if cond3 and cond4:
                entry_conditions.append(True)
                entry_strategies.append("MACD bearish (stabile)")
            cond5 = last["rsi"] < 45  # PATCH: RSI sotto 45, più ribassista
            cond6 = last["ema20"] < last["ema50"]
            if cond5 and cond6:
                entry_conditions.append(True)
                entry_strategies.append("Trend EMA + RSI (bearish)")

        # PATCH: almeno 2 condizioni ribassiste vere per entrare short
        if len(entry_conditions) >= 2:
            log(f"[STRATEGY][{symbol}] Segnale ENTRY SHORT generato: strategie attive: {entry_strategies}")
            return "entry", ", ".join(entry_strategies), price
        else:
            log(f"[STRATEGY][{symbol}] Nessun segnale ENTRY SHORT: condizioni soddisfatte = {len(entry_conditions)}")

        # --- EXIT SHORT: almeno una condizione bullish ---
        cond_exit1 = last["Close"] > last["bb_upper"]
        cond_exit2 = last["rsi"] > 60  # PATCH: RSI sopra 60, più bullish
        if cond_exit1 and cond_exit2:
            log(f"[STRATEGY][{symbol}] Segnale EXIT SHORT: Rimbalzo RSI + BB (bullish)")
            return "exit", "Rimbalzo RSI + BB (bullish)", price
        cond_exit3 = last["macd"] > last["macd_signal"]
        cond_exit4 = last["adx"] > adx_threshold
        if cond_exit3 and cond_exit4:
            log(f"[STRATEGY][{symbol}] Segnale EXIT SHORT: MACD bullish + ADX")
            return "exit", "MACD bullish + ADX", price

        log(f"[STRATEGY][{symbol}] Nessun segnale EXIT SHORT generato")
        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

# ...existing code...

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT [SHORT] AVVIATO - In ascolto per segnali di ingresso/uscita")


TEST_MODE = False  # Acquisti e vendite normali abilitati



MIN_HOLDING_MINUTES = 1  # Tempo minimo in minuti da attendere dopo l'acquisto prima di poter attivare uno stop loss
# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}

def sync_positions_from_wallet():
    log("[SYNC] Avvio scansione posizioni short dal wallet...")
    trovate = 0
    for symbol in ASSETS:
        if symbol == "USDT":
            continue
        # PATCH: log dettagliato per ogni asset
        qty = get_open_short_qty(symbol)
        log(f"[SYNC-DEBUG] {symbol}: qty short trovata = {qty}")
        if qty and qty > 0:
            price = get_last_price(symbol)
            if not price:
                continue
            open_positions.add(symbol)
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
            sl = price + (atr_val * SL_FACTOR)
            min_sl = price * 1.01
            if sl < min_sl:
                sl = min_sl
            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_min": price
            }
            trovate += 1
            log(f"[SYNC] Posizione trovata: {symbol} qty={qty} entry={entry_price:.4f} SL={sl:.4f} TP={tp:.4f}")
    log(f"[SYNC] Totale posizioni short recuperate dal wallet: {trovate}")

# --- Esegui sync all'avvio ---

# Aggiorna la lista asset all'avvio
update_assets()
sync_positions_from_wallet()

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

def calculate_stop_loss(entry_price, current_price, p_max, trailing_active):
    if not trailing_active:
        return entry_price * (1 - INITIAL_STOP_LOSS_PCT)
    else:
        return p_max * (1 - TRAILING_DISTANCE)


import threading
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
def log_trade_to_google(symbol, entry, exit, pnl_pct, strategy, result_type, usdt_enter=None, usdt_exit=None, delta_usd=None):
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
            result_type,
            usdt_enter if usdt_enter is not None else "",
            usdt_exit if usdt_exit is not None else "",
            delta_usd if delta_usd is not None else ""
        ])
    except Exception as e:
        log(f"❌ Errore log su Google Sheets: {e}")



# --- LOGICA 70/30 SU VALORE TOTALE PORTAFOGLIO (USDT + coin) ---
def get_portfolio_value():
    usdt_balance = get_usdt_balance()
    total = usdt_balance
    coin_values = {}
    for symbol in ASSETS:
        if symbol == "USDT":
            continue
        qty = get_free_qty(symbol)
        if qty and qty > 0:
            price = get_last_price(symbol)
            if price:
                value = qty * price
                coin_values[symbol] = value
                total += value
    return total, usdt_balance, coin_values

low_balance_alerted = False  # Deve essere fuori dal ciclo per persistere tra i cicli
def trailing_stop_worker():
    log("[DEBUG] Avvio ciclo trailing_stop_worker")
    while True:
        for symbol in list(open_positions):
            log(f"[DEBUG] Worker processa: {symbol} | open_positions: {open_positions} | position_data: {position_data.keys()}")
            if symbol not in position_data:
                continue
            saldo = get_open_short_qty(symbol)
            log(f"[DEBUG] Quantità short effettiva per {symbol}: {saldo}")
            if saldo is None or saldo < 1:
                log(f"[CLEANUP] {symbol}: saldo troppo basso ({saldo}), rimuovo da open_positions e position_data (polvere)")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue
            entry = position_data[symbol]
            holding_seconds = time.time() - entry.get("entry_time", 0)
            if holding_seconds < MIN_HOLDING_MINUTES * 60:
                log(f"[HOLDING][FAST] {symbol}: attendo ancora {MIN_HOLDING_MINUTES - holding_seconds/60:.1f} min prima di attivare SL/TSL")
                continue
            current_price = get_last_price(symbol)
            if not current_price:
                continue
            entry_price = entry.get("entry_price", current_price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            qty = get_open_short_qty(symbol)
            # PATCH MAX LOSS SHORT
            if qty > 0 and entry_price and current_price:
                pnl_pct = ((entry_price - current_price) / entry_price) * 100
                max_loss_pct = -2.0  # Soglia massima di perdita accettata (-2%)
                if pnl_pct < max_loss_pct:
                    log(f"🔴 [MAX LOSS] Ricopro SHORT su {symbol} per perdita superiore al {abs(max_loss_pct)}% | PnL: {pnl_pct:.2f}%")
                    notify_telegram(f"🔴 [MAX LOSS] Ricopertura SHORT su {symbol} per perdita > {abs(max_loss_pct)}%\nPnL: {pnl_pct:.2f}%")
                    resp = market_cover(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        exit_value = current_price * qty
                        delta = exit_value - entry_cost
                        log_trade_to_google(symbol, entry_price, current_price, pnl_pct, "MAX LOSS", "Forced Exit", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    continue
            if symbol in VOLATILE_ASSETS:
                trailing_threshold = 0.02
            else:
                trailing_threshold = 0.005
            soglia_attivazione = entry["entry_price"] * (1 - trailing_threshold)
            log(f"[TRAILING CHECK][SHORT] {symbol} | entry_price={entry['entry_price']:.4f} | current_price={current_price:.4f} | soglia={soglia_attivazione:.4f} | trailing_active={entry['trailing_active']} | threshold={trailing_threshold}")
            if not entry["trailing_active"] and current_price <= soglia_attivazione:
                entry["trailing_active"] = True
                log(f"🔛 Trailing Stop SHORT attivato per {symbol} sotto soglia → Prezzo: {current_price:.4f}")
                notify_telegram(f"🔛 Trailing Stop [SHORT] attivo su {symbol}\nPrezzo: {current_price:.4f}")
            if entry["trailing_active"]:
                if current_price < entry.get("p_min", entry["entry_price"]):
                    entry["p_min"] = current_price
                    new_sl = current_price * (1 + TRAILING_SL_BUFFER)
                    if new_sl < entry["sl"]:
                        log(f"📉 SL SHORT aggiornato per {symbol}: da {entry['sl']:.4f} a {new_sl:.4f}")
                        entry["sl"] = new_sl
            sl_triggered = False
            sl_type = None
            if entry["trailing_active"] and current_price >= entry["sl"]:
                sl_triggered = True
                sl_type = "Trailing Stop SHORT"
            elif not entry["trailing_active"] and current_price >= entry["sl"]:
                sl_triggered = True
                sl_type = "Stop Loss SHORT"
            if sl_triggered:
                qty = get_free_qty(symbol)
                log(f"[TEST][SL_TRIGGER] {symbol} | SL type: {sl_type} | qty: {qty} | current_price: {current_price} | SL: {entry['sl']}")
                notify_telegram(f"[TEST] SL_TRIGGER {sl_type} per {symbol}\nQty: {qty}\nPrezzo attuale: {current_price}\nSL: {entry['sl']}")
                if qty > 0:
                    usdt_before = get_usdt_balance()
                    resp = market_cover(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        entry_price = entry["entry_price"]
                        entry_cost = entry.get("entry_cost", ORDER_USDT)
                        qty = entry.get("qty", qty)
                        exit_value = current_price * qty
                        delta = exit_value - entry_cost
                        pnl = (delta / entry_cost) * 100
                        log(f"[TEST][SL_OK] {symbol} | {sl_type} attivato → Prezzo: {current_price:.4f} | SL: {entry['sl']:.4f} | PnL: {pnl:.2f}%")
                        notify_telegram(f"[TEST] {sl_type} [SHORT] ricoperto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                        log_trade_to_google(symbol, entry_price, current_price, pnl, sl_type, "SL Triggered", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    else:
                        log(f"[TEST][SL_FAIL] Ricopertura fallita con {sl_type} per {symbol}")
                        notify_telegram(f"[TEST] ❌❗️ RICOPERTURA [SHORT] NON RIUSCITA per {symbol} durante {sl_type}!")
                else:
                    log(f"[TEST][SL_FAIL] Quantità nulla o troppo piccola per ricopertura {sl_type} su {symbol}")
        time.sleep(60)

trailing_thread = threading.Thread(target=trailing_stop_worker, daemon=True)
trailing_thread.start()

while True:
    # Aggiorna la lista asset dinamicamente ogni ciclo
    update_assets()
    sync_positions_from_wallet()
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()
    volatile_budget = portfolio_value * 0.7
    stable_budget = portfolio_value * 0.3
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
    log(f"[PORTAFOGLIO] Totale: {portfolio_value:.2f} USDT | Volatili: {volatile_invested:.2f} ({perc_volatile:.1f}%) | Meno volatili: {stable_invested:.2f} ({perc_stable:.1f}%) | USDT: {usdt_balance:.2f}")

    # --- Avviso saldo basso: invia solo una volta finché non torna sopra soglia ---
    # low_balance_alerted ora è globale rispetto al ciclo

    for symbol in ASSETS:
        if symbol in STABLECOIN_BLACKLIST:
            continue
        if not is_symbol_linear(symbol):
            log(f"[SKIP] {symbol} non disponibile su futures linear, salto.")
            continue
        signal, strategy, price = analyze_asset(symbol)
        log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ❌ Filtra segnali nulli
        if signal is None or strategy is None or price is None:
            continue

        # ✅ ENTRATA SHORT
        if signal == "entry":
            # Cooldown
            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if elapsed < COOLDOWN_MINUTES * 60:
                    log(f"⏳ Cooldown attivo per {symbol} ({elapsed:.0f}s), salto ingresso")
                    continue

            if symbol in open_positions:
                log(f"⏩ Ignoro apertura short: già in posizione su {symbol}")
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

            group_available = group_budget - group_invested
            log(f"[BUDGET] {symbol} ({group_label}) - Budget gruppo: {group_budget:.2f}, Già investito: {group_invested:.2f}, Disponibile: {group_available:.2f}")
            if group_available < ORDER_USDT:
                log(f"💸 Budget {group_label} insufficiente per {symbol} (disponibile: {group_available:.2f})")
                continue

            # 📊 Valuta la forza del segnale in base alla strategia
            strategy_strength = {
                "Breakout Bollinger": 1.0,
                "MACD bullish + ADX": 0.9,
                "Incrocio SMA 20/50": 0.75,
                "Incrocio EMA 20/50": 0.7,
                "MACD bullish (stabile)": 0.65,
                "Trend EMA + RSI": 0.6
            }
            strength = strategy_strength.get(strategy, 0.5)  # default prudente

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
                    log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo molto alto ({atr_ratio:.2%}), size ordine dimezzata.")
                elif atr_ratio > 0.04:
                    strength *= 0.75
                    log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo elevato ({atr_ratio:.2%}), size ordine ridotta del 25%.")

            max_invest = min(group_available, usdt_balance) * strength
            order_amount = min(max_invest, group_available, usdt_balance, 250)
            log(f"[FORZA] {symbol} - Strategia: {strategy}, Strength: {strength}, Investo: {order_amount:.2f} USDT (Saldo: {usdt_balance:.2f})")

            # BLOCCO: non tentare short se order_amount < min_order_amt
            min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
            if order_amount < min_order_amt:
                log(f"❌ Saldo troppo basso per aprire short su {symbol}: {order_amount:.2f} < min_order_amt {min_order_amt}")
                if not low_balance_alerted:
                    notify_telegram(f"❗️ Saldo USDT troppo basso per nuovi short. Ricarica il wallet per continuare a operare.")
                    low_balance_alerted = True
                continue
            else:
                low_balance_alerted = False

            # Logga la quantità calcolata PRIMA dell'apertura short
            qty_str = calculate_quantity(symbol, order_amount)
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
            actual_cost = qty * get_last_price(symbol)
            log(f"🟢 SHORT aperto per {symbol}. Investito effettivo: {actual_cost:.2f} USDT")

            # Calcolo ATR, SL, TP per SHORT
            df = fetch_history(symbol)
            if df is None or "Close" not in df.columns:
                log(f"❌ Dati storici mancanti per {symbol}")
                continue
            atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
            last = df.iloc[-1]
            atr_val = last["atr"] if "atr" in last else atr.iloc[-1]
            atr_ratio = atr_val / get_last_price(symbol) if get_last_price(symbol) > 0 else 0
            tp_factor = min(TP_MAX, max(TP_MIN, TP_FACTOR + atr_ratio * 5))
            sl_factor = min(SL_MAX, max(SL_MIN, SL_FACTOR + atr_ratio * 3))
            tp = get_last_price(symbol) - (atr_val * tp_factor)  # PATCH: TP SOTTO ENTRY
            sl = get_last_price(symbol) + (atr_val * sl_factor)  # PATCH: SL SOPRA ENTRY
            min_sl = get_last_price(symbol) * 1.01  # PATCH: SL almeno 1% SOPRA entry
            if sl < min_sl:
                log(f"[SL PATCH] SL troppo vicino al prezzo di ingresso ({sl:.4f} < {min_sl:.4f}), imposto SL a {min_sl:.4f}")
                sl = min_sl

            log(f"[ENTRY-DETAIL] {symbol} | Entry: {get_last_price(symbol):.4f} | SL: {sl:.4f} | TP: {tp:.4f} | ATR: {atr_val:.4f}")

            position_data[symbol] = {
                "entry_price": get_last_price(symbol),
                "tp": tp,
                "sl": sl,
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_min": get_last_price(symbol)  # PATCH: p_min per trailing SHORT
            }
            open_positions.add(symbol)
            notify_telegram(f"🟢📉 SHORT aperto per {symbol}\nPrezzo: {get_last_price(symbol):.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f} USDT\nSL: {sl:.4f}\nTP: {tp:.4f}")
            time.sleep(3)

    # PATCH: rimuovi posizioni con saldo < 1 (polvere) anche nel ciclo principale
    for symbol in list(open_positions):
        saldo = get_open_short_qty(symbol)
        if saldo is None or saldo < 1:
            log(f"[CLEANUP] {symbol}: saldo troppo basso ({saldo}), rimuovo da open_positions e position_data (polvere)")
            open_positions.discard(symbol)
            position_data.pop(symbol, None)
            continue

        entry = position_data.get(symbol, {})
        holding_seconds = time.time() - entry.get("entry_time", 0)
        if holding_seconds < MIN_HOLDING_MINUTES * 60:
            log(f"[HOLDING][EXIT] {symbol}: attendo ancora {MIN_HOLDING_MINUTES - holding_seconds/60:.1f} min prima di poter ricoprire")
            continue

        # 🔴 USCITA SHORT (EXIT)
        elif signal == "exit" and symbol in open_positions:
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            qty = get_open_short_qty(symbol)  # PATCH: usa sempre la quantità effettiva short aperta
            log(f"[TEST][EXIT_SIGNAL] {symbol} | qty effettiva: {qty} | entry_price: {entry_price} | current_price: {price}")
            notify_telegram(f"[TEST] EXIT_SIGNAL per {symbol}\nQty effettiva: {qty}\nEntry: {entry_price}\nPrezzo attuale: {price}")
            if qty <= 0:
                log(f"[TEST][EXIT_FAIL] Nessuna quantità short effettiva da ricoprire per {symbol}")
                notify_telegram(f"[TEST] ❌❗️ Nessuna quantità short effettiva da ricoprire per {symbol} durante EXIT SIGNAL!")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue
            usdt_before = get_usdt_balance()
            resp = market_cover(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                price = get_last_price(symbol)
                price = round(price, 6)
                exit_value = price * qty
                delta = exit_value - entry_cost
                pnl = (delta / entry_cost) * 100
                log(f"[TEST][EXIT_OK] Ricopertura completata per {symbol} | PnL stimato: {pnl:.2f}% | Delta: {delta:.2f}")
                notify_telegram(f"[TEST] Ricopertura [SHORT] per {symbol} a {price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}%")
                log_trade_to_google(symbol, entry_price, price, pnl, strategy, "Exit Signal", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
                position_data.pop(symbol, None)
            else:
                saldo_attuale = get_free_qty(symbol)
                log(f"[TEST][EXIT_FAIL] Ricopertura fallita per {symbol}")
                notify_telegram(f"[TEST] ❌❗️ RICOPERTURA [SHORT] NON RIUSCITA per {symbol} durante EXIT SIGNAL! (saldo attuale: {saldo_attuale})")

    # Sicurezza: attesa tra i cicli principali
    time.sleep(INTERVAL_MINUTES * 60)