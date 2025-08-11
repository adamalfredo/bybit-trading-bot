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

# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
LIQUIDITY_MIN_VOLUME = 1_000_000  # Soglia minima volume 24h USDT (consigliato)

# --- BLACKLIST STABLECOIN ---
STABLECOIN_BLACKLIST = [
    "USDCUSDT", "USDEUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "EURUSDT", "USDTUSDT"
]

def update_assets(top_n=18, n_stable=7):
    """
    Aggiorna ASSETS, LESS_VOLATILE_ASSETS e VOLATILE_ASSETS:
    - Prende i top N asset spot per volume 24h USDT
    - I primi n_stable per market cap (BTC, ETH, ... se presenti) sono i meno volatili
    - Gli altri sono considerati volatili
    """
    global ASSETS, LESS_VOLATILE_ASSETS, VOLATILE_ASSETS
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "spot"}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"[ASSETS] Errore API tickers: {data}")
            return
        tickers = data["result"]["list"]
        # Filtra solo coppie USDT, con volume sufficiente, ed esclude le stablecoin
        usdt_tickers = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and float(t.get("turnover24h", 0)) >= LIQUIDITY_MIN_VOLUME
            and t["symbol"] not in STABLECOIN_BLACKLIST
            and float(t.get("lastPrice", 0)) >= 0.01  # <--- FILTRO PREZZO!
        ]
        # Ordina per volume 24h (turnover24h)
        usdt_tickers.sort(key=lambda x: float(x.get("turnover24h", 0)), reverse=True)
        # Prendi i top N
        top = usdt_tickers[:top_n]
        ASSETS = [t["symbol"] for t in top]
        # Ordina per market cap stimata (lastPrice * totalVolume)
        top.sort(key=lambda x: float(x.get("lastPrice", 0)) * float(x.get("totalVolume", 0)), reverse=True)
        LESS_VOLATILE_ASSETS = [t["symbol"] for t in top[:n_stable]]
        VOLATILE_ASSETS = [s for s in ASSETS if s not in LESS_VOLATILE_ASSETS]
        log(f"[ASSETS] Aggiornati: {ASSETS}\nMeno volatili: {LESS_VOLATILE_ASSETS}\nVolatili: {VOLATILE_ASSETS}")
    except Exception as e:
        log(f"[ASSETS] Errore aggiornamento lista asset: {e}")

INTERVAL_MINUTES = 15
ATR_WINDOW = 14
TP_FACTOR = 2.0
SL_FACTOR = 1.5
# Soglie dinamiche consigliate
TP_MIN = 1.5
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.5
TRAILING_MIN = 0.005  # 0.5%
TRAILING_MAX = 0.03   # 3%
TRAILING_ACTIVATION_THRESHOLD = 0.02
TRAILING_SL_BUFFER = 0.01
TRAILING_DISTANCE = 0.02
INITIAL_STOP_LOSS_PCT = 0.02
COOLDOWN_MINUTES = 60
cooldown = {}

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

def format_quantity_bybit(qty: float, qty_step: float, precision: Optional[int] = None) -> str:
    from decimal import Decimal, ROUND_DOWN
    def get_decimals(step):
        s = str(step)
        if '.' in s:
            return len(s.split('.')[-1].rstrip('0'))
        return 0
    if precision is None:
        precision = get_decimals(qty_step)
    step_dec = Decimal(str(qty_step))
    qty_dec = Decimal(str(qty))
    floored_qty = (qty_dec // step_dec) * step_dec
    quantize_str = '1.' + '0'*precision if precision > 0 else '1'
    floored_qty = floored_qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)
    # Garantisce che sia multiplo esatto di qty_step
    if (floored_qty / step_dec) % 1 != 0:
        floored_qty = (floored_qty // step_dec) * step_dec
        floored_qty = floored_qty.quantize(Decimal(quantize_str), rounding=ROUND_DOWN)
    fmt = f"{{0:.{precision}f}}"
    log(f"[DECIMALI][FORMAT_QTY] qty={qty} | qty_step={qty_step} | precision={precision} | floored_qty={floored_qty} | quantize_str={quantize_str}")
    return fmt.format(floored_qty)

def get_max_qty_from_orderbook(symbol, usdt_amount, qty_step, precision):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/orderbook"
    params = {"category": "spot", "symbol": symbol}
    resp = requests.get(endpoint, params=params, timeout=10)
    data = resp.json()
    if data.get("retCode") != 0:
        log(f"[ORDERBOOK] Errore API: {data}")
        return None
    asks = data["result"]["a"]
    total_qty = Decimal("0")
    total_usdt = Decimal("0")
    for ask in asks:
        price = Decimal(str(ask[0]))
        qty = Decimal(str(ask[1]))
        value = price * qty
        if total_usdt + value >= Decimal(str(usdt_amount)):
            needed = (Decimal(str(usdt_amount)) - total_usdt) / price
            total_qty += needed
            break
        else:
            total_qty += qty
            total_usdt += value
    step_dec = Decimal(str(qty_step))
    total_qty = (total_qty // step_dec) * step_dec
    total_qty = total_qty.quantize(Decimal('1.' + '0'*precision), rounding=ROUND_DOWN)
    return str(total_qty)

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
    
def get_best_ask(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/orderbook"
        params = {"category": "spot", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            asks = data["result"]["a"]
            if asks and len(asks) > 0:
                return float(asks[0][0])
        return None
    except Exception as e:
        log(f"[BYBIT] Errore get_best_ask {symbol}: {e}")
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
            max_qty = float(info.get("lotSizeFilter", {}).get("maxOrderQty", 0.0))  # <-- aggiunto max_qty
            return {
                "qty_step": qty_step,
                "price_step": price_step,
                "precision": precision,
                "min_order_amt": min_order_amt,
                "min_qty": min_qty,
                "max_qty": max_qty  # <-- aggiunto max_qty
            }
        else:
            log(f"[BYBIT] Errore get_instrument_info {symbol}: {data}")
            return {"qty_step": 0.0001, "precision": 4, "min_order_amt": 5, "min_qty": 0.0, "max_qty": 0.0}
    except Exception as e:
        log(f"[BYBIT] Errore get_instrument_info {symbol}: {e}")
        return {"qty_step": 0.0001, "precision": 4, "min_order_amt": 5, "min_qty": 0.0, "max_qty": 0.0}

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
                # log(f"[BYBIT BALANCE DEBUG] {coin}: {c}")
                # Ordine di priorità: availableToWithdraw > walletBalance > equity
                raw = c.get("walletBalance", "0")
                try:
                    qty = float(raw) if raw else 0.0
                    if qty > 0:
                        log(f"📦 Saldo trovato per {coin}: {qty}")
                    else:
                        log(f"🟡 Nessun saldo disponibile per {coin}")
                    return qty
                except Exception as e:
                    log(f"⚠️ Errore conversione quantità {coin}: {e} (raw={raw})")
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
    min_qty = info.get("min_qty", 0.0)
    min_order_amt = info.get("min_order_amt", 5)
    def step_decimals(step):
        s = str(step)
        if '.' in s:
            return len(s.split('.')[-1].rstrip('0'))
        return 0
    price_decimals = step_decimals(price_step)
    retry = 0
    max_retry = 2
    qty_str = calculate_quantity(symbol, usdt_amount)
    if not qty_str:
        log(f"❌ Quantità non valida per acquisto di {symbol}")
        return None
    qty_step_dec = Decimal(str(qty_step))
    qty_decimal = Decimal(qty_str)
    while retry <= max_retry:
        qty_str_finale = format_quantity_bybit(float(qty_decimal), float(qty_step), precision=precision)
        price_fmt = f"{{0:.{price_decimals}f}}"
        price_str = price_fmt.format(limit_price)
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": qty_str_finale,
            "price": price_str,
            "timeInForce": "GTC"
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
            log(f"LIMIT BUY BODY: {body_json}")
            resp_json = response.json()
            log(f"RESPONSE: {response.status_code} {resp_json}")
            if response.status_code == 200 and resp_json.get("retCode") == 0:
                log(f"🟢 Ordine LIMIT inviato per {symbol} qty={body['qty']} price={price_str}")
                notify_telegram(f"🟢 Ordine LIMIT inviato per {symbol} qty={body['qty']} price={price_str}")
                return resp_json
            elif resp_json.get("retMsg", "").lower().find("too many decimals") >= 0:
                qty_decimal = qty_decimal - qty_step_dec
                qty_decimal = (qty_decimal // qty_step_dec) * qty_step_dec
                qty_decimal = qty_decimal.quantize(Decimal('1.' + '0'*precision), rounding=ROUND_DOWN)
                if qty_decimal < Decimal(str(min_qty)):
                    log(f"❌ Quantità scesa sotto il minimo per {symbol} durante fallback LIMIT_BUY")
                    break
                log(f"[DECIMALI][LIMIT_BUY][FALLBACK] {symbol} | nuovo qty_decimal={qty_decimal} | qty_step={qty_step} | precision={precision}")
                retry += 1
                log(f"🔄 Tentativo fallback LIMIT_BUY {retry}: provo qty={qty_decimal}")
                continue
            else:
                log(f"❌ Ordine LIMIT fallito per {symbol}: {resp_json.get('retMsg')}")
                notify_telegram(f"❌ Ordine LIMIT fallito per {symbol}: {resp_json.get('retMsg')}")
                retry += 1
        except Exception as e:
            log(f"❌ Errore invio ordine LIMIT BUY: {e}")
            retry += 1
    log(f"❌ Tutti i tentativi LIMIT BUY falliti per {symbol}")
    # notify_telegram(f"❌ Tutti i tentativi LIMIT BUY falliti per {symbol}")
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
    max_qty = info.get("max_qty", 0.0)
    precision = info.get("precision", 4)
    if usdt_amount < min_order_amt:
        log(f"❌ Budget troppo basso per {symbol}: {usdt_amount:.2f} < min_order_amt {min_order_amt}")
        return None
    try:
        raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
        log(f"[DECIMALI][CALC_QTY] {symbol} | usdt_amount={usdt_amount} | price={price} | raw_qty={raw_qty} | qty_step={qty_step} | precision={precision}")
        qty_str = format_quantity_bybit(float(raw_qty), float(qty_step), precision=precision)
        qty_dec = Decimal(qty_str)
        log(f"[DECIMALI][FORMAT_QTY_PATCH] {symbol} | qty_step={qty_step} | price={price} | precision={precision} | qty_str={qty_str}")
        log(f"[DEBUG-QTY] {symbol} | qty_step={qty_step} | qty_str={qty_str} | qty_dec={qty_dec}")

        min_qty_dec = Decimal(str(min_qty))

        # NON applicare limiti artificiali sulla quantità!
        # Solo controllo Bybit: min_qty e min_order_amt
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

        # LOG DIAGNOSTICO FINALE
        log(f"[DEBUG-ORDER] {symbol} | USDT da investire: {usdt_amount} | Prezzo: {price} | Quantità calcolata: {qty_dec} | Valore ordine: {order_value:.8f} USDT | qty_step: {qty_step} | precision: {precision}")

        return qty_str
    except Exception as e:
        log(f"❌ Errore calcolo quantità per {symbol}: {e}")
        return None

def market_buy(symbol: str, usdt_amount: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None
    safe_usdt_amount = usdt_amount * 0.98
    info = get_instrument_info(symbol)
    min_qty = info.get("min_qty", 0.0)
    min_order_amt = info.get("min_order_amt", 5)
    precision = info.get("precision", 4)
    qty_step = info.get("qty_step", 0.0001)
    max_qty = info.get("max_qty", 0.0)
    log(f"[BYBIT LIMITS] {symbol} | min_qty={min_qty} | max_qty={max_qty} | qty_step={qty_step} | precision={precision} | min_order_amt={min_order_amt}")

    # PATCH: fallback automatico in caso di insufficient balance
    max_fallback = 5
    fallback_count = 0
    qty_str = calculate_quantity(symbol, safe_usdt_amount)
    if not qty_str:
        log(f"❌ Quantità non valida per acquisto di {symbol} (con margine sicurezza)")
        return None

    def _send_order(qty_str, usdt_for_qty):
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
        response = requests.post(f"{BYBIT_BASE_URL}/v5/order/create", headers=headers, data=body_json)
        log(f"BUY BODY: {body_json}")
        try:
            resp_json = response.json()
        except Exception:
            resp_json = {}
        log(f"RESPONSE: {response.status_code} {resp_json}")
        if resp_json.get("retCode") != 0:
            log(f"[BYBIT ERROR] Motivo rifiuto acquisto {symbol}: {resp_json.get('retMsg')}")
        if 'result' in resp_json:
            result = resp_json['result']
            filled = result.get('cumExecQty') or result.get('execQty') or result.get('qty')
            order_status = result.get('orderStatus')
            log(f"[BYBIT ORDER RESULT] filled: {filled}, orderStatus: {order_status}, result: {result}")
        return response, resp_json

    qty_decimal = Decimal(qty_str)
    while fallback_count <= max_fallback:
        # Ricalcola il prezzo e la quantità ad ogni fallback!
        price_now = get_best_ask(symbol) or get_last_price(symbol)
        if not price_now:
            log(f"❌ Prezzo non disponibile per {symbol} durante fallback")
            return None
        usdt_balance_now = get_usdt_balance()
        usdt_for_qty = min(safe_usdt_amount, usdt_balance_now)
        qty_decimal = (Decimal(usdt_for_qty) / Decimal(str(price_now))) * Decimal("0.98")
        step_dec = Decimal(str(qty_step))
        qty_decimal = (qty_decimal // step_dec) * step_dec
        qty_decimal = qty_decimal.quantize(Decimal('1.' + '0'*precision), rounding=ROUND_DOWN)
        log(f"[CHECK QTY] {symbol} | qty_decimal={qty_decimal} | precision={precision} | step_dec={step_dec}")
        # Controlla che la quantità sia nei limiti Bybit
        if qty_decimal < Decimal(str(min_qty)):
            log(f"❌ Quantità calcolata troppo piccola per {symbol} (qty={qty_decimal}, min_qty={min_qty})")
            return None
        if max_qty and qty_decimal > Decimal(str(max_qty)):
            qty_decimal = Decimal(str(max_qty))
            log(f"⚠️ Quantità calcolata troppo grande per {symbol}, imposto a max_qty={max_qty}")
        log(f"[FALLBACK][BUY] {symbol} | Ricalcolo qty con prezzo attuale {price_now} e usdt {usdt_for_qty} → qty={qty_decimal}")
        log(f"[DEBUG][ORDER] {symbol} | qty_decimal={qty_decimal} | str(qty_decimal)={str(qty_decimal)} | type={type(qty_decimal)}")
        order_value = float(qty_decimal) * float(price_now)
        log(f"[DEBUG][ORDER_VALUE] {symbol} | qty={qty_decimal} | price={price_now} | order_value={order_value} | usdt_balance_now={usdt_balance_now}")
        response, resp_json = _send_order(str(qty_decimal), usdt_for_qty)
        if response.status_code == 200 and resp_json.get("retCode") == 0:
            time.sleep(2)
            qty_after = get_free_qty(symbol)
            if not qty_after or qty_after == 0:
                time.sleep(3)
                qty_after = get_free_qty(symbol)
            try:
                qty_requested = float(qty_decimal)
            except Exception:
                qty_requested = None
            if qty_requested and qty_after < 0.8 * qty_requested:
                log(f"⚠️ Quantità acquistata ({qty_after}) molto inferiore a quella richiesta ({qty_requested}) per {symbol}")
                diff = qty_requested - qty_after
                price2 = get_last_price(symbol)
                if diff > min_qty and price2 and (diff * price2) > min_order_amt:
                    diff_str = f"{diff:.{precision}f}".rstrip('0').rstrip('.')
                    log(f"🔁 TENTO RIACQUISTO della differenza: {diff_str} {symbol}")
                    fallback_count += 1
                    qty_decimal = Decimal(diff_str)
                    continue
                else:
                    log(f"❌ Differenza troppo piccola per riacquisto su {symbol}")
                    return qty_after
            if qty_after and qty_after > 0:
                log(f"🟢 Acquisto registrato per {symbol}")
                return qty_after
            else:
                log(f"⚠️ Acquisto riuscito ma saldo non aggiornato per {symbol}")
                return None
        elif resp_json.get("retMsg", "").lower().find("insufficient balance") >= 0:
            fallback_count += 1
            continue
        elif resp_json.get("retMsg", "").lower().find("too many decimals") >= 0:
            fallback_count += 1
            # Riduci la precisione
            precision = max(0, precision - 1)
            qty_decimal = qty_decimal.quantize(Decimal('1.' + '0'*precision), rounding=ROUND_DOWN)
            log(f"[FALLBACK][BUY] {symbol} | Troppi decimali, provo con precisione {precision}")
            continue
        else:
            log(f"❌ Ordine MARKET fallito per {symbol}: {resp_json.get('retMsg')}")
            return None
    log(f"❌ Tutti i tentativi MARKET BUY falliti per {symbol}")
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

    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.0001)
    precision = info.get("precision", 4)
    min_qty = info.get("min_qty", qty_step)
    min_order_amt = info.get("min_order_amt", 5)
    if not qty_step or qty_step <= 0:
        qty_step = 0.0001
        precision = 4

    # PATCH: vendi sempre il saldo effettivo arrotondato al passo consentito
    safe_qty = get_free_qty(symbol)
    if safe_qty < min_qty:
        log(f"❌ Quantità troppo piccola per vendita {symbol}: {safe_qty} < min_qty {min_qty}")
        return

    max_fallback = 5    
    fallback_count = 0
    orig_precision = precision
    qty_to_try = safe_qty
    while fallback_count <= max_fallback:
        try:
            use_precision = max(0, orig_precision - fallback_count)
            qty_str = format_quantity_bybit(qty_to_try, qty_step, use_precision)
            valore_usd = float(qty_str) * price
            log(f"[DECIMALI][SELL] {symbol} | qty={qty} | safe_qty={qty_to_try} | qty_step={qty_step} | min_qty={min_qty} | min_order_amt={min_order_amt} | qty_str={qty_str} | valore_usd={valore_usd:.4f} | fallback={fallback_count}")
            if valore_usd < min_order_amt or Decimal(qty_str) < Decimal(str(min_qty)) or Decimal(qty_str) <= 0:
                saldo_attuale = get_free_qty(symbol)
                log(f"❌ Quantità troppo piccola o valore troppo basso per {symbol} (qty={qty_str}, valore_usd={valore_usd:.4f}, min_order_amt={min_order_amt})")
                # notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} (valore troppo basso: {valore_usd:.4f} USDT, min richiesto: {min_order_amt})")
                return
        except Exception as e:
            log(f"❌ Errore arrotondamento quantità {symbol}: {e}")
            saldo_attuale = get_free_qty(symbol)
            notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} (errore quantità, saldo: {saldo_attuale})")
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
            try:
                resp_json = resp.json()
            except Exception:
                resp_json = {}
            if resp.status_code == 200 and resp_json.get("retCode") == 0:
                return resp
            elif resp_json.get("retMsg", "").lower().find("insufficient balance") >= 0:
                # PATCH: riduci la quantità e riprova
                fallback_count += 1
                qty_to_try = float(qty_str) - 2 * float(qty_step)
                log(f"[DECIMALI][SELL][FALLBACK] {symbol} | Insufficient balance, riprovo con qty={qty_to_try}")
                continue
            elif resp_json.get("retMsg", "").lower().find("too many decimals") >= 0:
                fallback_count += 1
                log(f"[DECIMALI][SELL][FALLBACK] {symbol} | Troppi decimali, provo con precisione {orig_precision-fallback_count}")
                continue
            else:
                notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol}! Codice: {resp.status_code} - Msg: {resp_json.get('retMsg','?')}")
                return resp
        except Exception as e:
            log(f"❌ Errore invio ordine SELL: {e}")
            notify_telegram(f"❌❗️ Errore invio ordine SELL per {symbol}: {e}")
            return None
    log(f"❌ Tutti i tentativi di vendita falliti per {symbol} (decimali/bilancio)")
    notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} (tutti i fallback decimali/bilancio esauriti)")
    return None

def fetch_history(symbol: str):
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "spot",
        "symbol": symbol,
        "interval": str(INTERVAL_MINUTES),
        "limit": 250  # aumentato per supportare indicatori lunghi
    }
    try:
        resp = requests.get(endpoint, params=params, timeout=10)
        # Logga la risposta grezza per debug
        # log(f"[KLINE-RAW] {symbol} | status={resp.status_code} | text={resp.text[:300]}")
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
        # Conversione a float e log dei NaN
        for col in ["Open", "High", "Low", "Close", "Volume"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        # Log delle prime 5 righe e conteggio NaN
        # log(f"[KLINE-DF] {symbol} | head:\n{df.head(5)}")
        # log(f"[KLINE-DF] {symbol} | NaN per colonna: {df.isna().sum().to_dict()}")
        # Logga le righe con almeno un NaN nelle colonne chiave
        nan_rows = df[df[["Open", "High", "Low", "Close", "Volume"]].isna().any(axis=1)]
        # if not nan_rows.empty:
        #     log(f"[KLINE-DF] {symbol} | Righe con NaN:\n{nan_rows.head(5)}")
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
        if df is None or len(df) < 3:
            log(f"[ANALYZE] Dati storici insufficienti per {symbol} (df is None o len < 3)")
            return None, None, None
        # Log approfondito prima del dropna
        # log(f"[ANALYZE-DF] {symbol} | Prima del dropna, len={len(df)}")
        # log(f"[ANALYZE-DF] {symbol} | head:\n{df.head(5)}")
        # log(f"[ANALYZE-DF] {symbol} | NaN per colonna: {df.isna().sum().to_dict()}")
        close = find_close_column(df)
        if close is None:
            log(f"[ANALYZE] Colonna close non trovata per {symbol}")
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
            "bb_upper", "bb_lower", "rsi", "sma20", "sma50", "ema20", "ema50", "ema200",
            "macd", "macd_signal", "adx", "atr"
        ], inplace=True)
        # Log dopo il dropna
        log(f"[ANALYZE-DF] {symbol} | Dopo dropna, len={len(df)}")
        # log(f"[ANALYZE-DF] {symbol} | head:\n{df.head(5)}")
        # log(f"[ANALYZE-DF] {symbol} | NaN per colonna: {df.isna().sum().to_dict()}")

        if len(df) < 3:
            # Logga anche la quantità di NaN per colonna PRIMA del dropna
            log(f"[ANALYZE-DF] {symbol} | PRIMA DEL DROPNNA: NaN per colonna: {df.isna().sum().to_dict()}")
            log(f"[ANALYZE] Dati storici insufficienti dopo dropna per {symbol} (len < 3)")
            return None, None, None

        is_volatile = symbol in VOLATILE_ASSETS
        adx_threshold = 20 if is_volatile else 15

        last = df.iloc[-1]
        prev = df.iloc[-2]
        price = float(last["Close"])

        # --- Filtro trend di fondo: solo se EMA50 > EMA200 (trend rialzista) ---
        if last["ema50"] <= last["ema200"] * 0.97:
            log(f"[STRATEGY][{symbol}] Filtro trend NON superato (soft): ema50={last['ema50']:.4f} <= 97% ema200={last['ema200']:.4f}")
            return None, None, None

        # --- Soglie dinamiche: TP/SL/trailing in base a volatilità ---
        atr_ratio = last["atr"] / price if price > 0 else 0
        VOLATILITY_MIN_RATIO = 0.004  # 0.4%
        if atr_ratio < VOLATILITY_MIN_RATIO:
            log(f"[VOLATILITY FILTER][{symbol}] ATR/Prezzo troppo basso ({atr_ratio:.2%}), nessun segnale ENTRY.")
            return None, None, None
        # TP dinamico tra 1.5x e 3x ATR
        tp_dyn = min(TP_MAX, max(TP_MIN, TP_FACTOR + atr_ratio * 5))
        # SL dinamico tra 1x e 2.5x ATR
        sl_dyn = min(SL_MAX, max(SL_MIN, SL_FACTOR + atr_ratio * 3))
        # Trailing dinamico tra 0.5% e 3%
        trailing_dyn = min(TRAILING_MAX, max(TRAILING_MIN, 0.005 + atr_ratio))

        # --- Nuova logica: almeno 2 condizioni di ingresso devono essere vere ---
        entry_conditions = []
        entry_strategies = []
        # Log dettagliato per ogni condizione
        if is_volatile:
            # Condizioni per asset volatili
            cond1 = last["Close"] > last["bb_upper"]
            cond2 = last["rsi"] < 70
            if cond1 and cond2:
                entry_conditions.append(True)
                entry_strategies.append("Breakout Bollinger")
            else:
                log(f"[STRATEGY][{symbol}] Condizione Breakout Bollinger: Close={last['Close']:.4f} > bb_upper={last['bb_upper']:.4f} = {cond1}, RSI={last['rsi']:.2f} < 70 = {cond2}")
            cond3 = prev["sma20"] < prev["sma50"]
            cond4 = last["sma20"] > last["sma50"]
            if cond3 and cond4:
                entry_conditions.append(True)
                entry_strategies.append("Incrocio SMA 20/50")
            else:
                log(f"[STRATEGY][{symbol}] Condizione Incrocio SMA 20/50: prev_sma20={prev['sma20']:.4f} < prev_sma50={prev['sma50']:.4f} = {cond3}, last_sma20={last['sma20']:.4f} > last_sma50={last['sma50']:.4f} = {cond4}")
            cond5 = last["macd"] > last["macd_signal"]
            cond6 = last["adx"] > adx_threshold
            if cond5 and cond6:
                entry_conditions.append(True)
                entry_strategies.append("MACD bullish + ADX")
            else:
                log(f"[STRATEGY][{symbol}] Condizione MACD bullish + ADX: macd={last['macd']:.4f} > macd_signal={last['macd_signal']:.4f} = {cond5}, adx={last['adx']:.2f} > soglia={adx_threshold} = {cond6}")
        else:
            # Condizioni per asset stabili
            cond1 = prev["ema20"] < prev["ema50"]
            cond2 = last["ema20"] > last["ema50"]
            if cond1 and cond2:
                entry_conditions.append(True)
                entry_strategies.append("Incrocio EMA 20/50")
            else:
                log(f"[STRATEGY][{symbol}] Condizione Incrocio EMA 20/50: prev_ema20={prev['ema20']:.4f} < prev_ema50={prev['ema50']:.4f} = {cond1}, last_ema20={last['ema20']:.4f} > last_ema50={last['ema50']:.4f} = {cond2}")
            cond3 = last["macd"] > last["macd_signal"]
            cond4 = last["adx"] > adx_threshold
            if cond3 and cond4:
                entry_conditions.append(True)
                entry_strategies.append("MACD bullish (stabile)")
            else:
                log(f"[STRATEGY][{symbol}] Condizione MACD bullish (stabile): macd={last['macd']:.4f} > macd_signal={last['macd_signal']:.4f} = {cond3}, adx={last['adx']:.2f} > soglia={adx_threshold} = {cond4}")
            cond5 = last["rsi"] > 50
            cond6 = last["ema20"] > last["ema50"]
            if cond5 and cond6:
                entry_conditions.append(True)
                entry_strategies.append("Trend EMA + RSI")
            else:
                log(f"[STRATEGY][{symbol}] Condizione Trend EMA + RSI: rsi={last['rsi']:.2f} > 50 = {cond5}, ema20={last['ema20']:.4f} > ema50={last['ema50']:.4f} = {cond6}")

        # CONDIZIONI DA RISPETTARE PER ACQUISTO
        # if (is_volatile and len(entry_conditions) >= 1) or (not is_volatile and len(entry_conditions) >= 2):
        if len(entry_conditions) >= 1:
            log(f"[STRATEGY][{symbol}] Segnale ENTRY generato: strategie attive: {entry_strategies}")
            return "entry", ", ".join(entry_strategies), price
        else:
            log(f"[STRATEGY][{symbol}] Nessun segnale ENTRY: condizioni soddisfatte = {len(entry_conditions)}")

        # EXIT comune a tutti
        cond_exit1 = last["Close"] < last["bb_lower"]
        cond_exit2 = last["rsi"] > 30
        if cond_exit1 and cond_exit2:
            log(f"[STRATEGY][{symbol}] Segnale EXIT: Rimbalzo RSI + BB")
            return "exit", "Rimbalzo RSI + BB", price
        else:
            log(f"[STRATEGY][{symbol}] Condizione EXIT Rimbalzo RSI + BB: Close={last['Close']:.4f} < bb_lower={last['bb_lower']:.4f} = {cond_exit1}, RSI={last['rsi']:.2f} > 30 = {cond_exit2}")
        cond_exit3 = last["macd"] < last["macd_signal"]
        cond_exit4 = last["adx"] > adx_threshold
        if cond_exit3 and cond_exit4:
            log(f"[STRATEGY][{symbol}] Segnale EXIT: MACD bearish + ADX")
            return "exit", "MACD bearish + ADX", price
        else:
            log(f"[STRATEGY][{symbol}] Condizione EXIT MACD bearish + ADX: macd={last['macd']:.4f} < macd_signal={last['macd_signal']:.4f} = {cond_exit3}, adx={last['adx']:.2f} > soglia={adx_threshold} = {cond_exit4}")

        log(f"[STRATEGY][{symbol}] Nessun segnale EXIT generato")
        return None, None, None
    except Exception as e:
        log(f"Errore analisi {symbol}: {e}")
        return None, None, None

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT AVVIATO - In ascolto per segnali di ingresso/uscita")

TEST_MODE = False  # Acquisti e vendite normali abilitati

MIN_HOLDING_MINUTES = 3  # Tempo minimo in minuti da attendere dopo l'acquisto prima di poter attivare uno stop loss
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
        price = get_last_price(symbol)
        if price is None:
            continue
        if price < 0.01:
            log(f"[SKIP] {symbol}: prezzo troppo basso ({price}), salto acquisto/vendita.")
            continue
        qty = get_free_qty(symbol)
        if qty and qty > 0:
            # ...resto del codice...
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
# Aggiorna la lista asset all'avvio
update_assets()
sync_positions_from_wallet()

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

def calculate_stop_loss(entry_price, current_price, p_max, trailing_active):
    if not trailing_active:
        return entry_price * (1 - INITIAL_STOP_LOSS_PCT)
    else:
        return p_max * (1 - TRAILING_DISTANCE)

import threading
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
def log_trade_to_google(symbol, entry, exit, pnl_pct, strategy, result_type, usdt_enter=None, usdt_exit=None, delta_usd=None):
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
            result_type,
            usdt_enter if usdt_enter is not None else "",
            usdt_exit if usdt_exit is not None else "",
            delta_usd if delta_usd is not None else ""
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
def trailing_stop_worker():
    while True:
        for symbol in list(open_positions):
            if symbol not in position_data:
                continue
            saldo = get_free_qty(symbol)
            prezzo = get_last_price(symbol)
            valore_usd = saldo * prezzo if saldo and prezzo else 0
            min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
            if saldo is None or valore_usd < min_order_amt:
                log(f"[CLEANUP] {symbol}: valore troppo basso ({valore_usd:.2f} USD), rimuovo da open_positions e position_data (polvere)")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue
            entry = position_data[symbol]
            holding_seconds = time.time() - entry.get("entry_time", 0)
            if holding_seconds < MIN_HOLDING_MINUTES * 60:
                log(f"[HOLDING][FAST] {symbol}: attendo ancora {MIN_HOLDING_MINUTES - holding_seconds/60:.1f} min prima di attivare SL/TSL")
                continue
            current_price = get_last_price(symbol)
            if not current_price:
                continue
            if symbol in VOLATILE_ASSETS:
                trailing_threshold = 0.015  # <--- MODIFICATO da 0.02 a 0.015
            else:
                trailing_threshold = 0.004  # <--- MODIFICATO da 0.005 a 0.004
            soglia_attivazione = entry["entry_price"] * (1 + trailing_threshold)
            log(f"[TRAILING CHECK][FAST] {symbol} | entry_price={entry['entry_price']:.4f} | current_price={current_price:.4f} | soglia={soglia_attivazione:.4f} | trailing_active={entry['trailing_active']} | threshold={trailing_threshold}")
            if not entry["trailing_active"] and current_price >= soglia_attivazione:
                entry["trailing_active"] = True
                log(f"🔛 Trailing SL attivato per {symbol} sopra soglia → Prezzo: {current_price:.4f}")
                notify_telegram(f"🔛🔻Trailing SL attivato su {symbol}\nPrezzo: {current_price:.4f}")
            if entry["trailing_active"]:
                if current_price > entry["p_max"]:
                    entry["p_max"] = current_price
                    new_sl = current_price * (1 - TRAILING_SL_BUFFER)
                    if new_sl > entry["sl"]:
                        log(f"📉 SL aggiornato per {symbol}: da {entry['sl']:.4f} a {new_sl:.4f}")
                        entry["sl"] = new_sl
            sl_triggered = False
            sl_type = None
            if entry["trailing_active"] and current_price <= entry["sl"]:
                sl_triggered = True
                sl_type = "Trailing SL"
            elif not entry["trailing_active"] and current_price <= entry["sl"]:
                sl_triggered = True
                sl_type = "SL"
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
                        notify_telegram(f"🎯🔻{sl_type} venduto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                        log_trade_to_google(symbol, entry_price, current_price, pnl, sl_type, "SL Triggered", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                        continue
                    else:
                        log(f"❌ Vendita fallita con {sl_type} per {symbol}")
                        notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} durante {sl_type}!")
                else:
                    log(f"❌ Quantità nulla o troppo piccola per vendita {sl_type} su {symbol}")
        time.sleep(15)

trailing_thread = threading.Thread(target=trailing_stop_worker, daemon=True)
trailing_thread.start()

try:
    while True:
        # Aggiorna la lista asset dinamicamente ogni ciclo
        update_assets()
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

        # --- Avviso saldo basso: invia solo una volta finché non torna sopra soglia ---
        # low_balance_alerted ora è globale rispetto al ciclo

        for symbol in ASSETS:
            if symbol in STABLECOIN_BLACKLIST:
                continue
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

                # PATCH: blocca acquisti se saldo USDT troppo basso
                if usdt_balance < ORDER_USDT:
                    # Notifica solo se il saldo USDT è davvero basso
                    log(f"💸 Saldo USDT ({usdt_balance:.2f}) o budget gruppo ({group_available:.2f}) insufficiente per {symbol}")
                    if not low_balance_alerted:
                        # notify_telegram(f"❗️ Saldo USDT troppo basso per nuovi acquisti. Ricarica il wallet per continuare a operare.")
                        low_balance_alerted = True
                    continue
                else:
                    low_balance_alerted = False

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

                # --- Adatta la size ordine in base alla volatilità (ATR/Prezzo) ---
                df_hist = fetch_history(symbol)
                if df_hist is not None and "atr" in df_hist.columns and "Close" in df_hist.columns:
                    last_hist = df_hist.iloc[-1]
                    atr_val = last_hist["atr"]
                    last_price = last_hist["Close"]
                    atr_ratio = atr_val / last_price if last_price > 0 else 0
                    # Se la volatilità è molto alta, riduci la size ordine
                    if atr_ratio > 0.08:
                        strength *= 0.5
                        log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo molto alto ({atr_ratio:.2%}), size ordine dimezzata.")
                    elif atr_ratio > 0.04:
                        strength *= 0.75
                        log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo elevato ({atr_ratio:.2%}), size ordine ridotta del 25%.")

                max_invest = min(group_available, usdt_balance) * strength
                order_amount = min(max_invest, group_available, usdt_balance, 250)
                usdt_balance = get_usdt_balance()  # <-- aggiorna saldo reale subito prima dell'acquisto
                order_amount = min(order_amount, usdt_balance)
                log(f"[DEBUG] Saldo USDT: {usdt_balance:.6f}")
                log(f"[DEBUG] Valore ordine previsto: {order_amount:.6f}")
                min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
                if order_amount < min_order_amt:
                    log(f"❌ Saldo troppo basso per acquisto di {symbol}: {order_amount:.2f} < min_order_amt {min_order_amt}")
                    if not low_balance_alerted:
                        # notify_telegram(f"❗️ Saldo USDT troppo basso per nuovi acquisti. Ricarica il wallet per continuare a operare.")
                        low_balance_alerted = True
                    continue
                else:
                    low_balance_alerted = False
                log(f"[FORZA] {symbol} - Strategia: {strategy}, Strength: {strength}, Investo: {order_amount:.2f} USDT (Saldo: {usdt_balance:.2f})")

                # BLOCCO: non tentare acquisto se order_amount < min_order_amt
                min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
                if order_amount < min_order_amt:
                    log(f"❌ Saldo troppo basso per acquisto di {symbol}: {order_amount:.2f} < min_order_amt {min_order_amt}")
                    if not low_balance_alerted:
                        # notify_telegram(f"❗️ Saldo USDT troppo basso per nuovi acquisti. Ricarica il wallet per continuare a operare.")
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

                # Scegli la funzione di acquisto in base al prezzo della coin
                last_price = get_last_price(symbol)
                if last_price is None:
                    log(f"❌ Prezzo non disponibile per {symbol} (acquisto)")
                    continue
                if last_price < 0.01:
                    log(f"[SKIP] {symbol}: prezzo troppo basso ({last_price}), salto acquisto.")
                    continue
                log(f"[DEBUG] Saldo USDT prima di acquistare {symbol}: {get_usdt_balance()}")
                
                if last_price < 100:
                    # Coin normali: usa MARKET BUY
                    qty = market_buy(symbol, order_amount)
                    if not qty or qty == 0:
                        log(f"❌ Nessuna quantità acquistata per {symbol} dopo MARKET BUY. Non registro la posizione.")
                        continue
                    actual_cost = qty * last_price
                    log(f"🟢 Ordine MARKET piazzato per {symbol}. Attendi esecuzione. Investito effettivo: {actual_cost:.2f} USDT")
                else:
                    # Coin grandi: usa LIMIT BUY
                    resp = limit_buy(symbol, order_amount)
                    if resp is None:
                        log(f"❌ Acquisto LIMIT fallito per {symbol}")
                        continue
                    log(f"🟢 Ordine LIMIT piazzato per {symbol}. Attendi esecuzione.")
                    time.sleep(2)
                    qty = get_free_qty(symbol)
                    if not qty or qty == 0:
                        log(f"❌ Nessuna quantità acquistata per {symbol} dopo LIMIT BUY. Non registro la posizione.")
                        continue
                    actual_cost = qty * last_price

                df = fetch_history(symbol)
                if df is None or "Close" not in df.columns:
                    log(f"❌ Dati storici mancanti per {symbol}")
                    continue

                atr = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
                last = df.iloc[-1]
                atr_val = last["atr"] if "atr" in last else atr.iloc[-1]

                # --- Adatta la distanza di SL/TP in base alla volatilità (ATR/Prezzo) e limiti consigliati ---
                atr_ratio = atr_val / price if price > 0 else 0
                tp_factor = min(TP_MAX, max(TP_MIN, TP_FACTOR + atr_ratio * 5))
                sl_factor = min(SL_MAX, max(SL_MIN, SL_FACTOR + atr_ratio * 3))
                tp = price + (atr_val * tp_factor)
                sl = price - (atr_val * sl_factor)

                # PATCH 1: SL deve essere almeno 1.5% sotto il prezzo di ingresso
                min_sl = price * 0.985  # 1.5% sotto
                if sl > min_sl:
                    log(f"[SL PATCH] SL troppo vicino al prezzo di ingresso ({sl:.4f} > {min_sl:.4f}), imposto SL a {min_sl:.4f}")
                    sl = min_sl

                log(f"[VOLATILITÀ] {symbol}: ATR/Prezzo={atr_ratio:.2%}, TPx={tp_factor:.2f}, SLx={sl_factor:.2f}")
                # PATCH 3: Log dettagliato su entry, SL e prezzo corrente subito dopo ogni acquisto
                log(f"[ENTRY-DETAIL] {symbol} | Entry: {price:.4f} | SL: {sl:.4f} | TP: {tp:.4f} | ATR: {atr_val:.4f}")

                # Take profit parziale: 40% posizione a 1.5x ATR, resto trailing
                partial_tp_ratio = 0.4
                qty_partial = qty * partial_tp_ratio
                qty_residual = qty - qty_partial
                tp_partial = price + (atr_val * 1.5)
                position_data[symbol] = {
                    "entry_price": price,
                    "tp": tp,
                    "sl": sl,
                    "entry_cost": actual_cost,
                    "qty": qty,
                    "qty_partial": qty_partial,
                    "qty_residual": qty_residual,
                    "tp_partial": tp_partial,
                    "entry_time": time.time(),
                    "trailing_active": False,
                    "p_max": price,
                    "trailing_tp_active": False,
                    "tp_max": price
                }

                open_positions.add(symbol)
                log(f"🟢 Acquisto registrato per {symbol} | Entry: {price:.4f} | TP: {tp:.4f} | SL: {sl:.4f} | TP parziale su {qty_partial:.4f} a {tp_partial:.4f}")
                # Notifica con importo effettivo investito per market_buy, altrimenti usa order_amount
                if last_price < 100:
                    notify_telegram(f"🟢📈 Acquisto per {symbol}\nPrezzo: {price:.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f} USDT\nSL: {sl:.4f}\nTP: {tp:.4f}")
                else:
                    notify_telegram(f"🟢📈 Acquisto per {symbol}\nPrezzo: {price:.4f}\nStrategia: {strategy}\nInvestito: {order_amount:.2f} USDT\nSL: {sl:.4f}\nTP: {tp:.4f}")
                time.sleep(3)

        # PATCH: rimuovi posizioni con saldo < 1 (polvere) anche nel ciclo principale
        for symbol in list(open_positions):
            saldo = get_free_qty(symbol)
            prezzo = get_last_price(symbol)
            valore_usd = saldo * prezzo if saldo and prezzo else 0
            min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
            if saldo is None or valore_usd < min_order_amt:
                log(f"[CLEANUP] {symbol}: valore troppo basso ({valore_usd:.2f} USD), rimuovo da open_positions e position_data (polvere)")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue

            entry = position_data.get(symbol, {})
            holding_seconds = time.time() - entry.get("entry_time", 0)
            if holding_seconds < MIN_HOLDING_MINUTES * 60:
                log(f"[HOLDING][EXIT] {symbol}: attendo ancora {MIN_HOLDING_MINUTES - holding_seconds/60:.1f} min prima di poter vendere")
                continue

            # 🔴 USCITA (EXIT)
            # Ricalcola il segnale per la coin corrente!
            signal_cleanup, strategy_cleanup, price_cleanup = analyze_asset(symbol)
            if signal_cleanup == "exit" and symbol in open_positions:
                entry_price = entry.get("entry_price", price_cleanup)
                current_price = get_last_price(symbol)
                trailing_active = entry.get("trailing_active", False)
                tp = entry.get("tp", None)
                # PATCH: blocca ogni vendita se il prezzo è sopra l'entry e non c'è trailing attivo
                if current_price and current_price > entry_price and not trailing_active:
                    log(f"[SKIP][EXIT] {symbol}: prezzo attuale {current_price:.8f} sopra entry {entry_price:.8f}, nessun trailing attivo, NON vendo.")
                    continue
                entry_cost = entry.get("entry_cost", ORDER_USDT)
                qty = entry.get("qty", get_free_qty(symbol))
                usdt_before = get_usdt_balance()
                log(f"[SELL ATTEMPT] {symbol} | qty={qty} | entry_cost={entry_cost} | usdt_before={usdt_before}")
                resp = market_sell(symbol, qty)
                if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                    price = get_last_price(symbol)
                    price = round(price, 6)
                    exit_value = price * qty
                    delta = exit_value - entry_cost
                    pnl = (delta / entry_cost) * 100
                    # PATCH: notifica take profit se prezzo >= TP
                    if tp and price >= tp:
                        notify_telegram(f"🟢🎯 TP raggiunto per {symbol} a {price:.4f}\nPnL: {pnl:.2f}%")
                    else:
                        notify_telegram(f"🔴📉 Vendita per {symbol} a {price:.4f}\nStrategia: {strategy_cleanup}\nPnL: {pnl:.2f}%")
                    log_trade_to_google(symbol, entry_price, price, pnl, strategy_cleanup, "Exit Signal", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                    open_positions.discard(symbol)
                    last_exit_time[symbol] = time.time()
                    position_data.pop(symbol, None)
                    continue
                else:
                    saldo_attuale = get_free_qty(symbol)
                    # PATCH: logga dettagli anche in caso di errore
                    log(f"❌ Vendita fallita per {symbol} | qty={qty} | entry_price={entry_price} | current_price={current_price} | trailing_active={trailing_active}")
                    if resp is not None:
                        try:
                            log(f"[BYBIT SELL ERROR] status={resp.status_code} resp={resp.json()}")
                        except Exception:
                            log(f"[BYBIT SELL ERROR] status={resp.status_code} resp=??")
                    notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} durante EXIT SIGNAL! (saldo attuale: {saldo_attuale})")

        time.sleep(1)

        # 🔁 Controllo Trailing Stop e Stop Loss statico per le posizioni aperte
        for symbol in list(open_positions):
            if symbol not in position_data:
                continue
            # CONTROLLO SICUREZZA: se il saldo effettivo è zero, rimuovi la posizione
            saldo = get_free_qty(symbol)
            prezzo = get_last_price(symbol)
            valore_usd = saldo * prezzo if saldo and prezzo else 0
            min_order_amt = get_instrument_info(symbol).get("min_order_amt", 5)
            if saldo is None or valore_usd < min_order_amt:
                log(f"[CLEANUP] {symbol}: valore troppo basso ({valore_usd:.2f} USD), rimuovo da open_positions e position_data")
                open_positions.discard(symbol)
                position_data.pop(symbol, None)
                continue
            entry = position_data[symbol]
            # Calcola da quanto tempo la posizione è aperta
            holding_seconds = time.time() - entry.get("entry_time", 0)
            if holding_seconds < MIN_HOLDING_MINUTES * 60:
                log(f"[HOLDING][TRAILING/SL] {symbol}: attendo ancora {MIN_HOLDING_MINUTES - holding_seconds/60:.1f} min prima di attivare SL/TSL")
                continue
            current_price = get_last_price(symbol)
            if not current_price:
                continue
            # Soglia trailing dinamica: 0.02 per asset volatili, 0.005 per asset stabili
            if symbol in VOLATILE_ASSETS:
                trailing_threshold = 0.015  # <--- MODIFICATO da 0.02 a 0.015
            else:
                trailing_threshold = 0.004  # <--- MODIFICATO da 0.005 a 0.004
            soglia_attivazione = entry["entry_price"] * (1 + trailing_threshold)
            log(f"[TRAILING CHECK] {symbol} | entry_price={entry['entry_price']:.4f} | current_price={current_price:.4f} | soglia={soglia_attivazione:.4f} | trailing_active={entry['trailing_active']} | threshold={trailing_threshold}")
            # 🧪 Attiva Trailing se supera la soglia
            if not entry["trailing_active"] and current_price >= soglia_attivazione:
                entry["trailing_active"] = True
                log(f"🔛 Trailing SL attivato per {symbol} sopra soglia → Prezzo: {current_price:.4f}")
                notify_telegram(f"🔛🔻Trailing SL attivato su {symbol}\nPrezzo: {current_price:.4f}")
            # ⬆️ Aggiorna massimo e SL se prezzo cresce
            if entry["trailing_active"]:
                if current_price > entry["p_max"]:
                    entry["p_max"] = current_price
                    new_sl = current_price * (1 - TRAILING_SL_BUFFER)
                    if new_sl > entry["sl"]:
                        log(f"📉 SL aggiornato per {symbol}: da {entry['sl']:.4f} a {new_sl:.4f}")
                        entry["sl"] = new_sl
            # PATCH TRAILING TP: attiva trailing TP se supera TP
            if not entry.get("trailing_tp_active", False) and current_price >= entry["tp"]:
                entry["trailing_tp_active"] = True
                entry["tp_max"] = current_price
                log(f"🔛 Trailing TP attivato per {symbol} sopra TP → Prezzo: {current_price:.4f}")
                notify_telegram(f"🔛🔺Trailing TP attivato su {symbol}\nPrezzo: {current_price:.4f}")
            
            if entry.get("trailing_tp_active", False):
                if current_price > entry["tp_max"]:
                    entry["tp_max"] = current_price
                    log(f"⬆️ TP massimo aggiornato per {symbol}: {entry['tp_max']:.4f}")
                tp_trailing_buffer = 0.015  # 1.5% sotto il massimo raggiunto
                trailing_tp_price = entry["tp_max"] * (1 - tp_trailing_buffer)
                if current_price <= trailing_tp_price:
                    log(f"🔺Trailing TP venduto per {symbol} → Prezzo: {current_price:.4f} | TP trailing: {trailing_tp_price:.4f}")
                    notify_telegram(f"🎯🔺Trailing TP venduto per {symbol} a {current_price:.4f}")
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
                            log(f"🔺Trailing TP venduto per {symbol} → Prezzo: {current_price:.4f} | TP trailing: {trailing_tp_price:.4f}")
                            notify_telegram(f"🎯🔺Trailing TP venduto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                            log_trade_to_google(symbol, entry_price, current_price, pnl, "Trailing TP", "TP Triggered", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                            open_positions.discard(symbol)
                            last_exit_time[symbol] = time.time()
                            position_data.pop(symbol, None)
                            continue
                        else:
                            log(f"❌ Vendita fallita con Trailing TP per {symbol}")
                            notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} durante Trailing TP!")
                    else:
                        log(f"❌ Quantità nulla o troppo piccola per vendita Trailing TP su {symbol}")
            # --- CHIUSURA AUTOMATICA: Trailing Stop o Stop Loss statico ---
            sl_triggered = False
            sl_type = None
            # Trailing SL
            if entry["trailing_active"] and current_price <= entry["sl"]:
                sl_triggered = True
                sl_type = "Trailing SL"
            # Stop Loss statico
            elif not entry["trailing_active"] and current_price <= entry["sl"]:
                sl_triggered = True
                sl_type = "SL"
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
                        notify_telegram(f"🎯🔻{sl_type} venduto per {symbol} a {current_price:.4f}\nPnL: {pnl:.2f}%")
                        log_trade_to_google(symbol, entry_price, current_price, pnl, sl_type, "SL Triggered", usdt_enter=entry_cost, usdt_exit=exit_value, delta_usd=delta)
                        # 🗑️ Pulizia
                        open_positions.discard(symbol)
                        last_exit_time[symbol] = time.time()
                        position_data.pop(symbol, None)
                        continue
                    else:
                        log(f"❌ Vendita fallita con {sl_type} per {symbol}")
                        notify_telegram(f"❌❗️ VENDITA NON RIUSCITA per {symbol} durante {sl_type}!")
                else:
                    log(f"❌ Quantità nulla o troppo piccola per vendita {sl_type} su {symbol}")
        # Sicurezza: attesa tra i cicli principali
        time.sleep(INTERVAL_MINUTES * 60)
except Exception as e:
    log(f"[FATAL ERROR] {e}")