import os
import time
import hmac
import hashlib
import requests
import json

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE_URL = "https://api.bybit.com"
ORDER_ENDPOINT = "/v5/order/create"

ORDER_QTY = "0.000050"  # Minimo 5 USDT


def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} {msg}")


def notify_telegram(message):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=data, timeout=10)
        except Exception as e:
            log(f"Errore invio Telegram: {e}")


def get_timestamp():
    return str(int(time.time() * 1000))


def sign_json(secret, json_str: str) -> str:
    return hmac.new(
        secret.encode("utf-8"),
        json_str.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def place_order(symbol, side, qty):
    timestamp = get_timestamp()

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": qty,
        "timeInForce": "IOC",
        "timestamp": timestamp
    }

    # Questo è ciò che va firmato
    json_body = json.dumps(body, separators=(",", ":"))
    signature = sign_json(API_SECRET, json_body)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

    log(f"[DEBUG] Parametri ordine inviati (headers): {headers}")
    log(f"[DEBUG] Corpo JSON (usato anche per sign): {json_body}")

    try:
        response = requests.post(BASE_URL + ORDER_ENDPOINT, headers=headers, json=body, timeout=10)
        result = response.json()
        log(f"Test ordine risultato: {result}")
        notify_telegram(f"[TEST] Risposta ordine: {result}")
        return result
    except Exception as e:
        log(f"Errore richiesta ordine: {e}")
        notify_telegram(f"Errore ordine: {e}")
        return None


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        raise ValueError("API_KEY o API_SECRET non trovati nelle variabili d'ambiente.")
    log(f"API_KEY loaded: {bool(API_KEY)}, API_SECRET loaded: {bool(API_SECRET)}")
    log("🟢 Avvio bot e test ordine iniziale")
    log(f"[DEBUG] Test ordine qty={ORDER_QTY}, apiKey={(API_KEY[:4] + '***') if API_KEY else 'None'}")
    place_order("BTCUSDT", "Buy", ORDER_QTY)
