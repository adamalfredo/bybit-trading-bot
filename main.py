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
    """Analizza l'asset e restituisce informazioni o errori."""
    symbol_clean = symbol.replace("-USD", "USDT")
    result = {"symbol": symbol_clean}
    try:
        df = yf.download(
            tickers=symbol,
            period="7d",
            interval="15m",
            progress=False,
            auto_adjust=True,
        )

        if df is None or df.empty or len(df) < 60:
            result["error"] = "dati insufficienti"
            return result

        # In alcune versioni `yf.download` restituisce colonne MultiIndex anche
        # per un singolo ticker. Questo causa errori nelle librerie di
        # analisi tecnica che si aspettano serie 1-D.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(-1)

        # Rende i nomi delle colonne uniformi in minuscolo e senza spazi
        df.columns = [str(c).strip().lower().replace(" ", "_") for c in df.columns]

        # In alcune release esiste solo "adj_close" oppure solo "close".
        if "close" not in df.columns and "adj_close" in df.columns:
            df.rename(columns={"adj_close": "close"}, inplace=True)

        if "close" not in df.columns:
            result["error"] = "colonna Close assente"
            return result

        df.dropna(inplace=True)

        # Indicatori tecnici
        bb = BollingerBands(close=df["close"], window=20, window_dev=2)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()

        rsi = RSIIndicator(close=df["close"], window=14)
        df["rsi"] = rsi.rsi()

        sma20 = SMAIndicator(close=df["close"], window=20)
        sma50 = SMAIndicator(close=df["close"], window=50)
        df["sma20"] = sma20.sma_indicator()
        df["sma50"] = sma50.sma_indicator()

        df.dropna(inplace=True)

        last = df.iloc[-1]
        prev = df.iloc[-2]
        last_price = float(last["close"])

        result.update(
            {
                "price": round(last_price, 2),
                "rsi": round(float(last["rsi"]), 2),
                "sma20": round(float(last["sma20"]), 2),
                "sma50": round(float(last["sma50"]), 2),
                "signal": None,
            }
        )

        if last_price > last["bb_upper"] and last["rsi"] < 70:
            result["signal"] = {"type": "entry", "strategy": "Breakout Bollinger"}
        elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
            result["signal"] = {"type": "entry", "strategy": "Golden Cross"}
        elif last_price < last["bb_lower"] and last["rsi"] > 30:
            result["signal"] = {"type": "exit", "strategy": "Breakdown"}

        return result

    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        result["error"] = str(e)
        return result


def scan_assets():
    for asset in ASSET_LIST:
        result = analyze_asset(asset)
        if "error" in result:
            err_msg = f"⚠️ Errore analisi {result['symbol']}: {result['error']}"
            log(err_msg)
            notify_telegram(err_msg)
            continue

        if result.get("signal"):
            sig = result["signal"]
            tipo = "📈 Segnale di ENTRATA" if sig["type"] == "entry" else "📉 Segnale di USCITA"
            msg = f"""{tipo}
Asset: {result['symbol']}
Prezzo: {result['price']}
Strategia: {sig['strategy']}"""
            log(msg.replace("\n", " | "))
            notify_telegram(msg)

        mini_msg = (
            f"📊 Mini-analisi {result['symbol']}\n"
            f"Prezzo: {result['price']}\n"
            f"RSI: {result['rsi']}\n"
            f"SMA20: {result['sma20']}\n"
            f"SMA50: {result['sma50']}"
        )
        log(mini_msg.replace("\n", " | "))
        notify_telegram(mini_msg)


if __name__ == "__main__":
    log("🔄 Avvio sistema di monitoraggio segnali reali")
    notify_telegram("🔔 Test: bot avviato correttamente")
    while True:
        try:
            scan_assets()
        except Exception as e:
            log(f"Errore nel ciclo principale: {e}")
        time.sleep(INTERVAL_MINUTES * 60)
