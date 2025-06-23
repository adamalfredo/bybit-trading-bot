import os
import time
import hmac
import hashlib
import requests
import json
import logging
from datetime import datetime

# === CONFIG ===
BASE_URL = "https://api.bybit.com"
SYMBOL = "BTCUSDT"
CATEGORY = "spot"
ORDER_TYPE = "Market"
TIME_IN_FORCE = "IOC"
SIDE = "Buy"
QUANTITY = 0.000495  # Modificabile per superare il limite minimo di $5

# === LOGGER ===
logging.basicConfig(level=logging.DEBUG, format='[%(asctime)s] %(levelname)s: %(message)s')

# === API KEYS ===
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not API_KEY or not API_SECRET:
    raise ValueError("API_KEY o API_SECRET non trovati nelle variabili d'ambiente.")

# === SIGNATURE ===
def generate_signature(params: dict, api_secret: str) -> str:
    param_str = json.dumps(params, separators=(',', ':'), sort_keys=True)
    logging.debug(f"Stringa da firmare: {param_str}")
    return hmac.new(api_secret.encode('utf-8'), param_str.encode('utf-8'), hashlib.sha256).hexdigest()

# === INVIO ORDINE ===
def send_test_order():
    timestamp = str(int(time.time() * 1000))

    body = {
        "category": CATEGORY,
        "symbol": SYMBOL,
        "side": SIDE,
        "orderType": ORDER_TYPE,
        "qty": f"{QUANTITY:.6f}",
        "timeInForce": TIME_IN_FORCE,
        "timestamp": timestamp
    }

    signature = generate_signature(body, API_SECRET)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

    logging.debug("Parametri ordine inviati (headers): %s", headers)
    logging.debug("Corpo JSON (usato anche per sign): %s", json.dumps(body))

    response = requests.post(f"{BASE_URL}/v5/order/create", headers=headers, json=body)
    logging.info("Test ordine risultato: %s", response.json())
    return response.json()

# === AVVIO ===
def main():
    logging.info("\U0001F7E2 Avvio bot e test ordine iniziale")
    try:
        result = send_test_order()
        print("[TEST] Risposta ordine:", result)
    except Exception as e:
        logging.error("Errore durante l'invio dell'ordine: %s", str(e))

if __name__ == "__main__":
    main()
