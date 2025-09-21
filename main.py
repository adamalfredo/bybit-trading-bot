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

MIN_BALANCE_USDT = 50.0

ASSETS = [
    "WIFUSDT", "PEPEUSDT", "BONKUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT",
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]

VOLATILE_ASSETS = [
    "BONKUSDT", "PEPEUSDT", "WIFUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT"
]

INTERVAL_MINUTES = 15
ATR_WINDOW = 14
SL_ATR_MULT = 1.0
TP_R_MULT   = 2.5
ATR_MIN_PCT = 0.003
ATR_MAX_PCT = 0.030
EXTENSION_ATR_MULT = 1.2
MAX_OPEN_POSITIONS = 5
COOLDOWN_MINUTES = 60
TRAIL_LOCK_FACTOR = 1.2
RISK_PCT = 0.01

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)
log(f"[CONFIG] TESTNET={BYBIT_TESTNET} BASE_URL={BYBIT_BASE_URL}")
def notify_telegram(message: str):
    if TELEGRAM_TOKEN and TELEGRAM_CHAT_ID:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        data = {"chat_id": TELEGRAM_CHAT_ID, "text": message}
        try:
            requests.post(url, data=data, timeout=10)
        except Exception as e:
            log(f"Errore invio Telegram: {e}")

def get_last_price(symbol: str) -> Optional[float]:
    try:
        response = requests.get(
            f"{BYBIT_BASE_URL}/v5/market/tickers",
            params={"category": "spot", "symbol": symbol},
            timeout=10
        )
        data = response.json()
        last_price = data["result"]["list"][0]["lastPrice"]
        return float(last_price)
    except Exception as e:
        log(f"❌ Errore in get_last_price: {e}")
        return None
    
def get_instrument_info(symbol: str) -> dict:
    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info?category=spot&symbol={symbol}"
    try:
        resp = requests.get(url)
        data = resp.json()
        if data["retCode"] != 0:
            log(f"❌ Errore fetch info strumento: {data['retMsg']}")
            return {}

        info = data["result"]["list"][0]
        lot = info["lotSizeFilter"]
        return {
            "min_qty": float(lot.get("minOrderQty", 0)),
            "qty_step": float(lot.get("qtyStep", 0.0001)),
            "precision": int(info.get("priceScale", 4)),
            "min_order_amt": float(info.get("minOrderAmt", 5))
        }

    except Exception as e:
        log(f"❌ Errore get_instrument_info: {e}")
        return {}

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

def market_sell(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile vendere")
        return

    order_value = qty * price
    if order_value < 5:
        log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT")
        return

    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    if not qty_step or qty_step <= 0:
        qty_step = 0.0001

    try:
        step = Decimal(str(qty_step))
        dec_qty = Decimal(str(qty))
        floored_qty = (dec_qty // step) * step

        # Deriva decimali dallo step
        step_str = f"{qty_step:.10f}".rstrip('0')
        if '.' in step_str:
            step_decimals = len(step_str.split('.')[1])
        else:
            step_decimals = 0

        if floored_qty <= 0:
            log(f"❌ Quantità troppo piccola per {symbol} (dopo arrotondamento)")
            return

        qty_str = f"{floored_qty:.{step_decimals}f}".rstrip('0').rstrip('.')
        if qty_str == '':
            qty_str = '0'

        log(f"[DEBUG-SELL-QTY] {symbol} req={qty} step={qty_step} send={qty_str}")

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
    body_json = json.dumps(body, separators=(",", ":"))
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
        log(f"❌ Errore invio ordine SELL: {e}")
        return None

def _qty_step_decimals(qty_step: float) -> int:
    step_str = f"{qty_step:.10f}".rstrip('0')
    if '.' in step_str:
        return len(step_str.split('.')[1])
    return 0

def market_buy_qty(symbol: str, qty: Decimal):
    """
    Invia un ordine MARKET BUY usando direttamente la qty (Decimal) già calcolata
    e allineata allo step. Non riconverte da notional.
    Ritorna True/False.
    """
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    min_order_amt = info.get("min_order_amt", 5)
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, abort buy")
        return False

    # Allineamento sicurezza
    step = Decimal(str(qty_step))
    qty_aligned = (qty // step) * step
    if qty_aligned <= 0:
        log(f"❌ Qty non valida per {symbol} ({qty_aligned})")
        return False

    notional = float(qty_aligned) * price
    if notional < min_order_amt:
        log(f"❌ Notional {notional:.2f} < min {min_order_amt} su {symbol}")
        return False

    decs = _qty_step_decimals(qty_step)
    qty_str = f"{qty_aligned:.{decs}f}".rstrip('0').rstrip('.')
    if qty_str == '':
        qty_str = '0'

    body = {
        "category": "spot",
        "symbol": symbol,
        "side": "Buy",
        "orderType": "Market",
        "qty": qty_str
    }
    ts = str(int(time.time() * 1000))
    body_json = json.dumps(body, separators=(",", ":"))
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
    resp = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
    try:
        rj = resp.json()
    except:
        rj = {}
    log(f"BUY_QTY BODY: {body_json}")
    log(f"BUY_QTY RESP: {resp.status_code} {rj}")
    if resp.status_code == 200 and rj.get("retCode") == 0:
        return True
    return False

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
        df["ema200"] = EMAIndicator(close=close, window=200).ema_indicator()
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=[
            "bb_upper","bb_lower","rsi","sma20","sma50","ema20","ema50","ema200",
            "macd","macd_signal","adx","atr"
        ], inplace=True)

        if len(df) < 2:
            return None, None, None

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = 20 if is_volatile else 15

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])
        atr_val = float(last["atr"])
        atr_pct = atr_val / price if price else 0
        # log(f"[ANALYZE] {symbol} ATR={atr_val:.5f} ({atr_pct:.2%})")

        # FILTRI + DEBUG (SOSTITUISCE il vecchio blocco "# FILTRI")
        if last["ema50"] <= last["ema200"]:
            log(f"[FILTER][{symbol}] Trend KO: ema50 {last['ema50']:.4f} <= ema200 {last['ema200']:.4f}")
            return None, None, None
        if not (ATR_MIN_PCT <= atr_pct <= ATR_MAX_PCT):
            log(f"[FILTER][{symbol}] ATR% {atr_pct:.4%} fuori range ({ATR_MIN_PCT:.2%}-{ATR_MAX_PCT:.2%})")
            return None, None, None
        limit_ext = last["ema20"] + EXTENSION_ATR_MULT * atr_val
        if price > limit_ext:
            log(f"[FILTER][{symbol}] Estensione: price {price:.4f} > ema20 {last['ema20']:.4f} + {EXTENSION_ATR_MULT}*ATR ({limit_ext:.4f})")
            return None, None, None
        
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


TEST_MODE = False  # Acquisti e vendite normali abilitati


# Inizializza struttura base
open_positions = set()
position_data = {}
last_exit_time = {}

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

# (RIMOSSO calculate_stop_loss: non più usato con nuovo modello R basato su ATR)

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
            if usdt_balance < MIN_BALANCE_USDT:
                log(f"💸 Saldo USDT insufficiente ({usdt_balance:.2f} < {MIN_BALANCE_USDT}) per {symbol}")
                continue

            # Limite posizioni aperte
            if len(open_positions) >= MAX_OPEN_POSITIONS:
                log(f"🚫 Limite posizioni raggiunto ({MAX_OPEN_POSITIONS}), salto {symbol}")
                continue

            # 📊 Forza strategia (usata come CAP massimo, non per il calcolo del rischio)
            strategy_strength = {
                "Breakout Bollinger": 1.0,
                "MACD bullish + ADX": 0.9,
                "Incrocio SMA 20/50": 0.75,
                "Incrocio EMA 20/50": 0.7,
                "MACD bullish (stabile)": 0.65,
                "Trend EMA + RSI": 0.6
            }
            strength = strategy_strength.get(strategy, 0.5)

            # === POSITION SIZING A RISCHIO FISSO ===
            # 1. Calcola ATR per determinare risk_per_unit (SL distance)
            df_sizing = fetch_history(symbol)
            if df_sizing is None or "Close" not in df_sizing.columns:
                log(f"❌ Dati storici mancanti per sizing {symbol}")
                continue
            atr_ind_sz = AverageTrueRange(
                high=df_sizing["High"], low=df_sizing["Low"],
                close=df_sizing["Close"], window=ATR_WINDOW
            ).average_true_range()
            atr_val = float(atr_ind_sz.iloc[-1])
            if atr_val <= 0:
                log(f"❌ ATR nullo per {symbol}")
                continue

            # Prezzo corrente (rileggi per ridurre drift)
            live_price = get_last_price(symbol)
            if not live_price:
                log(f"❌ Prezzo non disponibile per sizing {symbol}")
                continue

            risk_per_unit = atr_val * SL_ATR_MULT   # distanza SL per unità
            equity = get_usdt_balance()
            risk_capital = equity * RISK_PCT
            if risk_capital < 5:
                log(f"💸 Rischio calcolato troppo basso ({risk_capital:.2f}) per {symbol}")
                continue

            # Qty teorica basata sul rischio
            qty_risk = risk_capital / risk_per_unit

            # Applica limiti di exchange (step / min order)
            info = get_instrument_info(symbol)
            qty_step = info.get("qty_step", 0.0001)
            min_qty = info.get("min_qty", 0.0)
            min_order_amt = info.get("min_order_amt", 5)

            from decimal import Decimal
            step_dec = Decimal(str(qty_step))
            qty_dec = Decimal(str(qty_risk))
            qty_adj = (qty_dec // step_dec) * step_dec
            if qty_adj < Decimal(str(min_qty)):
                qty_adj = Decimal(str(min_qty))

            order_amount = float(qty_adj) * live_price

            # CAP addizionale: non investire oltre strength * equity né oltre 250 USDT
            cap_strength = equity * strength
            cap_global = 250.0
            max_notional = min(cap_strength, cap_global, equity)
            if order_amount > max_notional:
                # Ridimensiona qty alla nuova soglia
                qty_adj = Decimal(str(max_notional / live_price))
                qty_adj = (qty_adj // step_dec) * step_dec
                order_amount = float(qty_adj) * live_price

            if order_amount < min_order_amt:
                log(f"❌ Notional {order_amount:.2f} < min_order_amt {min_order_amt} per {symbol}")
                continue

            if float(qty_adj) <= 0:
                log(f"❌ Qty finale nulla per {symbol}")
                continue

            # Acquisto (usiamo order_amount in USDT)
            if TEST_MODE:
                log(f"[TEST_MODE] (NO BUY) {symbol} qty={qty_adj} notional={order_amount:.2f}")
                continue

            pre_qty = get_free_qty(symbol)  # saldo prima
            if not market_buy_qty(symbol, qty_adj):
                log(f"❌ Acquisto fallito per {symbol}")
                continue
            time.sleep(2)
            post_qty = get_free_qty(symbol)
            qty_filled = max(0.0, post_qty - pre_qty)
            if qty_filled <= 0:
                # fallback: usa differenza minima (possibile residuo precedente)
                qty_filled = post_qty
            if qty_filled <= 0:
                log(f"❌ Nessuna quantità risultante per {symbol} (post esecuzione)")
                continue

            entry_price = live_price  # usiamo il prezzo live usato per sizing
            sl = entry_price - risk_per_unit
            tp = entry_price + (risk_per_unit * TP_R_MULT)
            actual_cost = entry_price * qty_filled
            used_risk = risk_per_unit * qty_filled  # rischio monetario effettivo

            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "initial_sl": sl,
                "risk_per_unit": risk_per_unit,
                "entry_cost": actual_cost,
                "qty": qty_filled,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": entry_price,
                "mfe": 0.0,    # Max Favorable Excursion (in R)
                "mae": 0.0,    # Max Adverse Excursion (in R, negativo)
                "used_risk": used_risk
            }
            open_positions.add(symbol)
            risk_pct_eff = (used_risk / equity) * 100 if equity else 0
            log(f"🟢 Acquisto {symbol} | Qty {qty_filled:.8f} | Entry {entry_price:.6f} | SL {sl:.6f} | TP {tp:.6f} | R/unit {risk_per_unit:.6f} | RiskCap {risk_capital:.2f} | UsedRisk {used_risk:.2f} ({risk_pct_eff:.2f}%)")
            notify_telegram(
                f"🟢📈 Acquisto {symbol}\nQty: {qty_filled:.6f}\nPrezzo: {entry_price:.6f}\nStrategia: {strategy}\nSL: {sl:.6f}\nTP: {tp:.6f}\nR/unit: {risk_per_unit:.6f}"
            )
            time.sleep(2)

        # 🔴 USCITA (EXIT)
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            qty = entry.get("qty", get_free_qty(symbol))
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", entry_price * qty)
            risk_per_unit = entry.get("risk_per_unit", None)
            mfe = entry.get("mfe", 0.0)
            mae = entry.get("mae", 0.0)

            # Rileggi prezzo prima della vendita (più aggiornato)
            latest_before = get_last_price(symbol)
            if latest_before:
                price = round(latest_before, 6)

            resp = market_sell(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                # Rileggi di nuovo dopo l'esecuzione (best effort)
                latest_after = get_last_price(symbol)
                if latest_after:
                    price = round(latest_after, 6)

                exit_value = price * qty
                delta = exit_value - entry_cost
                pnl = (delta / entry_cost) * 100
                r_multiple = (price - entry_price) / risk_per_unit if risk_per_unit else 0

                log(f"📊 EXIT {symbol} PnL: {pnl:.2f}% | R={r_multiple:.2f} | MFE={mfe:.2f}R | MAE={mae:.2f}R")
                notify_telegram(
                    f"🔴📉 Vendita {symbol} @ {price:.6f}\n"
                    f"PnL: {pnl:.2f}% | R={r_multiple:.2f}\n"
                    f"MFE={mfe:.2f}R MAE={mae:.2f}R"
                )
                log_trade_to_google(
                    symbol,
                    entry_price,
                    price,
                    pnl,
                    f"{strategy} | R={r_multiple:.2f} | MFE={mfe:.2f} | MAE={mae:.2f}",
                    "Exit Signal"
                )

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

        risk = entry.get("risk_per_unit")
        if not risk or risk <= 0:
            continue

        entry_price = entry["entry_price"]

        # Aggiorna MFE / MAE (in R)
        r_current = (current_price - entry_price) / risk
        if r_current > entry["mfe"]:
            entry["mfe"] = r_current
        if r_current < entry["mae"]:
            entry["mae"] = r_current

        # TP hard
        if current_price >= entry["tp"]:
            qty = get_free_qty(symbol)
            if qty > 0:
                resp = market_sell(symbol, qty)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    fill_price = get_last_price(symbol) or current_price
                    pnl_val = (fill_price - entry_price) * qty
                    pnl_pct = (pnl_val / entry["entry_cost"]) * 100
                    r_mult = (fill_price - entry_price) / risk
                    log(f"🎯 TP {symbol} @ {fill_price:.6f} | PnL {pnl_pct:.2f}% | R={r_mult:.2f} | MFE={entry['mfe']:.2f}R | MAE={entry['mae']:.2f}R")
                    notify_telegram(f"🎯 TP {symbol} @ {fill_price:.6f}\nPnL: {pnl_pct:.2f}% | R={r_mult:.2f}\nMFE={entry['mfe']:.2f}R MAE={entry['mae']:.2f}R")
                    log_trade_to_google(symbol, entry_price, fill_price, pnl_pct, f"TP | R={r_mult:.2f} | MFE={entry['mfe']:.2f} | MAE={entry['mae']:.2f}", "TP Hit")
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
                    continue
                else:
                    log(f"❌ Vendita TP fallita per {symbol}")

        # Attiva trailing a ≥1R
        if not entry["trailing_active"] and r_current >= 1.0:
            entry["trailing_active"] = True
            entry["sl"] = entry_price  # BE
            log(f"🔛 Trailing attivo {symbol} (≥1R) | SL→BE {entry_price:.6f}")
            notify_telegram(f"🔛 Trailing attivo {symbol} (≥1R)\nSL→BE {entry_price:.6f}")

        # Gestione trailing
        if entry["trailing_active"]:
            if current_price > entry["p_max"]:
                entry["p_max"] = current_price
                desired_sl = max(entry_price, current_price - (risk * TRAIL_LOCK_FACTOR))
                if desired_sl > entry["sl"]:
                    log(f"📉 SL trail {symbol}: {entry['sl']:.6f} → {desired_sl:.6f}")
                    entry["sl"] = desired_sl

            # Stop colpito
            if current_price <= entry["sl"]:
                qty = get_free_qty(symbol)
                if qty > 0:
                    resp = market_sell(symbol, qty)
                    if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                        fill_price = get_last_price(symbol) or current_price
                        pnl_val = (fill_price - entry_price) * qty
                        pnl_pct = (pnl_val / entry["entry_cost"]) * 100
                        r_mult = (fill_price - entry_price) / risk
                        log(f"🔻 Trailing Stop {symbol} @ {fill_price:.6f} | PnL {pnl_pct:.2f}% | R={r_mult:.2f} | MFE={entry['mfe']:.2f}R | MAE={entry['mae']:.2f}R")
                        notify_telegram(f"🔻 Trailing Stop {symbol} @ {fill_price:.6f}\nPnL: {pnl_pct:.2f}% | R={r_mult:.2f}\nMFE={entry['mfe']:.2f}R MAE={entry['mae']:.2f}R")
                        log_trade_to_google(symbol, entry_price, fill_price, pnl_pct, f"Trailing | R={r_mult:.2f} | MFE={entry['mfe']:.2f} | MAE={entry['mae']:.2f}", "SL Triggered")
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                    else:
                        log(f"❌ Vendita fallita Trailing {symbol}")

    # Sicurezza: attesa tra i cicli principali
    # Aggiungi pausa di sicurezza per evitare ciclo troppo veloce se tutto salta
    time.sleep(INTERVAL_MINUTES * 60)