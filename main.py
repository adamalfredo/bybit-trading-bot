import time
import hmac
import hashlib
import requests
import os
import json
from dotenv import load_dotenv
from datetime import datetime

# Carica variabili da Railway o da .env in locale
load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTCUSDT"]
RSI_PERIOD = 14
EMA_PERIOD = 50
TAKE_PROFIT = 1.07
STOP_LOSS = 0.97
TRADE_AMOUNT_USDT = 50  # aumentato
BASE_URL = "https://api.bybit.com"

positions = {}
DEBUG = True

def log(msg):
    if DEBUG:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        log(f"[Telegram] Errore invio messaggio: {e}")

def sign_request_v5(timestamp, api_key, secret, body):
    recv_window = "5000"
    payload = f"{timestamp}{api_key}{recv_window}{body}"
    signature = hmac.new(
        secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature, recv_window

def get_klines(symbol):
    url = BASE_URL + "/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": "15",
        "limit": 100
    }
    try:
        response = requests.get(url, params=params)
        data = response.json()
        if "result" in data and "list" in data["result"]:
            closes = [float(x[4]) for x in data["result"]["list"]]
            volumes = [float(x[5]) for x in data["result"]["list"]]
            return closes, volumes
        else:
            log(f"[{symbol}] Dati non validi: {data}")
    except Exception as e:
        log(f"[{symbol}] Errore richiesta dati: {e}")
    return [], []

def calculate_rsi(prices):
    gains, losses = [], []
    for i in range(1, len(prices)):
        delta = prices[i] - prices[i - 1]
        gains.append(max(0, delta))
        losses.append(max(0, -delta))
    avg_gain = sum(gains[-RSI_PERIOD:]) / RSI_PERIOD
    avg_loss = sum(losses[-RSI_PERIOD:]) / RSI_PERIOD
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_ema(prices, period):
    k = 2 / (period + 1)
    ema = prices[0]
    for price in prices[1:]:
        ema = price * k + ema * (1 - k)
    return ema

def place_order(symbol, side, qty):
    timestamp = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": f"{qty:.6f}",
        "timeInForce": "IOC",
        "timestamp": timestamp
    }
    json_body = json.dumps(body, separators=(',', ':'), sort_keys=True)

    signature, recv_window = sign_request_v5(timestamp, API_KEY, API_SECRET, json_body)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

    log(f"[DEBUG] Parametri ordine inviati (headers): {headers}")
    log(f"[DEBUG] Corpo JSON (usato anche per sign): {json_body}")

    url = BASE_URL + "/v5/order/create"
    try:
        response = requests.post(url, headers=headers, data=json_body)
        return response.json()
    except Exception as e:
        log(f"[{symbol}] Errore ordine: {e}")
        return {}

def test_order():
    test_symbol = "BTCUSDT"
    test_price = 51000  # ipotetico
    test_qty = round(TRADE_AMOUNT_USDT / test_price, 6)
    log(f"[DEBUG] Test ordine qty={test_qty}, apiKey={(API_KEY[:4] + '***') if API_KEY else 'None'}")
    result = place_order(test_symbol, "Buy", test_qty)
    send_telegram(f"[TEST] Risposta ordine: {result}")
    log(f"Test ordine risultato: {result}")

if __name__ == "__main__":
    log(f"API_KEY loaded: {bool(API_KEY)}, API_SECRET loaded: {bool(API_SECRET)}")
    log("🟢 Avvio bot e test ordine iniziale")
    test_order()
