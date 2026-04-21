# STRATEGIA MIGLIORATA NEL TIMEFRAME 5/3/2026 AL 20/3/2026
from typing import Optional
import os
import time
import hmac
import json
import hashlib
from decimal import Decimal, ROUND_DOWN, ROUND_UP
import requests
import pandas as pd
from ta.volatility import BollingerBands, AverageTrueRange
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator, MACD, ADXIndicator, SMAIndicator
import threading
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# Indici posizione Bybit
LONG_IDX = 1
SHORT_IDX = 2

# --- Sizing per trade (notional) ---
DEFAULT_LEVERAGE = 10          # leva usata sul conto (Cross/Isolated)
MARGIN_USE_PCT = 0.35
TARGET_NOTIONAL_PER_TRADE = 200.0

INTERVAL_MINUTES = 60  # era 15
ATR_WINDOW = 14
TP_FACTOR = 2.5
SL_FACTOR = 1.2
# Soglie dinamiche consigliate
TP_MIN = 2.0
TP_MAX = 3.0
SL_MIN = 1.0
SL_MAX = 2.0
TRAILING_MIN = 0.02   # trailing più conservativo
TRAILING_MAX = 0.08   # trailing più conservativo
INITIAL_STOP_LOSS_PCT = 0.03          # era 0.02, SL iniziale più largo
COOLDOWN_MINUTES = 60
MAX_OPEN_POSITIONS = 2         # massimo posizioni simultanee (ridotto: esposizione correlata in rally)
FUNDING_SHORT_MIN = -0.0005    # blocca nuovi SHORT se funding < -0.05% (shorts sovraccaricati = pressione rialzista)
# Nuovi parametri protezione guadagni (SHORT)
TRIGGER_BY = "LastPrice"

# Persistenza stato ratchet tra deploy
STATE_FILE = "/tmp/position_state_short.json"

def save_positions_state():
    """Salva mfe_roi, floor_roi e cooldown state su file per sopravvivere ai restart."""
    try:
        state = {}
        for symbol in list(open_positions):
            entry = position_data.get(symbol)
            if entry:
                state[symbol] = {
                    "mfe_roi": entry.get("mfe_roi", 0.0),
                    "floor_roi": entry.get("floor_roi"),
                    "entry_price": entry.get("entry_price"),
                    "floor_updated_ts": entry.get("floor_updated_ts", 0),
                }
        # Persisti cooldown post-loss: sopravvive ai restart di Railway
        cooldown_state = {
            sym: {"last_exit_time": last_exit_time[sym], "recent_losses": recent_losses.get(sym, 0)}
            for sym in last_exit_time
        }
        state["__cooldown__"] = cooldown_state
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as _e:
        pass  # non bloccare il bot per un errore di salvataggio

def load_positions_state() -> dict:
    """Carica lo stato ratchet salvato, se esiste."""
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r") as f:
                return json.load(f)
    except Exception:
        pass
    return {}

RATCHET_TIERS_ROI = [
    (15, 7),    # FIX: era (30,15) → soglie realistiche a leva 10x
    (25, 15),
    (40, 25),
    (60, 40),
    (80, 60),
    (100, 80),  # trade eccezionale: se +100% proteggi +80%
    (125, 100), # trade eccezionale: se +125% proteggi +100%
    (150, 120), # moonshot: se +150% proteggi +120%
]
FLOOR_BUFFER_PCT = 0.0015          # 0.15% di prezzo per sicurezza esecuzione
FLOOR_UPDATE_COOLDOWN_SEC = 45     # cooldown più lungo per evitare rumore
FLOOR_TRIGGER_BY = "MarkPrice"     # usa Mark per coerenza con SL

# >>> PATCH: parametri breakeven lock (SHORT)
BREAKEVEN_LOCK_PCT = 0.025     # FIX2: era 0.015, attiva BE al -2.5% di prezzo (più respiro prima del lock)
BREAKEVEN_BUFFER   = -0.012   # FIX2: era -0.006, buffer più largo per evitare noise-stop su BE
MAX_LOSS_CAP_PCT        = 0.15  # hard cap emergenza: SL primario è ATR-based; questo scatta solo se ATR > 15% (volatile)
MAX_LOSS_CAP_PCT_STABLE = 0.10  # hard cap emergenza: SL primario è ATR-based; questo scatta solo se ATR > 10% (stabile: AAVE, LINK)

# >>> regime semplificato: contesto BTC 4h + drawdown giornaliero
DAILY_DD_CAP_PCT = 0.04         # blocca nuovi ingressi se equity < -4% dal livello di inizio giorno
WEEKLY_DD_CAP_PCT = float(os.getenv("WEEKLY_DD_CAP_PCT", "0.08"))  # Gap3: blocca nuovi ingressi se equity < -8% rispetto a 7gg fa
_btc_favorable_short = False    # True se BTC 4h in downtrend → contesto favorevole per SHORT
_btc_favorable_short_prev = False  # Gap1: stato precedente per rilevare transizione True→False
_btc_uptrend_short = False      # True se BTC 4h in uptrend → blocco totale nuove aperture SHORT
_btc_daily_down_short = False   # True se BTC daily in downtrend (EMA200 descend + price < ema200)
_btc_pumping_short = False      # True se BTC 15m sale > +1.5% in 30 min (pump guard)
_btc_ctx_ts = 0                 # timestamp ultimo aggiornamento contesto BTC
_btc_4h_chg_short: float = 0.0  # Imp1/Imp2: variazione BTC su 4h per RS relativa e pesi adattivi
_daily_start_equity = None
_weekly_start_equity: float | None = None   # Gap3: equity snapshot di 7gg fa
_weekly_anchor_ts: float = 0.0              # Gap3: timestamp ultimo aggiornamento settimanale
_weekly_protection_active: bool = False     # Gap3: True = nessun nuovo ingresso SHORT
_trading_paused_until = 0
# Report giornaliero
_daily_trades_opened: int = 0
_daily_trades_closed: int = 0
_daily_pnl_sum: float = 0.0   # somma PnL % netti del giorno
_last_report_day: str = ""     # "YYYY-MM-DD" dell'ultimo report inviato
# BEGIN PATCH: throttle DD (no pausa forzata di default)
ENABLE_DD_PAUSE = os.getenv("ENABLE_DD_PAUSE", "0") == "1"
DD_PAUSE_MINUTES = int(os.getenv("DD_PAUSE_MINUTES", "120"))
RISK_THROTTLE_LEVEL = 0  # 0=off, 1=DD > cap, 2=DD > 2*cap
ORDER_USDT = 50.0
ENABLE_BREAKOUT_FILTER = False  # FIX2: disabilitato - il breakdown obbligatorio causa late-entry dopo il minimo
# --- ASSET DINAMICI: aggiorna la lista dei migliori asset spot per volume 24h ---
ASSETS = []
LESS_VOLATILE_ASSETS = []
VOLATILE_ASSETS = []
# --- SYNC POSIZIONI APERTE DA WALLET ALL'AVVIO ---
open_positions = set()
position_data = {}
last_exit_time = {}
last_exit_was_loss = {}  # True se l'ultima uscita su quel simbolo era una perdita
recent_losses = {}          # conteggio loss consecutivi per simbolo
MAX_CONSEC_LOSSES = 2       # dopo 2 loss consecutivi blocca nuovi ingressi
FORCED_WAIT_MIN = 90        # attesa minima (minuti) se il contesto resta sfavorevole
# ---- Logging flags (accensione selettiva via env) ----
LOG_DEBUG_ASSETS     = os.getenv("LOG_DEBUG_ASSETS", "0") == "1"
LOG_DEBUG_DECIMALS   = os.getenv("LOG_DEBUG_DECIMALS", "0") == "1"
LOG_DEBUG_SYNC       = os.getenv("LOG_DEBUG_SYNC", "0") == "1"
LOG_DEBUG_STRATEGY   = os.getenv("LOG_DEBUG_STRATEGY", "0") == "1"
LOG_DEBUG_PORTFOLIO  = os.getenv("LOG_DEBUG_PORTFOLIO", "0") == "1"
# --- Loosening via env ---
MIN_CONFLUENCE = 2   # FIX: era 1, richiede almeno 2 indicatori allineati
ENTRY_TF_VOLATILE = 60  # FIX2: allineato al loop principale (60m) per evitare segnali stantii
ENTRY_TF_STABLE = 60   # FIX2: allineato al loop principale (60m)
ENTRY_ADX_VOLATILE = 27       # fisso
ENTRY_ADX_STABLE = 25         # alzato da 24: riduce entry in trend deboli
ADX_RELAX_EVENT = 3.0
RSI_SHORT_THRESHOLD = 46.0
LIQUIDITY_MIN_VOLUME = 1_000_000
LINEAR_MIN_TURNOVER = 50_000_000  # 50M: esclude micro-cap speculativi e meme coin (era 10M, alzato dopo FART/BOME/RAVE)
# Large-cap con minQty elevata: abilita auto-bump del notional al minimo (come nel LONG)
LARGE_CAPS = {"BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT"}

# --- Nuova gestione rischio e R-multipli ---
RISK_PCT = float(os.getenv("RISK_PCT", "0.0075"))   # 0.75% equity per trade
MAX_MIN_QTY_RISK_FACTOR = 1.5  # max 1.5× il rischio atteso dopo bump min_qty (evita token come RAVEUSDT con minQty enorme)
SL_ATR_MULT = float(os.getenv("SL_ATR_MULT", "2.0"))   # FIX: era 1.4, SL più largo per ridurre noise-stop
TP1_R = float(os.getenv("TP1_R", "2.5"))             # FIX: era 1.0, R:R almeno 2.5:1
TP1_PARTIAL = float(os.getenv("TP1_PARTIAL", "0.65"))  # OPT: alzato da 0.50 a 0.65 — incassa più profitto al TP1, migliora avg win
BE_AT_R = float(os.getenv("BE_AT_R", "1.0"))
TRAIL_START_R = float(os.getenv("TRAIL_START_R", "1.0"))  # FIX: alzato da 0.5 a 1.0 — a 0.5R il BE floor era sopra il prezzo → Bybit rifiutava SL
TRAIL_ATR_MULT = float(os.getenv("TRAIL_ATR_MULT", "1.3"))

# --- Stima fee per expectancy (percentuali lato notional) ---
FEES_TAKER_PCT = float(os.getenv("FEES_TAKER_PCT", "0.0006"))  # ~0.06%
FEES_MAKER_PCT = float(os.getenv("FEES_MAKER_PCT", "0.0001"))  # ~0.01%

# --- Cassaforte in USDT (lock minimo di profitto) ---
PNL_TRIGGER_USDT = 3.2   # quando l'Unrealized >= 3.2 USDT
PNL_LOCK_USDT    = 3.0   # fissa uno SL che garantisca ≳ 3.0 USDT
PNL_LOCK_BUFFER_PCT = 0.001  # 0.1% buffer per evitare SL sopra/sotto il prezzo attuale

# Cache leggera prezzo (TTL in secondi)
LAST_PRICE_TTL_SEC = 2
_last_price_cache = {}

# Cache Open Interest per simbolo (TTL 5 min: OI orario non cambia a ogni tick)
OI_CACHE_TTL = 300
_oi_cache: dict = {}  # symbol -> (timestamp, oi_change_pct)

# Locks per strutture condivise
_state_lock = threading.RLock()
_instr_lock = threading.RLock()
_price_lock = threading.RLock()

# Helpers atomici per lo stato
def set_position(symbol: str, entry: dict) -> None:
    with _state_lock:
        position_data[symbol] = entry

def add_open(symbol: str) -> None:
    with _state_lock:
        open_positions.add(symbol)

def discard_open(symbol: str) -> None:
    with _state_lock:
        open_positions.discard(symbol)

# --- BLACKLIST STABLECOIN ---
STABLECOIN_BLACKLIST = [
    "USDCUSDT", "USDEUSDT", "TUSDUSDT", "USDPUSDT", "BUSDUSDT", "FDUSDUSDT", "DAIUSDT", "EURUSDT", "USDTUSDT"
]
EXCLUSION_LIST = [
    "FUSDT", "YBUSDT", "ZBTUSDT", "RECALLUSDT", "XPLUSDT", "BRETTUSDT", "STABLEUSDT",
    # Commodity / metalli: seguono oro/argento, non crypto → indicatori 60m inutili su questi asset
    "PAXGUSDT", "XAUTUSDT", "XAUUSDT", "XAGUSDT",
    # Blacklist performance: peggior asset per PnL storico (mese corrente)
    "BTCUSDT",    # notional enorme per ogni SL hit, risultati sistematicamente negativi
    "LABUSDT",   # -1.05 USDT storico, entrate durante crash-loop
]

def is_trending_down(symbol: str, tf: str = "240"):
    """
    Ritorna True se l'asset è in downtrend su timeframe superiore (default 4h).
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": tf,
        "limit": 220  # almeno 200 barre per EMA200
    }
    try:
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return False
        raw = list(reversed(data["result"]["list"]))  # FIX: Bybit ritorna newest-first; invertiamo per avere oldest-first
        df = pd.DataFrame(raw, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "turnover"
        ])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 200:
            return False
        ema200 = EMAIndicator(close=df["Close"], window=200).ema_indicator()
        # Downtrend se EMA200 decrescente e prezzo sotto EMA200
        return df["Close"].iloc[-1] < ema200.iloc[-1] and ema200.iloc[-1] <= ema200.iloc[-2]
    except Exception:
        return False

def is_trending_down_1h(symbol: str, tf: str = "60"):
    """
    Ritorna True se l'asset è in downtrend su timeframe 1h.
    """
    endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
    params = {
        "category": "linear",
        "symbol": symbol,
        "interval": tf,
        "limit": 120  # almeno 100 barre per EMA100
    }
    try:
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            return False
        raw = list(reversed(data["result"]["list"]))  # FIX: Bybit ritorna newest-first; invertiamo per avere oldest-first
        df = pd.DataFrame(raw, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "turnover"
        ])
        df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
        df.dropna(subset=["Close"], inplace=True)
        if len(df) < 100:
            return False
        ema100 = EMAIndicator(close=df["Close"], window=100).ema_indicator()
        # Downtrend se EMA100 decrescente e prezzo sotto EMA100
        return df["Close"].iloc[-1] < ema100.iloc[-1] and ema100.iloc[-1] <= ema100.iloc[-2]
    except Exception:
        return False

def _on_regime_bullish_short():
    """
    Gap1 — Chiamata quando btc_fav transita True→False per SHORT (bear regime finisce).
    Per ogni posizione SHORT aperta senza ratchet attivo, sposta lo SL al breakeven
    per evitare che posizioni aperte in un regime che si è girato portino perdite.
    BREAKEVEN_BUFFER è negativo per SHORT (BE = entry * (1 + BREAKEVEN_BUFFER) = leggermente sotto entry).
    """
    log("[REGIME-CHANGE][SHORT] btc_fav True→False: stringo SL delle posizioni SHORT non protette → BE")
    # Throttle: max 1 notifica ogni 30 minuti
    _rc_key = "regime_change_short_tg"
    if time.time() - _last_log_times.get(_rc_key, 0) >= 1800:
        _last_log_times[_rc_key] = time.time()
        notify_telegram("⚠️ [REGIME-CHANGE] BTC perde il downtrend: sposto SL a breakeven sulle posizioni SHORT senza protezione")
    for symbol in list(open_positions):
        try:
            entry = position_data.get(symbol)
            if not entry:
                continue
            # Già protette: be_locked o ratchet attivo (floor_roi impostato)
            if entry.get("be_locked") or entry.get("floor_roi") is not None:
                continue
            entry_price = entry.get("entry_price")
            if not entry_price:
                continue
            price_now = get_last_price(symbol)
            if not price_now:
                continue
            # Solo posizioni in profitto (SHORT: price_now < entry_price = profitto)
            if price_now >= float(entry_price):
                log(f"[REGIME-CHANGE][SHORT] {symbol} già in perdita, non modifico SL")
                continue
            be_price = float(entry_price) * (1.0 + BREAKEVEN_BUFFER)  # BREAKEVEN_BUFFER negativo
            # Guard: il conditional SL per SHORT richiede trigger > prezzo corrente.
            # Se be_price <= price_now il prezzo è già salito sopra il BE → skip.
            if be_price <= price_now:
                log(f"[REGIME-CHANGE][SHORT] {symbol} BE {be_price:.6f} <= price {price_now:.6f}: prezzo già sopra BE, skip")
                continue
            qty_live = get_open_short_qty(symbol)
            if qty_live and qty_live > 0:
                ok_csl = place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                ok_psl = set_position_stoploss_short(symbol, be_price)
                if ok_csl or ok_psl:
                    entry["be_locked"] = True
                    entry["be_price"] = be_price
                    set_position(symbol, entry)
                    log(f"[REGIME-CHANGE][SHORT] {symbol} SL→BE {be_price:.6f} (entry={entry_price:.6f})")
        except Exception as _e:
            log(f"[REGIME-CHANGE][SHORT] {symbol} errore: {_e}")


def _check_weekly_dd_short(portfolio_value: float) -> bool:
    """
    Gap3 — Verifica drawdown settimanale per SHORT.
    Aggiorna lo snapshot settimanale ogni 7 giorni (168h).
    Ritorna True se il weekly DD supera WEEKLY_DD_CAP_PCT → blocca nuovi ingressi.
    Con protezione attiva: si disattiva quando l'equity recupera il 50% della perdita.
    """
    global _weekly_start_equity, _weekly_anchor_ts, _weekly_protection_active
    now = time.time()
    # Inizializza snapshot se mai impostato
    if _weekly_start_equity is None or _weekly_anchor_ts == 0.0:
        _weekly_start_equity = portfolio_value
        _weekly_anchor_ts = now
        return False
    # Aggiorna snapshot ogni 7 giorni
    if now - _weekly_anchor_ts >= 7 * 24 * 3600:
        _weekly_start_equity = portfolio_value
        _weekly_anchor_ts = now
        _weekly_protection_active = False
        log(f"[WEEKLY-DD] Snapshot aggiornato: equity={portfolio_value:.2f} USDT")
        return False
    # Calcola drawdown settimanale
    weekly_dd = (portfolio_value - _weekly_start_equity) / max(1e-9, _weekly_start_equity)
    if weekly_dd < -WEEKLY_DD_CAP_PCT:
        if not _weekly_protection_active:
            _weekly_protection_active = True
            log(f"[WEEKLY-DD] ⛔ DD settimanale {-weekly_dd*100:.1f}% > cap {WEEKLY_DD_CAP_PCT*100:.0f}% → protezione attiva")
            notify_telegram(f"⛔ [WEEKLY-DD] Drawdown settimanale {-weekly_dd*100:.1f}% supera cap {WEEKLY_DD_CAP_PCT*100:.0f}%\nNessun nuovo SHORT fino a recupero parziale")
        return True
    # Disattiva protezione se recupera ≥50% della perdita
    if _weekly_protection_active:
        loss = _weekly_start_equity * WEEKLY_DD_CAP_PCT
        recovered = portfolio_value - (_weekly_start_equity * (1.0 - WEEKLY_DD_CAP_PCT))
        if recovered >= loss * 0.5:
            _weekly_protection_active = False
            log(f"[WEEKLY-DD] ✅ Recupero sufficiente, protezione disattivata. Equity={portfolio_value:.2f}")
            notify_telegram(f"✅ [WEEKLY-DD] Recupero raggiunto, nuovi SHORT abilitati")
    return _weekly_protection_active


def _update_btc_context_short():
    """Aggiorna il contesto BTC ogni 3 min.
    Bear confirmed: SHORT opera solo quando BTC 4h + Daily ENTRAMBI in downtrend.
    Fix #3: se BTC 24h in positivo forza btc_fav=False.
    Fix #1: pump guard 15m.
    Uptrend guard: blocco totale se 4h uptrend.
    Gap1: rileva transizione True→False e attiva regime change handler."""
    global _btc_favorable_short, _btc_favorable_short_prev, _btc_uptrend_short, _btc_daily_down_short, _btc_pumping_short, _btc_ctx_ts, _btc_4h_chg_short
    if time.time() - _btc_ctx_ts > 180:
        try:
            _btc_favorable_short = is_trending_down("BTCUSDT", "240")
            # Bear confirmed: check BTC daily downtrend
            _btc_daily_down_short = is_trending_down("BTCUSDT", "D")
            # Uptrend guard: controlla se BTC 4h è in uptrend (EMA200 crescente + prezzo sopra EMA200)
            try:
                from ta.trend import EMAIndicator as _EMA
                _ru = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/kline",
                                  params={"category": "linear", "symbol": "BTCUSDT",
                                          "interval": "240", "limit": 220}, timeout=8)
                _du = _ru.json()
                if _du.get("retCode") == 0 and _du["result"]["list"]:
                    import pandas as _pd
                    _df = _pd.DataFrame(_du["result"]["list"],
                                        columns=["ts","O","H","L","C","V","T"])
                    _df["C"] = _pd.to_numeric(_df["C"], errors="coerce")
                    _ema200 = _EMA(close=_df["C"], window=200).ema_indicator()
                    _btc_uptrend_short = (
                        float(_df["C"].iloc[-1]) > float(_ema200.iloc[-1]) and
                        float(_ema200.iloc[-1]) >= float(_ema200.iloc[-2])
                    )
                    if _btc_uptrend_short:
                        tlog("btc_uptrend", f"[CTX] BTC 4h UPTREND → blocco totale SHORT", 180)
            except Exception:
                pass
            # Fix #3: override se BTC 24h già in positivo (rimbalzo in corso)
            try:
                _r = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/tickers",
                                 params={"category": "linear", "symbol": "BTCUSDT"}, timeout=5)
                _d = _r.json()
                if _d.get("retCode") == 0 and _d["result"]["list"]:
                    _pct24h = float(_d["result"]["list"][0].get("price24hPcnt", 0)) * 100
                    if _pct24h > 0.5:
                        _btc_favorable_short = False
                        tlog("btc_ctx_pct", f"[CTX] BTC 24h={_pct24h:+.2f}% → btc_fav override=False", 180)
            except Exception:
                pass
        except Exception:
            pass
        # Fix #1: momentum BTC 15m — pump guard
        try:
            _r2 = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/kline",
                              params={"category": "linear", "symbol": "BTCUSDT",
                                      "interval": "15", "limit": 5}, timeout=5)
            _d2 = _r2.json()
            if _d2.get("retCode") == 0 and len(_d2["result"]["list"]) >= 3:
                _cls = _d2["result"]["list"]  # ordine Bybit: [0]=più recente
                _chg_30m = (float(_cls[0][4]) - float(_cls[2][4])) / float(_cls[2][4]) * 100
                _btc_pumping_short = _chg_30m > 1.5
                if _btc_pumping_short:
                    tlog("btc_pump", f"[CTX] BTC 15m pump={_chg_30m:+.2f}% → PUMP-GATE attivo", 60)
        except Exception:
            pass
        # Imp1/Imp2: BTC 4h change — baseline RS per update_assets() e analyze_asset()
        try:
            _r4h = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/kline",
                               params={"category": "linear", "symbol": "BTCUSDT",
                                       "interval": "240", "limit": 6}, timeout=5)
            _d4h = _r4h.json()
            if _d4h.get("retCode") == 0 and len(_d4h["result"]["list"]) >= 5:
                _c4h = _d4h["result"]["list"]  # [0]=più recente
                _btc_4h_chg_short = (float(_c4h[0][4]) - float(_c4h[4][4])) / max(1e-9, float(_c4h[4][4])) * 100
        except Exception:
            pass
        _btc_ctx_ts = time.time()
        _bear_confirmed = _btc_favorable_short and _btc_daily_down_short
        tlog("btc_ctx", f"[CTX] BTC 4h_down={_btc_favorable_short} | daily_down={_btc_daily_down_short} | bear_confirmed={_bear_confirmed} | uptrend={_btc_uptrend_short} | pumping={_btc_pumping_short} | btc_4h_chg={_btc_4h_chg_short:+.2f}%", 180)
        # Gap1: transizione bear→bull → stringi SL posizioni SHORT non protette
        if _btc_favorable_short_prev and not _btc_favorable_short and open_positions:
            try:
                _on_regime_bullish_short()
            except Exception as _re:
                log(f"[REGIME-CHANGE][SHORT] errore handler: {_re}")
        _btc_favorable_short_prev = _btc_favorable_short

def _get_oi_change(symbol: str) -> float | None:
    """Restituisce la variazione % di Open Interest nell'ultima ora (intervalTime=1h).
    Positivo = OI cresce (nuove posizioni aperte) = conferma genuinità del trend.
    Cache TTL 5 min per minimizzare API calls extra durante l'analisi parallela.
    """
    now = time.time()
    cached = _oi_cache.get(symbol)
    if cached and now - cached[0] < OI_CACHE_TTL:
        return cached[1]
    try:
        resp = SESSION.get(
            f"{BYBIT_BASE_URL}/v5/market/open-interest",
            params={"category": "linear", "symbol": symbol, "intervalTime": "1h", "limit": 3},
            timeout=8
        )
        lst = resp.json().get("result", {}).get("list", [])
        if len(lst) < 2:
            return None
        # list è in ordine decrescente (più recente prima)
        oi_now  = float(lst[0]["openInterest"])
        oi_prev = float(lst[1]["openInterest"])
        if oi_prev == 0:
            return None
        change_pct = (oi_now - oi_prev) / oi_prev * 100
        _oi_cache[symbol] = (now, change_pct)
        return change_pct
    except:
        return None

def _equity_now():
    total, usdt_balance, coin_values = get_portfolio_value()
    return total

def _update_daily_anchor_and_btc_context():
    """Aggiorna ancora giornaliera di equity e contesto BTC."""
    global _daily_start_equity
    if _daily_start_equity is None or time.strftime("%Y-%m-%d") != time.strftime("%Y-%m-%d", time.gmtime()):
        _daily_start_equity = _equity_now()
    _update_btc_context_short()

def _send_daily_report():
    """Invia una volta al giorno (intorno alle 21:00 UTC / 23:00 ora italiana) il riepilogo su Telegram."""
    global _last_report_day, _daily_trades_opened, _daily_trades_closed, _daily_pnl_sum
    now_utc = time.gmtime()
    today = time.strftime("%Y-%m-%d", now_utc)
    if today == _last_report_day:
        return
    # Invia solo dopo le 21:55 UTC (= 23:55 ora italiana, a fine giornata)
    if now_utc.tm_hour < 21 or (now_utc.tm_hour == 21 and now_utc.tm_min < 55):
        return
    try:
        equity = _equity_now()
        pnl_day = equity - (_daily_start_equity or equity)
        pnl_day_pct = (pnl_day / max(1e-9, _daily_start_equity or equity)) * 100.0
        pnl_emoji = "📈" if pnl_day >= 0 else "📉"
        bear_status = "✅ confermato" if (_btc_favorable_short and _btc_daily_down_short) else "❌ non confermato"
        pos_list = ", ".join(sorted(open_positions)) if open_positions else "nessuna"
        avg_pnl = (_daily_pnl_sum / _daily_trades_closed) if _daily_trades_closed > 0 else 0.0
        msg = (
            f"📋 Report giornaliero SHORT — {today}\n"
            f"{pnl_emoji} PnL giorno: {pnl_day:+.2f} USDT ({pnl_day_pct:+.2f}%)\n"
            f"💰 Equity: {equity:.2f} USDT\n"
            f"📂 Trade aperti oggi: {_daily_trades_opened}\n"
            f"✅ Trade chiusi oggi: {_daily_trades_closed}\n"
            f"📊 PnL medio chiusi: {avg_pnl:+.2f}% (fee incl.)\n"
            f"🐻 Bear confermato: {bear_status}\n"
            f"🔓 Posizioni attive: {pos_list}"
        )
        notify_telegram(msg)
        _last_report_day = today
        # Reset contatori per il giorno successivo
        _daily_trades_opened = 0
        _daily_trades_closed = 0
        _daily_pnl_sum = 0.0
    except Exception as e:
        log(f"[DAILY-REPORT] Errore invio report: {e}")

def is_breaking_weekly_low(symbol: str):
    """
    True se il prezzo attuale è sotto il minimo delle ultime 6 ore.
    """
    df = fetch_history(symbol, interval=INTERVAL_MINUTES)
    bars = int(6 * 60 / INTERVAL_MINUTES)
    if df is None or len(df) < bars:
        return False
    last_close = df["Close"].iloc[-1]
    low = df["Low"].iloc[-bars:].min()
    return last_close <= low * 0.995  # tolleranza 0.5% sotto il minimo

def update_assets(top_n=12):
    """
    Aggiorna ASSETS, LESS_VOLATILE_ASSETS e VOLATILE_ASSETS.
    Selezione ottimizzata (SHORT):
    - Pool: tutti i futures linear USDT con turnover24h >= LINEAR_MIN_TURNOVER (una sola chiamata API)
    - Ranking: 20% volume normalizzato + 55% momentum 24h + 25% debolezza relativa vs BTC
    - Bias SHORT: sweet spot -1%→-12%; premia coin più deboli di BTC (debolezza idiosincratica)
    - VOLATILE: |price24hPcnt| > 5% → ADX threshold 27; altrimenti LESS_VOLATILE → ADX 24
    """
    global ASSETS, LESS_VOLATILE_ASSETS, VOLATILE_ASSETS
    try:
        # Unica chiamata API: futures linear contengono tutto (liquidità + momentum)
        resp = SESSION.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category": "linear"}, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            log(f"[ASSETS] Errore API linear: {data}")
            return

        tickers = data["result"]["list"]

        # BTC 24h pct per calcolo debolezza relativa (già presente nella stessa risposta)
        _btc_t = next((t for t in tickers if t["symbol"] == "BTCUSDT"), None)
        btc_pct = float(_btc_t.get("price24hPcnt", 0)) * 100 if _btc_t else 0.0

        # Pool: tutti i linear USDT liquidi, escluse blacklist
        # Token leveraged da escludere (pattern sul suffisso prima di USDT)
        _LEV_SUFFIXES = ("3L", "3S", "2L", "2S", "BULL", "BEAR")
        pool = [
            t for t in tickers
            if t["symbol"].endswith("USDT")
            and float(t.get("turnover24h", 0)) >= LINEAR_MIN_TURNOVER
            and float(t.get("fundingRate", 0)) > -0.0020  # SHORT: funding negativo alto = troppi shorts già dentro, rischio squeeze
            and t["symbol"] not in STABLECOIN_BLACKLIST
            and t["symbol"] not in EXCLUSION_LIST
            and not t["symbol"][:-4].endswith(_LEV_SUFFIXES)  # esclude token leveraged (3L/3S/BULL/BEAR)
        ]

        if not pool:
            return

        def _momentum_score_short(pct: float) -> float:
            """Punteggio 0-1: favorisce momentum moderatamente negativo, penalizza estremi."""
            if pct < -20 or pct > 15:
                return 0.05  # dump/pump estremo: movimento probabilmente esaurito
            if -12.0 <= pct <= -1.0:
                return 1.0   # sweet spot: trend ribassista iniziato ma non esaurito
            if -1.0 < pct <= 0.0:
                return 0.65  # partenza, può ancora svilupparsi
            if -20.0 <= pct < -12.0:
                return 0.35  # esteso, rischio rimbalzo imminente
            return 0.2       # positivo: contro la direzione SHORT

        def _relative_score_short(rel: float) -> float:
            """Punteggio 0-1: premia debolezza relativa vs BTC (rel = coin_pct - btc_pct).
            Coin più debole di BTC = segnale idiosincratico = migliore candidata SHORT."""
            if rel <= -4.0: return 1.0   # molto più debole di BTC: ottimo per SHORT
            if rel <= -1.0: return 0.75  # moderatamente più debole
            if rel <=  1.0: return 0.5   # in linea con BTC
            if rel <=  4.0: return 0.25  # più forte di BTC: scarso per SHORT
            return 0.05                   # molto più forte: da evitare

        prev = set(ASSETS)
        candidates = []
        for t in pool:
            sym = t["symbol"]
            vol = float(t.get("turnover24h", 0))
            pct = float(t.get("price24hPcnt", 0)) * 100  # Bybit restituisce valore frazionario
            rel = pct - btc_pct
            mom = _momentum_score_short(pct)
            rel_s = _relative_score_short(rel)
            candidates.append((sym, vol, pct, mom, rel, rel_s))

        # Score finale — Imp1: pesi adattivi in sideways (BTC non fav e non pump)
        max_vol = max(c[1] for c in candidates) or 1.0
        if not _btc_favorable_short and not _btc_pumping_short:
            _w_vol, _w_mom, _w_rs = 0.20, 0.30, 0.50  # sideways: debolezza relativa più predittiva
        else:
            _w_vol, _w_mom, _w_rs = 0.20, 0.55, 0.25  # bear/pump: momentum guida
        scored = sorted(
            candidates,
            key=lambda c: _w_vol * (c[1] / max_vol) + _w_mom * c[3] + _w_rs * c[5],
            reverse=True
        )

        # -- BREAKOUT SLOTS: 2 slot garantiti per i top loser del momento --
        # Cattura coin con forte ribasso in atto (-15%→-60%) attualmente penalizzate
        # dallo score 0.05 e quindi mai selezionate dalla formula normale.
        BREAKOUT_SLOTS    = 2
        BREAKOUT_LOSS_MIN = 15.0
        BREAKOUT_LOSS_MAX = 25.0   # OPT: tagliato da 60% a 25% per evitare dump già esauriti (-40%+ = momentum terminale)
        top_base         = scored[:top_n - BREAKOUT_SLOTS]
        already_selected = {c[0] for c in top_base}
        breakout_cands   = sorted(
            [c for c in candidates
             if -BREAKOUT_LOSS_MAX <= c[2] <= -BREAKOUT_LOSS_MIN
             and c[1] >= 200_000_000  # ALZATO 50M→200M: breakout slot solo su coin con mercato reale
             and c[0] not in already_selected],
            key=lambda c: c[2]
        )[:BREAKOUT_SLOTS]
        top = top_base + breakout_cands
        if breakout_cands:
            log(f"[BREAKOUT-SLOTS][SHORT] Aggiunti: {[(c[0], f'{c[2]:+.1f}%') for c in breakout_cands]}")

        ASSETS = [c[0] for c in top]
        # VOLATILE = asset che si muovono più del 5% in 24h (in abs) → ADX threshold 27
        # LESS_VOLATILE = gli altri → ADX threshold 24
        VOLATILE_ASSETS      = [c[0] for c in top if abs(c[2]) > 5.0]
        LESS_VOLATILE_ASSETS = [c[0] for c in top if abs(c[2]) <= 5.0]

        changed = set(ASSETS) != prev
        if changed or LOG_DEBUG_ASSETS:
            added   = list(set(ASSETS) - prev)
            removed = list(prev - set(ASSETS))
            if LOG_DEBUG_ASSETS:
                mom_info = {c[0]: f"{c[2]:+.1f}% (rel={c[4]:+.1f}%)" for c in top}
                log(f"[ASSETS] BTC_24h={btc_pct:+.1f}% | Aggiornati: {ASSETS}\nMomento+Rel: {mom_info}\nVolatili(>5%): {VOLATILE_ASSETS}\nStabili(≤5%): {LESS_VOLATILE_ASSETS}")
            else:
                log(f"[ASSETS] Totali={len(ASSETS)} (+{len(added)}/-{len(removed)}) | BTC_24h={btc_pct:+.1f}% | Added={added[:5]} Removed={removed[:5]}")
    except Exception as e:
        log(f"[ASSETS] Errore aggiornamento lista asset: {e}")

def log(msg):
    print(time.strftime("[%Y-%m-%d %H:%M:%S]"), msg)

# Throttling semplice per log ripetitivi
_last_log_times = {}
def tlog(key: str, msg: str, interval_sec: int = 60):
    now = time.time()
    last = _last_log_times.get(key, 0)
    if now - last >= interval_sec:
        _last_log_times[key] = now
        log(msg)

# Livello log globale: DEBUG/INFO/WARN/ERROR (default INFO)
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# --- HTTP sessione condivisa con retry/backoff ---
RETRY_STRATEGY = Retry(
    total=3,
    backoff_factor=0.5,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["GET", "POST"],
)
SESSION = requests.Session()
ADAPTER = HTTPAdapter(max_retries=RETRY_STRATEGY, pool_maxsize=50)
SESSION.mount("https://", ADAPTER)
SESSION.mount("http://", ADAPTER)

# --- Logging trade su CSV ---
def _trade_log(event: str, symbol: str, side: str, entry_price: float = 0.0, qty: float = 0.0,
               sl: float = 0.0, tp: float = 0.0, r_dist: float = 0.0, extra: dict | None = None):
    try:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "trades.csv")
        header_needed = not os.path.exists(path)
        with open(path, "a", encoding="utf-8") as f:
            if header_needed:
                f.write("ts,event,symbol,side,entry,qty,sl,tp,r_dist,extra\n")
            jextra = json.dumps(extra or {}, separators=(",", ":"))
            f.write(f"{int(time.time())},{event},{symbol},{side},{entry_price},{qty},{sl},{tp},{r_dist},{jextra}\n")
    except Exception:
        pass

def _expectancy_log(pnl_pct: float, entry_notional: float, exit_notional: float,
                    maker_entry: bool = False, maker_exit: bool = False):
    try:
        os.makedirs("logs", exist_ok=True)
        path = os.path.join("logs", "expectancy.csv")
        header_needed = not os.path.exists(path)
        fee_entry = entry_notional * (FEES_MAKER_PCT if maker_entry else FEES_TAKER_PCT)
        fee_exit = exit_notional * (FEES_MAKER_PCT if maker_exit else FEES_TAKER_PCT)
        with open(path, "a", encoding="utf-8") as f:
            if header_needed:
                f.write("ts,pnl_pct,entry_notional,exit_notional,fee_entry,fee_exit\n")
            f.write(f"{int(time.time())},{pnl_pct:.6f},{entry_notional:.6f},{exit_notional:.6f},{fee_entry:.6f},{fee_exit:.6f}\n")
    except Exception:
        pass

# --- Helper richieste firmate Bybit (centralizzati) ---
def _bybit_signed_get(path: str, params: dict):
    try:
        from urllib.parse import urlencode
        query_string = urlencode(sorted(params.items()))
        ts = str(int(time.time() * 1000))
        recv_window = "10000"
        payload = f"{ts}{KEY}{recv_window}{query_string}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window
        }
        url = f"{BYBIT_BASE_URL}{path}"
        return SESSION.get(url, headers=headers, params=params, timeout=10)
    except Exception as e:
        tlog("signed_get_exc", f"[SIGNED-GET][{path}] exc: {e}", 300)
        raise

def _bybit_signed_post(path: str, body: dict):
    try:
        ts = str(int(time.time() * 1000))
        recv_window = "10000"
        body_json = json.dumps(body, separators=(",", ":"))
        payload = f"{ts}{KEY}{recv_window}{body_json}"
        sign = hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window,
            "X-BAPI-SIGN-TYPE": "2",
            "Content-Type": "application/json"
        }
        url = f"{BYBIT_BASE_URL}{path}"
        return SESSION.post(url, headers=headers, data=body_json, timeout=10)
    except Exception as e:
        tlog("signed_post_exc", f"[SIGNED-POST][{path}] exc: {e}", 300)
        raise

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
    if LOG_DEBUG_DECIMALS:
        log(f"[DECIMALI][FORMAT_QTY] qty={qty} | qty_step={qty_step} | precision={precision} | floored_qty={floored_qty} | quantize_str={quantize_str}")
    return fmt.format(floored_qty)

def format_price_bybit(price: float, tick_size: float) -> str:
    step = Decimal(str(tick_size))
    p = Decimal(str(price))
    floored = (p // step) * step
    dec = -step.as_tuple().exponent if step.as_tuple().exponent < 0 else 0
    return f"{floored:.{dec}f}"

def compute_trailing_distance(symbol: str, atr_val: float) -> float:
    price = get_last_price(symbol) or 0.0
    if price <= 0:
        return max(atr_val * 1.5, 0.0)
    min_abs = price * TRAILING_MIN
    max_abs = price * TRAILING_MAX
    dist = atr_val * 1.5
    return float(max(min_abs, min(max_abs, dist)))

def get_open_short_qty(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
        params = {"category": "linear", "symbol": symbol}
        resp = _bybit_signed_get("/v5/position/list", params)
        from urllib.parse import urlencode
        query_string = urlencode(sorted(params.items()))
        ts = str(int(time.time() * 1000))
        recv_window = "10000"
        sign_payload = f"{ts}{KEY}{recv_window}{query_string}"
        sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window
        }
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_SYNC:
                tlog(f"qty_err:{symbol}", f"[BYBIT-RAW][ERRORE] get_open_short_qty {symbol}: {json.dumps(data)}", 300)
            return 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Sell":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        if LOG_DEBUG_SYNC:
            tlog(f"qty_exc:{symbol}", f"❌ Errore get_open_short_qty per {symbol}: {e}", 300)
        return 0.0

def get_open_long_qty(symbol):
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
        params = {"category": "linear", "symbol": symbol}
        resp = _bybit_signed_get("/v5/position/list", params)
        from urllib.parse import urlencode
        query_string = urlencode(sorted(params.items()))
        ts = str(int(time.time() * 1000))
        recv_window = "10000"
        sign_payload = f"{ts}{KEY}{recv_window}{query_string}"
        sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
        headers = {
            "X-BAPI-API-KEY": KEY,
            "X-BAPI-SIGN": sign,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv_window
        }
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_SYNC:
                tlog(f"qty_err_long:{symbol}", f"[BYBIT-RAW][ERRORE] get_open_long_qty {symbol}: {json.dumps(data)}", 300)
            return 0.0
        for pos in data["result"]["list"]:
            if pos.get("side") == "Buy":
                qty = float(pos.get("size", 0))
                return qty if qty > 0 else 0.0
        return 0.0
    except Exception as e:
        if LOG_DEBUG_SYNC:
            tlog(f"qty_exc_long:{symbol}", f"❌ Errore get_open_long_qty per {symbol}: {e}", 300)
        return 0.0

# --- FUNZIONI DI SUPPORTO BYBIT E TELEGRAM ---
def get_last_price(symbol):
    try:
        now = time.time()
        with _price_lock:
            cached = _last_price_cache.get(symbol)
            if cached and (now - cached.get("ts", 0)) <= LAST_PRICE_TTL_SEC:
                return cached.get("price")
        endpoint = f"{BYBIT_BASE_URL}/v5/market/tickers"
        params = {"category": "linear", "symbol": symbol}  # PATCH: era "spot"
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            item = data["result"]["list"][0]
            price = float(item["lastPrice"])
            bid1 = float(item.get("bid1Price") or price)
            ask1 = float(item.get("ask1Price") or price)
            with _price_lock:
                _last_price_cache[symbol] = {"price": price, "bid1": bid1, "ask1": ask1, "ts": now}
            return price
        else:
            tlog(f"lp_err:{symbol}", f"[BYBIT] Errore get_last_price {symbol}: {data}", 300)
            return None
    except Exception as e:
        tlog(f"lp_exc:{symbol}", f"[BYBIT] Errore get_last_price {symbol}: {e}", 300)
        return None

def get_ask_price(symbol) -> Optional[float]:
    """Ritorna ask1Price dal ticker (cacheato da get_last_price)."""
    get_last_price(symbol)
    with _price_lock:
        c = _last_price_cache.get(symbol, {})
        return c.get("ask1") or c.get("price")

def get_instrument_info(symbol: str) -> dict:
    """
    Info strumento con cache 5m.
    Fallback conservativo: qty_step=0.01, min_order_amt=10 per evitare 170137/170140 ripetitivi.
    """
    now = time.time()
    # Cache semplice (aggiungi queste variabili globali in alto)
    global _instrument_cache
    with _instr_lock:
        if '_instrument_cache' not in globals():
            _instrument_cache = {}
        cached = _instrument_cache.get(symbol)
        if cached and (now - cached["ts"] < 300):
            return cached["data"]

    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "linear", "symbol": symbol}
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") != 0:
            tlog(f"instr_err:{symbol}", f"❌ get_instrument_info retCode {data.get('retCode')} → fallback {symbol}", 600)
            parsed = {
                "min_qty": 0.01,
                "qty_step": 0.01,
                "precision": 4,
                "price_step": 0.01,
                "min_order_amt": 10.0
            }
            with _instr_lock:
                _instrument_cache[symbol] = {"data": parsed, "ts": now}
            return parsed
        
        lst = data.get("result", {}).get("list", [])
        if not lst:
            tlog(f"instr_empty:{symbol}", f"❌ get_instrument_info lista vuota → fallback {symbol}", 600)
            parsed = {
                "min_qty": 0.01,
                "qty_step": 0.01,
                "precision": 4,
                "price_step": 0.01,
                "min_order_amt": 10.0
            }
            with _instr_lock:
                _instrument_cache[symbol] = {"data": parsed, "ts": now}
            return parsed
            
        info = lst[0]
        lot = info.get("lotSizeFilter", {})
        price_filter = info.get("priceFilter", {})
        
        qty_step_raw = lot.get("qtyStep", "0.01") or "0.01"
        try:
            qty_step = float(qty_step_raw)
        except:
            qty_step = 0.01
            
        parsed = {
            "min_qty": float(lot.get("minOrderQty", 0) or 0),
            "qty_step": qty_step,
            "precision": int(info.get("priceScale", 4) or 4),
            "price_step": float(price_filter.get("tickSize", "0.01") or "0.01"),
            "min_order_amt": float(lot.get("minNotionalValue", "5") or "5")
        }
        with _instr_lock:
            _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed
        
    except Exception as e:
        tlog(f"instr_exc:{symbol}", f"❌ Errore get_instrument_info eccezione → fallback {symbol}: {e}", 600)
        parsed = {
            "min_qty": 0.01,
            "qty_step": 0.01,
            "precision": 4,
            "price_step": 0.01,
            "min_order_amt": 10.0
        }
        _instrument_cache[symbol] = {"data": parsed, "ts": now}
        return parsed

def _format_qty_with_step(qty: float, step: float) -> str:
    step_dec = Decimal(str(step))
    q = Decimal(str(qty))
    floored = (q // step_dec) * step_dec
    step_decimals = -step_dec.as_tuple().exponent if step_dec.as_tuple().exponent < 0 else 0
    pattern = Decimal('1.' + '0'*step_decimals) if step_decimals > 0 else Decimal('1')
    floored = floored.quantize(pattern, rounding=ROUND_DOWN)
    if step_decimals > 0:
        return f"{floored:.{step_decimals}f}".rstrip('0').rstrip('.') or "0"
    return f"{int(floored)}"

def get_free_qty(symbol):
    # Normalizza coin
    if symbol.endswith("USDT") and len(symbol) > 4:
        coin = symbol.replace("USDT", "")
    elif symbol == "USDT":
        coin = "USDT"
    else:
        coin = symbol

    params = {"accountType": BYBIT_ACCOUNT_TYPE}

    try:
        resp = _bybit_signed_get("/v5/account/wallet-balance", params)
        data = resp.json()

        if "result" not in data or "list" not in data["result"]:
            if LOG_DEBUG_PORTFOLIO:
                log(f"❗ Struttura inattesa da Bybit per {symbol}: {resp.text}")
            return 0.0

        coin_list = data["result"]["list"][0].get("coin", [])
        for c in coin_list:
            if c["coin"] == coin:
                raw = c.get("walletBalance", "0")
                try:
                    qty = float(raw) if raw else 0.0
                    # Log SOLO per USDT e con throttling
                    if coin == "USDT":
                        tlog("balance_usdt", f"📦 Saldo USDT: {qty}", 1800)  # max 1 riga/10min
                    else:
                        if LOG_DEBUG_PORTFOLIO:
                            log(f"[BALANCE] {coin}: {qty}")
                    return qty
                except Exception as e:
                    if LOG_DEBUG_PORTFOLIO:
                        log(f"⚠️ Errore conversione quantità {coin}: {e}")
                    return 0.0

        if LOG_DEBUG_PORTFOLIO:
            log(f"🔍 Coin {coin} non trovata nel saldo.")
        return 0.0

    except Exception as e:
        if LOG_DEBUG_PORTFOLIO:
            log(f"❌ Errore nel recupero saldo per {symbol}: {e}")
        return 0.0

def notify_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log("[TELEGRAM] Token o chat_id non configurati")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": f"[SHORT] {msg}"}
    try:
        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        log(f"[TELEGRAM] Errore invio messaggio: {e}")

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

    min_notional = max(float(min_order_amt), float(min_qty) * float(price))
    if usdt_amount < min_notional:
        log(f"❌ Budget {usdt_amount:.2f} USDT insufficiente per notional minimo {min_notional:.2f} su {symbol}")
        return None
    try:
        raw_qty = Decimal(str(usdt_amount)) / Decimal(str(price))
        if LOG_DEBUG_DECIMALS:
            log(f"[DECIMALI][CALC_QTY] {symbol} | usdt_amount={usdt_amount} | price={price} | raw_qty={raw_qty} | qty_step={qty_step} | precision={precision}")
        qty_str = format_quantity_bybit(float(raw_qty), float(qty_step), precision=precision)
        qty_dec = Decimal(qty_str)
        min_qty_dec = Decimal(str(min_qty))
        if qty_dec < min_qty_dec:
            if LOG_DEBUG_DECIMALS:
                log(f"[DECIMALI][CALC_QTY] {symbol} | qty_dec < min_qty_dec: {qty_dec} < {min_qty_dec}")
            qty_dec = min_qty_dec
            qty_str = format_quantity_bybit(float(qty_dec), float(qty_step), precision=precision)
        order_value = qty_dec * Decimal(str(price))
        if LOG_DEBUG_DECIMALS:
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
        if LOG_DEBUG_DECIMALS:
            log(f"[DECIMALI][CALC_QTY][RETURN] {symbol} | qty_str={qty_str}")
        return qty_str
    except Exception as e:
        log(f"❌ Errore calcolo quantità per {symbol}: {e}")
        return None

def _try_limit_entry_short(symbol: str, qty_str: str, ask_price_str: str) -> Optional[float]:
    """Tenta ingresso SHORT come maker (PostOnly Limit a ask1Price).
    Polling fill max 3 secondi. Cancella e ritorna None se non eseguito (fallback Market)."""
    body = {
        "category": "linear",
        "symbol": symbol,
        "side": "Sell",
        "orderType": "Limit",
        "timeInForce": "PostOnly",
        "qty": qty_str,
        "price": ask_price_str,
        "positionIdx": SHORT_IDX
    }
    resp = _bybit_signed_post("/v5/order/create", body)
    try:
        data = resp.json()
    except Exception:
        return None
    if data.get("retCode") != 0:
        if LOG_DEBUG_STRATEGY:
            log(f"[LIMIT-ENTRY][SHORT][{symbol}] PostOnly rifiutato ({data.get('retCode')}), fallback Market")
        return None
    order_id = data.get("result", {}).get("orderId", "")
    # Polling fill: max 3 secondi (6 × 0.5s)
    for _ in range(6):
        time.sleep(0.5)
        filled_qty = get_open_short_qty(symbol)
        if filled_qty and filled_qty > 0:
            log(f"[LIMIT-ENTRY][SHORT][{symbol}] PostOnly @ {ask_price_str} eseguito ✓ (fee maker)")
            return filled_qty
    # Timeout: cancella ordine e segnala fallback
    if order_id:
        try:
            _bybit_signed_post("/v5/order/cancel", {"category": "linear", "symbol": symbol, "orderId": order_id})
        except Exception:
            pass
    if LOG_DEBUG_STRATEGY:
        log(f"[LIMIT-ENTRY][SHORT][{symbol}] PostOnly timeout, fallback Market")
    return None


def market_short(symbol: str, usdt_amount: float, qty_exact: Optional[str] = None):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}")
        return None
    
    info = get_instrument_info(symbol)
    qty_step = float(info.get("qty_step", 0.01))
    min_qty = float(info.get("min_qty", qty_step))
    min_order_amt = float(info.get("min_order_amt", 10.0))

    step_dec = Decimal(str(qty_step))
    # Se è stata fornita una quantità esatta (già conforme ai passi), usala direttamente
    if qty_exact is not None:
        try:
            qty_aligned = Decimal(str(qty_exact))
        except Exception:
            qty_aligned = Decimal("0")
    else:
        safe_usdt_amount = usdt_amount * 0.98
        raw_qty = Decimal(str(safe_usdt_amount)) / Decimal(str(price))
        qty_aligned = (raw_qty // step_dec) * step_dec

    # Guardie: evita qty 0 e rispetta minimi exchange
    if float(qty_aligned) <= 0 or float(qty_aligned) < min_qty:
        qty_aligned = Decimal(str(min_qty))
        if LOG_DEBUG_STRATEGY:
            log(f"[QTY-GUARD][{symbol}] qty riallineata a min_qty={float(qty_aligned)}")

    needed = Decimal(str(min_order_amt)) / Decimal(str(price))
    multiples = (needed / step_dec).quantize(Decimal('1'), rounding=ROUND_UP)
    min_notional_qty = multiples * step_dec
    if qty_aligned * Decimal(str(price)) < Decimal(str(min_order_amt)):
        qty_aligned = max(qty_aligned, min_notional_qty)
        if LOG_DEBUG_STRATEGY:
            log(f"[NOTIONAL-GUARD][{symbol}] qty alzata per min_order_amt → {float(qty_aligned)}")

    # --- TENTATIVO INGRESSO MAKER (PostOnly Limit a ask1Price) ---
    ask_price = get_ask_price(symbol) or 0.0
    if ask_price > 0:
        price_step = float(info.get("price_step", 0.01))
        ask_str = format_price_bybit(ask_price, price_step)
        qty_str_limit = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str_limit) > 0:
            limit_qty = _try_limit_entry_short(symbol, qty_str_limit, ask_str)
            if limit_qty:
                return limit_qty

    max_retries = 3
    for attempt in range(1, max_retries + 1):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        if float(qty_str) <= 0:
            log(f"❌ qty_str=0 per {symbol}, skip ordine")
            return None

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Sell",
            "orderType": "Market",
            "qty": qty_str,
            "positionIdx": SHORT_IDX
        }
        response = _bybit_signed_post("/v5/order/create", body)
        if LOG_DEBUG_STRATEGY:
            log(f"[SHORT][{symbol}] attempt {attempt}/{max_retries} BODY={json.dumps(body, separators=(',', ':'))}")

        try:
            resp_json = response.json()
        except:
            resp_json = {}
        if LOG_DEBUG_STRATEGY:
            log(f"[SHORT][{symbol}] RESP {response.status_code} {resp_json}")

        if resp_json.get("retCode") == 0:
            return float(qty_str)

        ret_code = resp_json.get("retCode")
        if ret_code == 170137:
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY][{symbol}] 170137 → refresh instrument e rifloor")
            try: _instrument_cache.pop(symbol, None)
            except Exception: pass
            info = get_instrument_info(symbol)
            qty_step = float(info.get("qty_step", qty_step))
            step_dec = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            continue
        elif ret_code == 170131:
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY][{symbol}] 170131 → riduco qty del 10%")
            qty_aligned = (qty_aligned * Decimal("0.9")) // step_dec * step_dec
            if qty_aligned <= 0:
                return None
            continue
        else:
            tlog(f"short_err:{symbol}:{ret_code}", f"[ERROR][{symbol}] Errore non gestito: {ret_code}", 300)
            break

    return None

def market_cover(symbol: str, qty: float):
    price = get_last_price(symbol)
    if not price:
        log(f"❌ Prezzo non disponibile per {symbol}, impossibile ricoprire")
        return None
    
    info = get_instrument_info(symbol)
    qty_step = info.get("qty_step", 0.01)  # Fallback conservativo
    
    # Allinea quantità al passo
    step_dec = Decimal(str(qty_step))
    qty_aligned = (Decimal(str(qty)) // step_dec) * step_dec
    
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        qty_str = _format_qty_with_step(float(qty_aligned), qty_step)
        
        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",  # Chiusura short = Buy
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,          # <--- FIX
            "positionIdx": SHORT_IDX
        }
        
        response = _bybit_signed_post("/v5/order/create", body)
        if LOG_DEBUG_STRATEGY:
            log(f"[COVER][{symbol}] attempt {attempt}/{max_retries} BODY={json.dumps(body, separators=(',', ':'))}")
        
        try:
            resp_json = response.json()
        except:
            resp_json = {}
        
        if LOG_DEBUG_STRATEGY:
            log(f"[COVER][{symbol}] RESP {response.status_code} {resp_json}")
        
        if resp_json.get("retCode") == 0:
            return response
            
        # Gestione errori con escalation
        ret_code = resp_json.get("retCode")
        if ret_code == 170137:  # Too many decimals
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY-COVER][{symbol}] 170137 → escalation passo")
            if qty_step < 0.1:
                qty_step = 0.1
            elif qty_step < 1.0:
                qty_step = 1.0
            else:
                qty_step = 10.0
            
            step_dec = Decimal(str(qty_step))
            qty_aligned = (qty_aligned // step_dec) * step_dec
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY-COVER][{symbol}] nuovo passo {qty_step}, qty→{qty_aligned}")
            continue
            
        elif ret_code == 170131:  # Insufficient balance (non dovrebbe accadere per cover)
            if LOG_DEBUG_STRATEGY:
                log(f"[RETRY-COVER][{symbol}] 170131 → problema inaspettato")
            break
            
        else:
            tlog(f"cover_err:{symbol}:{ret_code}", f"[ERROR-COVER][{symbol}] Errore non gestito: {ret_code}", 300)
            break
    
    return None

def cancel_all_orders(symbol: str, order_filter: Optional[str] = None) -> bool:
    body = {"category": "linear", "symbol": symbol}
    if order_filter:
        body["orderFilter"] = order_filter  # es: "StopOrder"
    try:
        resp = _bybit_signed_post("/v5/order/cancel-all", body)
        ok = resp.json().get("retCode") == 0
        if not ok:
            tlog(f"cancel_all_err:{symbol}", f"[CANCEL-ALL] {symbol} resp={resp.text}", 300)
        return ok
    except Exception as e:
        tlog(f"cancel_all_exc:{symbol}", f"[CANCEL-ALL] {symbol} exc: {e}", 300)
        return False

# >>> PATCH: funzioni per impostare lo stopLoss sulla posizione (SHORT) e worker BE
def set_position_stoploss_short(symbol: str, sl_price: float) -> bool:
    # Guard preventivo: per SHORT il SL deve essere SOPRA il prezzo corrente.
    # Se il valore è stale (prezzo si è mosso contro la SHORT dopo che il ratchet
    # aveva abbassato il floor), skippare silenziosamente evita retCode=10001.
    cur = get_last_price(symbol)
    if cur and sl_price <= cur:
        tlog(f"sl_invalid_short:{symbol}", f"[POS-SL][SHORT] {symbol} SL={sl_price:.6f} <= prezzo={cur:.6f}: valore stale, skip", 300)
        return False

    info = get_instrument_info(symbol)
    price_step = info.get("price_step", 0.01)
    stop_str = format_price_bybit(sl_price, price_step)
    body = {
        "category": "linear",
        "symbol": symbol,
        "stopLoss": stop_str,
        "slTriggerBy": "MarkPrice",
        "positionIdx": SHORT_IDX,
        "tpslMode": "Full"
    }
    try:
        resp = _bybit_signed_post("/v5/position/trading-stop", body)
        data = resp.json()
        ret = data.get("retCode")
        ok = ret == 0
        if not ok:
            if ret == 34040:  # "not modified": SL già impostato a questo valore, non è un errore reale
                log(f"[POS-SL][SHORT] {symbol} già impostato ({stop_str}), skip")
            elif ret == 10001 and "greater" in (data.get("retMsg") or "").lower():
                # SL sotto prezzo corrente: guard sopra dovrebbe prevenirlo, ma per sicurezza
                tlog(f"sl_invalid_short:{symbol}", f"[POS-SL][SHORT] {symbol} retCode=10001 SL stale ({stop_str} < prezzo), skip", 300)
            else:
                log(f"[POS-SL][SHORT] {symbol} FALLITO retCode={ret} msg={data.get('retMsg')} stopLoss={stop_str}")
                notify_telegram(f"⚠️ [POS-SL][SHORT] {symbol} position-SL FALLITO\nretCode={ret} {data.get('retMsg')}\nSL target={stop_str}")
        return ok
    except Exception as e:
        log(f"[POS-SL][SHORT] {symbol} eccezione: {e}")
        return False

def breakeven_lock_worker_short():
    # Porta lo stop della POSIZIONE a breakeven e piazza anche uno Stop-Market a BE
    while True:
        for symbol in list(open_positions):
            with _state_lock:
                entry = position_data.get(symbol)
            if not entry:
                continue

            be_locked = entry.get("be_locked", False)
            price_now = get_last_price(symbol)
            if not price_now:
                continue

            entry_price = entry.get("entry_price", price_now)
            # Attiva trailing-stop oltre soglia di R (SHORT)
            try:
                trailing_active = entry.get("trailing_active", False)
                r_dist = entry.get("r_dist")
                if (r_dist is not None) and (not trailing_active) and price_now <= entry_price - (TRAIL_START_R * r_dist):
                    df_hist = fetch_history(symbol, interval=INTERVAL_MINUTES)
                    atr_val = None
                    if df_hist is not None and "Close" in df_hist.columns and len(df_hist) > ATR_WINDOW + 2:
                        atr_series = AverageTrueRange(high=df_hist["High"], low=df_hist["Low"], close=df_hist["Close"], window=ATR_WINDOW).average_true_range()
                        last_atr = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
                        atr_val = last_atr
                    if atr_val is None or atr_val <= 0:
                        atr_val = float(r_dist) / max(1e-9, SL_ATR_MULT)
                    trailing_base = atr_val * TRAIL_ATR_MULT
                    trailing_dist = compute_trailing_distance(symbol, trailing_base)
                    if place_trailing_stop_short(symbol, trailing_dist):
                        entry["trailing_active"] = True
                        # FIX: imposta immediatamente BE come floor minimo del position SL (SHORT).
                        # Il trailing inizia a (price_now + trailing_dist) che può essere
                        # sopra entry se TRAIL_START_R < TRAIL_ATR_MULT/SL_ATR_MULT.
                        be_floor = float(entry_price) * (1.0 + abs(BREAKEVEN_BUFFER))  # SHORT: SL sopra entry
                        # Guard: per SHORT il be_floor non può mai essere sotto il prezzo corrente
                        be_floor = max(be_floor, price_now * 1.001)
                        set_position_stoploss_short(symbol, be_floor)
                        entry["be_locked"] = True
                        entry["be_price"] = be_floor
                        set_position(symbol, entry)
                        tlog(f"trail_on_short:{symbol}", f"[TRAIL-ON][SHORT] {symbol} attivo dist={trailing_dist:.6f} BE-floor={be_floor:.6f}", 60)
                        notify_telegram(f"🎯 Trailing attivato SHORT {symbol}\nPrezzo: {price_now:.4f}\nDistanza trailing: {trailing_dist:.6f}\nBE floor: {be_floor:.4f}")
            except Exception as _e:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"trail_on_exc_short:{symbol}", f"[TRAIL-ON-EXC][SHORT] {symbol} exc={_e}", 300)
            if be_locked:
                continue

            # Fix #2: pump guard BE — se BTC sta pompando, forza BE su SHORT in profitto
            if _btc_pumping_short and price_now is not None and price_now < entry_price:
                be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)
                qty_live = get_open_short_qty(symbol)
                ok_csl = False
                ok_psl = False
                if qty_live and qty_live > 0:
                    ok_csl = place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                    ok_psl = set_position_stoploss_short(symbol, be_price)
                if ok_csl or ok_psl:
                    entry["be_locked"] = True
                    entry["be_price"] = be_price
                    set_position(symbol, entry)
                    tlog(f"pump_be:{symbol}", f"[PUMP-BE][SHORT] {symbol} SL→BE {be_price:.6f} (BTC pump)", 60)
                try:
                    notify_telegram(f"⚠️ PUMP-BE SHORT {symbol}: SL→BE {be_price:.6f} (BTC +1.5%/30m)")
                except Exception:
                    pass
                continue

            r_dist = entry.get("r_dist")  # distanza 1R in prezzo
            # Se abbiamo r_dist, be quando prezzo ha guadagnato 1R
            cond_be = (r_dist is not None and price_now <= entry_price - (BE_AT_R * r_dist))
            # Fallback legacy: usa percentuale
            if r_dist is None:
                cond_be = price_now <= entry_price * (1.0 - BREAKEVEN_LOCK_PCT)
            if cond_be:
                # Buffer negativo: BE leggermente sotto l'entry per coprire fee/slippage
                be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)
                qty_live = get_open_short_qty(symbol)
                ok_csl = False
                ok_psl = False
                if qty_live and qty_live > 0:
                    # Piazza sia trading-stop di posizione sia uno stop-market di backup
                    ok_csl = place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                    ok_psl = set_position_stoploss_short(symbol, be_price)
                if ok_csl or ok_psl:
                    entry["be_locked"] = True
                    entry["be_price"] = be_price
                    set_position(symbol, entry)
                    tlog(f"be_lock:{symbol}", f"[BE-LOCK][SHORT] {symbol} SL→BE {be_price:.6f}", 60)
        time.sleep(2)

def _pick_floor_roi_short(mfe_roi: float) -> Optional[float]:
    """
    Ritorna il floor ROI (SHORT) oppure None se non superata la prima soglia.
    Ignora floor=0 (non applica nulla).
    """
    if not RATCHET_TIERS_ROI:
        return None
    first_threshold = RATCHET_TIERS_ROI[0][0]
    if mfe_roi < first_threshold:
        return None
    target = None
    for th, floor in RATCHET_TIERS_ROI:
        if mfe_roi >= th and floor > 0:
            target = floor
    return target

def profit_floor_worker_short():
    """
    Aggiorna lo stopLoss della posizione SHORT a scalini di ROI (ratchet).
    IMPORTANTE: il ratchet gira SEMPRE, anche quando trailing_active=True.
    Con trailing attivo: piazza SL manuale floor + stringe la distanza del trailing
    in modo che non possa dare indietro più di (MFE - floor) in ROI.
    """
    log("[RATCHET-SHORT] Worker avviato")
    while True:
        try:
         for symbol in list(open_positions):
            entry = position_data.get(symbol) or {}
            entry_price = entry.get("entry_price")
            qty_live = get_open_short_qty(symbol)
            if not entry_price or not qty_live or qty_live <= 0:
                continue

            trailing_active = entry.get("trailing_active", False)
            usdt_floor_locked = entry.get("usdt_floor_locked", False)
            price_now = get_last_price(symbol)
            if not price_now:
                continue

            # Cassaforte in USDT: se trailing non attivo e non già lockato
            if (not trailing_active) and (not usdt_floor_locked):
                unrealized = (float(entry_price) - price_now) * float(qty_live)
                if unrealized >= PNL_TRIGGER_USDT:
                    try:
                        target_sl = float(entry_price) - (float(PNL_LOCK_USDT) / max(1e-9, float(qty_live)))
                        # SHORT: stop sopra il prezzo attuale; applica buffer
                        target_sl = max(target_sl, price_now * (1.0 + PNL_LOCK_BUFFER_PCT))
                        if target_sl < float(entry_price):
                            set_ok = set_position_stoploss_short(symbol, target_sl)
                            entry["usdt_floor_locked"] = True
                            entry["usdt_floor_price"] = target_sl
                            entry["usdt_floor_pnl"] = PNL_LOCK_USDT
                            entry["floor_updated_ts"] = time.time()
                            tlog(f"usdt_floor_short:{symbol}", f"[USDT-FLOOR][SHORT] {symbol} SL→{target_sl:.6f} (lock≈{PNL_LOCK_USDT} USDT) set={set_ok}", 30)
                            set_position(symbol, entry)
                            continue
                    except Exception as _e:
                        if LOG_DEBUG_STRATEGY:
                            tlog(f"usdt_floor_exc_short:{symbol}", f"[USDT-FLOOR-EXC][SHORT] {symbol} exc={_e}", 180)

            # --- RATCHET ROI (gira sempre, anche con trailing attivo) ---
            # ROI (SHORT): prezzo scende → ROI positivo
            price_move_pct = ((float(entry_price) - price_now) / float(entry_price)) * 100.0
            roi_now = price_move_pct * DEFAULT_LEVERAGE

            # Aggiorna MFE ROI
            mfe_roi = max(entry.get("mfe_roi", 0.0), roi_now)
            entry["mfe_roi"] = mfe_roi

            # Determina floor ROI (None finché non superi la prima soglia)
            target_floor_roi = _pick_floor_roi_short(mfe_roi)
            prev_floor_roi = entry.get("floor_roi", None)

            # Se ancora nessuna soglia valida → non fare nulla
            if target_floor_roi is None:
                set_position(symbol, entry)
                continue

            # Non aggiornare se il floor non cresce
            if prev_floor_roi is not None and target_floor_roi <= prev_floor_roi:
                set_position(symbol, entry)
                continue

            # NOTA: cooldown rimosso per i tier upgrade — il check target<=prev già
            # impedisce duplicati. Il cooldown bloccava salti rapidi di tier (es. 7%→15%)
            # causando mancati aggiornamenti quando il prezzo rimbalzava dopo il cooldown.

            # Converti floor ROI in prezzo (SHORT: entry * (1 - delta))
            delta_pct_price = (target_floor_roi / max(1, DEFAULT_LEVERAGE)) / 100.0
            floor_price = float(entry_price) * (1.0 - delta_pct_price)

            # Buffer (SHORT → leggermente sopra il floor per attivarsi prima se risale)
            floor_price *= (1.0 + FLOOR_BUFFER_PCT)

            # Se floor_price ≤ prezzo attuale, lo stop sarebbe sotto → inutile
            if floor_price <= price_now:
                entry["floor_roi"] = target_floor_roi
                entry["floor_price"] = floor_price
                entry["floor_updated_ts"] = time.time()
                set_position(symbol, entry)
                tlog(
                    f"floor_up_short_skip:{symbol}",
                    f"[FLOOR-UP-SKIP][SHORT] {symbol} MFE={mfe_roi:.1f}% targetROI={target_floor_roi:.1f}% floorPrice={floor_price:.6f} ≤ current={price_now:.6f}",
                    120,
                )
                continue

            # Piazza SL manuale al floor — funziona sia con che senza trailing attivo
            set_ok = set_position_stoploss_short(symbol, floor_price)

            # Se trailing attivo: stringe anche la distanza trailing in modo che
            # non possa cedere più di (mfe_roi - target_floor_roi) in ROI
            if trailing_active:
                allowed_drawdown_roi = max(5.0, mfe_roi - target_floor_roi)  # min 5% ROI di spazio
                max_trailing_pct = (allowed_drawdown_roi / max(1, DEFAULT_LEVERAGE)) / 100.0
                new_trailing_dist = price_now * max_trailing_pct
                new_trailing_dist = max(price_now * TRAILING_MIN, min(new_trailing_dist, price_now * TRAILING_MAX))
                place_trailing_stop_short(symbol, new_trailing_dist)
                tlog(f"trail_tighten_short:{symbol}",
                     f"[TRAIL-TIGHTEN][SHORT] {symbol} MFE={mfe_roi:.1f}% floor={target_floor_roi:.1f}% → trailing_dist={new_trailing_dist:.6f}", 60)

            entry["floor_roi"] = target_floor_roi
            entry["floor_price"] = floor_price
            entry["floor_updated_ts"] = time.time()

            tlog(
                f"floor_up_short:{symbol}",
                f"[FLOOR-UP][SHORT] {symbol} MFE={mfe_roi:.1f}% → FloorROI={target_floor_roi:.1f}% → SL={floor_price:.6f} trailing={trailing_active} set={set_ok}",
                30,
            )
            set_position(symbol, entry)
            save_positions_state()  # persisti mfe_roi/floor_roi su disco

        except Exception as _worker_exc:
            log(f"[RATCHET-SHORT][CRASH] Eccezione nel worker: {_worker_exc}")
        time.sleep(3)

def place_conditional_sl_short(symbol: str, stop_price: float, qty: float, trigger_by: str = TRIGGER_BY) -> bool:
    """
    Piazza/aggiorna uno stop-market reduceOnly per proteggere la posizione SHORT.
    Side=Buy (cover), reduceOnly=true, triggerPrice = stop_price.
    """
    try:
        # Guard fondamentale: per SHORT il trigger DEVE essere sopra il prezzo corrente.
        # Bybit rifiuta con retCode 110093 se stop_price <= mark_price.
        _cur = get_last_price(symbol)
        if _cur and stop_price <= _cur:
            tlog(f"csl_skip:{symbol}", f"[CSL-SKIP][SHORT] {symbol} stop {stop_price:.6f} <= current {_cur:.6f}, skip", 60)
            return False
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.01)
        price_step = info.get("price_step", 0.01)                 # <<< aggiunto
        qty_str = _format_qty_with_step(float(qty), qty_step)
        stop_str = format_price_bybit(stop_price, price_step)     # <<< aggiunto

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Market",
            "qty": qty_str,
            "reduceOnly": True,
            "positionIdx": SHORT_IDX,
            "triggerBy": trigger_by,
            "triggerPrice": stop_str,
            "triggerDirection": 1,
            "closeOnTrigger": True
        }
        if LOG_DEBUG_STRATEGY:
            log(f"[SL-DEBUG-BODY][SHORT] {json.dumps(body)}")
        resp = _bybit_signed_post("/v5/order/create", body)
        try:
            data = resp.json()
        except:
            data = {}
        if data.get("retCode") == 0:
            return True
        log(f"[SL-PLACE][SHORT] {symbol} FALLITO dopo cancel! retCode={data.get('retCode')} msg={data.get('retMsg')} triggerPrice={stop_str}")
        notify_telegram(f"🚨 SL conditional FALLITO {symbol} SHORT!\ntriggerPrice={stop_str}\nretCode={data.get('retCode')} {data.get('retMsg')}\n⚠️ VERIFICA MANUALE")
        return False
    except Exception as e:
        log(f"[SL-PLACE][SHORT] {symbol} eccezione: {e}")
        notify_telegram(f"🚨 SL conditional ECCEZIONE {symbol} SHORT!\n{e}\n⚠️ VERIFICA MANUALE")
        return False

def place_takeprofit_short(symbol: str, tp_price: float, qty: float) -> tuple[bool, str]:
    try:
        info = get_instrument_info(symbol)
        qty_step = info.get("qty_step", 0.01)
        min_qty = float(info.get("min_qty", 0.0))
        price_step = info.get("price_step", 0.01)
        qty_f = float(qty)
        if qty_f < max(min_qty, float(qty_step)):
            tlog(f"tp_skip_min_short:{symbol}", f"[TP-SKIP][SHORT] qty parziale {qty_f} < min_qty {min_qty} (step {qty_step})", 120)
            return False, ""
        qty_str = _format_qty_with_step(qty_f, qty_step)
        try:
            from decimal import Decimal
            if Decimal(qty_str) <= 0:
                tlog(f"tp_skip_zero_short:{symbol}", f"[TP-SKIP][SHORT] qty_str={qty_str} non valido (≤0)", 120)
                return False, ""
        except Exception:
            pass
        tp_str = format_price_bybit(tp_price, price_step)

        body = {
            "category": "linear",
            "symbol": symbol,
            "side": "Buy",
            "orderType": "Limit",
            "qty": qty_str,
            "price": tp_str,                                    # <<< sostituito
            "timeInForce": "PostOnly",
            "reduceOnly": True,
            "positionIdx": SHORT_IDX
        }
        resp = _bybit_signed_post("/v5/order/create", body)
        try:
            data = resp.json()
        except:
            data = {}
        if data.get("retCode") == 0:
            oid = data.get("result", {}).get("orderId", "") or ""
            tlog(f"tp_place_short:{symbol}", f"[TP-PLACE] {symbol} tp={tp_price:.6f} qty={qty_str} orderId={oid}", 30)
            return True, oid
        tlog(f"tp_create_err_short:{symbol}", f"[TP-PLACE][SHORT] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
        return False, ""
    except Exception as e:
        tlog(f"tp_create_exc_short:{symbol}", f"[TP-PLACE][SHORT] exc: {e}", 300)
        return False, ""

def place_trailing_stop_short(symbol: str, trailing_dist: float):
    body = {
        "category": "linear",
        "symbol": symbol,
        "trailingStop": str(trailing_dist),
        "positionIdx": SHORT_IDX
    }
    resp = _bybit_signed_post("/v5/position/trading-stop", body)
    try:
        data = resp.json()
    except:
        data = {}
    if data.get("retCode") == 0:
        tlog(f"trailing_short:{symbol}", f"[TRAILING-PLACE-SHORT] {symbol} trailing={trailing_dist}", 30)
        return True
    tlog(f"trailing_short_err:{symbol}", f"[TRAILING-PLACE-SHORT][ERR] retCode={data.get('retCode')} msg={data.get('retMsg')}", 300)
    return False

def fetch_history(symbol: str, interval=INTERVAL_MINUTES, limit=400):
    """
    Scarica la cronologia dei prezzi per il simbolo dato da Bybit (linear/futures).
    """
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/kline"
        params = {
            "category": "linear",
            "symbol": symbol,
            "interval": str(interval),
            "limit": limit
        }
        resp = SESSION.get(endpoint, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 10006:
            tlog(f"fetch_rl:{symbol}", f"[BYBIT] Rate limit su {symbol}, piccolo backoff...", 10)
            time.sleep(1.2)
            return None
        if data.get("retCode") != 0 or "result" not in data or "list" not in data["result"]:
            tlog(f"fetch_err:{symbol}", f"[BYBIT] Errore fetch_history {symbol}: {data}", 600)
            return None
        klines = list(reversed(data["result"]["list"]))  # FIX: Bybit ritorna newest-first; invertiamo per avere oldest-first
        df = pd.DataFrame(klines, columns=[
            "timestamp", "Open", "High", "Low", "Close", "Volume", "Turnover"
        ])
        # Conversioni di tipo
        for col in ["Open", "High", "Low", "Close", "Volume", "Turnover"]:
            df[col] = df[col].astype(float)
        return df
    except Exception as e:
        tlog(f"fetch_exc:{symbol}", f"[BYBIT] Errore fetch_history {symbol}: {e}", 600)
        return None

def find_close_column(df):
    """
    Restituisce la colonna 'Close' se presente, altrimenti None.
    """
    for col in df.columns:
        if col.lower() == "close":
            return df[col]
    return None
def is_symbol_linear(symbol):
    """
    Verifica se il simbolo è disponibile su Bybit futures linear.
    """
    try:
        endpoint = f"{BYBIT_BASE_URL}/v5/market/instruments-info"
        params = {"category": "linear", "symbol": symbol}
        resp = requests.get(endpoint, params=params, timeout=10)
        data = resp.json()
        return data.get("retCode") == 0 and data["result"]["list"]
    except Exception:
        return False
    
def record_exit(symbol: str, entry_price: float, exit_price: float, side: str):
    last_exit_time[symbol] = time.time()
    if side == "LONG":
        pnl_pct = ((exit_price - entry_price) / entry_price) * 100.0
    else:  # SHORT
        pnl_pct = ((entry_price - exit_price) / entry_price) * 100.0
    if pnl_pct < 0:
        recent_losses[symbol] = recent_losses.get(symbol, 0) + 1
        last_exit_was_loss[symbol] = True
    else:
        recent_losses[symbol] = 0
        last_exit_was_loss[symbol] = False

def analyze_asset(symbol: str):
    funding_rate = None  # funding rate corrente (da tickers API), usato come filtro
    # Telemetria: raccogliamo segnali e contesto per misurare l'edge
    # (ADX 1h/4h, slope EMA, RSI, breakout, price24hPcnt)
    try:
        # Kline 1h per ADX/EMA100
        resp1 = requests.get(f"{BYBIT_BASE_URL}/v5/market/kline", params={"category":"linear","symbol":symbol,"interval":"60","limit":120}, timeout=10)
        d1 = resp1.json()
        adx1h = None
        ema100_slope = None
        if d1.get("retCode") == 0 and d1.get("result",{}).get("list"):
            raw1 = d1["result"]["list"]
            df1 = pd.DataFrame(raw1, columns=["timestamp","Open","High","Low","Close","Volume","turnover"])
            for col in ("Open","High","Low","Close"):
                df1[col] = pd.to_numeric(df1[col], errors="coerce")
            df1.dropna(subset=["Close"], inplace=True)
            if len(df1) >= 100:
                adx_series = ADXIndicator(high=df1["High"].astype(float), low=df1["Low"].astype(float), close=df1["Close"].astype(float), window=14).adx()
                if len(adx_series) > 0:
                    adx1h = float(adx_series.iloc[-1])
                ema100 = EMAIndicator(close=df1["Close"], window=100).ema_indicator()
                ema100_slope = float(ema100.iloc[-1] - ema100.iloc[-2]) if len(ema100) >= 2 else None
        # Kline 4h per EMA200
        resp4 = requests.get(f"{BYBIT_BASE_URL}/v5/market/kline", params={"category":"linear","symbol":symbol,"interval":"240","limit":220}, timeout=10)
        d4 = resp4.json()
        ema200_slope = None
        coin_4h_chg = None  # Imp2: variazione 4h coin per RS breakdown
        if d4.get("retCode") == 0 and d4.get("result",{}).get("list"):
            raw4 = d4["result"]["list"]
            df4 = pd.DataFrame(raw4, columns=["timestamp","Open","High","Low","Close","Volume","turnover"])
            for col in ("Open","High","Low","Close"):
                df4[col] = pd.to_numeric(df4[col], errors="coerce")
            df4.dropna(subset=["Close"], inplace=True)
            if len(df4) >= 200:
                ema200 = EMAIndicator(close=df4["Close"], window=200).ema_indicator()
                ema200_slope = float(ema200.iloc[-1] - ema200.iloc[-2]) if len(ema200) >= 2 else None
            # Imp2: coin 4h change per RS breakdown
            if len(df4) >= 5:
                try:
                    coin_4h_chg = (float(df4["Close"].iloc[-1]) - float(df4["Close"].iloc[-5])) / max(1e-9, float(df4["Close"].iloc[-5])) * 100
                except Exception:
                    pass
        rs_4h = (coin_4h_chg - _btc_4h_chg_short) if coin_4h_chg is not None else None
        # RSI 1h
        rsi1h = None
        if d1.get("retCode") == 0 and d1.get("result",{}).get("list"):
            try:
                rsi1h = float(RSIIndicator(close=df1["Close"], window=14).rsi().iloc[-1]) if len(df1) >= 15 else None
            except:
                rsi1h = None
        # Breakout flag
        breakout_ok = is_breaking_weekly_low(symbol) if ENABLE_BREAKOUT_FILTER else None
        # price24hPcnt e funding rate
        chg = None
        try:
            tick = requests.get(f"{BYBIT_BASE_URL}/v5/market/tickers", params={"category":"linear","symbol":symbol}, timeout=10).json()
            if tick.get("retCode") == 0 and tick.get("result", {}).get("list"):
                lst = tick["result"]["list"]
                chg = float(lst[0].get("price24hPcnt", 0.0))
                funding_rate = float(lst[0].get("fundingRate") or 0.0)
        except:
            chg = None
        tlog(f"telem_short:{symbol}", f"[TELEM][SHORT][{symbol}] adx1h={adx1h} ema100_slope={ema100_slope} ema200_slope={ema200_slope} rsi1h={rsi1h} chg24h={chg} funding={funding_rate} rs_4h={f'{rs_4h:+.2f}%' if rs_4h is not None else 'n/a'}", 300)
    except Exception as e:
        log(f"[TELEM][SHORT][{symbol}] errore telemetria: {e}")

    # Filtro trend configurabile (SHORT)
    down_4h = is_trending_down(symbol, "240")
    down_1h = is_trending_down_1h(symbol, "60")

    # BTC favorevole (4h downtrend) → accettiamo solo 4h; altrimenti richiediamo 4h+1h
    trend_ok = down_4h if _btc_favorable_short else (down_4h and down_1h)

    # Breakout-exempt: bypass filtro BTC per coin con crash esplosivo confermato
    # Condizioni: BTC non favorevole + coin in VOLATILE_ASSETS (>15% loss) + ADX1h > 35 + max 1 exempt aperta
    _is_breakout_exempt = False
    if (not _btc_favorable_short and symbol in VOLATILE_ASSETS
            and adx1h is not None and adx1h > 35):
        breakout_exempt_open = sum(1 for s in open_positions if position_data.get(s, {}).get("breakout_exempt"))
        if breakout_exempt_open < 1:
            _is_breakout_exempt = True
            log(f"[BREAKOUT-EXEMPT][SHORT] {symbol} bypass BTC filter: adx1h={adx1h:.1f}, VOLATILE, btc_fav=False")

    # Filtro trend: obbligatorio quando BTC non è in downtrend (contesto sfavorevole)
    if not _btc_favorable_short and not trend_ok and not _is_breakout_exempt:
        tlog(f"trend_short:{symbol}", f"[TREND-FILTER][{symbol}] BTC sfavorevole, trend non idoneo", 600)
        return None, None, None

    # Breakout filter: permetti fallback se trend è forte anche senza breakdown
    if ENABLE_BREAKOUT_FILTER:
        brk = is_breaking_weekly_low(symbol)
        if not brk:
            adx_thresh = ENTRY_ADX_VOLATILE if (symbol in VOLATILE_ASSETS) else ENTRY_ADX_STABLE
            ema_down = (ema200_slope is not None and ema200_slope < 0) or (ema100_slope is not None and ema100_slope < 0)
            strong_trend = trend_ok and (adx1h is not None and adx1h >= adx_thresh) and ema_down
            if not strong_trend and not _is_breakout_exempt:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"breakout_short:{symbol}", f"[BREAKOUT-FILTER][{symbol}] No breakdown e fallback non soddisfatto → skip", 600)
                return None, None, None

    try:
        is_volatile = symbol in VOLATILE_ASSETS
        tf_minutes = ENTRY_TF_VOLATILE if is_volatile else ENTRY_TF_STABLE

        df = fetch_history(symbol, interval=tf_minutes)
        if df is None or len(df) < 4:
            # Riduce spam: messaggio ogni 5 minuti per simbolo
            tlog(f"analyze:data:{symbol}", f"[ANALYZE][{symbol}] Dati insufficienti ({tf_minutes}m)", 300)
            return None, None, None

        close = find_close_column(df)
        if close is None:
            # Riduce spam: messaggio ogni 5 minuti per simbolo
            tlog(f"analyze:close:{symbol}", f"[ANALYZE][{symbol}] Colonna Close assente", 300)
            return None, None, None

        # Indicatori
        bb = BollingerBands(close=close)
        df["bb_upper"] = bb.bollinger_hband()
        df["bb_lower"] = bb.bollinger_lband()
        df["rsi"] = RSIIndicator(close=close).rsi()
        df["ema20"] = EMAIndicator(close=close, window=20).ema_indicator()
        df["ema50"] = EMAIndicator(close=close, window=50).ema_indicator()
        df["ema200"] = EMAIndicator(close=close, window=200).ema_indicator()
        df["sma20"] = SMAIndicator(close=close, window=20).sma_indicator()
        df["sma50"] = SMAIndicator(close=close, window=50).sma_indicator()
        macd = MACD(close=close)
        df["macd"] = macd.macd()
        df["macd_signal"] = macd.macd_signal()
        df["adx"] = ADXIndicator(high=df["High"], low=df["Low"], close=close).adx()
        atr = AverageTrueRange(high=df["High"], low=df["Low"], close=close, window=ATR_WINDOW)
        df["atr"] = atr.average_true_range()

        df.dropna(subset=["bb_upper","bb_lower","rsi","ema20","ema50","ema200","macd","macd_signal","adx","atr"], inplace=True)
        if len(df) < 4:
            return None, None, None

        # ADX base: permissivo quando BTC favorevole (downtrend 4h), standard altrimenti
        if _btc_favorable_short:
            adx_threshold = (ENTRY_ADX_VOLATILE - 3) if is_volatile else (ENTRY_ADX_STABLE - 3)  # 24 / 21
        else:
            adx_threshold = ENTRY_ADX_VOLATILE if is_volatile else ENTRY_ADX_STABLE               # 27 / 24
        # Usa SOLO candele chiuse per i segnali (evita repaint)
        last = df.iloc[-2]       # candela appena chiusa
        prev = df.iloc[-3]       # candela chiusa precedente
        price = float(df["Close"].iloc[-1])  # prezzo attuale
        tf_tag = f"({tf_minutes}m)"
        # Filtro estensione: evita SHORT troppo sotto EMA20 - k*ATR (rischio rimbalzo)
        # Per breakout-exempt k è rilassato (3.5x) perché la coin è PER DEFINIZIONE estesa
        ema20v = float(last["ema20"]); atrv = float(last["atr"])
        if _is_breakout_exempt:
            k = 3.5
        elif symbol in LARGE_CAPS:
            k = 1.8
        else:
            k = 1.5
        ext_floor = ema20v - k * atrv
        if float(last["Close"]) < ext_floor:
            if LOG_DEBUG_STRATEGY:
                tlog(f"ext_short:{symbol}", f"[FILTER][{symbol}] Estensione: close {last['Close']:.6f} < ema20 {ema20v:.6f} - {k}*ATR ({ext_floor:.6f})", 600)
            return None, None, None

        # Eventi (trigger anticipato)
        rsi_th = RSI_SHORT_THRESHOLD
        ema_bearish_cross = (prev["ema20"] >= prev["ema50"]) and (last["ema20"] < last["ema50"])
        macd_bearish_cross = (prev["macd"] >= prev["macd_signal"]) and (last["macd"] < last["macd_signal"])
        rsi_break = (prev["rsi"] >= rsi_th) and (last["rsi"] < rsi_th)

        # Stati
        ema_state = last["ema20"] < last["ema50"]
        macd_state = last["macd"] < last["macd_signal"]
        rsi_state = last["rsi"] < rsi_th

        event_triggered = ema_bearish_cross or macd_bearish_cross or rsi_break
        # OI come 4° indicatore: OI crescente con prezzo in calo = nuovi short genuini (non solo liquidazioni)
        # OI deve essere >= 0 (mercato neutro o in espansione—non in withdrawal): filtro obbligatorio se dato disponibile
        oi_change = _get_oi_change(symbol)
        if oi_change is not None and oi_change < 0:
            tlog(f"oi_filter:{symbol}", f"[OI-FILTER][SHORT] {symbol} OI={oi_change:+.2f}% < 0, pressione in calo, skip", 300)
            return None, None, None
        oi_confirms = oi_change is not None and oi_change > 0.2
        # Imp3: ATR expansion — volatilità crescente segnala movimento genuino (laterale → direzionale)
        _atr_s = df["atr"]
        _avg_atr_20 = float(_atr_s.iloc[-22:-2].mean()) if len(_atr_s) >= 22 else float(_atr_s.mean())
        atr_expanding = float(last["atr"]) > _avg_atr_20 * 1.25
        conf_count = [ema_state, macd_state, rsi_state, oi_confirms, atr_expanding].count(True)

        # Confluenza richiesta: più alta quando BTC non favorevole
        if not _btc_favorable_short:
            required_confluence = MIN_CONFLUENCE + 1
        else:
            required_confluence = MIN_CONFLUENCE
        # Breakout-exempt: ADX1h > 35 già confermato, riduci requisito confluence di 1
        if _is_breakout_exempt:
            required_confluence = max(1, required_confluence - 1)

        # Quando BTC non favorevole, richiedi SEMPRE un evento reale (cross/break)
        if not _btc_favorable_short and not event_triggered and not _is_breakout_exempt:
            return None, None, None

        # ADX richiesto + bonus extra quando BTC non favorevole
        adx_needed = max(0.0, adx_threshold - (ADX_RELAX_EVENT if event_triggered else 0.0))
        if not _btc_favorable_short:
            adx_needed += 1.5

        # >>> PATCH: throttle DD → più conferme, ADX più alto, e richiedi evento se in DD
        required_confluence += RISK_THROTTLE_LEVEL
        adx_needed += 1.5 * RISK_THROTTLE_LEVEL
        if RISK_THROTTLE_LEVEL >= 1 and not event_triggered:
            return None, None, None

        tlog(
            f"entry_chk_short:{symbol}",
            f"[ENTRY-CHECK][SHORT] conf={conf_count}/{required_confluence} | ADX={last['adx']:.1f}>{adx_needed:.1f} | event={event_triggered} | oi={f'{oi_change:+.2f}%' if oi_change is not None else 'n/a'} | btc_fav={_btc_favorable_short} | tf={tf_tag}",
            300
        )

        # Guardrail loss consecutivi (SHORT): se troppe perdite recenti e prezzo sopra ema50 → aspetta
        if recent_losses.get(symbol, 0) >= MAX_CONSEC_LOSSES:
            wait_min = (time.time() - last_exit_time.get(symbol, 0)) / 60
            if price > last["ema50"] and wait_min < FORCED_WAIT_MIN:
                if LOG_DEBUG_STRATEGY:
                    tlog(f"loss_guard_short:{symbol}",
                         f"[LOSS-GUARD][SHORT] Blocco {symbol} (loss={recent_losses.get(symbol)}) sopra EMA50 wait={wait_min:.1f}m",
                         300)
                return None, None, None

        # Segnale ingresso SHORT: richiede evento fresco E confluenza minima
        # FIX: rimosso "OR event_triggered" che permetteva ingressi con un solo cross
        min_conf_with_event = max(1, required_confluence - 1)  # evento = bonus -1 di confluenza
        entry_condition = (event_triggered and conf_count >= min_conf_with_event) or (conf_count >= required_confluence)
        if (entry_condition and float(last["adx"]) > adx_needed):
            # Filtro volume: segnale deve avere almeno 60% del volume medio (ultimi 20 periodi chiusi)
            vol_series = pd.to_numeric(df["Volume"], errors="coerce")
            vol_avg20 = vol_series.iloc[-22:-2].mean() if len(vol_series) >= 22 else 0.0
            vol_last = float(vol_series.iloc[-2])
            vol_ratio = vol_last / vol_avg20 if vol_avg20 > 0 else 1.0
            # Breakout-exempt richiede volume almeno 1.5x media (conferma breakdown genuino)
            vol_min = 1.5 if _is_breakout_exempt else 0.6
            if vol_ratio < vol_min:
                tlog(f"vol_low:{symbol}", f"[VOL-FILTER][SHORT] {symbol} volume={vol_ratio:.2f}x media (min={vol_min:.1f}x), segnale debole, skip", 300)
                return None, None, None
            # Filtro funding: se shorts sovraccaricati (funding negativo) → pressione rialzista
            if funding_rate is not None and funding_rate < FUNDING_SHORT_MIN:
                tlog(f"funding:{symbol}", f"[FUNDING-FILTER][SHORT] {symbol} funding={funding_rate:.4%} < min {FUNDING_SHORT_MIN:.4%}, skip", 300)
                return None, None, None
            entry_strategies = []
            if ema_state: entry_strategies.append(f"EMA Bearish {tf_tag}")
            if macd_state: entry_strategies.append(f"MACD Bearish {tf_tag}")
            if rsi_state: entry_strategies.append(f"RSI Bearish {tf_tag}")
            if oi_confirms: entry_strategies.append(f"OI↑{oi_change:+.2f}%")
            # ADX forte (>soglia+8) conta come punto score; altrimenti "ADX Trend" viene ignorato
            if float(last["adx"]) > adx_threshold + 8:
                entry_strategies.append(f"Trend Forte({last['adx']:.0f})")
            else:
                entry_strategies.append("ADX Trend")
            if _is_breakout_exempt:
                entry_strategies.append("BREAKOUT-EXEMPT")
            if LOG_DEBUG_STRATEGY:
                log(f"[ENTRY-SHORT][{symbol}] EVENTO/CONFLUENZA → {entry_strategies}")
            return "entry", ", ".join(entry_strategies), price

        # Imp2: RS Breakdown — coin underperforma BTC di ≥4% su 4h + vol expansion + RSI<48 + ADX>20
        # Cattura movimenti idiosincratici ribassisti prima che EMA/MACD lagging si allineino
        if (rs_4h is not None and rs_4h <= -4.0
                and rsi1h is not None and rsi1h < 48
                and adx1h is not None and adx1h > 20
                and RISK_THROTTLE_LEVEL == 0
                and (funding_rate is None or funding_rate >= FUNDING_SHORT_MIN)):
            _rs_vol_s = pd.to_numeric(df["Volume"], errors="coerce")
            _rs_vol_avg = _rs_vol_s.iloc[-22:-2].mean() if len(_rs_vol_s) >= 22 else float(_rs_vol_s.mean())
            _rs_vol_last = float(_rs_vol_s.iloc[-2])
            _rs_vol_ratio = _rs_vol_last / _rs_vol_avg if _rs_vol_avg > 0 else 0.0
            if _rs_vol_ratio >= 1.8:
                tlog(f"rs_breakdown_short:{symbol}",
                     f"[RS-BREAKDOWN][SHORT] {symbol} | rs_4h={rs_4h:+.2f}% | rsi1h={rsi1h:.1f} | adx1h={adx1h:.1f} | vol={_rs_vol_ratio:.1f}x",
                     300)
                return "entry", f"RS-Breakdown {rs_4h:.1f}% vs BTC (vol {_rs_vol_ratio:.1f}x)", price

        # OVERRIDE: rimbalzo su trend BEAR (mean reversion speculare al LONG)
        # Attivo solo in BEAR con 4h ancora down e RSI 1h fortemente ipercomprato
        # Cattura i rimbalzi tecnici senza aspettare la conferma lagging degli EMA/MACD
        if (_btc_favorable_short
                and ema200_slope is not None and ema200_slope < 0   # 4h ancora in downtrend
                and rsi1h is not None and rsi1h > 68                # RSI 1h ipercomprato
                and adx1h is not None and adx1h > 18                # il movimento ha forza, non è rumore
                and price < last["ema200"]                          # prezzo sotto EMA200 60m (non in recupero strutturale)
                and RISK_THROTTLE_LEVEL == 0                        # nessun drawdown attivo
                and (funding_rate is None or funding_rate >= FUNDING_SHORT_MIN)):  # no funding estremo
            tlog(f"pullback_short:{symbol}",
                 f"[PULLBACK-OVERRIDE][SHORT] {symbol} | rsi1h={rsi1h:.1f} adx1h={adx1h:.1f} ema200_slope={ema200_slope:.4f} → ingresso rimbalzo BEAR",
                 300)
            _rb_parts = [f"Rimbalzo BEAR RSI{rsi1h:.0f} (1h)"]
            if rsi1h > 72: _rb_parts.append("Overbought Estremo")
            if adx1h is not None and adx1h > 25: _rb_parts.append(f"Trend Forte({adx1h:.0f})")
            return "entry", ", ".join(_rb_parts), price

        # Segnale uscita (chiudi SHORT se eccesso di compressione)
        cond_exit1 = last["Close"] > last["bb_upper"] and last["rsi"] > 55
        def can_exit(symbol, current_price):
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price")
            entry_time = entry.get("entry_time")
            if not entry_price or not entry_time:
                return True
            r = abs(current_price - entry_price) / (entry_price * INITIAL_STOP_LOSS_PCT)
            holding_min = (time.time() - entry_time) / 60
            # FIX2: uscita solo dopo 90 minuti (allineato a candele 60m) o se molto in perdita
            return (r > 0.5) or (holding_min > 90)

        if cond_exit1 and can_exit(symbol, price):
            return "exit", "Breakout BB + RSI (bullish)", price

        exit_1h = False
        try:
            df_1h = fetch_history(symbol, interval=60)
            if df_1h is not None and len(df_1h) > 2:
                macd_1h = MACD(close=df_1h["Close"])
                df_1h["macd"] = macd_1h.macd()
                df_1h["macd_signal"] = macd_1h.macd_signal()
                df_1h["adx"] = ADXIndicator(high=df_1h["High"], low=df_1h["Low"], close=df_1h["Close"]).adx()
                last_1h = df_1h.iloc[-1]
                if last_1h["macd"] > last_1h["macd_signal"] and last_1h["adx"] > adx_threshold:
                    exit_1h = True
        except Exception:
            exit_1h = False

        if last["macd"] > last["macd_signal"] and last["adx"] > adx_threshold and exit_1h and can_exit(symbol, price):
            return "exit", "MACD bullish + ADX", price

        return None, None, None
    except Exception as e:
        log(f"Errore analisi SHORT {symbol}: {e}")
        return None, None, None

log("🔄 Avvio sistema di monitoraggio segnali reali")
notify_telegram("🤖 BOT [SHORT] AVVIATO - In ascolto per segnali di ingresso/uscita")

TEST_MODE = False  # Acquisti e vendite normali abilitati

def _sync_tp_order_short(symbol: str, tp_price: float, full_qty: float):
    """Verifica se esiste un TP Limit attivo su Bybit per la posizione SHORT; se mancante, lo ricrea."""
    try:
        resp = _bybit_signed_get("/v5/order/realtime", {"category": "linear", "symbol": symbol})
        orders = resp.json().get("result", {}).get("list", [])
        has_tp = any(
            o.get("side") == "Buy"
            and o.get("orderType") == "Limit"
            and str(o.get("reduceOnly", "false")).lower() == "true"
            and int(o.get("positionIdx", 0)) == SHORT_IDX
            for o in orders
        )
        if has_tp:
            log(f"[SYNC-TP][SHORT] {symbol}: TP Limit già attivo su Bybit, skip")
            return
        qty_tp1 = max(0.0, full_qty * TP1_PARTIAL)
        ok, oid = place_takeprofit_short(symbol, tp_price, qty_tp1)
        if ok:
            log(f"[SYNC-TP][SHORT] {symbol}: TP ripiazzato @ {tp_price:.6f} qty={qty_tp1:.4f} orderId={oid}")
            if symbol in position_data:
                position_data[symbol]["tp_order_id"] = oid
        else:
            log(f"[SYNC-TP][SHORT] {symbol}: TP mancante, ripiazzo FALLITO @ {tp_price:.6f}")
    except Exception as e:
        log(f"[SYNC-TP][SHORT] {symbol} errore: {e}")

def sync_positions_from_wallet():
    log("[SYNC] Avvio scansione posizioni short DAL CONTO (tutti i simboli linear)...")
    trovate = 0
    _saved_state = load_positions_state()  # carica stato ratchet pre-restart
    # Legge l'elenco completo posizioni aperte
    endpoint = f"{BYBIT_BASE_URL}/v5/position/list"
    params = {"category": "linear", "settleCoin": "USDT"}
    from urllib.parse import urlencode
    query_string = urlencode(sorted(params.items()))
    ts = str(int(time.time() * 1000))
    recv_window = "10000"
    sign_payload = f"{ts}{KEY}{recv_window}{query_string}"
    sign = hmac.new(SECRET.encode(), sign_payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": KEY,
        "X-BAPI-SIGN": sign,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window
    }
    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        data = resp.json()
        if data.get("retCode") == 0:
            pos_list = data.get("result", {}).get("list", [])
        else:
            log(f"[SYNC] ⚠️ Bybit pos/list retCode={data.get('retCode')} msg={data.get('retMsg')} — fallback su ASSETS")
            pos_list = []
    except Exception as _e:
        log(f"[SYNC] ⚠️ Eccezione fetch pos/list: {_e} — fallback su ASSETS")
        pos_list = []

    # Per ciascuna posizione short aperta
    # IMPORTANT: usa i simboli da Bybit (pos_list) se disponibili, altrimenti ASSETS.
    # Non usare solo ASSETS come fallback: al riavvio ASSETS potrebbe non contenere i simboli aperti.
    bybit_open_symbols = {p["symbol"] for p in pos_list if p.get("side") == "Sell" and float(p.get("size", 0) or 0) > 0}
    symbols = bybit_open_symbols if bybit_open_symbols else set(ASSETS)
    for symbol in symbols:
        if symbol == "USDT":
            continue
        # PATCH: log dettagliato per ogni asset
        qty = get_open_short_qty(symbol)
        if LOG_DEBUG_SYNC:
            log(f"[SYNC-DEBUG] {symbol}: qty short trovata = {qty}")
        if qty and qty > 0:
            price = get_last_price(symbol)
            if not price:
                continue
            add_open(symbol)
            # dentro sync_positions_from_wallet(), prima di calcolare tp/sl:
            try:
                pos = next(p for p in pos_list if p.get("symbol") == symbol and p.get("side") == "Sell")
                entry_price = float(pos.get("avgPrice") or price)
            except StopIteration:
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
            tp = price - (atr_val * TP_FACTOR)
            sl_atr = entry_price + (atr_val * SL_FACTOR)       # riferimento entry
            _hard_cap = MAX_LOSS_CAP_PCT if symbol in VOLATILE_ASSETS else MAX_LOSS_CAP_PCT_STABLE
            sl_hard_ceil = entry_price * (1.0 + _hard_cap)     # hard cap emergenza
            final_sl = min(sl_atr, sl_hard_ceil)

            # Recupera MFE ROI dal movimento attuale (entry vs price, SHORT: entry > price = profitto)
            price_move_pct = ((entry_price - price) / max(1e-9, entry_price)) * 100.0
            roi_now = price_move_pct * DEFAULT_LEVERAGE
            recovered_mfe_roi = max(0.0, roi_now)  # stima conservativa: MFE = ROI attuale

            # Stima floor ROI dalla posizione attuale (per non riscrivere SL troppo lontano)
            recovered_floor_roi = _pick_floor_roi_short(recovered_mfe_roi)
            if recovered_floor_roi is not None:
                delta_pct_floor = (recovered_floor_roi / max(1, DEFAULT_LEVERAGE)) / 100.0
                floor_price_recovered = entry_price * (1.0 - delta_pct_floor) * (1.0 + FLOOR_BUFFER_PCT)
                # Usa il floor recuperato se è più basso dello SL ATR-based (più protettivo)
                if floor_price_recovered < final_sl:
                    final_sl = floor_price_recovered

            # Recupera trailing_active dall'exchange: se Bybit ha un trailing già impostato
            trailing_already_active = False
            try:
                pos_detail = next((p for p in pos_list if p.get("symbol") == symbol and p.get("side") == "Sell"), None)
                if pos_detail and float(pos_detail.get("trailingStop", 0) or 0) > 0:
                    trailing_already_active = True
            except Exception:
                pass

            position_data[symbol] = {
                "entry_price": entry_price,
                "tp": tp,
                "sl": final_sl,
                "entry_cost": entry_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": trailing_already_active,
                "p_min": price,
                "mfe_roi": recovered_mfe_roi,
                "floor_roi": recovered_floor_roi,
                "usdt_floor_locked": recovered_floor_roi is not None,  # non riscrivere cassaforte se ratchet già attivo
            }
            # Ripristina mfe_roi/floor_roi storici dal file se migliori del valore attuale
            saved = _saved_state.get(symbol, {})
            saved_entry = saved.get("entry_price")
            if saved_entry and abs(float(saved_entry) - float(entry_price)) / float(entry_price) < 0.001:
                # stessa posizione (entry price entro 0.1%)
                if saved.get("mfe_roi", 0) > recovered_mfe_roi:
                    position_data[symbol]["mfe_roi"] = saved["mfe_roi"]
                    log(f"[SYNC-STATE][SHORT] {symbol} MFE ripristinato: {saved['mfe_roi']:.1f}% (era {recovered_mfe_roi:.1f}%)")
                if saved.get("floor_roi") and (recovered_floor_roi is None or saved["floor_roi"] > recovered_floor_roi):
                    position_data[symbol]["floor_roi"] = saved["floor_roi"]
                    position_data[symbol]["floor_updated_ts"] = saved.get("floor_updated_ts", 0)
                    log(f"[SYNC-STATE][SHORT] {symbol} FloorROI ripristinato: {saved['floor_roi']:.1f}%")
            trovate += 1
            log(f"[SYNC] Posizione trovata: {symbol} qty={qty} entry={entry_price:.4f} SL={final_sl:.4f} TP={tp:.4f}")
            set_position_stoploss_short(symbol, final_sl)
            place_conditional_sl_short(symbol, final_sl, qty, trigger_by="MarkPrice")
            # Sync TP: verifica e ripristina il TP order se mancante su Bybit
            tp_sync_price = entry_price - (TP1_R * atr_val * SL_ATR_MULT)
            _sync_tp_order_short(symbol, tp_sync_price, qty)
            # >>> PATCH: BE-LOCK immediato se già oltre soglia al riavvio (SHORT)
            try:
                if price <= entry_price * (1.0 - BREAKEVEN_LOCK_PCT) and not position_data[symbol].get("be_locked"):
                    be_price = entry_price * (1.0 - BREAKEVEN_BUFFER)  # SHORT: stop SOTTO entry (profitto garantito)
                    qty_live = get_open_short_qty(symbol)
                    if qty_live and qty_live > 0:
                        place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                        set_position_stoploss_short(symbol, be_price)
                        position_data[symbol]["be_locked"] = True
                        position_data[symbol]["be_price"] = be_price
                        tlog(f"be_lock_sync:{symbol}", f"[BE-LOCK-SYNC][SHORT] SL→BE {be_price:.6f}", 300)
            except Exception as e:
                tlog(f"be_lock_sync_exc:{symbol}", f"[BE-LOCK-SYNC][SHORT] exc: {e}", 300)

    log(f"[SYNC] Totale posizioni short recuperate dal wallet: {trovate}")

# --- Esegui sync all'avvio ---

# Aggiorna la lista asset all'avvio
update_assets()
sync_positions_from_wallet()

# Ripristina cooldown state post-loss dal file (sopravvive ai restart)
try:
    _cd_state = load_positions_state().get("__cooldown__", {})
    for _sym, _v in _cd_state.items():
        if "last_exit_time" in _v:
            last_exit_time[_sym] = float(_v["last_exit_time"])
        if "recent_losses" in _v:
            recent_losses[_sym] = int(_v["recent_losses"])
    if _cd_state:
        log(f"[COOLDOWN-RESTORE] Ripristinati cooldown per {list(_cd_state.keys())}")
except Exception as _e:
    log(f"[COOLDOWN-RESTORE] Errore: {_e}")

def get_usdt_balance() -> float:
    return get_free_qty("USDT")

import threading

 

# --- LOGICA 70/30 SU VALORE TOTALE PORTAFOGLIO (USDT + coin) ---
def get_portfolio_value():
    """
    Restituisce (equity totale reale, saldo USDT disponibile, esposizione per simbolo).
    Equity viene presa da /v5/account/wallet-balance per evitare gonfiaggi dovuti al notional.
    """
    try:
        resp = _bybit_signed_get("/v5/account/wallet-balance", {"accountType": BYBIT_ACCOUNT_TYPE})
        data = resp.json()
        acct = data.get("result", {}).get("list", [{}])[0]
        total_equity = float(acct.get("totalEquity") or acct.get("totalAvailableBalance") or 0.0)
        usdt_balance = 0.0
        for c in acct.get("coin", []):
            if c.get("coin") == "USDT":
                usdt_balance = float(c.get("availableToWithdraw") or c.get("availableBalance") or c.get("walletBalance") or 0.0)
                break
    except Exception:
        total_equity = get_usdt_balance() or 0.0
        usdt_balance = total_equity

    coin_values = {}
    symbols = set(ASSETS) | set(open_positions)
    for symbol in symbols:
        if symbol == "USDT":
            continue
        qty = get_open_short_qty(symbol)
        price = get_last_price(symbol)
        if qty and qty > 0 and price:
            coin_values[symbol] = qty * price

    return total_equity, usdt_balance, coin_values

 

# >>> PATCH: avvio worker di breakeven lock (SHORT)
be_lock_thread_short = threading.Thread(target=breakeven_lock_worker_short, daemon=True)
be_lock_thread_short.start()
profit_floor_thread_short = threading.Thread(target=profit_floor_worker_short, daemon=True)
profit_floor_thread_short.start()

def sl_watchdog_worker_short():
    """
    Worker di sicurezza: ogni 5 minuti controlla che ogni posizione SHORT aperta
    abbia il position-level SL impostato su Bybit. Se manca, usa solo
    set_position_stoploss_short (nessun conditional order per evitare cancel_all loop).
    """
    log("[SL-WATCHDOG][SHORT] Worker avviato")
    while True:
        try:
            time.sleep(300)  # ogni 5 minuti
            params = {"category": "linear", "settleCoin": "USDT"}
            resp = _bybit_signed_get("/v5/position/list", params)
            data = resp.json() if hasattr(resp, 'json') else {}
            if data.get("retCode") != 0:
                continue
            pos_list = data.get("result", {}).get("list", [])
            for pos in pos_list:
                if pos.get("side") != "Sell":
                    continue
                qty = float(pos.get("size", 0) or 0)
                if qty <= 0:
                    continue
                symbol = pos.get("symbol", "")
                sl_val = float(pos.get("stopLoss", 0) or 0)
                if sl_val > 0:
                    continue  # SL position-level già impostato, ok
                # SL mancante: calcola e reimpianta SOLO position-level (no conditional per evitare loop)
                entry = position_data.get(symbol)
                if not entry:
                    continue
                sl_price = entry.get("sl")
                if not sl_price:
                    entry_price = float(entry.get("entry_price", 0))
                    r_dist = float(entry.get("r_dist", entry_price * 0.04))
                    _hard_cap = MAX_LOSS_CAP_PCT if symbol in VOLATILE_ASSETS else MAX_LOSS_CAP_PCT_STABLE
                    sl_price = min(entry_price + r_dist, entry_price * (1.0 + _hard_cap))
                cur = get_last_price(symbol)
                if cur and sl_price <= cur:
                    sl_price = cur * (1.0 + 0.02)
                ok_pos = set_position_stoploss_short(symbol, sl_price)
                if ok_pos:
                    log(f"[SL-WATCHDOG][SHORT] ✅ SL reimpostato su {symbol} @ {sl_price:.6f}")
                    notify_telegram(f"⚠️ [SL-WATCHDOG] SL mancante rilevato e reimpostato\n{symbol} SHORT @ {sl_price:.4f}")
                else:
                    log(f"[SL-WATCHDOG][SHORT] 🚨 SL REIMPOSTAZIONE FALLITA su {symbol} @ {sl_price:.6f}")
                    notify_telegram(f"🚨 [SL-WATCHDOG] SL MANCANTE e FALLITO su {symbol}!\nEntry={entry.get('entry_price'):.4f} SL target={sl_price:.4f}\n⚠️ INTERVIENI MANUALMENTE")
        except Exception as _e:
            log(f"[SL-WATCHDOG][SHORT] Eccezione: {_e}")

sl_watchdog_thread_short = threading.Thread(target=sl_watchdog_worker_short, daemon=True)
sl_watchdog_thread_short.start()

while True:
    # Aggiorna la lista asset dinamicamente ogni ciclo
    update_assets()
    _update_daily_anchor_and_btc_context()
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()

     # >>> PATCH: throttle DD (selettivo, niente stop forzato salvo ENABLE_DD_PAUSE=1)
    if _daily_start_equity:
        dd_pct = (portfolio_value - _daily_start_equity) / max(1e-9, _daily_start_equity)
        if ENABLE_DD_PAUSE and dd_pct < -DAILY_DD_CAP_PCT:
            tlog("dd_cap", f"🛑 DD giornaliero {-dd_pct*100:.2f}% > cap {DAILY_DD_CAP_PCT*100:.1f}%, stop nuovi SHORT per {DD_PAUSE_MINUTES}m", 600)
            _trading_paused_until = time.time() + DD_PAUSE_MINUTES * 60
        else:
            draw = -dd_pct
            RISK_THROTTLE_LEVEL = 2 if draw > DAILY_DD_CAP_PCT * 2 else (1 if draw > DAILY_DD_CAP_PCT else 0)
            if RISK_THROTTLE_LEVEL > 0:
                tlog("dd_throttle", f"[THROTTLE] DD={draw*100:.2f}% → livello={RISK_THROTTLE_LEVEL}", 600)
    # Gap3: weekly drawdown check
    _weekly_block = _check_weekly_dd_short(portfolio_value)
    if _weekly_block:
        tlog("weekly_dd", f"[WEEKLY-DD] ⛔ Protezione settimanale attiva, skip nuovi SHORT", 600)

    # sync_positions_from_wallet()  # evita di resettare position_data/trailing ad ogni ciclo
    portfolio_value, usdt_balance, coin_values = get_portfolio_value()
    # SHORT: più conservativo sui volatili, più aggressivo su large cap
    volatile_budget = portfolio_value * 0.4  # Era 0.7
    stable_budget = portfolio_value * 0.6    # Era 0.3
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
    if LOG_DEBUG_PORTFOLIO:
        tlog("portfolio", f"[PORTAFOGLIO] Totale: {portfolio_value:.2f} USDT | Volatili: {volatile_invested:.2f} ({perc_volatile:.1f}%) | Meno volatili: {stable_invested:.2f} ({perc_stable:.1f}%) | USDT: {usdt_balance:.2f}", 900)
    tlog("portfolio_short", f"[PORTAFOGLIO] equity={portfolio_value:.2f} USDT | pos={len(open_positions)} | liberi={usdt_balance:.2f} | btc_fav={_btc_favorable_short}", 900)

    # Analisi in parallelo con prefiltraggio
    eligible_symbols = [s for s in ASSETS if s not in STABLECOIN_BLACKLIST and is_symbol_linear(s)]
    log(f"[CICLO][SHORT] simboli={len(eligible_symbols)} | pos_aperte={len(open_positions)} | btc_fav={_btc_favorable_short} | equity={portfolio_value:.2f}")
    results = {}
    if eligible_symbols:
        with ThreadPoolExecutor(max_workers=4) as ex:
            future_map = {ex.submit(analyze_asset, s): s for s in eligible_symbols}
            for fut in as_completed(future_map):
                s = future_map[fut]
                try:
                    results[s] = fut.result()
                except Exception as e:
                    tlog(f"analyze_exc:{s}", f"[ANALYZE-EXC] {s} {e}", 300)
    for symbol in eligible_symbols:
        signal, strategy, price = results.get(symbol, (None, None, None))
        if signal is None or strategy is None or price is None:
            continue
        log(f"📊 ANALISI: {symbol} → Segnale: {signal}, Strategia: {strategy}, Prezzo: {price}")

        # ✅ ENTRATA SHORT
        if signal == "entry":
            # GATE: blocca solo le NUOVE APERTURE (non le uscite)
            if ENABLE_DD_PAUSE and time.time() < _trading_paused_until:
                 tlog(f"paused:{symbol}", f"[PAUSE] trading sospeso (DD cap), skip SHORT {symbol}", 600)
                 continue
            # Gap3: weekly DD cap gate
            if _weekly_block:
                tlog(f"weekly_block:{symbol}", f"[WEEKLY-DD] protezione settimanale attiva, skip SHORT {symbol}", 600)
                continue
            # BEAR-GATE: SHORT apre se almeno UNO tra 4h o daily è in downtrend.
            # Il doppio lock (4h AND daily) era troppo restrittivo: in un mercato laterale
            # BTC oscilla e il bot non apriva mai. Basta che il daily sia ribassista.
            if not (_btc_favorable_short or _btc_daily_down_short):
                tlog(f"bear_gate:{symbol}", f"[BEAR-GATE][SHORT] né 4h né daily in downtrend, skip {symbol}", 600)
                continue
            # Uptrend guard allentato: blocca solo se BTC pump attivo (15m), non su EMA200 4h.
            # L'EMA200 4h si attiva su qualsiasi rimbalzo tecnico e bloccava per ore inutilmente.
            # Il pump guard 15m è sufficiente come protezione contro rimbalzi improvvisi.
            # Fix #1: pump guard — BTC sta salendo velocemente, skip nuove aperture SHORT
            if _btc_pumping_short:
                tlog(f"pump_gate:{symbol}", f"[PUMP-GATE][SHORT] BTC pump attivo, skip entry {symbol}", 300)
                continue
            # if CURRENT_REGIME == "BULL":
            #     tlog(f"reg_gate:{symbol}", f"[REGIME-GATE] BULL → skip SHORT {symbol}", 600)
            #     continue
            #     Regime: niente blocco. In BULL verranno già irrigiditi i filtri a monte (analyze_asset).

            # Cooldown: 12h se 2+ loss consecutive, 4h post-loss singola, 1h post-win
            if symbol in last_exit_time:
                elapsed = time.time() - last_exit_time[symbol]
                if last_exit_was_loss.get(symbol):
                    consec = recent_losses.get(symbol, 1)
                    cd_min = COOLDOWN_MINUTES * 12 if consec >= 2 else COOLDOWN_MINUTES * 4  # 12h o 4h
                else:
                    cd_min = COOLDOWN_MINUTES
                if elapsed < cd_min * 60:
                    tlog(f"cooldown:{symbol}", f"⏳ Cooldown {'post-loss' if last_exit_was_loss.get(symbol) else 'post-win'} attivo per {symbol} ({elapsed:.0f}s / {cd_min*60:.0f}s), salto ingresso", 300)
                    continue

            if len(open_positions) >= MAX_OPEN_POSITIONS:
                tlog(f"maxpos", f"[MAX-POS] {len(open_positions)}/{MAX_OPEN_POSITIONS} posizioni aperte, skip {symbol}", 300)
                continue
            if symbol in open_positions:
                tlog(f"inpos:{symbol}", f"⏩ Ignoro apertura short: già in posizione su {symbol}", 600)
                continue

            # Se c’è già una posizione LONG aperta (altro bot), non aprire lo SHORT sullo stesso simbolo
            if get_open_long_qty(symbol) > 0:
                tlog(f"opp_side:{symbol}", f"[SKIP] {symbol} ha LONG aperto, salto SHORT", 300)
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

            group_available = max(0.0, group_budget - group_invested)
            # [BUDGET] rimosso per ridurre rumore log: usare LOG_DEBUG_PORTFOLIO per dettagli

            # 📊 Valuta la forza del segnale in base alla strategia (usata solo come attenuatore 0.5-1.0)
            weights_no_tf = {
                # Nuovi nomi (confluenza)
                "EMA Bearish": 0.75,
                "MACD Bearish": 0.70,
                "RSI Bearish": 0.60,
                "ADX Trend": 0.85,
                # Vecchi nomi (compatibilità)
                "Breakdown BB": 1.00,
                "MACD bearish + ADX": 0.90,
                "Incrocio EMA 20/50": 0.75,
                "EMA20<EMA50": 0.70,
                "MACD bearish": 0.65,
                "Trend EMA+RSI": 0.60
            }
            parts = [p.strip().split(" (")[0] for p in (strategy or "").split(",") if p.strip()]
            if parts:
                base = max(weights_no_tf.get(p, 0.5) for p in parts)
                bonus = min(0.1 * (len(parts) - 1), 0.3)
                strength = min(1.0, base + bonus)
            else:
                strength = 0.5
            # >>> PATCH: throttle DD – riduci aggressività
            if RISK_THROTTLE_LEVEL == 1:
                strength *= 0.7
            elif RISK_THROTTLE_LEVEL >= 2:
                strength *= 0.5

            # --- Adatta la forza in base alla volatilità (ATR/Prezzo) ---
            df_hist = fetch_history(symbol)
            if df_hist is not None and "atr" in df_hist.columns and "Close" in df_hist.columns:
                last_hist = df_hist.iloc[-1]
                atr_val = last_hist["atr"]
                last_price = last_hist["Close"]
                atr_ratio = atr_val / last_price if last_price > 0 else 0
                # Hard skip: ATR > 8% del prezzo → SL ATR-based sarebbe >16% (160% ROI loss a 10x)
                # Questo filtra strutturalmente meme coin e asset in crash, senza blacklist hardcoded
                if atr_ratio > 0.08:
                    tlog(f"atr_volatile:{symbol}", f"[SKIP-ATR][SHORT] {symbol} ATR/prezzo={atr_ratio:.1%} > 8%, troppo volatile per SL ATR-based", 600)
                    continue
                elif atr_ratio > 0.04:
                    strength *= 0.75

            # --- Sizing basato sul rischio (ATR 4h e R) ---
            price_now_calc = get_last_price(symbol) or price
            df = fetch_history(symbol, interval=240)  # ATR su 4h per SL più stabile (meno noise)
            if df is None or len(df) < max(ATR_WINDOW+2, 20):
                tlog(f"no_hist:{symbol}", f"[SKIP] Storico 4h insufficiente per sizing ATR su {symbol}", 600)
                continue
            atr_series = AverageTrueRange(high=df["High"], low=df["Low"], close=df["Close"], window=ATR_WINDOW).average_true_range()
            atr_val = float(atr_series.iloc[-1]) if not pd.isna(atr_series.iloc[-1]) else 0.0
            if atr_val <= 0:
                tlog(f"atr_zero:{symbol}", f"[SKIP] ATR nullo per {symbol}", 600)
                continue
            r_dist = atr_val * SL_ATR_MULT
            risk_usdt = max(0.0, float(portfolio_value) * RISK_PCT)
            qty_target = risk_usdt / max(1e-9, r_dist)
            notional_target = qty_target * price_now_calc
            # Limiti: group budget e margine
            max_notional_by_margin = usdt_balance * DEFAULT_LEVERAGE * MARGIN_USE_PCT
            order_amount = min(notional_target * max(0.5, min(1.0, strength)), group_available, max_notional_by_margin, 1000.0)
            tlog(
                f"risk_sizing:{symbol}",
                f"[RISK] {symbol} ATR={atr_val:.6f} r_dist={r_dist:.6f} riskUSDT={risk_usdt:.2f} notional={order_amount:.2f}",
                300,
            )

            # BLOCCO: non tentare short se order_amount < min_order_amt
            info_i = get_instrument_info(symbol)
            min_order_amt = float(info_i.get("min_order_amt", 5))
            min_qty = float(info_i.get("min_qty", 0.0))
            price_now_chk = get_last_price(symbol) or 0.0
            min_notional = max(min_order_amt, (min_qty or 0.0) * price_now_chk)
            if order_amount < min_notional:
                bump = min_notional * 1.01  # +1% cuscinetto
                max_by_margin = max_notional_by_margin
                if max_by_margin >= bump:
                    old = order_amount
                    order_amount = min(bump, max_by_margin, 1000.0)
                    tlog(f"bump_notional:{symbol}", f"[BUMP-NOTIONAL][{symbol}] alzato notional da {old:.2f} a {order_amount:.2f} per rispettare min_qty/min_notional", 600)
                else:
                    tlog(f"min_notional:{symbol}", f"❌ Notional richiesto {order_amount:.2f} < minimo {min_notional:.2f} per {symbol} (min_qty={min_qty}, price={price_now_chk})", 300)
                    continue

            # Guard: se min_qty impone rischio reale > 1.5× atteso, il token è troppo caro per il sizing → skip
            if min_qty > 0 and (min_qty * r_dist) > risk_usdt * MAX_MIN_QTY_RISK_FACTOR:
                _real_risk = min_qty * r_dist
                tlog(f"minqty_risk:{symbol}", f"[SKIP-MINQTY][SHORT] {symbol} min_qty={min_qty} × r_dist={r_dist:.4f} = rischio reale {_real_risk:.4f} USDT > {risk_usdt * MAX_MIN_QTY_RISK_FACTOR:.4f} (1.5× budget), skip", 600)
                continue

            # Logga la quantità calcolata PRIMA dell'apertura short
            qty_str = calculate_quantity(symbol, order_amount)
            if LOG_DEBUG_STRATEGY:
                log(f"[DEBUG-ENTRY] Quantità calcolata per {symbol} con {order_amount:.2f} USDT: {qty_str}")
            if not qty_str:
                log(f"❌ Quantità non valida per short di {symbol}")
                continue

            if TEST_MODE:
                log(f"[TEST_MODE] SHORT inibiti per {symbol}")
                continue

            # APERTURA SHORT
            qty = market_short(symbol, order_amount, qty_exact=qty_str)
            if not qty or qty == 0:
                log(f"❌ Nessuna quantità shortata per {symbol}. Non registro la posizione.")
                continue
            # >>> TP1 a 1R (parziale) e SL tramite trading-stop
            # TP1_R regime-aware: in BTC favorevole lascia correre (2.5R), altrimenti prende profitto prima
            _tp1_r = TP1_R if _btc_favorable_short else 1.8
            tp_oid = None
            price_now = get_last_price(symbol) or price
            tp1_price = price_now - (_tp1_r * r_dist)
            qty_tp1 = max(0.0, qty * TP1_PARTIAL)
            if qty_tp1 > 0:
                ok_tp, tp_oid = place_takeprofit_short(symbol, tp1_price, qty_tp1)
                if ok_tp:
                    tlog(f"tp1_short:{symbol}", f"[TP1] {symbol} tp1={tp1_price:.6f} qty={qty_tp1}", 60)
            # SL basato su ATR (adattivo alla volatilità reale dell'asset)
            # Hard cap solo come fallback di emergenza se ATR è fuori range
            _hard_cap = MAX_LOSS_CAP_PCT if symbol in VOLATILE_ASSETS else MAX_LOSS_CAP_PCT_STABLE
            sl_hard_ceil = price_now * (1.0 + _hard_cap)
            final_sl = min(price_now + r_dist, sl_hard_ceil)
            ok_pos_sl = set_position_stoploss_short(symbol, final_sl)
            # Backup: piazza anche uno Stop-Market reduceOnly
            ok_cond_sl = False
            try:
                ok_cond_sl = place_conditional_sl_short(symbol, final_sl, qty, trigger_by="MarkPrice")
            except Exception as e:
                log(f"[SL-FAIL][SHORT] {symbol} eccezione conditional SL: {e}")
            if not ok_pos_sl and not ok_cond_sl:
                log(f"🚨 [SL-FAIL][SHORT] {symbol} NESSUN SL impostato! Entry={price_now:.6f} SL={final_sl:.6f}")
                notify_telegram(f"🚨 SL NON IMPOSTATO {symbol} SHORT!\nEntry={price_now:.4f} SL target={final_sl:.4f}\n⚠️ IMPOSTA MANUALMENTE!")
            elif not ok_pos_sl:
                log(f"[SL-WARN][SHORT] {symbol} position-SL fallito, ma conditional SL OK")
            elif not ok_cond_sl:
                log(f"[SL-WARN][SHORT] {symbol} conditional SL fallito, ma position-SL OK")
            actual_cost = qty * price_now
            log(f"🟢 SHORT aperto per {symbol}. Investito effettivo: {actual_cost:.2f} USDT")

            # Niente conditional SL duplicati e niente trailing immediato; trailing sarà attivato più avanti se > 2R

            trail_threshold = price_now - (TRAIL_START_R * r_dist)
            log(f"[ENTRY-DETAIL] {symbol} | Entry: {price_now:.4f} | SL: {final_sl:.4f} | TP1: {tp1_price:.4f} | ATR: {atr_val:.4f} | Trail@≤{trail_threshold:.4f}")
            _trade_log("entry", symbol, "SHORT", entry_price=price_now, qty=qty, sl=final_sl, tp=tp1_price, r_dist=r_dist,
                       extra={"tp1_qty": qty_tp1})
            
            set_position(symbol, {
                "entry_price": price_now,
                "tp": tp1_price,
                "tp_order_id": tp_oid if 'tp_oid' in locals() else None,
                "sl_order_id": None,
                "sl": final_sl,
                "entry_cost": actual_cost,
                "qty": qty,
                "entry_time": time.time(),
                "trailing_active": False,
                "p_min": price_now,
                "r_dist": r_dist,
                "breakout_exempt": "BREAKOUT-EXEMPT" in (strategy or "")
            })
            add_open(symbol)
            _daily_trades_opened += 1
            notify_telegram(f"🟢📉 SHORT aperto per {symbol}\nPrezzo: {price_now:.4f}\nStrategia: {strategy}\nInvestito: {actual_cost:.2f} USDT\nSL: {final_sl:.4f}\nTP1: {tp1_price:.4f}\nScore: {len([s for s in strategy.split(',') if 'ADX' not in s and s.strip()]) + (1 if (_btc_favorable_short and _btc_daily_down_short) else 0)}/5")
            time.sleep(3)

        # 🔴 USCITA SHORT (EXIT) - INSERISCI QUI
        elif signal == "exit" and symbol in open_positions:
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", price)
            entry_cost = entry.get("entry_cost", ORDER_USDT)

            # Se ratchet floor o BE lock sono già attivi, il SL sul broker è già impostato
            # in territorio profittevole. Non chiudere con market order —
            # lascia che il SL del broker gestisca e permetti al trend di continuare.
            if entry.get("floor_roi") is not None or entry.get("be_locked"):
                tlog(f"exit_skip:{symbol}", f"[EXIT-SKIP][SHORT] {symbol} ratchet floor={entry.get('floor_roi')}% / be_locked={entry.get('be_locked')} attivo → ignoro exit signal, SL broker gestisce", 300)
                continue
            
            qty = get_open_short_qty(symbol)
            info = get_instrument_info(symbol)
            min_qty = info.get("min_qty", 0.0)
            qty_step = info.get("qty_step", 0.0)
            
            if qty is None or qty < min_qty or qty < qty_step:
                tlog(f"exit_cleanup:{symbol}", f"[CLEANUP][EXIT] {symbol}: qty troppo piccola ({qty} < min {min_qty})", 120)
                discard_open(symbol)
                last_exit_time[symbol] = time.time()  # cooldown anche se già chiusa dall'exchange
                with _state_lock:
                    position_data.pop(symbol, None)
                continue
            
            if qty <= 0:
                tlog(f"exit_fail_qty:{symbol}", f"[EXIT-FAIL] Nessuna qty short effettiva da ricoprire per {symbol}", 120)
                discard_open(symbol)
                last_exit_time[symbol] = time.time()  # cooldown anche se già chiusa dall'exchange
                with _state_lock:
                    position_data.pop(symbol, None)
                continue
            
            # Esegui chiusura
            resp = market_cover(symbol, qty)
            if resp and resp.status_code == 200 and resp.json().get("retCode") == 0:
                current_price = get_last_price(symbol)
                exit_value = current_price * qty
                pnl_gross = ((entry_price - current_price) / entry_price) * 100  # PnL SHORT corretto
                pnl = pnl_gross - (FEES_TAKER_PCT * 2 * 100.0)  # fee round-trip
                pnl_emoji = "📈" if pnl >= 0 else "📉"
                
                log(f"[EXIT-OK] Ricopertura completata per {symbol} | PnL: {pnl:.2f}%")
                notify_telegram(f"{pnl_emoji} Exit SHORT {symbol} a {current_price:.4f}\nStrategia: {strategy}\nPnL: {pnl:.2f}% (fee incluse)")
                _daily_trades_closed += 1
                _daily_pnl_sum += pnl
                record_exit(symbol, entry_price, current_price, "SHORT")
                try:
                    _expectancy_log(pnl, qty * entry_price, exit_value, maker_entry=False, maker_exit=False)
                except Exception:
                    pass
                _trade_log("exit", symbol, "SHORT", entry_price=entry_price, qty=qty, sl=entry.get("sl", 0.0), tp=entry.get("tp", 0.0), r_dist=entry.get("r_dist", 0.0), extra={"pnl_pct": pnl})
                # (Report Google Sheets rimosso)
                
                open_positions.discard(symbol)
                last_exit_time[symbol] = time.time()
                position_data.pop(symbol, None)
                if get_open_long_qty(symbol) == 0:
                    cancel_all_orders(symbol)
            else:
                log(f"[EXIT-FAIL] Ricopertura fallita per {symbol}")
                try:
                    log(f"[BYBIT ERROR] status={resp.status_code} resp={resp.json()}")
                except:
                    log(f"[BYBIT ERROR] status={resp.status_code} resp=non-json")

    # PATCH: rimuovi posizioni con saldo < 1 (polvere) anche nel ciclo principale
    for symbol in list(open_positions):
        saldo = get_open_short_qty(symbol)
        info = get_instrument_info(symbol)
        min_qty = info.get("min_qty", 0.0)
        # cleanup SOLO se lettura qty è valida e < min_qty
        if (saldo is not None) and (saldo < min_qty):
            tlog(f"ext_close:{symbol}", f"[CLEANUP][SHORT] {symbol} chiusa lato exchange (qty={saldo}). Cancello TP/SL.", 60)
            discard_open(symbol)
            entry = position_data.get(symbol, {})
            entry_price = entry.get("entry_price", get_last_price(symbol) or 0.0)
            exit_price = get_last_price(symbol) or 0.0
            record_exit(symbol, entry_price, exit_price, "SHORT")
            # Aggiorna contatori report giornaliero (SL/TP Bybit)
            try:
                if entry_price and exit_price:
                    pnl_raw_pct = ((float(entry_price) - float(exit_price)) / float(entry_price)) * 100.0
                    pnl_net_pct = pnl_raw_pct - (FEES_TAKER_PCT * 2 * 100.0)
                    _daily_trades_closed += 1
                    _daily_pnl_sum += pnl_net_pct
            except Exception:
                pass
            # Notifica Telegram chiusura da SL/TP Bybit
            try:
                if entry_price and exit_price:
                    pnl_pct = ((float(entry_price) - float(exit_price)) / float(entry_price)) * 100.0 * DEFAULT_LEVERAGE
                    floor_roi = entry.get("floor_roi")
                    floor_info = f"\nFloor ratchet: {floor_roi:.1f}%" if floor_roi else ""
                    notify_telegram(
                        f"🔴📉 Posizione SHORT chiusa da exchange (SL/TP)\n"
                        f"Simbolo: {symbol}\n"
                        f"Entry: {float(entry_price):.6f}\n"
                        f"Uscita: {float(exit_price):.6f}\n"
                        f"ROI stimato: {pnl_pct:.2f}%{floor_info}"
                    )
            except Exception as _tg_exc:
                log(f"[TELEGRAM-CLEANUP][SHORT] errore notifica {symbol}: {_tg_exc}")
            with _state_lock:
                position_data.pop(symbol, None)
            if get_open_long_qty(symbol) == 0:
                cancel_all_orders(symbol)
    
    # --- SAFETY: impone il BE se il worker non è riuscito a piazzarlo ---
    for symbol in list(open_positions):
        entry = position_data.get(symbol)
        if not entry or entry.get("be_locked"):
            continue
        price_now = get_last_price(symbol)
        if not price_now:
            continue
        entry_price = entry.get("entry_price", price_now)
        # SHORT: trigger BE se prezzo ≤ entry*(1 - 1%)
        if price_now <= entry_price * (1.0 - BREAKEVEN_LOCK_PCT):
            be_price = entry_price * (1.0 + BREAKEVEN_BUFFER)  # buffer negativo → sotto entry
            qty_live = get_open_short_qty(symbol)
            if qty_live and qty_live > 0:
                place_conditional_sl_short(symbol, be_price, qty_live, trigger_by="MarkPrice")
                set_position_stoploss_short(symbol, be_price)
                entry["be_locked"] = True
                entry["be_price"] = be_price
                set_position(symbol, entry)
                tlog(f"be_lock_safety:{symbol}", f"[BE-LOCK-SAFETY][SHORT] SL→BE {be_price:.6f}", 60)

    # Sicurezza: attesa tra i cicli principali
    # time.sleep(INTERVAL_MINUTES * 60)
    try:
        _send_daily_report()
    except Exception as _rep_exc:
        log(f"[DAILY-REPORT][ERRORE LOOP] {_rep_exc}")
    time.sleep(180)  # analizza ogni 3 minuti per ridurre carico API