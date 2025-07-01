import os, time, hmac, hashlib, json, requests
import yfinance as yf
import pandas as pd
from ta.trend import SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands
from dotenv import load_dotenv

# Carica le variabili d'ambiente
load_dotenv()
KEY = os.getenv("BYBIT_API_KEY")
SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

# Costanti
BASE = "https://api.bybit.com"
ORDER_USDT = 10
ASSETS = ["DOGEUSDT", "BTCUSDT"]
INTERVAL_MINUTES = 15


def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)


def notify_telegram(message: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=data, timeout=10)
        except:
            pass


def market_buy(symbol: str, qty: float):
    endpoint = f"{BASE}/v5/order/create"
    ts = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": f"{qty:.2f}"
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
    try:
        resp = requests.post(endpoint, headers=headers, data=body_json)
        log(f"BODY: {body_json}")
        log(f"RESPONSE: {resp.status_code} {resp.json()}")
    except Exception as e:
        log(f"Errore invio ordine: {e}")


def analyze(symbol: str):
    try:
        df = yf.download(tickers=symbol.replace("USDT", "-USD"), period="7d", interval="15m", progress=False, auto_adjust=True)
        if df is None or df.empty or len(df) < 60:
            return None

        df.dropna(inplace=True)
        close = df["Close"]
        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()

        df.dropna(inplace=True)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        if last["Close"] > last["bb_upper"] and last["rsi"] < 70:
            return "entry"
        elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
            return "entry"
        elif last["Close"] < last["bb_lower"] and last["rsi"] > 30:
            return "exit"
        return None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None


def get_balance(coin: str):
    endpoint = f"{BASE}/v5/account/wallet-balance"
    ts = str(int(time.time() * 1000))
    recv_window = "5000"
    query = f"accountType=UNIFIED&coin={coin}"
    payload = f"{ts}{KEY}{recv_window}{query}"
    sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2"
    }
    try:
        url = f"{endpoint}?{query}"
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            balances = data.get("result", {}).get("list", [])
            for entry in balances:
                for coin_data in entry.get("coin", []):
                    if coin_data.get("coin") == coin:
                        return float(coin_data.get("availableToWithdraw", 0))
        return 0
    except:
        return 0


def sell_all(symbol: str):
    coin = symbol.replace("USDT", "")
    balance = get_balance(coin)
    if balance > 0:
        market_sell(symbol, balance)


def market_sell(symbol: str, qty: float):
    endpoint = f"{BASE}/v5/order/create"
    ts = str(int(time.time() * 1000))
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "qty": f"{qty:.6f}"
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
    try:
        resp = requests.post(endpoint, headers=headers, data=body_json)
        log(f"SELL BODY: {body_json}")
        log(f"SELL RESPONSE: {resp.status_code} {resp.json()}")
    except Exception as e:
        log(f"Errore invio ordine SELL: {e}")


if __name__ == "__main__":
    log("🔄 Avvio sistema di acquisto iniziale (DOGE + BTC)")
    market_buy("DOGEUSDT", 10.00)
    market_buy("BTCUSDT", 10.00)

    while True:
        for symbol in ASSETS:
            signal = analyze(symbol)
            if signal:
                msg = f"📢 Segnale {signal.upper()} su {symbol}"
                log(msg)
                notify_telegram(msg)
                if signal == "entry":
                    market_buy(symbol, ORDER_USDT)
                elif signal == "exit":
                    sell_all(symbol)
        time.sleep(INTERVAL_MINUTES * 60)
