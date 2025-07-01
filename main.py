import os
import time
import hmac
import json
import hashlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
import yfinance as yf
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from dotenv import load_dotenv
from typing import Optional

# Carica variabili da .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = (
    "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
)
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()

MIN_ORDER_USDT = 52
ORDER_USDT = max(MIN_ORDER_USDT, float(os.getenv("ORDER_USDT", str(MIN_ORDER_USDT))))

ASSET_LIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INTERVAL_MINUTES = 15
DOWNLOAD_RETRIES = 3
INSTRUMENT_CACHE = {}

def market_buy(symbol: str, usdt: float):
    endpoint = "https://api.bybit.com/v5/order/create"
    ts = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": f"{usdt:.2f}"
    }
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
    payload = f"{ts}{BYBIT_API_KEY}5000{body_json}"
    sign = hmac.new(BYBIT_API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }
    resp = requests.post(endpoint, headers=headers, data=body_json)
    print("BODY:", body_json)
    print("RESPONSE:", resp.status_code, resp.json())

def log(msg):
    timestamp = time.strftime("[%Y-%m-%d %H:%M:%S]")
    print(f"{timestamp} {msg}")

def notify_telegram(message: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data, timeout=10)
    except Exception as e:
        log(f"Errore Telegram: {e}")

def _sign(payload: str) -> str:
    if not BYBIT_API_SECRET:
        return ""
    return hmac.new(BYBIT_API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

def send_order(symbol: str, side: str, quantity: float, precision: int, price: float):
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log("Chiavi Bybit mancanti: ordine non inviato")
        return

    if quantity <= 0:
        log(f"Quantità non valida per l'ordine {symbol}")
        return

    endpoint = f"{BYBIT_BASE_URL}/v5/order/create"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "MARKET",
        "timeInForce": "IOC"
    }

    if side.upper() == "BUY":
        # Logica testata: usare qty + marketUnit=quoteCoin per quantità in USDT
        body["qty"] = f"{quantity:.2f}"
        body["marketUnit"] = "quoteCoin"
        usdt_display = quantity
    else:
        qty_str = _format_quantity(quantity, precision)
        body["qty"] = qty_str
        usdt_display = float(qty_str) * price

    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
    signature_payload = f"{timestamp}{BYBIT_API_KEY}{recv_window}{body_json}"
    signature = _sign(signature_payload)

    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json",
    }

    try:
        debug_body = "&".join(f"{k}={v}" for k, v in body.items())
        log(f"[DEBUG] Endpoint: {endpoint}")
        log(f"[DEBUG] Headers: {headers}")
        log(f"[DEBUG] Body: {debug_body}")
        resp = requests.post(endpoint, headers=headers, data=body_json, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            code = data.get("retCode")
            if code == 170140:
                msg = f"Ordine troppo piccolo per {symbol}. Aumenta ORDER_USDT."
            elif code == 170131:
                msg = f"Saldo insufficiente per {symbol}."
            elif code == 170137:
                msg = f"Decimali eccessivi per {symbol}."
            elif code == 170003:
                msg = f"❌ Errore parametri: {data.get('retMsg', '')} (probabile malformazione quoteQty)"
            else:
                msg = f"Errore ordine {symbol}: {data}"
            log(msg)
            notify_telegram(msg)
        else:
            msg = f"✅ Ordine {side} {symbol} inviato: ~{usdt_display:.2f} USDT"
            log(msg)
            notify_telegram(msg)
    except Exception as e:
        msg = f"Errore invio ordine {symbol}: {e}"
        log(msg)
        notify_telegram(msg)

if __name__ == "__main__":
    print("🔄 Avvio sistema di acquisto iniziale (DOGE + BTC)")
    market_buy("DOGEUSDT", 10.00)
    market_buy("BTCUSDT", 10.00)