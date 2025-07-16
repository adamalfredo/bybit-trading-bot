import os
import time
import hmac
import json
import hashlib
from decimal import Decimal, ROUND_DOWN
import requests
import pandas as pd
from ta.volatility import BollingerBands, AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
from typing import Optional

# Env vars (Railway)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
BYBIT_API_KEY = os.getenv("BYBIT_API_KEY")
BYBIT_API_SECRET = os.getenv("BYBIT_API_SECRET")
KEY = BYBIT_API_KEY
SECRET = BYBIT_API_SECRET
BYBIT_TESTNET = os.getenv("BYBIT_TESTNET", "false").lower() == "true"
BYBIT_BASE_URL = "https://api-testnet.bybit.com" if BYBIT_TESTNET else "https://api.bybit.com"
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()

ORDER_USDT = 50

ASSETS = [
    "WIFUSDT", "PEPEUSDT", "BONKUSDT", "INJUSDT", "RNDRUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT",
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]

VOLATILE_ASSETS = [
    "BONKUSDT", "PEPEUSDT", "WIFUSDT", "RNDRUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT"
]

INTERVAL_MINUTES = 15
ATR_WINDOW = 14
TP_FACTOR = 2.0
SL_FACTOR = 1.5
# TRAILING_ACTIVATION_THRESHOLD = 0.001 # +0.1% activation threshold
TRAILING_ACTIVATION_THRESHOLD = 0.02
TRAILING_SL_BUFFER = 0.007
TRAILING_DISTANCE = 0.02
INITIAL_STOP_LOSS_PCT = 0.02
COOLDOWN_MINUTES = 60
cooldown = {}

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

def is_bullish_breakout_confirmed(df: pd.DataFrame) -> bool:
    if len(df) < 2:
        return False
    last = df.iloc[-1]
    prev = df.iloc[-2]
    body = abs(last["Close"] - last["Open"])
    full_range = last["High"] - last["Low"]
    if last["Close"] > last["Open"] and full_range > 0 and body > 0.6 * full_range and last["Close"] > prev["Close"]:
        return True
    return False

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

def send_signed_request(method, endpoint, params=None):
    import time, hmac, hashlib
    if params is None:
        params = {}

    api_key = BYBIT_API_KEY
    api_secret = BYBIT_API_SECRET
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"

    body = json.dumps(params, separators=(",", ":")) if method == "POST" else ""

    payload = f"{timestamp}{api_key}{recv_window}{body}"
    signature = hmac.new(
        api_secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-SIGN": signature,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "Content-Type": "application/json",
    }

    url = f"https://api.bybit.com{endpoint}"

    if method == "POST":
        response = requests.post(url, headers=headers, data=body)
    else:
        response = requests.get(url, headers=headers, params=params)

    return response.json()

def market_buy(symbol: str, order_usdt: float = 50.0):
    try:
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "quoteOrderQty": str(order_usdt)
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
            "Content-Type": "application/json"
        }

        url = "https://api.bybit.com/v5/order/create"
        response = requests.post(url, headers=headers, json=body)  # ✅ CORRETTO QUI
        data = response.json()

        log(f"BUY BODY: {body}")
        log(f"RESPONSE: {response.status_code} {data}")

        if data["retCode"] == 0:
            return True
        else:
            log(f"❌ Acquisto fallito per {symbol}")
            return False
    except Exception as e:
        log(f"❌ Errore acquisto per {symbol}: {e}")
        return False

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
    return 0.0001, 4

def market_sell(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile vendere")
        return
    order_value = qty * price
    if order_value < 5:
        log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT")
        return
    qty_step, precision = get_instrument_info(symbol)
    try:
        dec_qty = Decimal(str(qty))
        step = Decimal(str(qty_step))
        rounded_qty = (dec_qty // step) * step
        if rounded_qty <= 0:
            log(f"❌ Quantità troppo piccola per {symbol} (dopo arrotondamento)")
            return
        qty_str = str(int(rounded_qty)) if precision == 0 else f"{rounded_qty:.{precision}f}".rstrip('0').rstrip('.')
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
        log(f"SELL BODY: {body_json}")
        log(f"RESPONSE: {resp.status_code} {resp.json()}")
        return resp
    except Exception as e:
        log(f"Errore invio ordine SELL: {e}")
        return None

def fetch_history(symbol: str):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": str(INTERVAL_MINUTES),
        "limit": 100
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            log(f"[!] Errore Kline per {symbol}: {data}")
            return None
        raw = data["result"]["list"]
        df = pd.DataFrame(raw, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "turnover"
        ])
        df["timestamp"] = pd.to_datetime(df["timestamp"].astype(int), unit="ms")
        df.set_index("timestamp", inplace=True)
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df
    except Exception as e:
        log(f"[!] Errore richiesta Kline per {symbol}: {e}")
        return None

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
        close = find_close_column(df)
        if close is None:
            return None, None, None

        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()
        df["ema20"] = EMAIndicator(close=close, window=20).ema_indicator()
        df["ema50"] = EMAIndicator(close=close, window=50).ema_indicator()
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=[
            "bb_upper", "bb_lower", "rsi", "sma20", "sma50", "ema20", "ema50",
            "macd", "macd_signal", "adx", "atr"
        ], inplace=True)

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = 20 if is_volatile else 15

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])

        # Strategie per asset volatili
        if is_volatile:
            if last["Close"] > last["bb_upper"] and last["rsi"] < 70:
                return "entry", "Breakout Bollinger", price
            elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
                return "entry", "Incrocio SMA 20/50", price
            elif last["macd"] > last["macd_signal"] and last["adx"] > adx_threshold:
                return "entry", "MACD bullish + ADX", price

        # Strategie per asset stabili
        else:
            if prev["ema20"] < prev["ema50"] and last["ema20"] > last["ema50"]:
                return "entry", "Incrocio EMA 20/50", price
            elif last["macd"] > last["macd_signal"] and last["adx"] > adx_threshold:
                return "entry", "MACD bullish (stabile)", price
            elif last["rsi"] > 50 and last["ema20"] > last["ema50"]:
                return "entry", "Trend EMA + RSI", price

        # EXIT comune a tutti
        if last["Close"] < last["bb_lower"] and last["rsi"] > 30:
            return "exit", "Rimbalzo RSI + BB", price
        elif last["macd"] < last["macd_signal"] and last["adx"] > adx_threshold:
            return "exit", "MACD bearish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT AVVIATO - In ascolto per segnali di ingresso/uscita")

# Inizializza struttura base
open_positions = set()
position_data = {}
last_exit_time = {}

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

def get_free_qty(symbol: str) -> float:
    if symbol.endswith("USDT") and len(symbol) > 4:
        coin = symbol.replace("USDT", "")
    elif symbol == "USDT":
        coin = "USDT"
    else:
        coin = symbol

    url = f"{BYBIT_BASE_URL}/v5/account/wallet-balance"
    params = {"accountType": BYBIT_ACCOUNT_TYPE}

    from urllib.parse import urlencode
    query_string = urlencode(params)
    timestamp = str(int(time.time() * 1000))
    sign_payload = f"{timestamp}{KEY}5000{query_string}"
    sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()

    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": "5000"
    }

    try:
        resp = requests.get(url, headers=headers, params=params)
        data = resp.json()

        if "result" not in data or "list" not in data["result"]:
            log(f"❗ Struttura inattesa da Bybit per {symbol}: {resp.text}")
            return 0.0

        coin_list = data["result"]["list"][0].get("coin", [])
        for c in coin_list:
            if c["coin"] == coin:
                raw = c.get("walletBalance", "0")
                try:
                    qty = float(raw) if raw else 0.0
                    if qty > 0:
                        log(f"📦 Saldo trovato per {coin}: {qty}")
                    else:
                        log(f"🟡 Nessun saldo disponibile per {coin}")
                    return qty
                except Exception as e:
                    log(f"⚠️ Errore conversione quantità {coin}: {e}")
                    return 0.0

        log(f"🔍 Coin {coin} non trovata nel saldo.")
        return 0.0

    except Exception as e:
        log(f"❌ Errore nel recupero saldo per {symbol}: {e}")
        return 0.0

def calculate_stop_loss(entry_price, current_price, p_max, trailing_active):
    if not trailing_active:
        return entry_price * (1 - INITIAL_STOP_LOSS_PCT)
    else:
        return p_max * (1 - TRAILING_DISTANCE)

import gspread
from google.oauth2.service_account import Credentials

# Config
SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"  # copia da URL: https://docs.google.com/spreadsheets/d/<QUESTO>/edit
SHEET_NAME = "Foglio1"  # o quello che hai scelto

# Setup una sola volta
def setup_gspread():
    scope = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_file("gspread-creds.json", scopes=scope)
    client = gspread.authorize(creds)
    return client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)

# Salva una riga nel foglio
def log_trade_to_google(symbol, entry, exit, pnl_pct, strategy, result_type):
    try:
        import gspread
        from google.oauth2.service_account import Credentials
        import base64

        SHEET_ID = "1KF4wPfewt5oBXbUaaoXOW5GKMqRk02ZMA94TlVkXzXg"
        SHEET_NAME = "Foglio1"

        # Decodifica la variabile base64 in file temporaneo
        encoded = os.getenv("GSPREAD_CREDS_B64")
        if not encoded:
            log("❌ Variabile GSPREAD_CREDS_B64 non trovata")
            return

        creds_path = "/tmp/gspread-creds.json"
        with open(creds_path, "wb") as f:
            f.write(base64.b64decode(encoded))

        scope = ["https://www.googleapis.com/auth/spreadsheets"]
        creds = Credentials.from_service_account_file(creds_path, scopes=scope)
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SHEET_ID).worksheet(SHEET_NAME)
        sheet.append_row([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            symbol,
            round(entry, 6),
            round(exit, 6),
            f"{pnl_pct:.2f}%",
            strategy,
            result_type
        ])
    except Exception as e:
        log(f"❌ Errore log su Google Sheets: {e}")

while True:
    for symbol in ASSETS:
        signal, strategy, price = analyze_asset(symbol)
        log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ❌ Filtra segnali nulli
        if signal is None or strategy is None or price is None:
            continue

        # ✅ ENTRATA
        if signal == "entry":
            # Cooldown
            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if elapsed < COOLDOWN_MINUTES * 60:
                    log(f"⏳ Cooldown attivo per {symbol} ({elapsed:.0f}s), salto ingresso")
                    continue

            if symbol in open_positions:
                log(f"⏩ Ignoro acquisto: già in posizione su {symbol}")
                continue

            usdt_balance = get_usdt_balance()
            if usdt_balance < ORDER_USDT:
                log(f"💸 Saldo USDT insufficiente per {symbol} ({usdt_balance:.2f})")
                continue

            resp = market_buy(symbol, ORDER_USDT)
            if not (resp and resp.status_code == 200 and resp.json().get("retCode") == 0):
                log(f"❌ Acquisto fallito per {symbol}")
                continue

            qty = get_free_qty(symbol)
            if qty == 0:
                log(f"❌ Nessuna quantità acquistata per {symbol}")
                continue

            df = fetch_history(symbol)
            if df is None or "Close" not in df.columns:
                log(f"❌ Dati storici mancanti per {symbol}")
                continue

            atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
            last = df.iloc[-1]
            atr_val = last["atr"] if "atr" in last else atr.iloc[-1]

            tp = price + (atr_val * TP_FACTOR)
            sl = price - (atr_val * SL_FACTOR)

            position_data[symbol] = {
                "entry_price": price,
                "tp": tp,
                "sl": sl,
                "entry_cost": ORDER_USDT,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price
            }

            open_positions.add(symbol)
            log(f"🟢 Acquisto registrato per {symbol} | Entry: {price:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")
            notify_telegram(f"🟢📈 Acquisto per {symbol}\nPrezzo: {price:.4f}\nStrategia: {strategy}")

        # 🔴 USCITA (EXIT)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)
            qty = entry.get("qty", get_free_qty(symbol))

            usdt_before = get_usdt_balance()
            resp = market_sell(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                price = round(price, 6)
                exit_value = price * qty
                delta = exit_value - entry_cost
                pnl = (delta / entry_cost) * 100

                log(f"🔴 Vendita completata per {symbol}")
                log(f"📊 PnL stimato: {pnl:.2f}% | Delta: {delta:.2f}")
                notify_telegram(f"🔴📉 Vendita per {symbol} a {price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}%")
                log_trade_to_google(symbol, entry_price, price, pnl, strategy, "Exit Signal")

                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
                position_data.pop(symbol, None)
            else:
                log(f"❌ Vendita fallita per {symbol}")

    time.sleep(1)

    # 🔁 Controllo Trailing Stop per le posizioni aperte
    for symbol in list(open_positions):
        if symbol not in position_data:
            continue

        entry = position_data[symbol]
        current_price = get_last_price(symbol)
        if not current_price:
            continue

        # 🧪 Attiva Trailing se supera la soglia
        if not entry["trailing_active"] and current_price >= entry["entry_price"] * (1 + TRAILING_ACTIVATION_THRESHOLD):
            entry["trailing_active"] = True
            log(f"🔛 Trailing Stop attivato per {symbol} sopra soglia → Prezzo: {current_price:.4f}")
            notify_telegram(f"🔛 Trailing Stop attivo su {symbol}\nPrezzo: {current_price:.4f}")

        # ⬆️ Aggiorna massimo e SL se prezzo cresce
        if entry["trailing_active"]:
            if current_price > entry["p_max"]:
                entry["p_max"] = current_price
                new_sl = current_price * (1 - TRAILING_SL_BUFFER)
                if new_sl > entry["sl"]:
                    log(f"📉 SL aggiornato per {symbol}: da {entry['sl']:.4f} a {new_sl:.4f}")
                    entry["sl"] = new_sl

            # ❌ Esegui vendita se SL raggiunto
            if current_price <= entry["sl"]:
                qty = get_free_qty(symbol)
                if qty > 0:
                    usdt_before = get_usdt_balance()
                    resp = market_sell(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        entry_price = entry["entry_price"]
                        entry_cost = entry.get("entry_cost", ORDER_USDT)
                        qty = entry.get("qty", qty)
                        exit_value = current_price * qty
                        delta = exit_value - entry_cost
                        pnl = (delta / entry_cost) * 100

                        log(f"🔻 Trailing Stop attivato per {symbol} → Prezzo: {current_price:.4f} | SL: {entry['sl']:.4f}")
                        notify_telegram(f"🔻 Trailing Stop venduto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                        log_trade_to_google(symbol, entry_price, current_price, pnl, "Trailing Stop", "SL Triggered")

                        # 🗑️ Pulizia
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    else:
                        log(f"❌ Vendita fallita con Trailing Stop per {symbol}")

    # Sicurezza: attesa tra i cicli principali
    time.sleep(INTERVAL_MINUTES * 60)