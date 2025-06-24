import os
import time
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
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
        df = yf.download(
            tickers=symbol,
            period="7d",
            interval="15m",
            progress=False,
            auto_adjust=True,
        )

        if df is None or df.empty or len(df) < 60:
            return None

        # In alcune versioni `yf.download` restituisce colonne MultiIndex anche
        # per un singolo ticker. Questo causa errori nelle librerie di
        # analisi tecnica che si aspettano serie 1-D.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(-1)

        # Normalizza i nomi delle colonne e gestisce la possibile presenza di
        # "Adj Close" al posto di "Close" nelle versioni recenti di yfinance.
        df.columns = [str(c).strip().title() for c in df.columns]
        if "Adj Close" in df.columns and "Close" not in df.columns:
            df.rename(columns={"Adj Close": "Close"}, inplace=True)

        if "Close" not in df.columns:
            return None

        df.dropna(inplace=True)

        # Indicatori tecnici
        bb = BollingerBands(close=df["Close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()

        rsi = RSIIndicator(close=df["Close"], window=14)
        df["rsi"] = rsi.rsi()

        sma20 = SMAIndicator(close=df["Close"], window=20)
        sma50 = SMAIndicator(close=df["Close"], window=50)
        df["sma20"] = sma20.sma_indicator()
        df["sma50"] = sma50.sma_indicator()

        df.dropna(inplace=True)

        last = df.iloc[-1]
        prev = df.iloc[-2]
        last_price = last["Close"]
        symbol_clean = symbol.replace("-USD", "USDT")

        if last_price > last["bb_upper"] and last["rsi"] < 70:
            return {
                "type": "entry",
                "symbol": symbol_clean,
                "price": round(last_price, 2),
                "strategy": "Breakout Bollinger"
            }

        if prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
            return {
                "type": "entry",
                "symbol": symbol_clean,
                "price": round(last_price, 2),
                "strategy": "Golden Cross"
            }

        if last_price < last["bb_lower"] and last["rsi"] > 30:
            return {
                "type": "exit",
                "symbol": symbol_clean,
                "price": round(last_price, 2),
                "strategy": "Breakdown"
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
