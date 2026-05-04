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
COOLDOWN_BARS        = 3        # barre di cooldown dopo un'uscita
FEES_PCT             = 0.00055  # 0.055% taker per lato
LINEAR_MIN_TURNOVER  = 50_000_000

# Partial TP: chiude il 50% della posizione a +1.5R, sposta SL a BE sull'altra metà
PARTIAL_TP_R         = 1.5      # prendi profitto parziale quando il trade è a +1.5R
PARTIAL_TP_PCT       = 0.5      # chiudi questa percentuale della posizione

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


# Parametri filtro qualità ingresso
RR_MIN_ROOM   = 1.5   # room-to-run minima: distanza a swing high >= 1.5 × SL dist
RSI_ENTRY_MAX = 68.0  # non entrare se RSI già overbought (movimento maturo)

# ─────────────────────────────────────────────
# Logica segnale ingresso (identica al bot)
# ─────────────────────────────────────────────

def signal_entry(row, prev, sl_mult: float) -> bool:
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
    ext_cap = row["ema20"] + 1.5 * row["atr"]
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
             partial_tp: bool = True) -> dict:
    """
    Simula tutti i trade su df. Gestisce SL ATR-based, exit signal, ratchet.
    partial_tp=True: chiude il 50% a +1.5R e sposta SL a BE, lascia correre il resto.
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

    for i in range(2, len(df)):
        row  = df.iloc[i]
        prev = df.iloc[i-1]

        equity_curve.append(equity)

        if not in_trade:
            if cooldown > 0:
                cooldown -= 1
                continue

            if not signal_entry(prev, df.iloc[i-2], sl_mult):
                continue

            # ── MACRO REGIME FILTER ────────────────────────────────────────
            if btc_regime is not None:
                candle_date = pd.to_datetime(row["ts"], unit="s").date()
                is_bull = btc_regime.get(candle_date, True)
                if not is_bull:
                    continue
            # ──────────────────────────────────────────────────────────────

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

            mfe = max(mfe, high_now)
            r_dist = entry_price - sl_price

            # ── PARTIAL TP a +1.5R ────────────────────────────────────────
            if partial_tp and not partial_done and r_dist > 0:
                partial_tp_price = entry_price + PARTIAL_TP_R * r_dist
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

            # Ratchet: trailing attivato dopo +1R
            if high_now >= entry_price + TRAIL_START_R * r_dist and not trail_active:
                trail_active = True
                trail_dist   = trail_mult * atr_now
                trail_sl     = max(trail_sl, entry_price)

            if trail_active:
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

            # Check exit signal
            r_current = (close_now - entry_price) / r_dist if r_dist > 0 else 0
            if (not trail_active and i - entry_idx > 3
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

def print_report(results: list, days: int, sl_mult: float):
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
    print(f"  BACKTEST REPORT — ultimi {days}gg | SL {sl_mult}×ATR | Trail {trail_mult}×ATR | RSI<{RSI_ENTRY_MAX:.0f} | {len(results)} simboli")
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
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()] or DEFAULT_SYMBOLS

    # Override globali
    global ENTRY_ADX_THRESH, RR_MIN_ROOM, RSI_ENTRY_MAX, trail_mult  # noqa: PLW0603
    ENTRY_ADX_THRESH = args.adx_thresh
    RR_MIN_ROOM      = args.rr_room
    RSI_ENTRY_MAX    = args.rsi_max
    trail_mult       = args.trail_mult

    print(f"\n{'─'*70}")
    print(f"  Backtest — {args.days}gg | SL {args.sl_mult}×ATR | Trail {args.trail_mult}×ATR | ADX>{args.adx_thresh:.0f} | RSI<{args.rsi_max:.0f} | {len(symbols)} simboli")
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
                           partial_tp=not args.no_partial_tp)
        results.append(result)
        pf_str = f"{result['pf']:.2f}" if result['pf'] != float("inf") else "∞"
        sign = "✅" if result["pnl_total"] > 0 else "❌"
        print(f"{sign} {result['n_trades']:3d} trade | WR {result['wr']:.0%} | PF {pf_str} | PnL {result['pnl_total']:+.4f}")
        time.sleep(0.15)  # rate limit Bybit

    if not results:
        print("\n  Nessun risultato. Verifica la connessione a Bybit.")
        sys.exit(1)

    print_report(results, days=args.days, sl_mult=args.sl_mult)


if __name__ == "__main__":
    main()
