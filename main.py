import os
import time
import hmac
import json
import hashlib
from decimal import Decimal
import requests
import pandas as pd
from ta.volatility import BollingerBands, AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
from typing import Optional

# ✅ Logger

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

# ✅ Telegram

def notify_telegram(message: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=data, timeout=10)
        except Exception as e:
            log(f"Errore invio Telegram: {e}")

# ✅ ENV VARS
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
KEY = BYBIT_API_KEY
SECRET = BYBIT_API_SECRET
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()
BYBIT_BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"

# ✅ CONFIG
ORDER_USDT = 50
INTERVAL_MINUTES = 15
ATR_WINDOW = 14
TP_FACTOR = 2.0
SL_FACTOR = 1.5
TRAILING_ACTIVATION_THRESHOLD = 0.001
TRAILING_SL_BUFFER = 0.007
TRAILING_DISTANCE = 0.02
INITIAL_STOP_LOSS_PCT = 0.02
COOLDOWN_MINUTES = 60

# ✅ ASSET LIST
ASSETS = [
    "WIFUSDT", "PEPEUSDT", "BONKUSDT", "INJUSDT", "RNDRUSDT", "SUIUSDT", "SEIUSDT",
    "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT",
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]

VOLATILE_ASSETS = [
    "BONKUSDT", "PEPEUSDT", "WIFUSDT", "RNDRUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT"
]

# ✅ GLOBAL STATE
open_positions = set()
position_data = {}
last_exit_time = {}
cooldown = {}

# ✅ PLACEHOLDERS

def market_buy(symbol, usdt):
    return type("resp", (), {"status_code": 200, "json": lambda: {"retCode": 0}})()

def market_sell(symbol, qty):
    return type("resp", (), {"status_code": 200, "json": lambda: {"retCode": 0}})()

def get_free_qty(symbol):
    return 100.0

def fetch_history(symbol):
    idx = pd.date_range(end=pd.Timestamp.now(), periods=100, freq=f"{INTERVAL_MINUTES}min")
    df = pd.DataFrame(index=idx)
    df["Open"] = df["High"] = df["Low"] = df["Close"] = df["Volume"] = 100.0
    return df

def analyze_asset(symbol):
    df = fetch_history(symbol)
    price = df["Close"].iloc[-1]
    return "entry" if symbol.endswith("USDT") else None, "Dummy", price

def notify_trade_result(symbol, signal, price, strategy):
    msg = f"✅ Operazione {signal} su {symbol} a {price:.2f} via {strategy}"
    notify_telegram(msg)

def log_trade_to_google(*args, **kwargs):
    pass

# ✅ OPERATING LOOP
if __name__ == "__main__":
    log("🔄 Avvio sistema di monitoraggio segnali reali")
    notify_telegram("🤖 BOT AVVIATO - In ascolto per segnali di ingresso/uscita")

    while True:
        for symbol in ASSETS:
            signal, strategy, price = analyze_asset(symbol)
            log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

            # ✅ Entry
            if signal == "entry" and symbol not in open_positions:
                last_exit = last_exit_time.get(symbol, 0)
                if time.time() - last_exit < COOLDOWN_MINUTES * 60:
                    log(f"⏳ Cooldown attivo per {symbol}, skip entry")
                    continue

                balance = get_free_qty("USDT")
                if balance < ORDER_USDT:
                    log(f"💸 USDT insufficiente per acquistare {symbol}")
                    continue

                resp = market_buy(symbol, ORDER_USDT)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    entry_price = price
                    df = fetch_history(symbol)
                    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range().iloc[-1]

                    rsi = RSIIndicator(close=df["Close"]).rsi().iloc[-1]
                    tp_mult = 1.5 if rsi > 65 else 2.0 if rsi > 55 else 2.5
                    sl_mult = 1.0 if rsi > 65 else 1.2 if rsi > 55 else 1.5
                    if symbol in VOLATILE_ASSETS:
                        tp_mult += 0.5
                        sl_mult = max(sl_mult - 0.2, 0.8)

                    tp = entry_price + atr * tp_mult
                    sl = entry_price - atr * sl_mult
                    qty = get_free_qty(symbol)

                    position_data[symbol] = {
                        "entry_price": entry_price,
                        "tp": tp,
                        "sl": sl,
                        "qty": qty,
                        "entry_cost": ORDER_USDT,
                        "entry_time": time.time(),
                        "trailing_active": False,
                        "p_max": entry_price
                    }

                    open_positions.add(symbol)
                    notify_trade_result(symbol, "entry", entry_price, strategy)
                    log(f"📌 Entry registrato per {symbol} → TP: {tp:.4f}, SL: {sl:.4f}")

            # ✅ Exit
            elif signal == "exit" and symbol in open_positions:
                qty = get_free_qty(symbol)
                if qty <= 0:
                    continue
                usdt_before = get_free_qty("USDT")
                resp = market_sell(symbol, qty)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    entry = position_data.get(symbol, {})
                    entry_price = entry.get("entry_price", price)
                    entry_cost = entry.get("entry_cost", ORDER_USDT)
                    exit_value = price * qty
                    delta = exit_value - entry_cost
                    pnl = (delta / entry_cost) * 100

                    notify_trade_result(symbol, "exit", price, strategy)
                    log_trade_to_google(symbol, entry_price, price, pnl, strategy, "Exit", entry_cost, exit_value, delta)
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
                    cooldown[symbol] = time.time()

            # ✅ TP & SL dinamico (solo se in posizione)
            if symbol in open_positions and symbol in position_data:
                current_price = price
                entry = position_data[symbol]

                if not entry.get("trailing_active") and current_price >= entry["entry_price"] * (1 + TRAILING_ACTIVATION_THRESHOLD):
                    entry["trailing_active"] = True
                    log(f"🔛 Trailing attivo per {symbol}")

                if entry.get("trailing_active"):
                    if current_price > entry["p_max"]:
                        entry["p_max"] = current_price
                        new_sl = current_price * (1 - TRAILING_SL_BUFFER)
                        if new_sl > entry["sl"]:
                            log(f"📉 Nuovo SL per {symbol}: {new_sl:.4f}")
                            entry["sl"] = new_sl

                if current_price >= entry["tp"]:
                    log(f"🎯 TP colpito per {symbol}")
                    signal = "exit"
                    strategy = "Take Profit"
                elif current_price <= entry["sl"]:
                    log(f"🛑 SL colpito per {symbol}")
                    signal = "exit"
                    strategy = "Stop Loss"

        time.sleep(1)
        time.sleep(INTERVAL_MINUTES * 60)
