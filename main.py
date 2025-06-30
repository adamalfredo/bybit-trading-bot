import os
import time
import hmac
import json
import hashlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
import yfinance as yf
import pandas as pd
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
BYBIT_ACCOUNT_TYPE = os.getenv("BYBIT_ACCOUNT_TYPE", "UNIFIED").upper()

MIN_ORDER_USDT = 50.0
ORDER_USDT = max(MIN_ORDER_USDT, float(os.getenv("ORDER_USDT", str(MIN_ORDER_USDT))))

ASSET_LIST = ["BTC-USD", "ETH-USD", "SOL-USD", "AVAX-USD", "LINK-USD", "DOGE-USD"]
INTERVAL_MINUTES = 15
DOWNLOAD_RETRIES = 3
# Cache delle informazioni sugli strumenti Bybit
INSTRUMENT_CACHE = {}

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


def _parse_precision(value, default=6):
    """Restituisce il numero di decimali partendo da un valore."""
    if value is None:
        return default
    s = str(value).strip()
    if s.isdigit():
        return int(s)
    try:
        dec = Decimal(s)
    except Exception:
        return default
    if dec == dec.to_integral():
        return 0
    s = format(dec.normalize(), "f")
    if "." in s:
        return len(s.split(".")[1].rstrip("0"))
    return default


def get_instrument_info(symbol: str):
    """Restituisce info dello strumento, inclusi minimi di ordine e step."""
    if symbol in INSTRUMENT_CACHE:
        return INSTRUMENT_CACHE[symbol]

    headers = {"User-Agent": "Mozilla/5.0"}

    # Endpoint principale (v5)
    url = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
    params = {"category": "spot", "symbol": symbol}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        if data.get("retCode") == 0 and data.get("result", {}).get("list"):
            info = data["result"]["list"][0]
            lot = info.get("lotSizeFilter", {})
            min_qty = float(lot.get("minOrderQty", 0))
            min_amt = float(lot.get("minOrderAmt", 0))
            qty_step = float(lot.get("qtyStep", 0))
            base_prec = _parse_precision(lot.get("basePrecision"), 6)
            if qty_step == 0:
                if base_prec:
                    qty_step = 10 ** -base_prec
                else:
                    qty_step = 10 ** -_parse_precision(min_qty)
            precision = max(_parse_precision(qty_step), base_prec)
            INSTRUMENT_CACHE[symbol] = (min_qty, min_amt, qty_step, precision)
            log(
                f"{symbol}: minOrderQty={min_qty}, minOrderAmt={min_amt}, qtyStep={qty_step}"
            )
            return INSTRUMENT_CACHE[symbol]
    except Exception as e:
        log(f"Errore info strumento {symbol} (v5): {e}")

    # Fallback per le vecchie API spot
    url = f"{BYBIT_BASE_URL}/spot/v3/public/symbols"
    try:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        symbols = data.get("result", {}).get("list", [])
        for item in symbols:
            if item.get("name") == symbol:
                min_qty = float(item.get("minTradeQty", 0))
                min_amt = float(item.get("minTradeAmount", item.get("minTradeAmt", 0)))
                qty_step = float(item.get("qtyStep", item.get("lotSize", 0)))
                base_prec = _parse_precision(item.get("basePrecision"), 6)
                if qty_step == 0:
                    if base_prec:
                        qty_step = 10 ** -base_prec
                    else:
                        qty_step = 10 ** -_parse_precision(min_qty)
                precision = max(_parse_precision(qty_step), base_prec)
                INSTRUMENT_CACHE[symbol] = (min_qty, min_amt, qty_step, precision)
                log(
                    f"{symbol}: minOrderQty={min_qty}, minOrderAmt={min_amt}, qtyStep={qty_step}"
                )
                return INSTRUMENT_CACHE[symbol]
    except Exception as e:
        log(f"Errore info strumento {symbol} (fallback): {e}")

    return 0.0, 0.0, 0.0, 6

def _round_up_step(value: float, step: float) -> float:
    step_dec = Decimal(str(step))
    val_dec = Decimal(str(value))
    return float((val_dec / step_dec).to_integral_value(rounding=ROUND_UP) * step_dec)

def calculate_quantity(
    symbol: str, usdt: float, price: float
) -> tuple[float, float, int]:
    """Calcola la quantità e l'USDT realmente utilizzato."""
    min_qty, min_amt, qty_step, precision = get_instrument_info(symbol)

    if min_qty == 0 and min_amt == 0:
        log(f"Limiti Bybit assenti per {symbol}: operazione ignorata")
        return 0.0, 0.0, precision

    if price <= 0:
        return 0.0, 0.0, precision

    target_qty = max(usdt / price, min_qty, min_amt / price)

    if qty_step:
        qty = _round_up_step(target_qty, qty_step)
    else:
        step = Decimal('1').scaleb(-precision)
        qty = float(
            Decimal(str(target_qty)).quantize(step, rounding=ROUND_UP)
        )

    actual_usdt = qty * price
    return qty, actual_usdt, precision

def _format_quantity(quantity: float, precision: int) -> str:
    """Restituisce la quantità con la precisione corretta."""
    q = Decimal(str(quantity)).quantize(
        Decimal(1) if precision == 0 else Decimal('1').scaleb(-precision),
        rounding=ROUND_DOWN,
    )
    if precision == 0:
        return str(int(q))
    return format(q, f'.{precision}f').rstrip('0').rstrip('.')


def send_order(symbol: str, side: str, quantity: float, precision: int) -> None:
    """Invia un ordine di mercato su Bybit."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        log("Chiavi Bybit mancanti: ordine non inviato")
        return

    if quantity <= 0:
        log(f"Quantit\u00e0 non valida per l'ordine {symbol}")
        return
    endpoint = f"{BYBIT_BASE_URL}/v5/order/create"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    qty_str = _format_quantity(quantity, precision)
    body = {
        "category": "spot",
        "symbol": symbol,
        "side": side,
        "orderType": "MARKET",
        "qty": qty_str,
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
            code = data.get("retCode")
            if code == 170140:
                min_qty, min_amt, qty_step, _ = get_instrument_info(symbol)
                msg = (
                    f"Ordine troppo piccolo per {symbol}. "
                    f"minQty={min_qty}, minAmt={min_amt}, qtyStep={qty_step}. "
                    "Aumenta ORDER_USDT."
                )
            elif code == 170131:
                msg = f"Saldo insufficiente per {symbol}."
            elif code == 170137:
                msg = f"Decimali eccessivi per {symbol}."
            else:
                msg = f"Errore ordine {symbol}: {data}"
            log(msg)
            notify_telegram(msg)
        else:
            msg = f"✅ Ordine {side} {symbol} inviato ({qty_str})"
            log(msg)
            notify_telegram(msg)
    except Exception as e:
        msg = f"Errore invio ordine {symbol}: {e}"
        log(msg)
        notify_telegram(msg)

def get_balance(coin: str) -> float:
    """Restituisce il saldo disponibile per la coin indicata."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        return 0.0

    endpoint = f"{BYBIT_BASE_URL}/v5/account/wallet-balance"
    timestamp = str(int(time.time() * 1000))
    recv_window = "5000"
    params = {"accountType": BYBIT_ACCOUNT_TYPE, "coin": coin}
    param_str = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    signature_payload = f"{timestamp}{BYBIT_API_KEY}{recv_window}{param_str}"
    sign = _sign(signature_payload)
    headers = {
        "X-BAPI-API-KEY": BYBIT_API_KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN-TYPE": "2",
    }
    try:
        resp = requests.get(
            f"{endpoint}?{param_str}", headers=headers, timeout=10
        )
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
            log(
                f"Coin {coin} non trovata nella risposta saldo: "
                f"{data.get('result')!r}"
            )
        else:
            log(f"Errore saldo {coin}: {data}")
    except Exception as e:
        log(f"Errore ottenimento saldo {coin}: {e}")
    return 0.0


def round_quantity(symbol: str, quantity: float, price: float) -> tuple[float, int]:
    """Arrotonda la quantità secondo lo step e verifica i minimi di Bybit."""
    min_qty, min_amt, qty_step, precision = get_instrument_info(symbol)

    if min_qty == 0 and min_amt == 0:
        log(f"Limiti Bybit assenti per {symbol}: operazione ignorata")
        return 0.0, precision

    if qty_step:
        step = Decimal(str(qty_step))
        quantity = (
            Decimal(str(quantity)) / step
        ).to_integral_value(rounding=ROUND_DOWN) * step
    else:
        quantity = Decimal(str(quantity)).quantize(
            Decimal(1) if precision == 0 else Decimal("1").scaleb(-precision),
            rounding=ROUND_DOWN,
        )

    qty_f = float(quantity)
    if qty_f < min_qty or qty_f * price < min_amt:
        return 0.0, precision

    return qty_f, precision

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
            msg = "✅ Connessione a Bybit riuscita"
            log(msg)
            notify_telegram(msg)
        else:
            msg = f"Test Bybit fallito: {data}"
            log(msg)
            notify_telegram(msg)
    except Exception as e:
        msg = f"Errore connessione Bybit: {e}"
        log(msg)
        notify_telegram(msg)

def get_last_price(symbol: str) -> Optional[float]:
    """Recupera l'ultimo prezzo disponibile da Bybit."""
    url = f"{BYBIT_BASE_URL}/v5/market/tickers"
    params = {"category": "spot", "symbol": symbol}
    try:
        resp = requests.get(url, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            lst = data.get("result", {}).get("list")
            if lst:
                price = lst[0].get("lastPrice")
                if price is not None:
                    return float(price)
    except Exception as e:
        log(f"Errore prezzo {symbol}: {e}")
    return None

def initial_btc_purchase() -> None:
    """Esegue un acquisto iniziale di BTC se possibile."""
    if not BYBIT_API_KEY or not BYBIT_API_SECRET:
        return
    price = get_last_price("BTCUSDT")
    if price is None:
        log("Prezzo BTC non disponibile: acquisto iniziale saltato")
        return
    qty, used_usdt, prec = calculate_quantity("BTCUSDT", ORDER_USDT, price)
    if qty <= 0:
        log("Quantit\u00e0 calcolata nulla per BTC")
        return
    usdt_balance = get_balance("USDT")
    if usdt_balance < used_usdt:
        log("Saldo USDT insufficiente per acquisto iniziale BTC")
        return
    log(f"Acquisto iniziale BTC: {used_usdt:.2f} USDT al prezzo {price}")
    send_order("BTCUSDT", "Buy", qty, prec)

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
            msg = f"""OK - {tipo}
Asset: {result['symbol']}
Prezzo: {result['price']}
Strategia: {sig['strategy']}"""
            log(msg.replace("\n", " | "))
            notify_telegram(msg)

            if sig["type"] == "entry":
                qty, used_usdt, prec = calculate_quantity(
                    result["symbol"], ORDER_USDT, result["price"]
                )
                if qty <= 0:
                    warn = f"Quantit\u00e0 calcolata nulla per {result['symbol']}"
                    log(warn)
                    notify_telegram(warn)
                    continue
                usdt_balance = get_balance("USDT")
                if usdt_balance < used_usdt:
                    warn = f"Saldo USDT insufficiente per acquistare {result['symbol']}"
                    log(warn)
                    notify_telegram(warn)
                    continue
                log(
                    f"Invio ordine da {used_usdt:.2f} USDT su {result['symbol']}"
                )
                send_order(result["symbol"], "Buy", qty, prec)
            else:
                coin = result["symbol"].replace("USDT", "")
                bal = get_balance(coin)
                if bal <= 0:
                    warn = f"Saldo {coin} insufficiente: nessuna vendita"
                    log(warn)
                    notify_telegram(warn)
                    continue
                qty, prec = round_quantity(result["symbol"], bal, result["price"])
                if qty <= 0:
                    warn = (
                        f"Saldo {coin} insufficiente per ordine minimo: nessuna vendita"
                    )
                    log(warn)
                    notify_telegram(warn)
                    continue
                log(
                    f"Vendo tutto {coin}: {qty} (~{qty * result['price']:.2f} USDT)"
                )
                send_order(result["symbol"], "Sell", qty, prec)
if __name__ == "__main__":
    log("🔄 Avvio sistema di monitoraggio segnali reali")
    # Testa la connessione alle API e poi esegue un acquisto iniziale di BTC
    test_bybit_connection()
    initial_btc_purchase()
    notify_telegram("🔔 Test: bot avviato correttamente")
    while True:
        try:
            scan_assets()
        except Exception as e:
            log(f"Errore nel ciclo principale: {e}")
        time.sleep(INTERVAL_MINUTES * 60)
