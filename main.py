import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
import ta
from dotenv import load_dotenv

# Carica variabili da .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSET_LIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INTERVAL_MINUTES = 15


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


def analyze_asset(symbol):
    try:
        df = yf.download(tickers=symbol, period="7d", interval="15m", progress=False)

        if df is None or df.empty or len(df) < 60:
            raise ValueError("Dati insufficienti")

        df.dropna(inplace=True)

        # Usa solo le Series, non DataFrame
        close = df['Close']
        high = df['High']
        low = df['Low']

        df['rsi'] = ta.momentum.RSIIndicator(close=close).rsi()
        bb = ta.volatility.BollingerBands(close=close)
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_lower'] = bb.bollinger_lband()
        df['sma_20'] = close.rolling(window=20).mean()
        df['sma_50'] = close.rolling(window=50).mean()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        if last['Close'] > last['bb_upper'] and last['rsi'] < 70:
            return {
                "type": "entry",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last['Close'], 2),
                "strategy": "Breakout Volatilità"
            }

        if prev['sma_20'] < prev['sma_50'] and last['sma_20'] > last['sma_50']:
            return {
                "type": "entry",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last['Close'], 2),
                "strategy": "Golden Cross"
            }

        if last['Close'] < last['bb_lower'] and last['rsi'] > 30:
            return {
                "type": "exit",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last['Close'], 2),
                "strategy": "Take Profit / Breakdown"
            }

        return None

    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None


def scan_assets():
    for asset in ASSET_LIST:
        signal = analyze_asset(asset)
        if signal:
            tipo = "📈 Segnale di ENTRATA" if signal["type"] == "entry" else "📉 Segnale di USCITA"
            msg = f"""{tipo}
Asset: {signal['symbol']}
Prezzo: {signal['price']}
Strategia: {signal['strategy']}"""
            log(msg.replace("\n", " | "))
            notify_telegram(msg)


if __name__ == "__main__":
    log("🔄 Avvio sistema di monitoraggio segnali reali")
    while True:
        try:
            scan_assets()
        except Exception as e:
            log(f"Errore nel ciclo principale: {e}")
        time.sleep(INTERVAL_MINUTES * 60)
