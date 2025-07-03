import os
import time
import hmac
import json
import hashlib
from decimal import Decimal
import requests
import yfinance as yf
import pandas as pd
from ta.volatility import BollingerBands
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from typing import Optional

# NON usare load_dotenv() su Railway!
# from dotenv import load_dotenv
# load_dotenv()

# Le variabili sono caricate automaticamente da Railway
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
KEY = BYBIT_API_KEY
SECRET = BYBIT_API_SECRET
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = (
    "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
)
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()
ORDER_USDT = 50
ASSETS = [
    "DOGEUSDT", "BTCUSDT", "AVAXUSDT", "SOLUSDT", "ETHUSDT", "LINKUSDT",
    "ARBUSDT", "OPUSDT", "LTCUSDT", "XRPUSDT",
    "TONUSDT", "MATICUSDT", "MNTUSDT"
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
        except Exception as e:
            log(f"Errore invio Telegram: {e}")

def market_buy(symbol: str, usdt: float):
    endpoint = f"{BYBIT_BASE_URL}/v5/order/create"
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

def get_instrument_info(symbol: str):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    try:
        params = {"category": "spot", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()

        if data.get("retCode") == 0:
            instruments = data.get("result", {}).get("list", [])
            if instruments:
                info = instruments[0]
                lot_filter = info.get("lotSizeFilter", {})

                # Priorità: qtyStep → basePrecision → fallback
                qty_step_str = lot_filter.get("qtyStep")
                if qty_step_str:
                    qty_step = float(qty_step_str)
                    precision = abs(Decimal(qty_step_str).as_tuple().exponent)
                    return qty_step, precision

                base_precision_str = lot_filter.get("basePrecision")
                if base_precision_str:
                    precision = abs(Decimal(base_precision_str).as_tuple().exponent)
                    qty_step = 1 / (10 ** precision) if precision > 0 else 1
                    return qty_step, precision

        log(f"⚠️ Errore get_instrument_info per {symbol}: {data}")
    except Exception as e:
        log(f"⚠️ Errore richiesta get_instrument_info: {e}")

    # Fallback se tutto fallisce
    return 0.0001, 4

def get_last_price(symbol: str) -> Optional[float]:
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "spot", "symbol": symbol}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            lst = data.get("result", {}).get("list")
            if lst and "lastPrice" in lst[0]:
                return float(lst[0]["lastPrice"])
    except Exception as e:
        log(f"Errore ottenimento prezzo {symbol}: {e}")
    return None

def market_sell(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile vendere")
        return

    qty_step, precision = get_instrument_info(symbol)
    try:
        dec_qty = Decimal(str(qty))
        step = Decimal(str(qty_step))
        rounded_qty = (dec_qty // step) * step

        if rounded_qty <= 0:
            log(f"❌ Quantità troppo piccola per {symbol} (dopo arrotondamento)")
            return

        if precision == 0:
            qty_str = str(int(rounded_qty))
        else:
            qty_str = f"{rounded_qty:.{precision}f}".rstrip('0').rstrip('.')

    except Exception as e:
        log(f"❌ Errore arrotondamento quantità {symbol}: {e}")
        return

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Market",
        "qty": qty_str
    }

    ts = str(int(time.time() * 1000))
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
        resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        data = resp.json()
        log(f"SELL BODY: {body_json}")
        log(f"RESPONSE: {resp.status_code} {data}")
        if data.get("retCode") != 0:
            notify_telegram(f"❌ Errore ordine SELL {symbol}: {data.get('retMsg')} ({data.get('retCode')})")
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
            return None, None, None

        df.dropna(inplace=True)
        close = find_close_column(df)
        if close is None:
            return None, None, None

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
            return "entry", "Breakout Bollinger", price
        elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
            return "entry", "Incrocio SMA 20/50", price
        elif last["Close"] < last["bb_lower"] and last["rsi"] > 30:
            return "exit", "Rimbalzo RSI + BB", price
        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

def get_free_qty(symbol: str) -> float:
    """Restituisce il saldo disponibile per la coin indicata (come nel vecchio main)."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        return 0.0

    endpoint = f"{BYBIT_BASE_URL}/v5/account/wallet-balance"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    coin = symbol.replace("USDT", "")
    params = {"accountType": BYBIT_ACCOUNT_TYPE, "coin": coin}
    param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature_payload = f"{timestamp}{BYBIT_API_KEY}{recv_window}{param_str}"
    sign = hmac.new(SECRET.encode(), signature_payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2",
    }
    try:
        resp = requests.get(f"{endpoint}?{param_str}", headers=headers, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            result = data.get("result", {})
            lists = result.get("list") or result.get("balances")
            if isinstance(lists, list):
                for item in lists:
                    coins = (
                        item.get("coin")
                        or item.get("coins")
                        or item.get("balances")
                        or []
                    )
                    for c in coins:
                        if c.get("coin") == coin:
                            for key in (
                                "availableToWithdraw",
                                "availableBalance",
                                "walletBalance",
                                "free",
                                "transferBalance",
                                "equity",
                                "total",
                            ):
                                if key in c and c[key] is not None:
                                    try:
                                        return float(c[key])
                                    except (TypeError, ValueError):
                                        continue
            log(f"Coin {coin} non trovata nella risposta saldo: {data.get('result')!r}")
        else:
            log(f"Errore saldo {coin}: {data}")
    except Exception as e:
        log(f"Errore ottenimento saldo {coin}: {e}")
    return 0.0

if __name__ == "__main__":
    log("🔄 Avvio sistema di acquisto")
    notify_telegram("🔄 Avvio sistema di acquisto")
    notify_telegram("✅ Connessione a Bybit riuscita")
    notify_telegram("🧪 BOT avviato correttamente")
    # ⚠️ TEST REALE DI VENDITA BTC (rimuovere dopo il test)
    # test_symbol = "BTCUSDT"
    # test_price = 88888.88
    # test_strategy = "TEST REALE - VENDITA BTC"
    # notify_telegram(f"📉 TEST DI VENDITA REALE\nAsset: {test_symbol}\nPrezzo: {test_price}\nStrategia: {test_strategy}")
    # qty = get_free_qty(test_symbol)
    # if qty > 0:
    #     market_sell(test_symbol, qty)
    #     log(f"✅ TEST vendita BTC completata")
    #     notify_telegram(f"✅ TEST vendita BTC completata")
    # else:
    #     log(f"❌ TEST vendita BTC fallita: saldo insufficiente o troppo piccolo")
    #     notify_telegram(f"❌ TEST vendita BTC fallita: saldo insufficiente o troppo piccolo")

    # market_buy("DOGEUSDT", ORDER_USDT)
    # market_buy("BTCUSDT", ORDER_USDT)

    # ⚠️ TEST NOTIFICA TELEGRAM CON ORDINE FINTA ENTRATA (da rimuovere dopo il test)
    # test_symbol = "BTCUSDT"
    # test_price = 99999.99
    # test_strategy = "TEST - Finto Segnale"
    # notify_telegram(f"📈 Segnale di ENTRATA\nAsset: {test_symbol}\nPrezzo: {test_price}\nStrategia: {test_strategy}")
    # market_buy(test_symbol, ORDER_USDT)
    # log(f"✅ TEST completato per {test_symbol} con ordine finto e notifica Telegram.")

    # ⚠️ TEST NOTIFICA TELEGRAM CON ORDINE FINTA USCITA (da rimuovere dopo il test)
    # test_symbol = "DOGEUSDT"
    # test_price = 88888.88
    # test_strategy = "TEST - Finto SELL"
    # notify_telegram(f"📉 Segnale di USCITA\nAsset: {test_symbol}\nPrezzo: {test_price}\nStrategia: {test_strategy}")
    # qty = get_free_qty(test_symbol)
    # qty_int = int(qty)  # 🧠 forza solo parte intera (es. 10.75 → 10)
    # if qty_int > 0:
    #     market_sell(test_symbol, qty_int)
    # else:
    #     log(f"❌ TEST vendita fallito: saldo insufficiente o troppo piccolo per {test_symbol}")

    while True:
        for symbol in ASSETS:
            signal, strategy, price = analyze_asset(symbol)
            log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")
            if signal:
                if signal == "entry":
                    notify_telegram(f"📈 Segnale di ENTRATA\nAsset: {symbol}\nPrezzo: {price:.2f}\nStrategia: {strategy}")
                    market_buy(symbol, ORDER_USDT)
                    log(f"✅ Acquisto completato per {symbol}")
                    notify_telegram(f"✅ Acquisto completato per {symbol}")
                elif signal == "exit":
                    notify_telegram(f"📉 Segnale di USCITA\nAsset: {symbol}\nPrezzo: {price:.2f}\nStrategia: {strategy}")
                    qty = get_free_qty(symbol)
                    if qty > 0:
                        market_sell(symbol, qty)
                        log(f"✅ Vendita completata per {symbol}")
                        notify_telegram(f"✅ Vendita completata per {symbol}")
                    else:
                        log(f"❌ Vendita ignorata per {symbol}: saldo insufficiente o troppo piccolo")
                        notify_telegram(f"❌ Vendita ignorata per {symbol}: saldo insufficiente o troppo piccolo")
        time.sleep(INTERVAL_MINUTES * 60)

