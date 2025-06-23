import time
import hmac
import hashlib
import requests
import os
import json
from dotenv import load_dotenv
from datetime import datetime

# Carica variabili ambiente (Railway o locale)
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
TRADE_AMOUNT_USDT = 5
BASE_URL = "https://api.bytick.com"

positions = {}
DEBUG = True

def log(msg):
    if DEBUG:
        print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}")

def get_public_ip():
    try:
        ip = requests.get("https://api.ipify.org").text
        log(f"[DEBUG] IP pubblico del container: {ip}")
    except Exception as e:
        log(f"[ERRORE] Impossibile ottenere l'IP pubblico: {e}")

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        log(f"[Telegram] Errore invio messaggio: {e}")

def sign_request(params):
    sorted_params = sorted((k, str(v)) for k, v in params.items())
    query_string = "&".join(f"{k}={v}" for k, v in sorted_params)
    signature = hmac.new(
        API_SECRET.encode("utf-8"),
        query_string.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()
    return signature

def place_order(symbol, side, qty):
    timestamp = str(int(time.time() * 1000))
    body = {
        "apiKey": API_KEY,
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "timestamp": timestamp
    }

    log(f"[DEBUG] Parametri ordine: apiKey={API_KEY}, apiSecret={(API_SECRET[:4] + '***') if API_SECRET else 'None'}")

    body["sign"] = sign_request(body)

    url = BASE_URL + "/v5/order/create"
    headers = {
        "Content-Type": "application/json"
    }

    try:
        response = requests.post(url, json=body, headers=headers)
        return response.json()
    except Exception as e:
        log(f"[{symbol}] Errore ordine: {e}")
        return {}


def test_order():
    test_symbol = "BTCUSDT"
    test_price = 102600
    test_qty = round(TRADE_AMOUNT_USDT / test_price, 6)
    log(f"[DEBUG] Test ordine qty={test_qty}, apiKey={(API_KEY[:4] + '***') if API_KEY else 'None'}")
    result = place_order(test_symbol, "Buy", test_qty)
    send_telegram(f"[TEST] Risposta ordine: {result}")
    log(f"Test ordine risultato: {result}")

if __name__ == "__main__":
    log(f"API_KEY loaded: {bool(API_KEY)}, API_SECRET loaded: {bool(API_SECRET)}")
    get_public_ip()
    log("\U0001F7E2 Avvio bot e test ordine iniziale")
    test_order()
