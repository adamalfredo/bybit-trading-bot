from typing import Optional
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

ORDER_USDT = 50.0


ASSETS = [
    "WIFUSDT", "PEPEUSDT", "BONKUSDT", "INJUSDT", "SUIUSDT",
    "SEIUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "TONUSDT", "DOGEUSDT", "MATICUSDT",
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]

# Coin meno volatili (gruppo 30%)
LESS_VOLATILE_ASSETS = [
    "BTCUSDT", "ETHUSDT", "LTCUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "SOLUSDT"
]
# Coin volatili (gruppo 70%)
VOLATILE_ASSETS = [s for s in ASSETS if s not in LESS_VOLATILE_ASSETS]

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

def format_quantity_bybit(qty: float, qty_step: float, precision: Optional[int] = None) -> str:
    """
    Restituisce la quantità formattata secondo i decimali accettati da Bybit per qty_step e basePrecision,
    troncando senza arrotondare e garantendo che sia un multiplo esatto di qty_step.
    """
    from decimal import Decimal, ROUND_DOWN
    def get_decimals(step):
        s = str(step)
        if '.' in s:
            return len(s.split('.')[-1].rstrip('0'))
        return 0
    if hasattr(qty_step, '__precision_override__'):
        precision = qty_step.__precision_override__
    if precision is None:
        precision = get_decimals(qty_step)
    step_dec = Decimal(str(qty_step))
    qty_dec = Decimal(str(qty))
    # Tronca la quantità al multiplo più basso di qty_step
    floored_qty = (qty_dec // step_dec) * step_dec
    # Troncamento ai decimali accettati
    quantize_str = '1.' + '0'*precision if precision > 0 else '1'
    floored_qty = floored_qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)
    # Garantisce che sia multiplo esatto di qty_step
    if (floored_qty / step_dec) % 1 != 0:
        floored_qty = (floored_qty // step_dec) * step_dec
        floored_qty = floored_qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)
    fmt = f"{{0:.{precision}f}}"
    # LOG DIAGNOSTICO
    log(f"[DECIMALI][FORMAT_QTY] qty={qty} | qty_step={qty_step} | precision={precision} | floored_qty={floored_qty} | quantize_str={quantize_str}")
    return fmt.format(floored_qty)

# --- FUNZIONI DI SUPPORTO BYBIT E TELEGRAM ---
def get_last_price(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "spot", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            price = data["result"]["list"][0]["lastPrice"]
            return float(price)
        else:
            log(f"[BYBIT] Errore get_last_price {symbol}: {data}")
            return None
    except Exception as e:
        log(f"[BYBIT] Errore get_last_price {symbol}: {e}")
        return None

def get_instrument_info(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "spot", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            info = data["result"]["list"][0]
            qty_step = float(info.get("lotSizeFilter", {}).get("qtyStep", 0.0001))
            price_step = float(info.get("priceFilter", {}).get("tickSize", 0.0001))
            precision = int(info.get("basePrecision", 4))
            min_order_amt = float(info.get("minOrderAmt", 5))
            min_qty = float(info.get("lotSizeFilter", {}).get("minOrderQty", 0.0))
            return {
                "qty_step": qty_step,
                "price_step": price_step,
                "precision": precision,
                "min_order_amt": min_order_amt,
                "min_qty": min_qty
            }
        else:
            log(f"[BYBIT] Errore get_instrument_info {symbol}: {data}")
            return {"qty_step": 0.0001, "precision": 4, "min_order_amt": 5, "min_qty": 0.0}
    except Exception as e:
        log(f"[BYBIT] Errore get_instrument_info {symbol}: {e}")
        return {"qty_step": 0.0001, "precision": 4, "min_order_amt": 5, "min_qty": 0.0}

def get_free_qty(symbol):
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

def notify_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TELEGRAM] Token o chat_id non configurati")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": msg}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        log(f"[TELEGRAM] Errore invio messaggio: {e}")

def limit_buy(symbol, usdt_amount, price_increase_pct=0.005):
    price = get_last_price(symbol)
    if not price or price <= 0:
        log(f"❌ Prezzo non disponibile o nullo per {symbol} | usdt_amount={usdt_amount}")
        return None
    limit_price = price * (1 + price_increase_pct)
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    price_step = info.get("price_step", 0.0001)
    precision = info.get("precision", 4)
    def step_decimals(step):
        s = str(step)
        if '.' in s:
            return len(s.split('.')[-1].rstrip('0'))
        return 0
    price_decimals = step_decimals(price_step)
    retry = 0
    max_retry = 2
    qty_str = calculate_quantity(symbol, usdt_amount)
    qty_step_dec = Decimal(str(qty_step))
    min_qty = Decimal(str(info.get("min_qty", 0.0)))
    min_order_amt = Decimal(str(info.get("min_order_amt", 5)))
    precision_str = '1.' + '0'*precision
    # Primo tentativo: applica la polvere
    if qty_str:
        qty_decimal = Decimal(qty_str)
        polvere = qty_step_dec * 2
        qty_teorica = qty_decimal
        acquistabile = qty_teorica - polvere
        if acquistabile < min_qty:
            acquistabile = min_qty
        acquistabile = (acquistabile // qty_step_dec) * qty_step_dec
        acquistabile = acquistabile.quantize(Decimal(precision_str), rounding=ROUND_DOWN)
        qty_str_fallback = format_quantity_bybit(float(acquistabile), float(qty_step), precision=precision)
        polvere_effettiva = qty_teorica - Decimal(qty_str_fallback)
        log(f"[DECIMALI][LIMIT_BUY][POLVERE][TRY 0] {symbol} | qty_teorica={qty_teorica} | acquistabile={acquistabile} | qty_str_fallback={qty_str_fallback} | polvere_lasciata={polvere_effettiva}")
        qty_decimal = Decimal(qty_str_fallback)
    else:
        log(f"❌ Quantità non valida per acquisto di {symbol} | usdt_amount={usdt_amount} | price={price} | min_order_amt={min_order_amt} | qty_str={qty_str}")
        return None
    while retry <= max_retry:
        # Dal secondo tentativo in poi, riduci solo di uno step
        if retry > 0:
            qty_decimal = qty_decimal - qty_step_dec
            qty_decimal = (qty_decimal // qty_step_dec) * qty_step_dec
            qty_decimal = qty_decimal.quantize(Decimal(precision_str), rounding=ROUND_DOWN)
            if qty_decimal < min_qty:
                log(f"❌ Quantità scesa sotto il minimo per {symbol} durante fallback LIMIT_BUY")
                break
            qty_str_fallback = format_quantity_bybit(float(qty_decimal), float(qty_step), precision=precision)
            log(f"[DECIMALI][LIMIT_BUY][FALLBACK][TRY {retry}] {symbol} | nuovo qty_decimal={qty_decimal} | qty_step={qty_step} | precision={precision} | qty_str_fallback={qty_str_fallback}")
        # LOG ULTRA DETTAGLIATO
        log(f"[ULTRA-LOG][LIMIT_BUY][TRY {retry}] {symbol} | usdt_amount={usdt_amount} | price={price} | qty_step={qty_step} | precision={precision} | min_qty={min_qty} | min_order_amt={min_order_amt} | qty_str={qty_str} | qty_decimal={qty_decimal}")
        log(f"[ULTRA-LOG][LIMIT_BUY][TRY {retry}] {symbol} | Corpo calcolato: qty={qty_str} (teorico), qty_decimal={qty_decimal}, qty_step={qty_step}, precision={precision}, min_qty={min_qty}, min_order_amt={min_order_amt}")
        price_fmt = f"{{0:.{price_decimals}f}}"
        price_str = price_fmt.format(limit_price)
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": qty_str_fallback,
            "price": price_str,
            "timeInForce": "GTC"
        }
        log(f"[ULTRA-LOG][LIMIT_BUY][SEND][TRY {retry}] {symbol} | BODY INVIATO: {json.dumps(body)}")
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
        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        log(f"LIMIT BUY BODY: {body_json}")
        try:
            resp_json = response.json()
        except Exception:
            resp_json = {}
        log(f"RESPONSE: {response.status_code} {resp_json}")
        log(f"[ULTRA-LOG][LIMIT_BUY][SEND][TRY {retry}] {symbol} | RISPOSTA BYBIT: {resp_json}")
        if response.status_code == 200 and resp_json.get("retCode") == 0:
            qty_accettata = body['qty']
            log(f"[ULTRA-LOG][LIMIT_BUY][SUCCESS][TRY {retry}] {symbol} | qty_teorica={qty_str} | qty_troncata={qty_str_fallback} | qty_accettata_bybit={qty_accettata}")
            log(f"[ULTRA-LOG][LIMIT_BUY][SUCCESS][TRY {retry}] {symbol} | Tutti i parametri: usdt_amount={usdt_amount} | price={price} | qty_step={qty_step} | precision={precision} | min_qty={min_qty} | min_order_amt={min_order_amt}")
            log(f"🟢 Ordine LIMIT inviato per {symbol} qty={body['qty']} price={price_str}")
            notify_telegram(f"🟢 Ordine LIMIT inviato per {symbol} qty={body['qty']} price={price_str}")
            return resp_json
        elif resp_json.get("retMsg", "").lower().find("too many decimals") >= 0:
            retry += 1
            log(f"🔄 Tentativo fallback LIMIT_BUY {retry}: provo qty={qty_decimal}")
            continue
        else:
            log(f"❌ Ordine LIMIT fallito per {symbol}: {resp_json.get('retMsg')} | usdt_amount={usdt_amount} | price={price} | qty_str={qty_str} | qty_decimal={qty_decimal} | min_order_amt={min_order_amt} | min_qty={min_qty}")
            if resp_json.get('retMsg', '').lower().find('insufficient balance') >= 0:
                log(f"[DEBUG][LIMIT_BUY][INSUFFICIENT_BALANCE] {symbol} | usdt_balance={get_usdt_balance()} | usdt_amount={usdt_amount} | qty_str={qty_str} | qty_decimal={qty_decimal}")
            if resp_json.get('retMsg', '').lower().find('data sent for paramter') >= 0:
                log(f"[DEBUG][LIMIT_BUY][PARAM_ERROR] {symbol} | body={body_json} | usdt_amount={usdt_amount} | price={price} | qty_str={qty_str} | qty_decimal={qty_decimal}")
            notify_telegram(f"❌ Ordine LIMIT fallito per {symbol}: {resp_json.get('retMsg')}")
            return None
    log(f"❌ Tutti i tentativi LIMIT BUY falliti per {symbol}")
    notify_telegram(f"❌ Tutti i tentativi LIMIT BUY falliti per {symbol}")
    return None

def calculate_quantity(symbol: str, usdt_amount: float) -> Optional[str]:
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    min_order_amt = info.get("min_order_amt", 5)
    min_qty = info.get("min_qty", 0.0)
    precision = info.get("precision", 4)
    if usdt_amount < min_order_amt:
        log(f"❌ Budget troppo basso per {symbol}: {usdt_amount:.2f} < min_order_amt {min_order_amt}")
        return None
    try:
        raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
        log(f"[DECIMALI][CALC_QTY] {symbol} | usdt_amount={usdt_amount} | price={price} | raw_qty={raw_qty} | qty_step={qty_step} | precision={precision}")
        qty_str = format_quantity_bybit(float(raw_qty), float(qty_step), precision=precision)
        qty_dec = Decimal(qty_str)
        min_qty_dec = Decimal(str(min_qty))
        if qty_dec < min_qty_dec:
            log(f"[DECIMALI][CALC_QTY] {symbol} | qty_dec < min_qty_dec: {qty_dec} < {min_qty_dec}")
            qty_dec = min_qty_dec
            qty_str = format_quantity_bybit(float(qty_dec), float(qty_step), precision=precision)
        order_value = qty_dec * Decimal(str(price))
        log(f"[DECIMALI][CALC_QTY] {symbol} | qty_dec={qty_dec} | order_value={order_value}")
        if order_value < Decimal(str(min_order_amt)):
            log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT (minimo richiesto: {min_order_amt})")
            return None
        if qty_dec <= 0:
            log(f"❌ Quantità calcolata troppo piccola per {symbol}")
            return None
        investito_effettivo = float(qty_dec) * float(price)
        if investito_effettivo < 0.95 * usdt_amount:
            log(f"⚠️ Attenzione: valore effettivo investito ({investito_effettivo:.2f} USDT) molto inferiore a quello richiesto ({usdt_amount:.2f} USDT)")
        log(f"[DECIMALI][CALC_QTY][RETURN] {symbol} | qty_str={qty_str}")
        return qty_str
    except Exception as e:
        log(f"❌ Errore calcolo quantità per {symbol}: {e}")
        return None

def market_buy(symbol: str, usdt_amount: float):
    retry = 0
    max_retry = 2
    while retry <= max_retry:
        qty_str = calculate_quantity(symbol, usdt_amount)
        if not qty_str:
            log(f"❌ Quantità non valida per acquisto di {symbol}")
            return None
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.0001)
        min_qty = info.get("min_qty", 0.0)
        min_order_amt = info.get("min_order_amt", 5)
        precision = info.get("precision", 4)
        log(f"[DECIMALI][MARKET_BUY] {symbol} | qty_step={qty_step} | precision={precision} | qty_richiesta={qty_str}")
        qty_str_finale = format_quantity_bybit(float(qty_str), float(qty_step), precision=precision)
        log(f"[DECIMALI][MARKET_BUY][TRY {retry}] {symbol} | qty_step={qty_step} | precision={precision} | qty_str_finale={qty_str_finale}")
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": qty_str_finale
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
            response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
            log(f"MARKET BUY BODY: {body_json}")
            resp_json = response.json()
            log(f"RESPONSE: {response.status_code} {resp_json}")
            if response.status_code == 200 and resp_json.get("retCode") == 0:
                log(f"🟢 Ordine MARKET inviato per {symbol} qty={body['qty']}")
                notify_telegram(f"🟢 Ordine MARKET inviato per {symbol} qty={body['qty']}")
                return resp_json
            elif resp_json.get("retMsg", "").lower().find("too many decimals") >= 0:
                qty_step_dec = Decimal(str(qty_step))
                qty_decimal = Decimal(qty_str) - qty_step_dec
                qty_decimal = (qty_decimal // qty_step_dec) * qty_step_dec
                qty_decimal = qty_decimal.quantize(Decimal('1.' + '0'*precision), rounding=ROUND_DOWN)
                if qty_decimal < Decimal(str(min_qty)):
                    log(f"❌ Quantità scesa sotto il minimo per {symbol} durante fallback BUY")
                    break
                qty_str = format_quantity_bybit(float(qty_decimal), float(qty_step), precision=precision)
                log(f"[DECIMALI][MARKET_BUY][FALLBACK] {symbol} | nuovo qty_decimal={qty_decimal} | qty_step={qty_step} | precision={precision} | qty_str_fallback={qty_str}")
                retry += 1
                log(f"🔄 Tentativo fallback BUY {retry}: provo qty={qty_str}")
                continue
            else:
                log(f"❌ Ordine MARKET fallito per {symbol}: {resp_json.get('retMsg')}")
                notify_telegram(f"❌ Ordine MARKET fallito per {symbol}: {resp_json.get('retMsg')}")
                retry += 1
        except Exception as e:
            log(f"❌ Errore invio ordine MARKET BUY: {e}")
            retry += 1
    log(f"❌ Tutti i tentativi MARKET BUY falliti per {symbol}")
    notify_telegram(f"❌ Tutti i tentativi MARKET BUY falliti per {symbol}")
    return None

def market_sell(symbol: str, qty: float):
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    min_qty = info.get("min_qty", 0.0)
    min_order_amt = info.get("min_order_amt", 5)
    precision = info.get("precision", 4)
    price = get_last_price(symbol)
    log(f"[DECIMALI][MARKET_SELL][PRE-CHECK] {symbol} | qty={qty} | qty_step={qty_step} | precision={precision}")
    log(f"[ULTRA-LOG][MARKET_SELL][PRE-CHECK] {symbol} | qty={qty} | qty_step={qty_step} | precision={precision} | min_qty={min_qty} | min_order_amt={min_order_amt}")
    if not price or price <= 0:
        log(f"❌ Prezzo non disponibile o nullo per {symbol}, impossibile vendere")
        return None
    try:
        dec_qty = Decimal(str(qty))
        step = Decimal(str(qty_step))
        min_qty_dec = Decimal(str(min_qty))
        # --- POLVERE: lascia sempre almeno 2*qty_step ---
        polvere = step * 2
        saldo_preciso = dec_qty
        # Calcola la quantità massima vendibile lasciando la polvere
        vendibile = saldo_preciso - polvere
        if vendibile < min_qty_dec:
            vendibile = min_qty_dec
        # Arrotonda per difetto al multiplo di step
        vendibile = (vendibile // step) * step
        vendibile = vendibile.quantize(Decimal('1.' + '0'*precision), rounding=ROUND_DOWN)
        qty_str = format_quantity_bybit(float(vendibile), float(qty_step), precision=precision)
        floored_qty = Decimal(qty_str)
        polvere_effettiva = saldo_preciso - floored_qty
        log(f"[DECIMALI][MARKET_SELL][POLVERE] {symbol} | saldo_preciso={saldo_preciso} | vendibile={vendibile} | qty_str={qty_str} | polvere_lasciata={polvere_effettiva}")
        log(f"[ULTRA-LOG][MARKET_SELL][POLVERE] {symbol} | saldo_preciso={saldo_preciso} | vendibile={vendibile} | qty_str={qty_str} | polvere_lasciata={polvere_effettiva}")
        if floored_qty < min_qty_dec:
            log(f"❌ Quantità da vendere troppo piccola per {symbol}: {floored_qty} < min_qty {min_qty}")
            return None
        price_dec = Decimal(str(price))
        order_value = floored_qty * price_dec
        log(f"[DECIMALI][MARKET_SELL] {symbol} | qty_step={qty_step} | precision={precision} | qty_richiesta={qty} | floored_qty={floored_qty} | qty_str={qty_str} | order_value={order_value}")
        if order_value < Decimal(str(min_order_amt)):
            log(f"❌ Valore ordine troppo basso per {symbol}: {order_value:.2f} USDT (minimo richiesto: {min_order_amt})")
            return None
        if (floored_qty / step) % 1 != 0:
            log(f"❌ Quantità {floored_qty} non multiplo di qty_step {qty_step} per {symbol}")
            return None
        if floored_qty <= 0:
            log(f"❌ Quantità calcolata troppo piccola per {symbol}")
            return None
    except Exception as e:
        log(f"❌ Errore calcolo quantità vendita {symbol}: {e}")
        return None
    # Invia ordine una sola volta (niente fallback: la polvere evita errori Bybit)
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
        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        log(f"MARKET SELL BODY: {body_json}")
        resp_json = response.json()
        log(f"RESPONSE: {response.status_code} {resp_json}")
        if response.status_code == 200 and resp_json.get("retCode") == 0:
            log(f"🟢 Ordine MARKET SELL inviato per {symbol} qty={qty_str}")
            notify_telegram(f"🟢 Ordine MARKET SELL inviato per {symbol} qty={qty_str}")
        else:
            log(f"❌ Ordine MARKET SELL fallito per {symbol}: {resp_json.get('retMsg')}")
            notify_telegram(f"❌ Ordine MARKET SELL fallito per {symbol}: {resp_json.get('retMsg')}")
        return response
    except Exception as e:
        log(f"❌ Errore invio ordine MARKET SELL: {e}")
        notify_telegram(f"❌ Errore invio ordine MARKET SELL per {symbol}: {e}")
        return None
    retry = 0
    max_retry = 1
    while retry <= max_retry:
        qty_str = calculate_quantity(symbol, usdt_amount)
        if not qty_str:
            log(f"❌ Quantità non valida per acquisto di {symbol}")
            return None

        info = get_instrument_info(symbol)
        min_qty = info.get("min_qty", 0.0)
        min_order_amt = info.get("min_order_amt", 5)
        precision = info.get("precision", 4)

        # Invia ordine MARKET BUY
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
        try:
            response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
            log(f"MARKET BUY BODY: {body_json}")
            resp_json = response.json()
            log(f"RESPONSE: {response.status_code} {resp_json}")
            if response.status_code == 200 and resp_json.get("retCode") == 0:
                log(f"🟢 Ordine MARKET inviato per {symbol} qty={qty_str}")
                notify_telegram(f"🟢 Ordine MARKET inviato per {symbol} qty={qty_str}")
                return resp_json
            else:
                log(f"❌ Ordine MARKET fallito per {symbol}: {resp_json.get('retMsg')}")
                notify_telegram(f"❌ Ordine MARKET fallito per {symbol}: {resp_json.get('retMsg')}")
                retry += 1
        except Exception as e:
            log(f"❌ Errore invio ordine MARKET BUY: {e}")
            retry += 1
    log(f"❌ Tutti i tentativi MARKET BUY falliti per {symbol}")
    notify_telegram(f"❌ Tutti i tentativi MARKET BUY falliti per {symbol}")
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


TEST_MODE = False  # Acquisti e vendite normali abilitati



# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}

def sync_positions_from_wallet():
    """
    Popola open_positions e position_data con tutte le coin con saldo > 0 all'avvio.
    """
    for symbol in ASSETS:
        if symbol == "USDT":
            continue
        qty = get_free_qty(symbol)
        if qty and qty > 0:
            price = get_last_price(symbol)
            if not price:
                continue
            open_positions.add(symbol)
            # Stima entry_price come prezzo attuale, entry_cost come qty*prezzo
            entry_price = price
            entry_cost = qty * price
            # Calcola ATR e SL/TP di default
            df = fetch_history(symbol)
            if df is not None and "Close" in df.columns:
                try:
                    atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                    last = df.iloc[-1]
                    atr_val = last["atr"] if "atr" in last else atr.iloc[-1]
                except Exception:
                    atr_val = price * 0.02
            else:
                atr_val = price * 0.02
            tp = price + (atr_val * TP_FACTOR)
            sl = price - (atr_val * SL_FACTOR)
            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price
            }
            log(f"[SYNC] Posizione trovata in wallet: {symbol} qty={qty} entry={entry_price:.4f} SL={sl:.4f} TP={tp:.4f}")

# --- Esegui sync all'avvio ---
sync_positions_from_wallet()

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

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



# --- LOGICA 70/30 SU VALORE TOTALE PORTAFOGLIO (USDT + coin) ---
def get_portfolio_value():
    usdt_balance = get_usdt_balance()
    total = usdt_balance
    coin_values = {}
    for symbol in ASSETS:
        if symbol == "USDT":
            continue
        qty = get_free_qty(symbol)
        if qty and qty > 0:
            price = get_last_price(symbol)
            if price:
                value = qty * price
                coin_values[symbol] = value
                total += value
    return total, usdt_balance, coin_values

low_balance_alerted = False  # Deve essere fuori dal ciclo per persistere tra i cicli
while True:
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()
    volatile_budget = portfolio_value * 0.7
    stable_budget = portfolio_value * 0.3
    volatile_invested = sum(
        coin_values.get(s, 0) for s in open_positions if s in VOLATILE_ASSETS
    )
    stable_invested = sum(
        coin_values.get(s, 0) for s in open_positions if s in LESS_VOLATILE_ASSETS
    )
    # Log dettagliato bilanciamento
    tot_invested = volatile_invested + stable_invested
    perc_volatile = (volatile_invested / portfolio_value * 100) if portfolio_value > 0 else 0
    perc_stable = (stable_invested / portfolio_value * 100) if portfolio_value > 0 else 0
    log(f"[PORTAFOGLIO] Totale: {portfolio_value:.2f} USDT | Volatili: {volatile_invested:.2f} ({perc_volatile:.1f}%) | Meno volatili: {stable_invested:.2f} ({perc_stable:.1f}%) | USDT: {usdt_balance:.2f}")

    # --- Avviso saldo basso: invia solo una volta finché non torna sopra soglia ---
    # low_balance_alerted ora è globale rispetto al ciclo

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

            # --- LOGICA 70/30: verifica budget disponibile ---
            is_volatile = symbol in VOLATILE_ASSETS
            if is_volatile:
                group_budget = volatile_budget
                group_invested = volatile_invested
                group_label = "VOLATILE"
            else:
                group_budget = stable_budget
                group_invested = stable_invested
                group_label = "MENO VOLATILE"

            group_available = group_budget - group_invested
            log(f"[BUDGET] {symbol} ({group_label}) - Budget gruppo: {group_budget:.2f}, Già investito: {group_invested:.2f}, Disponibile: {group_available:.2f}")
            if group_available < ORDER_USDT:
                log(f"💸 Budget {group_label} insufficiente per {symbol} (disponibile: {group_available:.2f})")
                continue

            # 📊 Valuta la forza del segnale in base alla strategia
            strategy_strength = {
                "Breakout Bollinger": 1.0,
                "MACD bullish + ADX": 0.9,
                "Incrocio SMA 20/50": 0.75,
                "Incrocio EMA 20/50": 0.7,
                "MACD bullish (stabile)": 0.65,
                "Trend EMA + RSI": 0.6
            }
            strength = strategy_strength.get(strategy, 0.5)  # default prudente

            max_invest = min(group_available, usdt_balance) * strength
            order_amount = min(max_invest, group_available, usdt_balance, 250)
            log(f"[FORZA] {symbol} - Strategia: {strategy}, Strength: {strength}, Investo: {order_amount:.2f} USDT (Saldo: {usdt_balance:.2f})")

            # BLOCCO: non tentare acquisto se order_amount < min_order_amt
            min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
            if order_amount < min_order_amt:
                log(f"❌ Saldo troppo basso per acquisto di {symbol}: {order_amount:.2f} < min_order_amt {min_order_amt}")
                if not low_balance_alerted:
                    notify_telegram(f"❗️ Saldo USDT troppo basso per nuovi acquisti. Ricarica il wallet per continuare a operare.")
                    low_balance_alerted = True
                continue
            else:
                low_balance_alerted = False

            # Logga la quantità calcolata PRIMA dell'acquisto
            qty_str = calculate_quantity(symbol, order_amount)
            log(f"[DEBUG-ENTRY] Quantità calcolata per {symbol} con {order_amount:.2f} USDT: {qty_str}")
            if not qty_str:
                log(f"❌ Quantità non valida per acquisto di {symbol}")
                continue

            # ⚠️ INIBISCI GLI ACQUISTI DURANTE IL TEST
            if TEST_MODE:
                log(f"[TEST_MODE] Acquisti inibiti per {symbol}")
                continue

            # Esegui l'acquisto effettivo con ordine LIMIT
            resp = limit_buy(symbol, order_amount)
            if resp is None:
                log(f"❌ Acquisto LIMIT fallito per {symbol}")
                continue

            log(f"🟢 Ordine LIMIT piazzato per {symbol}. Attendi esecuzione.")

            # Dopo l'esecuzione dell'ordine, aggiorna qty e actual_cost SOLO se qty > 0
            time.sleep(2)
            qty = get_free_qty(symbol)
            if not qty or qty == 0:
                log(f"❌ Nessuna quantità acquistata per {symbol} dopo LIMIT BUY. Non registro la posizione.")
                continue
            last_price = get_last_price(symbol)
            if qty and last_price:
                actual_cost = qty * last_price
            else:
                actual_cost = order_amount

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
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_max": price
            }

            open_positions.add(symbol)
            log(f"🟢 Acquisto registrato per {symbol} | Entry: {price:.4f} | TP: {tp:.4f} | SL: {sl:.4f}")
            notify_telegram(f"🟢📈 Acquisto per {symbol}\nPrezzo: {price:.4f}\nStrategia: {strategy}\nInvestito: {order_amount:.2f} USDT")
            time.sleep(3)

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

    # 🔁 Controllo Trailing Stop e Stop Loss statico per le posizioni aperte
    for symbol in list(open_positions):
        if symbol not in position_data:
            continue
        entry = position_data[symbol]
        current_price = get_last_price(symbol)
        if not current_price:
            continue
        # Soglia trailing dinamica: 0.02 per asset volatili, 0.005 per asset stabili
        if symbol in VOLATILE_ASSETS:
            trailing_threshold = 0.02
        else:
            trailing_threshold = 0.005
        soglia_attivazione = entry["entry_price"] * (1 + trailing_threshold)
        log(f"[TRAILING CHECK] {symbol} | entry_price={entry['entry_price']:.4f} | current_price={current_price:.4f} | soglia={soglia_attivazione:.4f} | trailing_active={entry['trailing_active']} | threshold={trailing_threshold}")
        # 🧪 Attiva Trailing se supera la soglia
        if not entry["trailing_active"] and current_price >= soglia_attivazione:
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
        # --- CHIUSURA AUTOMATICA: Trailing Stop o Stop Loss statico ---
        sl_triggered = False
        sl_type = None
        # Trailing SL
        if entry["trailing_active"] and current_price <= entry["sl"]:
            sl_triggered = True
            sl_type = "Trailing Stop"
        # Stop Loss statico
        elif not entry["trailing_active"] and current_price <= entry["sl"]:
            sl_triggered = True
            sl_type = "Stop Loss"
        if sl_triggered:
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
                    log(f"🔻 {sl_type} attivato per {symbol} → Prezzo: {current_price:.4f} | SL: {entry['sl']:.4f}")
                    notify_telegram(f"🔻 {sl_type} venduto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                    log_trade_to_google(symbol, entry_price, current_price, pnl, sl_type, "SL Triggered")
                    # 🗑️ Pulizia
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
                else:
                    log(f"❌ Vendita fallita con {sl_type} per {symbol}")
            else:
                log(f"❌ Quantità nulla o troppo piccola per vendita {sl_type} su {symbol}")
    # Sicurezza: attesa tra i cicli principali
    time.sleep(INTERVAL_MINUTES * 60)