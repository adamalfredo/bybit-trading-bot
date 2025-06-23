import os
import time
import hmac
import hashlib
import requests
import json
from datetime import datetime

# Lettura chiavi API dalle variabili d'ambiente
API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")

if not API_KEY or not API_SECRET:
    raise ValueError("API_KEY o API_SECRET non trovati nelle variabili d'ambiente.")

# Funzione per ottenere timestamp corrente in millisecondi
def get_timestamp():
    return str(int(time.time() * 1000))

# Funzione per generare firma

def generate_signature(secret, payload):
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()

# Funzione per inviare ordine

def send_test_order():
    url = "https://api.bybit.com/v5/order/create"

    timestamp = get_timestamp()
    body = {
        "category": "spot",
        "symbol": "BTCUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "0.000495",
        "timeInForce": "IOC",
        "timestamp": timestamp
    }

    json_body = json.dumps(body, separators=(',', ':'))

    sign_payload = json_body
    signature = generate_signature(API_SECRET, sign_payload)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-SIGN": signature,
        "X-BAPI-RECV-WINDOW": "5000",
        "Content-Type": "application/json"
    }

    print("[DEBUG] Parametri ordine inviati (headers):", headers)
    print("[DEBUG] Corpo JSON (usato anche per sign):", json_body)

    try:
        response = requests.post(url, headers=headers, data=json_body)
        return response.json()
    except Exception as e:
        return {"error": str(e)}

# Invio ordine di test
print("\n🟢 Avvio bot e test ordine iniziale")
response = send_test_order()
print("[TEST] Risposta ordine:", response)
