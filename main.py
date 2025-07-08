import os
import time
import hmac
import json
import hashlib
from decimal import Decimal
import requests
# import yfinance as yf
import pandas as pd
from ta.volatility import BollingerBands
from ta.volatility import AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import SMAIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator
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
cooldown = {}  # Dizionario che memorizza il timestamp dell'ultima uscita per ciascun simbolo
COOLDOWN_MINUTES = 60  # Durata del cooldown in minuti (modificabile)

ATR_WINDOW = 14
TP_FACTOR = 2.0     # TP = entry + 2 * ATR
SL_FACTOR = 1.5     # SL = entry - 1.5 * ATR

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

    if (
        last["Close"] > last["Open"] and
        full_range > 0 and
        body > 0.6 * full_range and
        last["Close"] > prev["Close"]
    ):
        return True
    return False

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
        return resp
    except Exception as e:
        log(f"Errore invio ordine BUY: {e}")
        return None

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
        return resp
    except Exception as e:
        log(f"Errore invio ordine SELL: {e}")
        return None

def fetch_history(symbol: str):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": str(INTERVAL_MINUTES),  # es. "15"
        "limit": 100  # ultimi 100 candle da 15 min (~1 giorno)
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

        # Converti i tipi e timestamp
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
            log(f"[!] Dati non disponibili per {symbol} → analisi saltata")
            return None, None, None

        close = find_close_column(df)
        if close is None:
            return None, None, None

        # Indicatori esistenti
        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()

        # Nuovi indicatori
        df["ema20"] = EMAIndicator(close=close, window=20).ema_indicator()
        df["ema50"] = EMAIndicator(close=close, window=50).ema_indicator()
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=[
            "bb_upper", "bb_lower", "rsi", "sma20", "sma50",
            "ema20", "ema50", "macd", "macd_signal", "adx", "atr"
        ], inplace=True)

        last = df.iloc[-1]
        prev = df.iloc[-2]

        price = float(last["Close"])

        # Logica esistente + nuove regole
        if last["Close"] > last["bb_upper"] and last["rsi"] < 70:
            return "entry", "Breakout Bollinger", price
        elif prev["sma20"] < prev["sma50"] and last["sma20"] > last["sma50"]:
            return "entry", "Incrocio SMA 20/50", price
        elif last["macd"] > last["macd_signal"] and last["adx"] > 20:
            return "entry", "MACD bullish + ADX", price
        elif last["Close"] < last["bb_lower"] and last["rsi"] > 30:
            return "exit", "Rimbalzo RSI + BB", price
        elif last["macd"] < last["macd_signal"] and last["adx"] > 20:
            return "exit", "MACD bearish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

# 3️⃣ Migliora le notifiche Telegram con gain/loss stimato (approssimato)
def notify_trade_result(symbol, signal, price, strategy):
    msg = ""
    if signal == "entry":
        balance = get_free_qty(symbol)
        value_usdt = balance * price if balance and price else 0
        msg += (
            f"🟢📈 Acquisto completato per {symbol}\n"
            f"Prezzo: {price:.4f}\n"
            f"Strategia: {strategy}\n"
            f"Saldo attuale: {balance:.6f} {symbol.replace('USDT', '')} (~{value_usdt:.2f} USDT)"
        )
    elif signal == "exit":
        balance = get_free_qty(symbol)
        value_usdt = balance * price if balance and price else 0
        msg += (
            f"🔴📉 Vendita completata per {symbol}\n"
            f"Prezzo: {price:.4f}\n"
            f"Strategia: {strategy}\n"
        )
    notify_telegram(msg)

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
        log(f"Errore log su Google Sheets: {e}")

def get_free_qty(symbol: str) -> float:
    """Restituisce il saldo disponibile per la coin indicata (robusto anche se i campi sono vuoti)."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        return 0.0

    endpoint = f"{BYBIT_BASE_URL}/v5/account/wallet-balance"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    coin = symbol.replace("USDT", "") if symbol != "USDT" else "USDT"
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
                    coins = item.get("coin") or item.get("coins") or item.get("balances") or []
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
                                val = c.get(key)
                                if val not in [None, "", "null"]:
                                    try:
                                        return float(val)
                                    except (TypeError, ValueError):
                                        continue
            log(f"Coin {coin} non trovata nella risposta saldo: {data.get('result')!r}")
        else:
            log(f"Errore saldo {coin}: {data}")
    except Exception as e:
        log(f"Errore ottenimento saldo {coin}: {e}")
    return 0.0

open_positions = set()
# Mappa delle posizioni aperte: salva entry, TP e SL
position_data = {}  # es: { "BTCUSDT": {"entry_price": 60000, "tp": 61200, "sl": 59100} }
last_exit_time = {}
if __name__ == "__main__":
    log("🔄 Avvio sistema di acquisto")
    notify_telegram("🔄 Avvio sistema di acquisto")
    notify_telegram("✅ Connessione a Bybit riuscita")
    notify_telegram("🧪 BOT avviato correttamente")

    def get_usdt_balance() -> float:
        """Restituisce il saldo disponibile in USDT."""
        return get_free_qty("USDT")
    
    while True:
        for symbol in ASSETS:
            # Controlla se la posizione ha raggiunto TP o SL
            if symbol in open_positions and symbol in position_data:
                current_price = get_last_price(symbol)
                if current_price:
                    entry = position_data[symbol]
                    if current_price >= entry["tp"]:
                        log(f"🎯 Take Profit raggiunto per {symbol} → {current_price:.4f}")
                        qty = get_free_qty(symbol)
                        if qty > 0:
                            resp = market_sell(symbol, qty)
                            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                                log(f"✅ Vendita TP per {symbol}")
                                pnl = (current_price - entry["entry_price"]) / entry["entry_price"] * 100
                                log(f"📈 Profitto stimato per {symbol}: +{pnl:.2f}%")
                                notify_telegram(
                                    f"🎯 Take Profit raggiunto per {symbol} a {current_price:.4f}\nProfitto stimato: +{pnl:.2f}%"
                                )
                                log_trade_to_google(symbol, entry["entry_price"], current_price, pnl, "TP", "Take Profit")
                                open_positions.discard(symbol)
                                last_exit_time[symbol] = time.time()
                                position_data.pop(symbol, None)
                                cooldown[symbol] = time.time()
                    elif current_price <= entry["sl"]:
                        log(f"🛑 Stop Loss attivato per {symbol} → {current_price:.4f}")
                        qty = get_free_qty(symbol)
                        if qty > 0:
                            resp = market_sell(symbol, qty)
                            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                                pnl = (current_price - entry["entry_price"]) / entry["entry_price"] * 100
                                log(f"📉 Perdita stimata per {symbol}: {pnl:.2f}%")
                                notify_telegram(
                                    f"🛑 Stop Loss attivato per {symbol} a {current_price:.4f}\nPerdita stimata: {pnl:.2f}%"
                                )
                                log_trade_to_google(symbol, entry["entry_price"], current_price, pnl, "SL", "Stop Loss")
                                open_positions.discard(symbol)
                                last_exit_time[symbol] = time.time()
                                position_data.pop(symbol, None)
                                cooldown[symbol] = time.time()

            signal, strategy, price = analyze_asset(symbol)
            log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")
            if signal == "entry":
                # Evita rientri troppo rapidi (cooldown)
                cooldown_duration = 3600  # 1 ora
                if symbol in last_exit_time and time.time() - last_exit_time[symbol] < cooldown_duration:
                    log(f"⏳ Cooldown attivo per {symbol}, nessun nuovo ingresso")
                    continue

                # Verifica cooldown
                last_exit = cooldown.get(symbol)
                if last_exit and (time.time() - last_exit) < COOLDOWN_MINUTES * 60:
                    log(f"⏳ Cooldown attivo per {symbol}, nessun nuovo ingresso")
                    continue

                if symbol in open_positions:
                    log(f"⏩ Acquisto ignorato per {symbol}: già in posizione")
                    continue

                # FILTRI DI QUALITÀ
                df = fetch_history(symbol)
                if df is None:
                    continue
                
                close = find_close_column(df)
                if close is None:
                    log(f"[!] Colonna 'Close' non trovata per {symbol}")
                    continue
                df["rsi"] = RSIIndicator(close=close).rsi()
                df["ema20"] = EMAIndicator(close=close, window=20).ema_indicator()
                df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()

                # Aggiungi anche MACD per evitare KeyError
                macd = MACD(close=close)
                df["macd"] = macd.macd()
                df["macd_signal"] = macd.macd_signal()

                df.dropna(subset=[
                    "rsi", "ema20", "adx", "macd", "macd_signal"
                ], inplace=True)

                last = df.iloc[-1]

                if last["adx"] < 25:
                    log(f"❌ Segnale debole per {symbol} → ADX {last['adx']:.2f} < 25")
                    continue
                if last["rsi"] > 70:
                    log(f"❌ RSI troppo alto per {symbol} → RSI {last['rsi']:.2f} > 70")
                    continue
                if price < last["ema20"]:
                    log(f"❌ Prezzo sotto EMA20 per {symbol} → no acquisto")
                    continue

                # Saldo sufficiente?
                usdt_balance = get_usdt_balance()
                if usdt_balance < ORDER_USDT:
                    log(f"⏩ Acquisto saltato per {symbol}: saldo USDT ({usdt_balance:.2f}) insufficiente")
                    continue

                # 🔍 Filtri aggiuntivi per confermare il segnale
                # MACD deve essere sopra la linea signal
                if last["macd"] <= last["macd_signal"]:
                    log(f"❌ MACD non confermato per {symbol} → MACD {last['macd']:.4f} <= Signal {last['macd_signal']:.4f}")
                    continue

                # Conferma breakout: candela verde con corpo significativo
                if not is_bullish_breakout_confirmed(df):
                    log(f"❌ Breakout non confermato visivamente per {symbol}")
                    continue

                resp = market_buy(symbol, ORDER_USDT)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    log(f"✅ Acquisto completato per {symbol}")
                    open_positions.add(symbol)

                    # Salva entry price, TP, SL
                    entry_price = price
                    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
                    df["atr"] = atr.average_true_range()
                    atr_value = df.iloc[-1]["atr"]
                    tp = entry_price + (atr_value * TP_FACTOR)
                    sl = entry_price - (atr_value * SL_FACTOR)
                    position_data[symbol] = {"entry_price": entry_price, "tp": tp, "sl": sl}
                    log(f"📊 ATR per {symbol}: {atr_value:.6f} → TP: {tp:.4f}, SL: {sl:.4f}")
                    notify_trade_result(symbol, "entry", price, strategy)
                else:
                    log(f"❌ Acquisto fallito per {symbol}, nessuna notifica inviata")

            elif signal == "exit":
                qty = get_free_qty(symbol)
                if qty <= 0:
                    log(f"⏩ Vendita saltata per {symbol}: saldo nullo o insufficiente")
                    continue

                resp = market_sell(symbol, qty)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    log(f"✅ Vendita completata per {symbol}")
                    notify_trade_result(symbol, "exit", price, strategy)
                    entry_price = position_data.get(symbol, {}).get("entry_price", price)
                    pnl = (price - entry_price) / entry_price * 100
                    log_trade_to_google(symbol, entry_price, price, pnl, strategy, "Exit Signal")
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
                    cooldown[symbol] = time.time()
                else:
                    log(f"❌ Vendita fallita per {symbol}, nessuna notifica inviata")

        time.sleep(INTERVAL_MINUTES * 60)