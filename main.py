import time
import hmac
import hashlib
import requests
import os
import json
from datetime import datetime

# Variabili ambiente
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTCUSDT"]
TRADE_AMOUNT_USDT = 50
BASE_URL = "https://api.bybit.com"
RECV_WINDOW = "5000"
DEBUG = True
positions = {}


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


def sign_request(payload: str, timestamp: str):
    to_sign = timestamp + API_KEY + RECV_WINDOW + payload
    return hmac.new(
        API_SECRET.encode("utf-8"),
        to_sign.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


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

    body_str = json.dumps(body, separators=(",", ":"))
    signature = sign_request(body_str, timestamp)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

    log(f"[DEBUG] Parametri ordine inviati (headers): {headers}")
    log(f"[DEBUG] Corpo JSON (usato anche per sign): {body_str}")

    url = BASE_URL + "/v5/order/create"
    try:
        response = requests.post(url, headers=headers, data=body_str)
        return response.json()
    except Exception as e:
        log(f"[{symbol}] Errore ordine: {e}")
        return {}


def test_order():
    symbol = "BTCUSDT"
    price = 101000
    qty = round(TRADE_AMOUNT_USDT / price, 6)
    log(f"[DEBUG] Test ordine qty={qty}, apiKey={(API_KEY[:4] + '***') if API_KEY else 'None'}")
    result = place_order(symbol, "Buy", qty)
    send_telegram(f"[TEST] Risposta ordine: {result}")
    log(f"Test ordine risultato: {result}")


if __name__ == "__main__":
    log(f"API_KEY loaded: {bool(API_KEY)}, API_SECRET loaded: {bool(API_SECRET)}")
    log("🟢 Avvio bot e test ordine iniziale")
    test_order()

    while True:
        for symbol in SYMBOLS:
            try:
                # Simulazione semplice (rimuovi se usi RSI/EMA reali)
                price = 101000
                if symbol not in positions:
                    qty = round(TRADE_AMOUNT_USDT / price, 6)
                    result = place_order(symbol, "Buy", qty)
                    if result.get("retCode") == 0:
                        positions[symbol] = {"entry": price, "qty": qty}
                        send_telegram(f"✅ ACQUISTO {symbol} a {price:.2f} (qty: {qty})")
                        log(f"ACQUISTO {symbol}: prezzo={price:.2f} qty={qty}")
                    else:
                        log(f"❌ Errore ordine: {result}")

            except Exception as e:
                log(f"[⚠️ {symbol}] Errore: {e}")

        time.sleep(60)
