import os
import time
import requests
import json
import yfinance as yf
import talib
import pandas as pd

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SYMBOLS = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD"]
SENT_SIGNALS_FILE = "sent_signals.json"  # per evitare duplicati

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

def fetch_data(symbol):
    data = yf.download(tickers=symbol, period="7d", interval="1h")
    return data

def generate_signal(df):
    df["MA_Short"] = talib.SMA(df["Close"], timeperiod=9)
    df["MA_Long"] = talib.SMA(df["Close"], timeperiod=21)
    df["Signal"] = 0
    df.loc[df["MA_Short"] > df["MA_Long"], "Signal"] = 1
    df.loc[df["MA_Short"] < df["MA_Long"], "Signal"] = -1
    return df

def get_new_signal(df):
    if df["Signal"].iloc[-1] == 1 and df["Signal"].iloc[-2] <= 0:
        return "BUY"
    elif df["Signal"].iloc[-1] == -1 and df["Signal"].iloc[-2] >= 0:
        return "SELL"
    return None

def load_sent_signals():
    try:
        with open(SENT_SIGNALS_FILE, "r") as f:
            return json.load(f)
    except:
        return {}

def save_sent_signals(signals):
    with open(SENT_SIGNALS_FILE, "w") as f:
        json.dump(signals, f)

def main():
    sent = load_sent_signals()

    for symbol in SYMBOLS:
        df = fetch_data(symbol)
        if df is None or df.empty:
            continue

        df = generate_signal(df)
        signal = get_new_signal(df)
        if not signal:
            continue

        price = round(df["Close"].iloc[-1], 2)
        msg_id = f"{symbol}_{df.index[-1].isoformat()}_{signal}"

        if msg_id not in sent:
            emoji = "📈" if signal == "BUY" else "📉"
            message = f"{emoji} Segnale di {'ENTRATA' if signal == 'BUY' else 'USCITA'}\nAsset: {symbol}\nPrezzo: {price}\nStrategia: Cross MA"
            notify_telegram(message)
            sent[msg_id] = True
            log(f"Segnale inviato: {message}")

    save_sent_signals(sent)

if __name__ == "__main__":
    log("🔍 Avvio analisi tecnica e invio segnali Telegram")
    main()
