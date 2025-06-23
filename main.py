import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from ta.volatility import BollingerBands
from ta.trend import SMAIndicator
from ta.momentum import RSIIndicator
from dotenv import load_dotenv

# Carica variabili da .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

ASSET_LIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]  # Rimosso MATIC-USD
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

        if df.empty or len(df) < 50:
            return None

        df.dropna(inplace=True)

        close = df["Close"]
        high = df["High"]
        low = df["Low"]

        sma_20 = SMAIndicator(close, window=20).sma_indicator()
        sma_50 = SMAIndicator(close, window=50).sma_indicator()
        rsi = RSIIndicator(close, window=14).rsi()
        bb = BollingerBands(close, window=20)
        upper = bb.bollinger_hband()
        lower = bb.bollinger_lband()

        last_price = close.iloc[-1]
        last_rsi = rsi.iloc[-1] if not pd.isna(rsi.iloc[-1]) else 50

        # Allineamento indici per confronto
        s20, s50 = sma_20.align(sma_50, join="inner")

        # Golden Cross
        if s20.iloc[-2] < s50.iloc[-2] and s20.iloc[-1] > s50.iloc[-1]:
            return {
                "type": "entry",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last_price, 2),
                "strategy": "Golden Cross"
            }

        # Breakout Volatilità
        if last_price > upper.iloc[-1] and last_rsi < 70:
            return {
                "type": "entry",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last_price, 2),
                "strategy": "Breakout Volatilità"
            }

        # Take Profit / Breakdown
        if last_price < lower.iloc[-1] and last_rsi > 30:
            return {
                "type": "exit",
                "symbol": symbol.replace("-USD", "USDT"),
                "price": round(last_price, 2),
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
