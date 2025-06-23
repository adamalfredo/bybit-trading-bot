import time
import hmac
import hashlib
import requests
import os
import json
from dotenv import load_dotenv
from datetime import datetime

# Carica variabili da Railway (usa .env solo in locale)
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
TRADE_AMOUNT_USDT = 50  # Aggiornato a 50 USDT
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

def sign_request(payload: str) -> str:
    return hmac.new(
        API_SECRET.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()

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

    payload = json.dumps(body, separators=(',', ':'))
    signature = sign_request(payload)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "Content-Type": "application/json"
    }

    log(f"[DEBUG] Parametri ordine inviati (headers): {headers}")
    log(f"[DEBUG] Corpo JSON (usato anche per sign): {payload}")

    try:
        response = requests.post(BASE_URL + "/v5/order/create", headers=headers, data=payload)
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
    log("🟢 Avvio bot e test ordine iniziale")
    test_order()

    while True:
        for symbol in SYMBOLS:
            try:
                closes, volumes = get_klines(symbol)
                if len(closes) < EMA_PERIOD:
                    continue

                rsi = calculate_rsi(closes)
                ema = calculate_ema(closes, EMA_PERIOD)
                price = closes[-1]
                avg_vol = sum(volumes[-20:]) / 20
                recent_vol = sum(volumes[-3:]) / 3
                has_position = symbol in positions

                if 50 < rsi < 65 and price > ema and recent_vol > avg_vol * 1.1:
                    if not has_position or positions[symbol]["entry"] != price:
                        qty = round(TRADE_AMOUNT_USDT / price, 5)
                        result = place_order(symbol, "Buy", qty)
                        positions[symbol] = {"entry": price, "qty": qty}
                        send_telegram(f"✅ ACQUISTO {symbol} a {price:.2f} (qty: {qty})")
                        log(f"ACQUISTO {symbol}: prezzo={price:.2f} qty={qty}")

                if has_position:
                    entry = positions[symbol]["entry"]
                    qty = positions[symbol]["qty"]
                    if price >= entry * TAKE_PROFIT or price <= entry * STOP_LOSS or price < ema:
                        result = place_order(symbol, "Sell", qty)
                        send_telegram(f"❌ VENDITA {symbol} a {price:.2f} (qty: {qty})")
                        log(f"VENDITA {symbol}: prezzo={price:.2f} qty={qty}")
                        del positions[symbol]

            except Exception as e:
                log(f"[⚠️ {symbol}] Errore: {e}")

        time.sleep(60)
