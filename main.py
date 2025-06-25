import os
import time
import hmac
import json
import hashlib
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from dotenv import load_dotenv
from typing import Optional

# Carica variabili da .env
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = (
    "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
)

ORDER_USDT = float(os.getenv("ORDER_USDT", "5"))

ASSET_LIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INTERVAL_MINUTES = 15
DOWNLOAD_RETRIES = 3


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


def _sign(payload: str) -> str:
    """Restituisce la firma HMAC richiesta dalle API Bybit."""
    if not BYBIT_API_SECRET:
        return ""
    return hmac.new(
        BYBIT_API_SECRET.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()


def fetch_history(symbol: str) -> pd.DataFrame:
    """Scarica i dati da Yahoo Finance con alcuni tentativi."""
    for attempt in range(1, DOWNLOAD_RETRIES + 1):
        try:
            df = yf.download(
                tickers=symbol,
                period="7d",
                interval="15m",
                progress=False,
                auto_adjust=True,
            )
            if df is not None and not df.empty:
                return df
        except Exception as e:
            log(f"Errore download {symbol} ({attempt}/{DOWNLOAD_RETRIES}): {e}")
        time.sleep(2)
    return pd.DataFrame()


def send_order(symbol: str, side: str, quantity: float) -> None:
    """Invia un ordine di mercato su Bybit."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log("Chiavi Bybit mancanti: ordine non inviato")
        return

    endpoint = f"{BYBIT_BASE_URL}/v5/order/create"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "Market",
        "qty": str(quantity),
        "timeInForce": "IOC",
    }
    body_json = json.dumps(body, separators=(",", ":"), sort_keys=True)
    signature_payload = f"{timestamp}{BYBIT_API_KEY}{recv_window}{body_json}"
    signature = _sign(signature_payload)

    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2",
        "Content-Type": "application/json",
    }

    try:
        resp = requests.post(endpoint, headers=headers, data=body_json, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"Errore ordine {symbol}: {data}")
        else:
            log(f"Ordine {side} {symbol} inviato: {data}")
    except Exception as e:
        log(f"Errore invio ordine {symbol}: {e}")


def test_bybit_connection() -> None:
    """Esegue una semplice chiamata autenticata per verificare le API."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log("Chiavi Bybit mancanti: impossibile testare la connessione")
        return

    endpoint = f"{BYBIT_BASE_URL}/v5/account/info"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    signature_payload = f"{timestamp}{BYBIT_API_KEY}{recv_window}"
    signature = _sign(signature_payload)
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2",
    }
    try:
        resp = requests.get(endpoint, headers=headers, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            log("✅ Connessione a Bybit riuscita")
        else:
            log(f"Test Bybit fallito: {data}")
    except Exception as e:
        log(f"Errore connessione Bybit: {e}")


def initial_buy_test() -> None:
    """Esegue un acquisto di prova di BTC per verificare il collegamento."""
    log(f"⚡ Ordine di test: acquisto BTC per {ORDER_USDT} USDT")
    df = fetch_history("BTC-USD")
    if df is None or df.empty:
        log("Impossibile ottenere il prezzo BTC per l'ordine di test")
        return

    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    close_col = find_close_column(df)
    if close_col and close_col != "close":
        df.rename(columns={close_col: "close"}, inplace=True)

    if "close" not in df.columns:
        cols = ", ".join(df.columns)
        log(f"Colonna Close assente nel test ({cols})")
        return

    df.dropna(inplace=True)

    price = float(df.iloc[-1]["close"])
    qty = round(ORDER_USDT / price, 6)
    send_order("BTCUSDT", "Buy", qty)


def find_close_column(df: pd.DataFrame) -> Optional[str]:
    """Trova il nome della colonna di chiusura, se esiste."""
    cols = [str(c).strip().lower().replace(" ", "_") for c in df.columns]
    df.columns = cols
    priority = [
        "close",
        "adj_close",
        "close_price",
        "closing_price",
        "closeprice",
        "closingprice",
        "last",
        "c",
    ]
    for p in priority:
        if p in df.columns:
            return p
    for c in df.columns:
        if "close" in c:
            return c
    return None


def analyze_asset(symbol):
    """Analizza l'asset e restituisce informazioni o errori."""
    symbol_clean = symbol.replace("-USD", "USDT")
    result = {"symbol": symbol_clean}
    try:
        df = fetch_history(symbol)

        if df is None or df.empty or len(df) < 60:
            result["error"] = "dati insufficienti"
            return result

        # In alcune versioni `yf.download` restituisce colonne MultiIndex anche
        # per un singolo ticker. Questo causa errori nelle librerie di
        # analisi tecnica che si aspettano serie 1-D. L'indice 0 contiene i
        # nomi delle colonne reali, mentre l'ultimo livello contiene il ticker
        # ripetuto. Viene quindi utilizzato `get_level_values(0)`.
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        close_col = find_close_column(df)
        if close_col and close_col != "close":
            df.rename(columns={close_col: "close"}, inplace=True)

        if "close" not in df.columns:
            cols = ", ".join(df.columns)
            result["error"] = f"colonna Close assente ({cols})"
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

            qty = round(ORDER_USDT / result["price"], 6)
            side = "Buy" if sig["type"] == "entry" else "Sell"
            send_order(result["symbol"], side, qty)

        # Le mini-analisi sono state rimosse: il bot ora invia solo i segnali


if __name__ == "__main__":
    log("🔄 Avvio sistema di monitoraggio segnali reali")
    test_bybit_connection()
    notify_telegram("🔔 Test: bot avviato correttamente")
    initial_buy_test()
    while True:
        try:
            scan_assets()
        except Exception as e:
            log(f"Errore nel ciclo principale: {e}")
        time.sleep(INTERVAL_MINUTES * 60)
