import os, time, hmac, hashlib, json, requests, yfinance as yf, pandas as pd
from ta.trend import SMAIndicator
from ta.momentum import RSIIndicator
from ta.volatility import BollingerBands
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("BYBIT_API_KEY")
SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BASE = "https://api.bybit.com"
ORDER_USDT = 10
ASSETS = [
    "DOGEUSDT", "BTCUSDT", "AVAXUSDT", "SOLUSDT", "ETHUSDT", "LINKUSDT", "ARBUSDT", "OPUSDT", "LTCUSDT", "XRPUSDT"
]
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
    try:
        resp = requests.post(endpoint, headers=headers, data=body_json)
        log(f"BODY: {body_json}")
        log(f"RESPONSE: {resp.status_code} {resp.json()}")
    except Exception as e:
        log(f"Errore invio ordine BUY: {e}")

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
        log(f"RESPONSE: {resp.status_code} {resp.json()}")
    except Exception as e:
        log(f"Errore invio ordine SELL: {e}")

def fetch_history(symbol: str):
    ticker = symbol.replace("USDT", "-USD")
    df = yf.download(tickers=ticker, period="7d", interval="15m", progress=False, auto_adjust=True)
    if df is None or df.empty or len(df) < 60:
        return None
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df

def find_close_column(df: pd.DataFrame):
    for name in df.columns:
        if "close" in name.lower():
            return df[name]
    return None

def analyze_asset(symbol: str):
    try:
        df = fetch_history(symbol)
        if df is None:
            return None, None

        df.dropna(inplace=True)
        close = find_close_column(df)
        if close is None:
            return None, None

        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()

        df.dropna(inplace=True)
        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last["Close"])

        if last["Close"] > last["bb_upper"] and last["rsi"] < 70:
            return "entry", f"Breakout Bollinger"
        elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
            return "entry", f"Incrocio SMA 20/50"
        elif last["Close"] < last["bb_lower"] and last["rsi"] > 30:
            return "exit", f"Rimbalzo RSI + BB"
        return None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None

def get_free_qty(symbol: str):
    try:
        endpoint = f"{BASE}/v5/account/wallet-balance"
        ts = str(int(time.time() * 1000))
        payload = f"{ts}{KEY}5000"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": "5000",
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json"
        }
        resp = requests.get(endpoint, headers=headers)
        data = resp.json()
        coin = symbol.replace("USDT", "")
        for item in data.get("result", {}).get("list", [])[0].get("coin", []):
            if item.get("coin") == coin:
                return float(item.get("availableToWithdraw", 0))
    except Exception as e:
        log(f"Errore lettura balance: {e}")
    return 0

if __name__ == "__main__":
    log("🔄 Avvio sistema di acquisto iniziale (DOGE + BTC)")
    notify_telegram("✅ Connessione a Bybit riuscita")
    notify_telegram("🧪 Test: bot avviato correttamente")

    # market_buy("DOGEUSDT", ORDER_USDT)
    # market_buy("BTCUSDT", ORDER_USDT)

    while True:
        for symbol in ASSETS:
            signal, strategy = analyze_asset(symbol)
            if signal:
                price = fetch_history(symbol).iloc[-1]["Close"]
                if signal == "entry":
                    notify_telegram(f"\uD83D\uDCC8 Segnale di ENTRATA\nAsset: {symbol}\nPrezzo: {price:.2f}\nStrategia: {strategy}")
                    market_buy(symbol, ORDER_USDT)
                elif signal == "exit":
                    notify_telegram(f"\uD83D\uDCC9 Segnale di USCITA\nAsset: {symbol}\nPrezzo: {price:.2f}\nStrategia: {strategy}")
                    qty = get_free_qty(symbol)
                    if qty > 0:
                        market_sell(symbol, qty)
        time.sleep(INTERVAL_MINUTES * 60)
