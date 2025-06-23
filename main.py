import os
import time
import requests
import yfinance as yf
import numpy as np
import pandas as pd
from dotenv import load_dotenv
import ta

# Carica le variabili di ambiente
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

        if df is None or len(df) < 50:
            return None

        df.dropna(inplace=True)

        df['sma_20'] = ta.trend.sma_indicator(df['Close'], window=20)
        df['sma_50'] = ta.trend.sma_indicator(df['Close'], window=50)
        bb = ta.volatility.BollingerBands(df['Close'], window=20)
        df['bb_upper'] = bb.bollinger_hband()
        df['bb_lower'] = bb.bollinger_lband()
        df['rsi'] = ta.momentum.RSIIndicator(df['Close'], window=14).rsi()

        last = df.iloc[-1]
        prev = df.iloc[-2]

        last_price = last['Close']
        last_rsi = last['rsi']

        # Breakout Bollinger con RSI non troppo alto
        if last_price > last['bb_upper'] and last_rsi < 70:
            return {
                "type": "entry",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last_price, 2),
                "strategy": "Breakout Volatilità"
            }

        # Golden Cross
        if prev['sma_20'] < prev['sma_50'] and last['sma_20'] > last['sma_50']:
            return {
                "type": "entry",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last_price, 2),
                "strategy": "Golden Cross"
            }

        # Uscita - take profit su breakdown e RSI in calo
        if last_price < last['bb_lower'] and last_rsi > 30:
            return {
                "type": "exit",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last_price, 2),
                "strategy": "Breakdown + RSI"
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
