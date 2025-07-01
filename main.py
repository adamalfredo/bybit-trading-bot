import os
import time
import hmac
import hashlib
import json
import requests
from dotenv import load_dotenv

load_dotenv()
API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
BASE_URL = "https://api.bybit.com"
ORDER_USDT = float(os.getenv("ORDER_USDT", 10))

def market_buy(symbol: str, usdt_amount: float):
    endpoint = f"{BASE_URL}/v5/order/create"
    timestamp = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": f"{usdt_amount:.2f}"
    }
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
    payload = f"{timestamp}{API_KEY}5000{body_json}"
    sign = hmac.new(API_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }

    response = requests.post(endpoint, headers=headers, data=body_json)
    print(f"📤 Ordine MARKET inviato per {symbol} con {usdt_amount:.2f} USDT")
    print("BODY:", body_json)
    print("RESPONSE:", response.status_code, response.json())

if __name__ == "__main__":
    market_buy("DOGEUSDT", ORDER_USDT)
    market_buy("BTCUSDT", ORDER_USDT)
