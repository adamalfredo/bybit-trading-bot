import time
import hmac
import hashlib
import requests
import os
import json
from dotenv import load_dotenv
from datetime import datetime

# Carica variabili da Railway
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
TRADE_AMOUNT_USDT = 50
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

def sign_request(timestamp, body):
    body_str = json.dumps(body, separators=(',', ':'), ensure_ascii=False)
    to_sign = f"{timestamp}{API_KEY}{body_str}"
    return hmac.new(
        API_SECRET.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

def get_ip():
    try:
        ip = requests.get("https://api.ipify.org").text
        log(f"[DEBUG] IP pubblico del container: {ip}")
    except Exception as e:
        log(f"[DEBUG] Errore nel recupero IP: {e}")

def place_order(symbol, side, qty):
    timestamp = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "timestamp": timestamp
    }

    signature = sign_request(timestamp, body)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }

    url = BASE_URL + "/v5/order/create"
    body_str = json.dumps(body, separators=(',', ':'))

    log(f"[DEBUG] Parametri ordine inviati (headers): {headers}")
    log(f"[DEBUG] Corpo JSON (usato anche per sign): {body_str}")

    try:
        response = requests.post(url, headers=headers, data=body_str)
        return response.json()
    except Exception as e:
        log(f"[{symbol}] Errore ordine: {e}")
        return {}

def test_order():
    test_symbol = "BTCUSDT"
    test_price = 102000
    test_qty = round(TRADE_AMOUNT_USDT / test_price, 6)
    log(f"[DEBUG] Test ordine qty={test_qty}, apiKey={(API_KEY[:4] + '***') if API_KEY else 'None'}")
    result = place_order(test_symbol, "Buy", test_qty)
    send_telegram(f"[TEST] Risposta ordine: {result}")
    log(f"Test ordine risultato: {result}")

if __name__ == "__main__":
    log(f"API_KEY loaded: {bool(API_KEY)}, API_SECRET loaded: {bool(API_SECRET)}")
    get_ip()
    log("🟢 Avvio bot e test ordine iniziale")
    test_order()
