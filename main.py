import time
import hmac
import hashlib
import requests
import os
import json
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TG_TOKEN = os.getenv("TELEGRAM_TOKEN")
TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "ADAUSDT", "XRPUSDT"]
RSI_PERIOD = 14
EMA_PERIOD = 50
TAKE_PROFIT = 1.07
STOP_LOSS = 0.97
TRADE_AMOUNT_USDT = 5

BASE_URL = "https://api.bytick.com"  # alternativo a bybit.com
positions = {}

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = {"chat_id": TG_CHAT_ID, "text": message}
    try:
        requests.post(url, data=data)
    except Exception as e:
        print(f"[Telegram] Errore invio messaggio: {e}")

def sign_request(params):
    param_str = "&".join([f"{key}={params[key]}" for key in sorted(params)])
    return hmac.new(API_SECRET.encode("utf-8"), param_str.encode("utf-8"), hashlib.sha256).hexdigest()

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
    endpoint = "/v5/market/kline"
    url = BASE_URL + endpoint
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
            print(f"[{symbol}] Nessun risultato valido nei dati ricevuti.")
    except Exception as e:
        print(f"[{symbol}] Errore durante richiesta dati: {e}")
    return [], []

def place_order(symbol, side, qty):
    endpoint = "/v5/order/create"
    url = BASE_URL + endpoint
    timestamp = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(qty),
        "timeInForce": "IOC",
        "timestamp": timestamp,
        "apiKey": API_KEY
    }
    sign = sign_request(body)
    body["sign"] = sign
    headers = {"Content-Type": "application/json"}
    try:
        response = requests.post(url, data=json.dumps(body), headers=headers)
        return response.json()
    except Exception as e:
        print(f"[{symbol}] Errore invio ordine: {e}")
        return {}

# Ciclo principale del bot
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

            if 50 < rsi < 65 and price > ema and recent_vol > avg_vol * 1.1 and not has_position:
                qty = round(TRADE_AMOUNT_USDT / price, 5)
                result = place_order(symbol, "Buy", qty)
                positions[symbol] = {"entry": price, "qty": qty}
                send_telegram(f"✅ ACQUISTO {symbol} a {price:.2f} (qty: {qty})")

            if has_position:
                entry = positions[symbol]["entry"]
                qty = positions[symbol]["qty"]
                if price >= entry * TAKE_PROFIT or price <= entry * STOP_LOSS or price < ema:
                    result = place_order(symbol, "Sell", qty)
                    send_telegram(f"❌ VENDITA {symbol} a {price:.2f} (qty: {qty})")
                    del positions[symbol]

        except Exception as e:
            # NON inviare più su Telegram
            print(f"[⚠️ {symbol}] Errore generale: {e}")

    time.sleep(60)
