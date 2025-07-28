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

ORDER_USDT = 50.0

ASSETS = [
    "WIFUSDT", "PEPEUSDT", "BONKUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT",
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]

VOLATILE_ASSETS = [
    "BONKUSDT", "PEPEUSDT", "WIFUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT"
]

INTERVAL_MINUTES = 15
ATR_WINDOW = 14
TP_FACTOR = 2.0
SL_FACTOR = 1.5
# TRAILING_ACTIVATION_THRESHOLD = 0.001 # +0.1% activation threshold
TRAILING_ACTIVATION_THRESHOLD = 0.02
TRAILING_SL_BUFFER = 0.007
TRAILING_DISTANCE = 0.02
INITIAL_STOP_LOSS_PCT = 0.02
COOLDOWN_MINUTES = 60
cooldown = {}

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

# --- FUNZIONI DI SUPPORTO BYBIT E TELEGRAM ---
def get_last_price(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "spot", "symbol": symbol}
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
        params = {"category": "spot", "symbol": symbol}
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

def limit_buy(symbol, usdt_amount, price_increase_pct=0.005):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None
    limit_price = price * (1 + price_increase_pct)
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    price_step = info.get("price_step", 0.0001)
    # Calcola i decimali per qty_step e price_step (es: 0.0001 -> 4 decimali)
    def step_decimals(step):
        s = str(step)
        if '.' in s:
            return len(s.split('.')[-1].rstrip('0'))
        return 0
    qty_decimals = step_decimals(qty_step)
    price_decimals = step_decimals(price_step)
    # Formatta quantità e prezzo con i decimali corretti
    qty_str = calculate_quantity(symbol, usdt_amount)
    if not qty_str:
        log(f"❌ Quantità non valida per acquisto di {symbol}")
        return None
    price_str = f"{limit_price:.{price_decimals}f}"
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Limit",
        "qty": qty_str,
        "price": price_str,
        "timeInForce": "GTC"
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
    log(f"LIMIT BUY BODY: {body_json}")
    try:
        resp_json = response.json()
    except Exception:
        resp_json = {}
    log(f"RESPONSE: {response.status_code} {resp_json}")
    if response.status_code == 200 and resp_json.get("retCode") == 0:
        log(f"🟢 Ordine LIMIT inviato per {symbol} qty={qty_str} price={price_str}")
        notify_telegram(f"🟢 Ordine LIMIT inviato per {symbol} qty={qty_str} price={price_str}")
        return resp_json
    else:
        log(f"❌ Ordine LIMIT fallito per {symbol}: {resp_json.get('retMsg')}")
        notify_telegram(f"❌ Ordine LIMIT fallito per {symbol}: {resp_json.get('retMsg')}")
        return None

def calculate_quantity(symbol: str, usdt_amount: float) -> Optional[str]:
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None

    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    precision = info.get("precision", 4)
    min_order_amt = info.get("min_order_amt", 5)
    min_qty = info.get("min_qty", 0.0)

    try:
        raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
        step = Decimal(str(qty_step))
        min_qty_dec = Decimal(str(min_qty))
        # Calcola la quantità come intero di step
        qty_int = int(raw_qty // step)
        floored_qty = qty_int * step
        # Se troppo piccola, porta a min_qty
        if floored_qty < min_qty_dec:
            floored_qty = min_qty_dec
        order_value = floored_qty * Decimal(str(price))
        # Se valore troppo basso, porta a min_qty per min_order_amt
        if order_value < Decimal(str(min_order_amt)):
            min_qty_for_amt = (Decimal(str(min_order_amt)) / Decimal(str(price)))
            min_qty_int = int(min_qty_for_amt // step)
            min_qty_for_amt = min_qty_int * step
            if min_qty_for_amt < min_qty_dec:
                min_qty_for_amt = min_qty_dec
            floored_qty = min_qty_for_amt
            order_value = floored_qty * Decimal(str(price))
            if order_value < Decimal(str(min_order_amt)):
                log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT (minimo richiesto: {min_order_amt})")
                return None
        # Verifica che la quantità sia multiplo esatto di qty_step
        if (floored_qty / step) % 1 != 0:
            log(f"❌ Quantità {floored_qty} non multiplo di qty_step {qty_step} per {symbol}")
            return None
        if floored_qty <= 0:
            log(f"❌ Quantità calcolata troppo piccola per {symbol}")
            return None
        investito_effettivo = float(floored_qty) * float(price)
        if investito_effettivo < 0.95 * usdt_amount:
            log(f"⚠️ Attenzione: valore effettivo investito ({investito_effettivo:.2f} USDT) molto inferiore a quello richiesto ({usdt_amount:.2f} USDT)")
        log(f"[DEBUG] {symbol} - price: {price}, qty_step: {qty_step}, min_qty: {min_qty}, min_order_amt: {min_order_amt}, richiesto: {usdt_amount}, calcolato: {floored_qty}, valore ordine: {order_value:.2f}")
        # Format con esattamente 'precision' decimali (basePrecision)
        fmt = f"{{0:.{precision}f}}"
        qty_str = fmt.format(floored_qty)
        # Rimuovi eventuali zeri finali e punto se intero
        if '.' in qty_str:
            qty_str = qty_str.rstrip('0').rstrip('.')
        return qty_str
    except Exception as e:
        log(f"❌ Errore calcolo quantità per {symbol}: {e}")
        return None

def market_buy(symbol: str, usdt_amount: float):
    retry = 0
    max_retry = 1
    while retry <= max_retry:
        qty_str = calculate_quantity(symbol, usdt_amount)
        if not qty_str:
            log(f"❌ Quantità non valida per acquisto di {symbol}")
            return None

        info = get_instrument_info(symbol)
        min_qty = info.get("min_qty", 0.0)
        min_order_amt = info.get("min_order_amt", 5)
        precision = info.get("precision", 4)

        def _send_order(qty_str):
            body = {
                "category": "spot",
                "symbol": symbol,
                "side": "Buy",
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
            log(f"BUY BODY: {body_json}")
            try:
                resp_json = response.json()
            except Exception:
                resp_json = {}
            log(f"RESPONSE: {response.status_code} {resp_json}")
            # Logga dettagli filled/cumExecQty/orderStatus se presenti
            if 'result' in resp_json:
                result = resp_json['result']
                filled = result.get('cumExecQty') or result.get('execQty') or result.get('qty')
                order_status = result.get('orderStatus')
                log(f"[BYBIT ORDER RESULT] filled: {filled}, orderStatus: {order_status}, result: {result}")
            return response, resp_json

        try:
            response, resp_json = _send_order(qty_str)
            if response.status_code == 200 and resp_json.get("retCode") == 0:
                time.sleep(2)
                qty_after = get_free_qty(symbol)
                if not qty_after or qty_after == 0:
                    time.sleep(3)
                    qty_after = get_free_qty(symbol)

                # Calcola la quantità richiesta in float
                try:
                    qty_requested = float(qty_str)
                except Exception:
                    qty_requested = None

                # Se la quantità effettiva è molto inferiore a quella richiesta, logga e notifica
                if qty_requested and qty_after < 0.8 * qty_requested:
                    log(f"⚠️ Quantità acquistata ({qty_after}) molto inferiore a quella richiesta ({qty_requested}) per {symbol}")
                    notify_telegram(f"⚠️ Ordine parzialmente eseguito per {symbol}: richiesto {qty_requested}, ottenuto {qty_after}")
                    # Tenta un solo riacquisto della differenza se supera i minimi
                    diff = qty_requested - qty_after
                    price = get_last_price(symbol)
                    if diff > min_qty and price and (diff * price) > min_order_amt:
                        diff_str = f"{diff:.{precision}f}".rstrip('0').rstrip('.')
                        log(f"🔁 TENTO RIACQUISTO della differenza: {diff_str} {symbol}")
                        response2, resp_json2 = _send_order(diff_str)
                        if response2.status_code == 200 and resp_json2.get("retCode") == 0:
                            time.sleep(2)
                            qty_final = get_free_qty(symbol)
                            log(f"🟢 Acquisto finale per {symbol}: {qty_final}")
                            return qty_final
                        else:
                            log(f"❌ Riacquisto fallito per {symbol}")
                            return qty_after
                    else:
                        log(f"❌ Differenza troppo piccola per riacquisto su {symbol}")
                        return qty_after
                if qty_after and qty_after > 0:
                    log(f"🟢 Acquisto registrato per {symbol}")
                    return qty_after
                else:
                    log(f"⚠️ Acquisto riuscito ma saldo non aggiornato per {symbol}")
            else:
                # Se errore Bybit, ricalcola e riprova una sola volta
                if retry < max_retry:
                    log(f"🔄 Retry acquisto per {symbol} dopo errore Bybit: {resp_json.get('retMsg')}")
                    retry += 1
                    continue
                else:
                    log(f"❌ Acquisto fallito per {symbol} dopo retry: {resp_json.get('retMsg')}")
                    return None
        except Exception as e:
            log(f"❌ Errore invio ordine market per {symbol}: {e}")
            return None
        break

def market_sell(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile vendere")
        return

    order_value = qty * price
    if order_value < 5:
        log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT")
        return

    # Recupera qty_step e precision con fallback robusto
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    precision = info.get("precision", 4)
    if not qty_step or qty_step <= 0:
        qty_step = 0.0001
        precision = 4

    try:
        dec_qty = Decimal(str(qty))
        step = Decimal(str(qty_step))
        # Arrotonda per difetto al multiplo di step, MAI supera il saldo
        floored_qty = (dec_qty // step) * step
        # Forza massimo 2 decimali (troncando, non arrotondando)
        floored_qty = floored_qty.quantize(Decimal('0.01'), rounding=ROUND_DOWN)
        # Rimuovi eventuali zeri e punto finale
        qty_str = f"{floored_qty:.2f}".rstrip('0').rstrip('.')
        if qty_str == '':
            qty_str = '0'

        if Decimal(qty_str) <= 0:
            log(f"❌ Quantità troppo piccola per {symbol} (dopo arrotondamento)")
            return

        # Log di debug
        log(f"[DEBUG] market_sell {symbol}: qty={qty}, step={qty_step}, floored={floored_qty}, qty_str={qty_str}")

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

def fetch_history(symbol: str):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": str(INTERVAL_MINUTES),
        "limit": 100
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
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        log(f"[!] Errore richiesta Kline per {symbol}: {e}")
        return None

def find_close_column(df: pd.DataFrame):
    for name in df.columns:
        if "close" in name.lower():
            return df[name]
    return None

def analyze_asset(symbol: str):
    try:
        df = fetch_history(symbol)
        if df is None:
            return None, None, None
        close = find_close_column(df)
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
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=[
            "bb_upper", "bb_lower", "rsi", "sma20", "sma50", "ema20", "ema50",
            "macd", "macd_signal", "adx", "atr"
        ], inplace=True)

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = 20 if is_volatile else 15

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])

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


TEST_MODE = False  # Acquisti e vendite normali abilitati


# Inizializza struttura base
open_positions = set()
position_data = {}
last_exit_time = {}

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

def calculate_stop_loss(entry_price, current_price, p_max, trailing_active):
    if not trailing_active:
        return entry_price * (1 - INITIAL_STOP_LOSS_PCT)
    else:
        return p_max * (1 - TRAILING_DISTANCE)

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

while True:

    for symbol in ASSETS:
        signal, strategy, price = analyze_asset(symbol)
        log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ❌ Filtra segnali nulli
        if signal is None or strategy is None or price is None:
            continue

        # ✅ ENTRATA
        if signal == "entry":
            # Cooldown
            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if elapsed < COOLDOWN_MINUTES * 60:
                    log(f"⏳ Cooldown attivo per {symbol} ({elapsed:.0f}s), salto ingresso")
                    continue

            if symbol in open_positions:
                log(f"⏩ Ignoro acquisto: già in posizione su {symbol}")
                continue

            usdt_balance = get_usdt_balance()
            log(f"[DEBUG-ENTRY] Saldo USDT prima dell'acquisto per {symbol}: {usdt_balance:.4f}")
            if usdt_balance < ORDER_USDT:
                log(f"💸 Saldo USDT insufficiente per {symbol} ({usdt_balance:.2f})")
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

            max_invest = usdt_balance * strength
            order_amount = min(max_invest, usdt_balance, 250) # tetto massimo se vuoi
            log(f"[FORZA] {symbol} - Strategia: {strategy}, Strength: {strength}, Investo: {order_amount:.2f} USDT (Saldo: {usdt_balance:.2f})")

            # Logga la quantità calcolata PRIMA dell'acquisto
            qty_str = calculate_quantity(symbol, order_amount)
            log(f"[DEBUG-ENTRY] Quantità calcolata per {symbol} con {order_amount:.2f} USDT: {qty_str}")

            # ⚠️ INIBISCI GLI ACQUISTI DURANTE IL TEST
            if TEST_MODE:
                log(f"[TEST_MODE] Acquisti inibiti per {symbol}")
                continue


            # Esegui l'acquisto effettivo con ordine LIMIT
            resp = limit_buy(symbol, order_amount)
            if resp is None:
                log(f"❌ Acquisto LIMIT fallito per {symbol}")
                continue

            log(f"🟢 Ordine LIMIT piazzato per {symbol}. Attendi esecuzione.")

            # Dopo l'esecuzione dell'ordine, aggiorna qty e actual_cost
            time.sleep(2)
            qty = get_free_qty(symbol)
            actual_cost = 0.0
            last_price = get_last_price(symbol)
            if qty and last_price:
                actual_cost = qty * last_price
            else:
                actual_cost = order_amount

            df = fetch_history(symbol)
            if df is None or "Close" not in df.columns:
                log(f"❌ Dati storici mancanti per {symbol}")
                continue

            atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
            last = df.iloc[-1]
            atr_val = last["atr"] if "atr" in last else atr.iloc[-1]

            tp = price + (atr_val * TP_FACTOR)
            sl = price - (atr_val * SL_FACTOR)

            position_data[symbol] = {
                "entry_price": price,
                "tp": tp,
                "sl": sl,
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price
            }

            open_positions.add(symbol)
            log(f"🟢 Acquisto registrato per {symbol} | Entry: {price:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")
            notify_telegram(f"🟢📈 Acquisto per {symbol}\nPrezzo: {price:.4f}\nStrategia: {strategy}\nInvestito: {order_amount:.2f} USDT")
            time.sleep(3)

        # 🔴 USCITA (EXIT)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            qty = entry.get("qty", get_free_qty(symbol))

            usdt_before = get_usdt_balance()
            resp = market_sell(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                price = round(price, 6)
                exit_value = price * qty
                delta = exit_value - entry_cost
                pnl = (delta / entry_cost) * 100

                log(f"🔴 Vendita completata per {symbol}")
                log(f"📊 PnL stimato: {pnl:.2f}% | Delta: {delta:.2f}")
                notify_telegram(f"🔴📉 Vendita per {symbol} a {price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}%")
                log_trade_to_google(symbol, entry_price, price, pnl, strategy, "Exit Signal")

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

        # 🧪 Attiva Trailing se supera la soglia
        if not entry["trailing_active"] and current_price >= entry["entry_price"] * (1 + TRAILING_ACTIVATION_THRESHOLD):
            entry["trailing_active"] = True
            log(f"🔛 Trailing Stop attivato per {symbol} sopra soglia → Prezzo: {current_price:.4f}")
            notify_telegram(f"🔛 Trailing Stop attivo su {symbol}\nPrezzo: {current_price:.4f}")

        # ⬆️ Aggiorna massimo e SL se prezzo cresce
        if entry["trailing_active"]:
            if current_price > entry["p_max"]:
                entry["p_max"] = current_price
                new_sl = current_price * (1 - TRAILING_SL_BUFFER)
                if new_sl > entry["sl"]:
                    log(f"📉 SL aggiornato per {symbol}: da {entry['sl']:.4f} a {new_sl:.4f}")
                    entry["sl"] = new_sl

            # ❌ Esegui vendita se SL raggiunto
            if current_price <= entry["sl"]:
                qty = get_free_qty(symbol)
                if qty > 0:
                    usdt_before = get_usdt_balance()
                    resp = market_sell(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        entry_price = entry["entry_price"]
                        entry_cost = entry.get("entry_cost", ORDER_USDT)
                        qty = entry.get("qty", qty)
                        exit_value = current_price * qty
                        delta = exit_value - entry_cost
                        pnl = (delta / entry_cost) * 100

                        log(f"🔻 Trailing Stop attivato per {symbol} → Prezzo: {current_price:.4f} | SL: {entry['sl']:.4f}")
                        notify_telegram(f"🔻 Trailing Stop venduto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                        log_trade_to_google(symbol, entry_price, current_price, pnl, "Trailing Stop", "SL Triggered")

                        # 🗑️ Pulizia
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    else:
                        log(f"❌ Vendita fallita con Trailing Stop per {symbol}")

    # Sicurezza: attesa tra i cicli principali
    # Aggiungi pausa di sicurezza per evitare ciclo troppo veloce se tutto salta
    time.sleep(INTERVAL_MINUTES * 60)