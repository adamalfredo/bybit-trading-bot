
import os, time, hmac, hashlib, json, requests, yfinance as yf, pandas as pd
from dotenv import load_dotenv
from ta.trend import SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands

load_dotenv()
KEY = os.getenv("BYBIT_API_KEY")
SECRET = os.getenv("BYBIT_API_SECRET")
BASE = "https://api.bybit.com"
ORDER_USDT = float(os.getenv("ORDER_USDT", "10"))

SYMBOLS = ["BTCUSDT", "DOGEUSDT"]

def market_buy(symbol: str, usdt: float):
    endpoint = f"{BASE}/v5/order/create"
    ts = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": f"{usdt:.2f}"
    }
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
    payload = f"{ts}{KEY}5000{body_json}"
    sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": "5000",
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json"
    }
    resp = requests.post(endpoint, headers=headers, data=body_json)
    print(f"BODY ({symbol}):", body_json)
    print(f"RESPONSE ({symbol}):", resp.status_code, resp.json())

def check_signal(symbol: str) -> bool:
    df = yf.download(tickers=symbol, period="7d", interval="15m", progress=False)
    if df.empty or len(df) < 20:
        return False
    close = df["Close"]
    sma = SMAIndicator(close, window=20).sma_indicator()
    rsi = RSIIndicator(close, window=14).rsi()
    bb = BollingerBands(close)
    upper = bb.bollinger_hband()
    lower = bb.bollinger_lband()
    if close.iloc[-1] > upper.iloc[-1] and rsi.iloc[-1] > 70:
        return True
    return False

if __name__ == "__main__":
    print("🔄 Avvio sistema di monitoraggio segnali reali")
    for symbol in SYMBOLS:
        if check_signal(symbol):
            print(f"📈 Segnale di acquisto rilevato per {symbol}")
            market_buy(symbol, ORDER_USDT)
        else:
            print(f"⛔ Nessun segnale per {symbol}")
