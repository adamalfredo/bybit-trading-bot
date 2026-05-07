"""
backtest.py — Backtester vectorizzato per i segnali di ingresso del bot LONG.

Metodologia:
  - Scarica dati storici OHLCV 60m da Bybit per N giorni
  - Su ogni candela chiusa, calcola gli stessi indicatori di analyze_asset()
  - Simula ingresso quando i segnali si allineano (EMA, MACD, RSI, ADX, volume)
  - Gestisce uscita con SL = entry - 2×ATR, ratchet semplificato, exit signal
  - Produce report completo: WR, avg win/loss, profit factor, max drawdown, curva equity

Utilizzo:
  python backtest.py [--days 90] [--symbols BTCUSDT,ETHUSDT,...] [--sl-mult 2.0]

Output:
  - Tabella risultati per simbolo
  - Curva equity ASCII
  - Metriche aggregate: profit factor, expectancy, max drawdown
"""

import argparse
import sys
import time
import os
from datetime import datetime, timedelta, timezone
from typing import Optional

import pandas as pd
import numpy as np
import requests

from ta.volatility import BollingerBands, AverageTrueRange
from ta.trend import MACD, EMAIndicator, SMAIndicator, ADXIndicator
from ta.momentum import RSIIndicator

# ─────────────────────────────────────────────
# Parametri di default (stesso del bot live)
# ─────────────────────────────────────────────
BYBIT_BASE = "https://api.bybit.com"
DEFAULT_LEVERAGE     = 10
SL_ATR_MULT          = 2.0      # SL = entry - SL_ATR_MULT × ATR
TRAIL_START_R        = 1.0      # ratchet parte a +1R
TRAIL_ATR_MULT       = 1.3      # distanza trailing = 1.3×ATR
ATR_WINDOW           = 14
RSI_LONG_THRESHOLD   = 54.0
ENTRY_ADX_THRESH     = 24.0
MIN_CONFLUENCE       = 2
ATR_RATIO_MAX_ENTRY  = 0.08     # skip se ATR/prezzo > 8%
EXT_K                = 1.5      # prezzo max = EMA20 + EXT_K × ATR (filtro estensione)
COOLDOWN_BARS        = 3        # barre di cooldown dopo un'uscita
FEES_PCT             = 0.00055  # 0.055% taker per lato
LINEAR_MIN_TURNOVER  = 50_000_000

# Partial TP: chiude il 50% della posizione a +1.5R, sposta SL a BE sull'altra metà
PARTIAL_TP_R         = 1.5      # prendi profitto parziale quando il trade è a +1.5R
PARTIAL_TP_PCT       = 0.5      # chiudi questa percentuale della posizione

# Pullback entry: aspetta che il prezzo torni a EMA20 prima di entrare
PULLBACK_BARS_MAX    = 8        # max barre di attesa pullback (8×60m = 8 ore)
PULLBACK_ZONE_PCT    = 0.010    # tollera fino a +1% sopra EMA20 come zona valida

# Simboli di default: le coin più attive degli ultimi 90gg dal bot
DEFAULT_SYMBOLS = [
    "SOLUSDT","ETHUSDT","ADAUSDT","XRPUSDT","DOGEUSDT",
    "ORDIUSDT","LDOUSDT","AAVEUSDT","ORCAUSDT","PENGUUSDT",
    "HYPEUSDT","ENSOUSDT","TRUMPUSDT","GALAUSDT","TIAUSDT",
    "SUIUSDT","DYDXUSDT","MOVRUSDT","ZECUSDT","1000PEPEUSDT",
    "MNTUSDT","TAOUSDT","FARTCOINUSDT","BOMEUSDT","COMPUSDT",
]

# ─────────────────────────────────────────────
# Fetch dati storici
# ─────────────────────────────────────────────

def fetch_ohlcv(symbol: str, interval: int, days: int) -> Optional[pd.DataFrame]:
    """Scarica dati OHLCV da Bybit con paginazione. Interval in minuti.
    Bybit limita a 1000 candele per chiamata, quindi per 90gg su 60m
    (2160 candele) servono 3 chiamate paginate all'indietro.
    """
    needed = days * 24 * 60 // interval + 250  # +250 barre warmup indicatori
    url = f"{BYBIT_BASE}/v5/market/kline"
    all_rows: list = []
    end_ts: Optional[int] = None  # timestamp fine (ms) per paginazione

    while len(all_rows) < needed:
        params: dict = {
            "category": "linear",
            "symbol": symbol,
            "interval": str(interval),
            "limit": 1000,
        }
        if end_ts is not None:
            params["end"] = end_ts
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
        except Exception as e:
            print(f"  [WARN] fetch_ohlcv {symbol}: {e}")
            return None
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            break
        batch = data["result"]["list"]
        if not batch:
            break
        all_rows = batch + all_rows  # batch è già ordinato desc → inversione
        # La candela più vecchia del batch diventa il nuovo end_ts (esclusa)
        oldest_ts = int(batch[-1][0])  # ts in ms
        if end_ts is not None and oldest_ts >= end_ts:
            break  # nessun progresso → fermati
        end_ts = oldest_ts
        if len(batch) < 1000:
            break  # fine dati disponibili
        time.sleep(0.1)

    if not all_rows:
        return None
    try:
        df = pd.DataFrame(all_rows, columns=["ts","Open","High","Low","Close","Volume","turnover"])
        for c in ("Open","High","Low","Close","Volume","turnover"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["ts"] = pd.to_numeric(df["ts"]) // 1000
        df.sort_values("ts", inplace=True)
        df.drop_duplicates(subset="ts", inplace=True)
        df.reset_index(drop=True, inplace=True)
        df.dropna(subset=["Close"], inplace=True)
        return df
    except Exception as e:
        print(f"  [WARN] fetch_ohlcv parse {symbol}: {e}")
        return None


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Calcola tutti gli indicatori necessari su un DataFrame OHLCV."""
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]

    bb = BollingerBands(close=close)
    df["bb_upper"] = bb.bollinger_hband()
    df["bb_lower"] = bb.bollinger_lband()
    df["rsi"]      = RSIIndicator(close=close).rsi()
    df["ema20"]    = EMAIndicator(close=close, window=20).ema_indicator()
    df["ema50"]    = EMAIndicator(close=close, window=50).ema_indicator()
    df["ema200"]   = EMAIndicator(close=close, window=200).ema_indicator()
    df["sma20"]    = SMAIndicator(close=close, window=20).sma_indicator()
    macd_obj       = MACD(close=close)
    df["macd"]     = macd_obj.macd()
    df["macd_sig"] = macd_obj.macd_signal()
    df["adx"]      = ADXIndicator(high=high, low=low, close=close).adx()
    df["atr"]      = AverageTrueRange(high=high, low=low, close=close, window=ATR_WINDOW).average_true_range()
    df["vol_avg20"]    = df["Volume"].rolling(20).mean().shift(1)  # vol medio CHIUSO (shift=1)
    df["swing_high_20"] = df["High"].rolling(20).max().shift(1)   # massimo swing CHIUSO (resistance)
    df["swing_low_20"]  = df["Low"].rolling(20).min().shift(1)    # minimo swing CHIUSO (support)
    df.dropna(inplace=True)
    df.reset_index(drop=True, inplace=True)
    return df


# ─────────────────────────────────────────────
# Swing-level entry (nuova strategia strutturale)
# ─────────────────────────────────────────────

# Parametri nuova strategia
SWING_LOOKBACK        = 40    # barre 1h per identificare swing low/high locali (40h ≈ 2 giorni)
SWING_MIN_PROMINENCE  = 0.5   # il pivot deve essere più basso/alto dei SWING_LOOKBACK/2 vicini di almeno 0.5×ATR
SWING_NEAR_ATR        = 1.0   # entra se il prezzo è entro SWING_NEAR_ATR × ATR dal supporto
SWING_SL_BELOW_ATR   = 0.3   # SL = supporto - SWING_SL_BELOW_ATR × ATR (sotto il livello)
SWING_RSI_MAX         = 50.0  # RSI max al momento dell'ingresso (non in momentum già maturo)
SWING_RSI_MIN         = 28.0  # RSI min (non comprare in free-fall)
SWING_VOL_MIN         = 1.2   # volume corrente >= 1.2× media (domanda che assorbe)
SWING_RR_MIN          = 1.8   # R:R minimo: distanza a resistenza / distanza a SL
# SHORT swing
SWING_RSI_MIN_SHORT   = 45.0  # RSI min per swing SHORT (non già in crollo)
SWING_RSI_MAX_SHORT   = 72.0  # RSI max per swing SHORT (zona satura ribassista)


def precompute_swing_pivots(df: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """
    Precomputa pivot lows e pivot highs sull'intero dataframe con numpy.
    Ritorna due array booleani della stessa lunghezza di df:
      - is_pivot_low[i]  = True se df.iloc[i] è un pivot low locale
      - is_pivot_high[i] = True se df.iloc[i] è un pivot high locale

    Un pivot low è il minimo locale entro ±half barre.
    Viene calcolato una sola volta per df e poi usato da find_swing_levels
    tramite lookup O(SWING_LOOKBACK) invece del loop Python O(n²).
    """
    half = max(4, SWING_LOOKBACK // 8)
    lows  = df["Low"].to_numpy(dtype=float)
    highs = df["High"].to_numpy(dtype=float)
    n = len(lows)

    is_pivot_low  = np.zeros(n, dtype=bool)
    is_pivot_high = np.zeros(n, dtype=bool)

    for i in range(half, n - half):
        window_lo = lows[i - half: i + half + 1]
        window_hi = highs[i - half: i + half + 1]
        if lows[i]  == window_lo.min():
            is_pivot_low[i]  = True
        if highs[i] == window_hi.max():
            is_pivot_high[i] = True

    return is_pivot_low, is_pivot_high


def find_swing_levels(df: pd.DataFrame, idx: int,
                      pivot_lows: np.ndarray,
                      pivot_highs: np.ndarray) -> tuple[Optional[float], Optional[float]]:
    """
    Lookup O(SWING_LOOKBACK) del supporto e resistenza più vicini al prezzo corrente,
    usando gli array di pivot precomputati da precompute_swing_pivots().

    Ritorna (support_level, resistance_level) o (None, None) se non trovati.
    """
    price_now = float(df.iloc[idx - 1]["Close"])

    # Finestra: ultime SWING_LOOKBACK barre chiuse (esclusa la corrente)
    start = max(0, idx - SWING_LOOKBACK)
    end   = idx  # escluso

    # Estrai i valori pivot nella finestra tramite maschera numpy
    lows_in_window  = df["Low"].to_numpy(dtype=float)[start:end]
    highs_in_window = df["High"].to_numpy(dtype=float)[start:end]
    pl_mask = pivot_lows[start:end]
    ph_mask = pivot_highs[start:end]

    pivot_low_vals  = lows_in_window[pl_mask]
    pivot_high_vals = highs_in_window[ph_mask]

    # Supporto: pivot low più alto ancora sotto il prezzo
    below = pivot_low_vals[pivot_low_vals < price_now]
    support = float(below.max()) if len(below) > 0 else None

    # Resistenza: pivot high più basso ancora sopra il prezzo
    above = pivot_high_vals[pivot_high_vals > price_now]
    resistance = float(above.min()) if len(above) > 0 else None

    return support, resistance


def signal_entry_swing(df: pd.DataFrame, idx: int,
                       pivot_lows: np.ndarray,
                       pivot_highs: np.ndarray) -> tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """
    Nuova logica di ingresso strutturale basata su swing levels.

    Condizioni di ingresso:
    1. Prezzo vicino a un supporto (swing low) identificato su 1h
    2. RSI in zona di ipervenduto relativo (28-50): non in free-fall, non già partito
    3. Volume ≥ 1.2× media: domanda che assorbe a quel livello
    4. EMA200 (1h) in pendenza positiva: macro trend favorevole
    5. R:R naturale ≥ 1.8: resistenza abbastanza lontana
    6. Candela corrente chiude SOPRA il supporto (rimbalzo confermato)

    Ritorna (segnale, sl_price, tp_price, entry_price) oppure (False, None, None, None).
    """
    if idx < SWING_LOOKBACK + 5:
        return False, None, None, None

    row  = df.iloc[idx - 1]  # candela appena chiusa
    prev = df.iloc[idx - 2]

    price   = float(row["Close"])
    atr     = float(row["atr"])
    rsi     = float(row["rsi"])
    vol     = float(row["Volume"])
    vol_avg = float(row["vol_avg20"]) if row["vol_avg20"] > 0 else 1.0
    ema200  = float(row["ema200"])

    # Filtro macro: prezzo sopra EMA200 (non comprare in downtrend macro)
    if price < ema200 * 0.98:
        return False, None, None, None

    # EMA200 in salita (macro trend sano)
    ema200_prev = float(prev["ema200"])
    if ema200 < ema200_prev:
        return False, None, None, None

    # ATR filter: meme coin troppo volatili
    if atr / price > ATR_RATIO_MAX_ENTRY:
        return False, None, None, None

    # Cerca livelli swing (lookup O(SWING_LOOKBACK) su pivot precomputati)
    support, resistance = find_swing_levels(df, idx, pivot_lows, pivot_highs)
    if support is None or resistance is None:
        return False, None, None, None

    # 1. Prezzo vicino al supporto
    dist_to_support = price - support
    if dist_to_support > SWING_NEAR_ATR * atr:
        return False, None, None, None  # troppo lontano dal supporto

    # 2. RSI in zona reversal (non in free-fall, non già in momentum)
    if not (SWING_RSI_MIN <= rsi <= SWING_RSI_MAX):
        return False, None, None, None

    # 3. RSI sta risalendo (conferma della domanda che assorbe)
    rsi_prev = float(prev["rsi"])
    if rsi <= rsi_prev:
        return False, None, None, None

    # 4. Volume ≥ minimo (domanda reale)
    if vol < SWING_VOL_MIN * vol_avg:
        return False, None, None, None

    # 5. Candela chiude SOPRA il supporto (no breakdown confermato)
    if price <= support:
        return False, None, None, None

    # 6. Calcola SL naturale (sotto il supporto) e TP naturale (alla resistenza)
    sl_natural  = support - SWING_SL_BELOW_ATR * atr
    if sl_natural <= 0:
        return False, None, None, None
    sl_dist = price - sl_natural
    if sl_dist <= 0:
        return False, None, None, None

    tp_natural  = resistance
    rr_natural  = (tp_natural - price) / sl_dist

    # 7. R:R minimo
    if rr_natural < SWING_RR_MIN:
        return False, None, None, None

    return True, sl_natural, tp_natural, price


def signal_entry_swing_short(df: pd.DataFrame, idx: int,
                            pivot_lows: np.ndarray,
                            pivot_highs: np.ndarray) -> tuple[bool, Optional[float], Optional[float], Optional[float]]:
    """
    Logica di ingresso swing SHORT: entry su resistenza strutturale.

    Condizioni di ingresso:
    1. Prezzo vicino a una resistenza (swing high) identificata su 1h
    2. RSI in zona di ipercomprato relativo (45-72): non già in crollo, non in momentum
    3. RSI sta SCENDENDO (pressione ribassista confermata)
    4. Volume >= SWING_VOL_MIN × media
    5. EMA200 (1h) in pendenza negativa: macro trend ribassista
    6. Prezzo sotto EMA200 × 1.02 (non in recupero strutturale)

    Ritorna (segnale, sl_price, tp_price, entry_price) oppure (False, None, None, None).
    """
    if idx < SWING_LOOKBACK + 5:
        return False, None, None, None

    row  = df.iloc[idx - 1]  # candela appena chiusa
    prev = df.iloc[idx - 2]

    price    = float(row["Close"])
    atr      = float(row["atr"])
    rsi      = float(row["rsi"])
    vol      = float(row["Volume"])
    vol_avg  = float(row["vol_avg20"]) if row["vol_avg20"] > 0 else 1.0
    ema200   = float(row["ema200"])
    ema200_prev = float(prev["ema200"])
    rsi_prev = float(prev["rsi"])

    # Filtro macro: prezzo sotto EMA200 (non shortare in uptrend strutturale)
    if price > ema200 * 1.02:
        return False, None, None, None

    # EMA200 in discesa (macro trend ribassista confermato)
    if ema200 > ema200_prev:
        return False, None, None, None

    # ATR filter: meme coin troppo volatili
    if atr / price > ATR_RATIO_MAX_ENTRY:
        return False, None, None, None

    # Cerca livelli swing
    support, resistance = find_swing_levels(df, idx, pivot_lows, pivot_highs)
    if support is None or resistance is None:
        return False, None, None, None

    # 1. Prezzo vicino alla resistenza
    dist_to_resistance = resistance - price
    if dist_to_resistance > SWING_NEAR_ATR * atr:
        return False, None, None, None  # troppo lontano dalla resistenza

    # 2. RSI in zona ribassista (non già collassato, non ancora partito al ribasso)
    if not (SWING_RSI_MIN_SHORT <= rsi <= SWING_RSI_MAX_SHORT):
        return False, None, None, None

    # 3. RSI sta scendendo (pressione di vendita che prende controllo)
    if rsi >= rsi_prev:
        return False, None, None, None

    # 4. Volume >= minimo (vendita reale)
    if vol < SWING_VOL_MIN * vol_avg:
        return False, None, None, None

    # 5. Candela chiude SOTTO la resistenza (no breakout confermato)
    if price >= resistance:
        return False, None, None, None

    # 6. Calcola SL naturale (sopra la resistenza) e TP naturale (al supporto)
    sl_natural = resistance + SWING_SL_BELOW_ATR * atr  # SL sopra resistenza
    sl_dist    = sl_natural - price
    if sl_dist <= 0:
        return False, None, None, None

    tp_natural = support  # TP al supporto strutturale
    rr_natural = (price - tp_natural) / sl_dist

    # 7. R:R minimo
    if rr_natural < SWING_RR_MIN:
        return False, None, None, None

    return True, sl_natural, tp_natural, price


# Parametri filtro qualità ingresso
RR_MIN_ROOM   = 1.5   # room-to-run minima: distanza a swing high >= 1.5 × SL dist
RSI_ENTRY_MAX = 68.0  # non entrare se RSI già overbought (movimento maturo)

# ─────────────────────────────────────────────
# Logica segnale ingresso (identica al bot)
# ─────────────────────────────────────────────

def signal_entry(row, prev, sl_mult: float, ext_k: float = None) -> bool:
    """Ritorna True se la candela 'row' (chiusa) genera un segnale di ingresso LONG."""
    # ATR filter
    atr_ratio = row["atr"] / row["Close"] if row["Close"] > 0 else 0
    if atr_ratio > ATR_RATIO_MAX_ENTRY:
        return False

    # Indicatori di stato
    ema_state  = row["ema20"] > row["ema50"]
    macd_state = row["macd"] > row["macd_sig"]
    rsi_state  = row["rsi"] > RSI_LONG_THRESHOLD
    adx_ok     = row["adx"] > ENTRY_ADX_THRESH

    # Eventi cross (usando riga precedente)
    ema_cross  = (prev["ema20"] <= prev["ema50"]) and ema_state
    macd_cross = (prev["macd"] <= prev["macd_sig"]) and macd_state
    rsi_break  = (prev["rsi"] <= RSI_LONG_THRESHOLD) and rsi_state
    event      = ema_cross or macd_cross or rsi_break

    # Confluenza
    conf = sum([ema_state, macd_state, rsi_state])
    min_conf = MIN_CONFLUENCE + (1 if not event else 0)

    # Volume sopra 60% media
    vol_ok = (row["Volume"] / row["vol_avg20"]) >= 0.6 if row["vol_avg20"] > 0 else True

    # Prezzo non troppo esteso sopra EMA20
    _k = ext_k if ext_k is not None else EXT_K
    ext_cap = row["ema20"] + _k * row["atr"]
    ext_ok = row["Close"] <= ext_cap

    # ── FILTRI QUALITÀ INGRESSO ──────────────────────────────────────────
    # 1. Macro trend filter: il prezzo deve essere sopra EMA200 al momento
    #    dell'ingresso. Comprare sotto EMA200 significa comprare contro la
    #    tendenza macro (downtrend) → R:R strutturalmente sfavorevole.
    if row["Close"] < row["ema200"]:
        return False

    # 2. RSI cap: non entrare se il movimento è già maturo (RSI già overbought).
    #    Il segnale ottimale è RSI 54-68: in trend ma non ancora esausto.
    if row["rsi"] > RSI_ENTRY_MAX:
        return False
    # ────────────────────────────────────────────────────────────────────

    return event and (conf >= min_conf or conf >= MIN_CONFLUENCE) and adx_ok and vol_ok and ext_ok


def signal_exit(row, prev) -> bool:
    """Ritorna True se la candela genera segnale di uscita LONG."""
    macd_bear = (row["macd"] < row["macd_sig"]) and row["adx"] > ENTRY_ADX_THRESH
    bb_break  = (row["Close"] < row["bb_lower"]) and row["rsi"] < 45
    return macd_bear or bb_break


# ─────────────────────────────────────────────
# Regime BTC (macro filter)
# ─────────────────────────────────────────────

def fetch_btc_regime(days: int) -> pd.Series:
    """Scarica BTC daily e ritorna una Series bool indicizzata per giorno.
    True = BTC sopra EMA200 daily = regime bull = LONG consentiti.
    False = BTC sotto EMA200 daily = regime bear = LONG bloccati.
    """
    # Usiamo interval=D (daily) su Bybit: interval=1 giorno = 'D'
    url = f"{BYBIT_BASE}/v5/market/kline"
    needed = days + 250  # +250 barre warmup EMA200
    all_rows: list = []
    end_ts: Optional[int] = None

    while len(all_rows) < needed:
        params: dict = {
            "category": "linear",
            "symbol":   "BTCUSDT",
            "interval": "D",
            "limit":    1000,
        }
        if end_ts is not None:
            params["end"] = end_ts
        try:
            r = requests.get(url, params=params, timeout=15)
            data = r.json()
        except Exception:
            break
        if data.get("retCode") != 0 or not data.get("result", {}).get("list"):
            break
        batch = data["result"]["list"]
        if not batch:
            break
        all_rows = batch + all_rows
        oldest_ts = int(batch[-1][0])
        if end_ts is not None and oldest_ts >= end_ts:
            break
        end_ts = oldest_ts
        if len(batch) < 1000:
            break
        time.sleep(0.1)

    if not all_rows:
        return pd.Series(dtype=bool)  # se fallisce, non filtrare

    df = pd.DataFrame(all_rows, columns=["ts","Open","High","Low","Close","Volume","turnover"])
    df["Close"] = pd.to_numeric(df["Close"], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"]) // 1000
    df.sort_values("ts", inplace=True)
    df.drop_duplicates(subset="ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df.dropna(subset=["Close"], inplace=True)

    ema50 = EMAIndicator(close=df["Close"], window=50).ema_indicator()
    bull = df["Close"] > ema50
    # Indice = giorno (date) dalla colonna ts (unix seconds)
    bull.index = pd.to_datetime(df["ts"], unit="s").dt.date
    return bull


# ─────────────────────────────────────────────
# Simulazione trade su singolo simbolo
# ─────────────────────────────────────────────

def simulate(symbol: str, df: pd.DataFrame, sl_mult: float,
             initial_equity: float = 100.0, risk_pct: float = 0.012,
             trail_mult: float = TRAIL_ATR_MULT,
             btc_regime: Optional[pd.Series] = None,
             partial_tp: bool = True,
             pullback_entry: bool = False,
             tp_r: float = PARTIAL_TP_R,
             swing_entry: bool = False,
             direction: str = "long",
             ext_k: float = None) -> dict:
    """
    Simula tutti i trade su df. Gestisce SL ATR-based, exit signal, ratchet.
    partial_tp=True:    chiude il 50% a +1.5R e sposta SL a BE, lascia correre il resto.
    pullback_entry=True: NON entra subito al segnale; aspetta che il prezzo torni a
                         EMA20 (zona di supporto del trend) entro PULLBACK_BARS_MAX barre.
                         Vantaggio: entry price migliore, SL più stretto, R:R maggiore.
    swing_entry=True:   Nuova strategia strutturale. Ignora EMA/MACD cross e aspetta
                        che il prezzo si avvicini a un livello di supporto (swing low)
                        con RSI in risalita e volume di assorbimento. SL sotto supporto,
                        TP alla resistenza naturale. R:R sempre >= SWING_RR_MIN.
    Ritorna dict con metriche e lista trade.
    """
    trades = []
    equity = initial_equity
    equity_curve = [equity]
    in_trade = False
    entry_price = sl_price = trail_sl = 0.0
    entry_idx   = 0
    cooldown    = 0
    max_equity  = equity

    # Stato pending per pullback_entry
    pending      = False   # segnale scattato, in attesa del pullback
    pending_bars = 0       # barre trascorse dall'evento
    pending_btc_ok = True  # regime BTC al momento del segnale

    # Precomputa pivot per swing entry (una volta sola, O(n))
    if swing_entry:
        _pivot_lows, _pivot_highs = precompute_swing_pivots(df)
    else:
        _pivot_lows = _pivot_highs = None
    _is_short = (direction == "short")

    def _open_trade(ep, atr_val):
        """Helper: apre il trade da entry price ep, ritorna (ok, state_dict)."""
        sd = sl_mult * atr_val
        if sd <= 0 or ep - sd <= 0 or atr_val / ep > ATR_RATIO_MAX_ENTRY:
            return False, {}
        ps = (equity * risk_pct) / sd
        return True, {"entry_price": ep, "sl_price": ep - sd,
                      "pos_size": ps, "remaining_size": ps}

    for i in range(2, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i-1]

        equity_curve.append(equity)

        if not in_trade:
            if cooldown > 0:
                cooldown -= 1
                continue

            # ── MODALITÀ SWING ENTRY ──────────────────────────────────────
            if swing_entry:
                if _is_short:
                    ok, sl_swing, tp_swing, ep_swing = signal_entry_swing_short(df, i, _pivot_lows, _pivot_highs)
                    if not ok:
                        continue
                    # BTC regime filter SHORT: favorevole quando BTC è RIBASSISTA
                    if btc_regime is not None:
                        candle_date = pd.to_datetime(row["ts"], unit="s").date()
                        is_bull = btc_regime.get(candle_date, True)
                        if is_bull:  # SHORT: skip quando BTC è rialzista
                            continue
                    entry_price    = ep_swing
                    sl_price       = sl_swing  # sopra entry
                    tp_full        = tp_swing  # sotto entry (supporto)
                    r_dist_swing   = sl_price - entry_price  # positivo
                    pos_size       = (equity * risk_pct) / r_dist_swing
                    remaining_size = pos_size
                    in_trade       = True
                    entry_idx      = i
                    trail_sl       = sl_price
                    trail_active   = False
                    mfe            = entry_price
                    partial_done   = False
                    continue
                else:
                    ok, sl_swing, tp_swing, ep_swing = signal_entry_swing(df, i, _pivot_lows, _pivot_highs)
                    if not ok:
                        continue
                    # BTC regime filter LONG: favorevole quando BTC è RIALZISTA
                    if btc_regime is not None:
                        candle_date = pd.to_datetime(row["ts"], unit="s").date()
                        is_bull = btc_regime.get(candle_date, True)
                        if not is_bull:
                            continue
                    entry_price    = ep_swing
                    sl_price       = sl_swing
                    tp_full        = tp_swing
                    r_dist_swing   = entry_price - sl_price
                    pos_size       = (equity * risk_pct) / r_dist_swing
                    remaining_size = pos_size
                    in_trade       = True
                    entry_idx      = i
                    trail_sl       = sl_price
                    trail_active   = False
                    mfe            = entry_price
                    partial_done   = False
                    continue
            # ─────────────────────────────────────────────────────────────

            # ── MODALITÀ PULLBACK: gestisci stato pending ─────────────────
            if pullback_entry and pending:
                pending_bars += 1

                # Condizioni di invalidazione (cancella attesa)
                signal_dead = (
                    row["rsi"] < 45 or                              # momentum perso
                    (row["macd"] < row["macd_sig"] and             # MACD tornato bearish
                     prev["macd"] >= prev["macd_sig"]) or
                    pending_bars >= PULLBACK_BARS_MAX               # timeout
                )
                if signal_dead:
                    pending = False
                    pending_bars = 0
                    continue

                # Zona pullback: Low scende a toccare EMA20 ± PULLBACK_ZONE_PCT
                pb_level = row["ema20"] * (1 + PULLBACK_ZONE_PCT)
                touched_ema20 = row["Low"] <= pb_level and row["Close"] > row["ema20"] * 0.985

                if touched_ema20:
                    # Entra all'EMA20 (prezzo realistico = ema20, simulato come tocco intra-barra)
                    ep = row["ema20"]
                    ok, st = _open_trade(ep, row["atr"])
                    if ok:
                        in_trade       = True
                        entry_price    = st["entry_price"]
                        sl_price       = st["sl_price"]
                        pos_size       = st["pos_size"]
                        remaining_size = st["remaining_size"]
                        trail_sl       = sl_price
                        trail_active   = False
                        mfe            = entry_price
                        partial_done   = False
                        entry_idx      = i
                    pending = False
                    pending_bars = 0
                continue
            # ─────────────────────────────────────────────────────────────

            if not signal_entry(prev, df.iloc[i-2], sl_mult, ext_k=ext_k):
                continue

            # ── MACRO REGIME FILTER ────────────────────────────────────────
            if btc_regime is not None:
                candle_date = pd.to_datetime(row["ts"], unit="s").date()
                is_bull = btc_regime.get(candle_date, True)
                if not is_bull:
                    continue
            # ──────────────────────────────────────────────────────────────

            if pullback_entry:
                # Controlla se siamo GIÀ vicini a EMA20 (pullback immediato nella stessa barra)
                pb_level = row["ema20"] * (1 + PULLBACK_ZONE_PCT)
                if row["Close"] <= pb_level:
                    # Siamo già nella zona: entra subito
                    ep = row["Close"]
                    ok, st = _open_trade(ep, row["atr"])
                    if ok:
                        in_trade       = True
                        entry_price    = st["entry_price"]
                        sl_price       = st["sl_price"]
                        pos_size       = st["pos_size"]
                        remaining_size = st["remaining_size"]
                        trail_sl       = sl_price
                        trail_active   = False
                        mfe            = entry_price
                        partial_done   = False
                        entry_idx      = i
                else:
                    # Segnale scattato ma troppo lontano da EMA20: metti in pending
                    pending      = True
                    pending_bars = 0
                continue

            # ── ENTRY IMMEDIATA (modalità classica) ───────────────────────
            atr = prev["atr"]
            sl_dist = sl_mult * atr
            if sl_dist <= 0:
                continue
            entry_price = row["Open"]
            sl_price    = entry_price - sl_dist
            if sl_price <= 0:
                continue
            atr_ratio = atr / entry_price
            if atr_ratio > ATR_RATIO_MAX_ENTRY:
                continue

            pos_size = (equity * risk_pct) / sl_dist
            notional = pos_size * entry_price
            in_trade       = True
            entry_idx      = i
            trail_sl       = sl_price
            trail_active   = False
            mfe            = entry_price
            partial_done   = False   # True dopo che il 50% è stato chiuso a +1.5R
            remaining_size = pos_size  # dimensione posizione residua

        else:
            high_now  = row["High"]
            low_now   = row["Low"]
            close_now = row["Close"]
            atr_now   = row["atr"]

            # ── SHORT SWING: gestione posizione invertita ─────────────────
            if _is_short and swing_entry:
                mfe = min(mfe, low_now)  # MFE per SHORT = prezzo più basso
                # TP: il prezzo è sceso fino al supporto
                if not partial_done and low_now <= tp_full:
                    pnl = (entry_price - tp_full) * remaining_size * (1 - FEES_PCT * 2)
                    trades.append({"symbol": symbol, "entry": entry_price,
                                   "exit": tp_full, "pnl": pnl,
                                   "bars": i - entry_idx, "reason": "SwingTP"})
                    equity    += pnl
                    max_equity = max(max_equity, equity)
                    in_trade   = False
                    cooldown   = COOLDOWN_BARS
                    continue
                # SL: il prezzo è salito sopra la resistenza
                if high_now >= sl_price:
                    exit_price = sl_price
                    pnl = (entry_price - exit_price) * remaining_size * (1 - FEES_PCT * 2)
                    trades.append({"symbol": symbol, "entry": entry_price,
                                   "exit": exit_price, "pnl": pnl,
                                   "bars": i - entry_idx, "reason": "SL"})
                    equity    += pnl
                    max_equity = max(max_equity, equity)
                    in_trade   = False
                    cooldown   = COOLDOWN_BARS
                    continue
                continue  # posizione SHORT ancora aperta, skip logica LONG
            # ─────────────────────────────────────────────────────────────

            mfe = max(mfe, high_now)
            r_dist = entry_price - sl_price

            # ── SWING: TP fisso alla resistenza naturale ──────────────────
            if swing_entry and not partial_done:
                if high_now >= tp_full:
                    pnl = (tp_full - entry_price) * remaining_size * (1 - FEES_PCT * 2)
                    trades.append({"symbol": symbol, "entry": entry_price,
                                   "exit": tp_full, "pnl": pnl,
                                   "bars": i - entry_idx, "reason": "SwingTP"})
                    equity    += pnl
                    max_equity = max(max_equity, equity)
                    in_trade   = False
                    cooldown   = COOLDOWN_BARS
                    continue
            # ─────────────────────────────────────────────────────────────

            # ── PARTIAL TP a +tp_r×R ─────────────────────────────────────
            if partial_tp and not swing_entry and not partial_done and r_dist > 0:
                partial_tp_price = entry_price + tp_r * r_dist
                if high_now >= partial_tp_price:
                    # Chiude il 50% della posizione a partial_tp_price
                    partial_size = pos_size * PARTIAL_TP_PCT
                    partial_pnl  = (partial_tp_price - entry_price) * partial_size * (1 - FEES_PCT * 2)
                    trades.append({"symbol": symbol, "entry": entry_price,
                                   "exit": partial_tp_price, "pnl": partial_pnl,
                                   "bars": i - entry_idx, "reason": "PartialTP"})
                    equity      += partial_pnl
                    max_equity   = max(max_equity, equity)
                    partial_done = True
                    remaining_size = pos_size * (1 - PARTIAL_TP_PCT)
                    # Sposta SL a breakeven sull'altra metà
                    sl_price     = entry_price
                    trail_sl     = entry_price
            # ──────────────────────────────────────────────────────────────

            # Ratchet: trailing attivato dopo +1R (non in swing — gestisce già il TP fisso)
            if not swing_entry and high_now >= entry_price + TRAIL_START_R * r_dist and not trail_active:
                trail_active = True
                trail_dist   = trail_mult * atr_now
                trail_sl     = max(trail_sl, entry_price)

            if trail_active and not swing_entry:
                trail_dist = trail_mult * atr_now
                trail_sl   = max(trail_sl, high_now - trail_dist)

            # Check SL hit
            effective_sl = max(sl_price, trail_sl)
            if low_now <= effective_sl:
                exit_price = effective_sl
                pnl = (exit_price - entry_price) * remaining_size * (1 - FEES_PCT * 2)
                trades.append({"symbol": symbol, "entry": entry_price, "exit": exit_price,
                                "pnl": pnl, "bars": i - entry_idx, "reason": "SL/Trail"})
                equity += pnl
                max_equity = max(max_equity, equity)
                in_trade   = False
                cooldown   = COOLDOWN_BARS
                continue

            # Check exit signal (non in swing — la swing ha solo SL e TP fisso)
            r_current = (close_now - entry_price) / r_dist if r_dist > 0 else 0
            if (not swing_entry and not trail_active and i - entry_idx > 3
                    and signal_exit(row, prev)
                    and r_current >= 0.5):
                exit_price = close_now
                pnl = (exit_price - entry_price) * remaining_size * (1 - FEES_PCT * 2)
                trades.append({"symbol": symbol, "entry": entry_price, "exit": exit_price,
                                "pnl": pnl, "bars": i - entry_idx, "reason": "ExitSignal"})
                equity += pnl
                max_equity = max(max_equity, equity)
                in_trade   = False
                cooldown   = COOLDOWN_BARS

    # Chiudi posizione aperta all'ultima barra
    if in_trade:
        exit_price = df.iloc[-1]["Close"]
        if _is_short:
            pnl = (entry_price - exit_price) * remaining_size * (1 - FEES_PCT * 2)
        else:
            pnl = (exit_price - entry_price) * remaining_size * (1 - FEES_PCT * 2)
        trades.append({"symbol": symbol, "entry": entry_price, "exit": exit_price,
                        "pnl": pnl, "bars": len(df) - entry_idx, "reason": "EndOfData"})
        equity += pnl

    equity_curve.append(equity)

    # Metriche
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    total_win  = sum(t["pnl"] for t in wins)
    total_loss = abs(sum(t["pnl"] for t in losses))
    pf = total_win / total_loss if total_loss > 0 else float("inf")
    avg_win  = total_win / len(wins)   if wins   else 0
    avg_loss = total_loss / len(losses) if losses else 0

    # Max drawdown
    peak = initial_equity
    max_dd = 0.0
    running = initial_equity
    for t in trades:
        running += t["pnl"]
        peak = max(peak, running)
        dd = (peak - running) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    return {
        "symbol":    symbol,
        "n_trades":  len(trades),
        "wr":        len(wins) / len(trades) if trades else 0,
        "pnl_total": sum(t["pnl"] for t in trades),
        "pf":        pf,
        "avg_win":   avg_win,
        "avg_loss":  avg_loss,
        "max_dd":    max_dd,
        "equity_curve": equity_curve,
        "trades":    trades,
    }


# ─────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────

def print_report(results: list, days: int, sl_mult: float, mode: str = "CLASSICO"):
    total_trades = sum(r["n_trades"] for r in results)
    total_wins   = sum(int(r["wr"] * r["n_trades"]) for r in results)
    all_trades   = [t for r in results for t in r["trades"]]
    all_wins     = [t for t in all_trades if t["pnl"] > 0]
    all_losses   = [t for t in all_trades if t["pnl"] <= 0]
    total_pnl    = sum(t["pnl"] for t in all_trades)
    gross_win    = sum(t["pnl"] for t in all_wins)
    gross_loss   = abs(sum(t["pnl"] for t in all_losses))
    pf_global    = gross_win / gross_loss if gross_loss > 0 else float("inf")
    avg_win_g    = gross_win / len(all_wins)   if all_wins   else 0
    avg_loss_g   = gross_loss / len(all_losses) if all_losses else 0
    wr_global    = total_wins / total_trades if total_trades > 0 else 0
    expectancy   = (wr_global * avg_win_g) - ((1 - wr_global) * avg_loss_g)

    # Max drawdown globale sull'equity aggregata
    running = 100.0; peak = 100.0; max_dd = 0.0
    for t in sorted(all_trades, key=lambda x: x.get("entry", 0)):
        running += t["pnl"]
        peak = max(peak, running)
        dd = (peak - running) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)

    print("\n" + "═"*70)
    print(f"  BACKTEST REPORT — ultimi {days}gg | {mode} | SL {sl_mult}×ATR | Trail {trail_mult}×ATR | {len(results)} simboli")
    print("═"*70)
    print(f"\n{'Simbolo':<20} {'Trade':>6} {'WR':>7} {'PnL':>9} {'PF':>7} {'AvgW':>8} {'AvgL':>8}")
    print("─"*70)

    by_pnl = sorted(results, key=lambda r: r["pnl_total"], reverse=True)
    for r in by_pnl:
        if r["n_trades"] == 0:
            continue
        sign = "✅" if r["pnl_total"] > 0 else "❌"
        pf_str = f"{r['pf']:.2f}" if r['pf'] != float("inf") else "∞"
        print(f"{sign} {r['symbol']:<18} {r['n_trades']:>6} {r['wr']:>6.0%}"
              f" {r['pnl_total']:>+9.4f} {pf_str:>7}"
              f" {r['avg_win']:>+8.4f} {-r['avg_loss']:>8.4f}")

    print("─"*70)
    pf_str = f"{pf_global:.2f}" if pf_global != float("inf") else "∞"
    print(f"\n  TOTALE:  {total_trades} trade | WR {wr_global:.1%} | PnL {total_pnl:+.4f}")
    print(f"  Profit Factor:  {pf_str}")
    print(f"  Avg vincita:   {avg_win_g:+.4f} | Avg perdita: {-avg_loss_g:.4f}")
    print(f"  Expectancy:    {expectancy:+.4f} per trade")
    print(f"  Max Drawdown:   {max_dd:.1%}")

    # Valutazione
    print("\n" + "─"*70)
    print("  VALUTAZIONE:")
    if pf_global >= 1.5:
        print("  ✅ EDGE POSITIVO — sistema profittevole su dati storici")
        print("     I parametri live sono calibrati correttamente.")
    elif pf_global >= 1.0:
        print("  ⚠️  EDGE MARGINALE — sistema appena sopra break-even")
        print("     Stringere SL o alzare filtri di ingresso potrebbe aiutare.")
    else:
        print("  ❌ NESSUN EDGE — i segnali NON hanno vantaggio statistico")
        print("     Continuare live in queste condizioni brucia capitale.")
        print("     → Ridisegnare i criteri di ingresso prima di riaprire i bot.")

    print("─"*70)

    # Suggerimenti parametrici se edge esiste ma è marginale
    if 1.0 <= pf_global < 1.5:
        print("\n  SCENARI ALTERNATIVI DA TESTARE:")
        print(f"    python backtest.py --sl-mult 1.5   (SL più stretto)")
        print(f"    python backtest.py --sl-mult 1.3   (SL molto stretto)")
        print(f"    python backtest.py --adx-thresh 28  (ingressi più selettivi)")

    print()


# ─────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────

def main():
    global ENTRY_ADX_THRESH, RR_MIN_ROOM, RSI_ENTRY_MAX, trail_mult  # noqa: PLW0603
    global SWING_RSI_MAX, SWING_RSI_MIN, SWING_NEAR_ATR, SWING_RR_MIN, SWING_VOL_MIN  # noqa: PLW0603
    global SWING_RSI_MIN_SHORT, SWING_RSI_MAX_SHORT, EXT_K  # noqa: PLW0603
    parser = argparse.ArgumentParser(description="Backtest segnali bot LONG")
    parser.add_argument("--days",       type=int,   default=90,    help="Giorni di storia (default 90)")
    parser.add_argument("--symbols",    type=str,   default="",    help="Simboli CSV (default: lista built-in)")
    parser.add_argument("--sl-mult",    type=float, default=SL_ATR_MULT, help=f"Moltiplicatore SL (default {SL_ATR_MULT})")
    parser.add_argument("--adx-thresh", type=float, default=24.0,  help="Soglia ADX (default 24.0)")
    parser.add_argument("--rr-room",    type=float, default=1.5,   help="Room-to-run minima in R (default 1.5)")
    parser.add_argument("--rsi-max",    type=float, default=68.0,  help="RSI max all ingresso (default 68.0)")
    parser.add_argument("--risk-pct",   type=float, default=0.012, help="Rischio per trade (default 0.012)")
    parser.add_argument("--trail-mult",  type=float, default=TRAIL_ATR_MULT, help=f"Trailing ATR mult (default {TRAIL_ATR_MULT})")
    parser.add_argument("--no-partial-tp", action="store_true", help="Disabilita partial TP a +1.5R (confronto)")
    parser.add_argument("--pullback",      action="store_true", help="Pullback entry: aspetta ritorno a EMA20 prima di entrare")
    parser.add_argument("--tp-r",          type=float, default=PARTIAL_TP_R, help=f"Multiplo R per partial TP (default {PARTIAL_TP_R})")
    parser.add_argument("--swing",         action="store_true", help="Nuova strategia swing-level: entry su supporto strutturale con R:R naturale")
    parser.add_argument("--swing-rsi-max",  type=float, default=SWING_RSI_MAX, help=f"RSI max per swing entry (default {SWING_RSI_MAX})")
    parser.add_argument("--swing-rsi-min",  type=float, default=SWING_RSI_MIN, help=f"RSI min per swing entry (default {SWING_RSI_MIN})")
    parser.add_argument("--swing-near-atr", type=float, default=SWING_NEAR_ATR, help=f"Distanza max dal supporto in ATR (default {SWING_NEAR_ATR})")
    parser.add_argument("--swing-rr-min",   type=float, default=SWING_RR_MIN,  help=f"R:R minimo per swing entry (default {SWING_RR_MIN})")
    parser.add_argument("--swing-vol-min",  type=float, default=SWING_VOL_MIN, help=f"Moltiplicatore volume minimo (default {SWING_VOL_MIN})")
    parser.add_argument("--ext-k",          type=float, default=None,          help="Filtro estensione: max = EMA20 + k*ATR (default: usa valore in codice 1.5)")
    parser.add_argument("--short",               action="store_true", help="Backtest SHORT swing-level (speculare al LONG)")
    parser.add_argument("--swing-rsi-min-short",  type=float, default=SWING_RSI_MIN_SHORT, help=f"RSI min per swing SHORT (default {SWING_RSI_MIN_SHORT})")
    parser.add_argument("--swing-rsi-max-short",  type=float, default=SWING_RSI_MAX_SHORT, help=f"RSI max per swing SHORT (default {SWING_RSI_MAX_SHORT})")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS

    # Override globali
    ENTRY_ADX_THRESH = args.adx_thresh
    RR_MIN_ROOM      = args.rr_room
    RSI_ENTRY_MAX    = args.rsi_max
    trail_mult       = args.trail_mult
    SWING_RSI_MAX    = args.swing_rsi_max
    SWING_RSI_MIN    = args.swing_rsi_min
    SWING_NEAR_ATR   = args.swing_near_atr
    SWING_RR_MIN          = args.swing_rr_min
    SWING_VOL_MIN         = args.swing_vol_min
    SWING_RSI_MIN_SHORT   = args.swing_rsi_min_short
    SWING_RSI_MAX_SHORT   = args.swing_rsi_max_short
    if args.ext_k is not None:
        EXT_K = args.ext_k

    print(f"\n{'─'*70}")
    if args.swing and args.short:
        mode_tag = "SWING-LEVEL SHORT"
    elif args.swing:
        mode_tag = "SWING-LEVEL LONG"
    elif args.pullback:
        mode_tag = "PULLBACK"
    else:
        mode_tag = "CLASSICO"
    print(f"  Backtest — {args.days}gg | {mode_tag} | SL {args.sl_mult}×ATR | Trail {args.trail_mult}×ATR | ADX>{args.adx_thresh:.0f} | {len(symbols)} simboli")
    print(f"{'─'*70}")

    trail_mult = args.trail_mult

    # Scarica regime BTC daily (una volta sola per tutti i simboli)
    print("  Scarico regime BTC daily (EMA200)...", end=" ", flush=True)
    btc_regime_raw = fetch_btc_regime(args.days)
    if len(btc_regime_raw) > 0:
        bull_days  = btc_regime_raw.sum()
        total_days = len(btc_regime_raw)
        pct_bull   = bull_days / total_days * 100
        print(f"✅ {total_days} giorni | BTC in uptrend {pct_bull:.0f}% del tempo")
        # Converti in dict date→bool per lookup O(1)
        btc_regime_dict = dict(zip(btc_regime_raw.index, btc_regime_raw.values))
        # Precomputa: per ogni data, qual è il regime del giorno precedente
        sorted_dates = sorted(btc_regime_dict.keys())
        # Cache: data → is_bull (usando il giorno precedente disponibile)
        btc_regime_cache: dict = {}
        for idx_d, day in enumerate(sorted_dates):
            prev_day = sorted_dates[idx_d - 1] if idx_d > 0 else None
            btc_regime_cache[day] = bool(btc_regime_dict[prev_day]) if prev_day is not None else True
        btc_regime = btc_regime_cache
    else:
        print("⚠️  fallito — regime filter disattivato")
        btc_regime = None

    results = []
    for i, symbol in enumerate(symbols):
        print(f"  [{i+1:2d}/{len(symbols)}] {symbol:<20}", end=" ", flush=True)
        df = fetch_ohlcv(symbol, interval=60, days=args.days)
        if df is None or len(df) < 300:
            print("⚠️  dati insufficienti")
            continue
        df = compute_indicators(df)
        if len(df) < 50:
            print("⚠️  indicatori insufficienti dopo dropna")
            continue
        result = simulate(symbol, df, sl_mult=args.sl_mult, risk_pct=args.risk_pct,
                           trail_mult=args.trail_mult, btc_regime=btc_regime,
                           partial_tp=not args.no_partial_tp,
                           pullback_entry=args.pullback,
                           tp_r=args.tp_r,
                           swing_entry=args.swing,
                           direction="short" if args.short else "long",
                           ext_k=args.ext_k)
        results.append(result)
        pf_str = f"{result['pf']:.2f}" if result['pf'] != float("inf") else "∞"
        sign = "✅" if result["pnl_total"] > 0 else "❌"
        print(f"{sign} {result['n_trades']:3d} trade | WR {result['wr']:.0%} | PF {pf_str} | PnL {result['pnl_total']:+.4f}")
        time.sleep(0.15)  # rate limit Bybit

    if not results:
        print("\n  Nessun risultato. Verifica la connessione a Bybit.")
        sys.exit(1)

    print_report(results, days=args.days, sl_mult=args.sl_mult, mode=mode_tag)


if __name__ == "__main__":
    main()
