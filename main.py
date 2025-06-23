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

BASE_URL = "https://api-testnet.bybit.com"
ORDER_ENDPOINT = "/v5/order/create"

ORDER_QTY = "0.000050"


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


def sign_v5(secret, api_key, timestamp, recv_window, json_str):
    string_to_sign = f"{api_key}{timestamp}{recv_window}{json_str}"
    return hmac.new(secret.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()

def test_signed_get():
    endpoint = "/v5/account/info"
    timestamp = get_timestamp()
    recv_window = "5000"
    query = ""

    string_to_sign = f"{API_KEY}{timestamp}{recv_window}{query}"
    signature = hmac.new(API_SECRET.encode(), string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature
    }

    url = BASE_URL + endpoint
    response = requests.get(url, headers=headers)
    log(f"GET /v5/account/info result: {response.json()}")

def place_order(symbol, side, qty):
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",  # Attenzione: "M" maiuscola!
        "qty": qty,
        "timeInForce": "IOC",
        "timestamp": timestamp
    }

    json_body_str = json.dumps(body, separators=(",", ":"))
    signature = sign_v5(API_SECRET, API_KEY, timestamp, recv_window, json_body_str)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": signature,
        "Content-Type": "application/json"
    }

    log(f"[DEBUG] Stringa per la firma: {API_KEY}{timestamp}{recv_window}{json_body_str}")
    log(f"[DEBUG] Parametri ordine inviati (headers): {headers}")
    log(f"[DEBUG] Corpo JSON (usato anche per sign): {json_body_str}")
    log(f"[DEBUG] Tipo di body inviato: {type(json_body_str)}")

    try:
        response = requests.post(BASE_URL + ORDER_ENDPOINT, headers=headers, data=json_body_str, timeout=10)
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
