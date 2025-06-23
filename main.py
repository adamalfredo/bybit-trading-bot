import os
import json
import hmac
import hashlib
import time
import requests
import logging
from dotenv import load_dotenv

# Setup logging
logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] %(levelname)s: %(message)s')

# Load .env if exists
load_dotenv()

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not API_KEY or not API_SECRET:
    raise ValueError("API_KEY o API_SECRET non trovati nelle variabili d'ambiente.")

# === FUNZIONE PER GENERARE LA FIRMA ===
def generate_signature(secret, payload_str):
    return hmac.new(
        secret.encode('utf-8'),
        payload_str.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()

# === INVIO ORDINE DI TEST ===
def invia_test_order():
    url = "https://api.bybit.com/v5/order/create"

    timestamp = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.000050",
        "timeInForce": "IOC",
        "timestamp": timestamp
    }

    # Serializza senza spazi e in ordine stabile
    json_body = json.dumps(body, separators=(',', ':'), ensure_ascii=False)

    logging.debug(f"Corpo JSON (usato anche per sign): {json_body}")

    signature = generate_signature(API_SECRET, json_body)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

    logging.debug(f"Parametri ordine inviati (headers): {headers}")

    response = requests.post(url, headers=headers, data=json_body)
    logging.debug(f"Test ordine risultato: {response.text}")

    try:
        result = response.json()
    except Exception as e:
        result = {"error": str(e), "text": response.text}

    return result

if __name__ == "__main__":
    logging.info("\U0001F7E2 Avvio bot e test ordine iniziale")
    risultato = invia_test_order()
    print("[TEST] Risposta ordine:", risultato)
