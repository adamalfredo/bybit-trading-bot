#!/usr/bin/env python3
"""
Backtest v2 — EMA Trend Following LONG (4h)

Strategia:
  - BTC filter: C > EMA200(4h) AND EMA200 slope UP → solo LONG
  - Entry: coin vicina a EMA20 (pullback), RSI(14) < 55, volume > 1.5x media
  - SL: 2.0 * ATR(14) sotto entry
  - Trailing: attiva quando gain >= 1R, trail a 2.0 * ATR dal massimo
  - Max 1 posizione per coin (no pyramiding)

Walk-forward:
  - TRAIN: 2024-01-01 → 2025-06-30
  - TEST:  2025-07-01 → 2026-05-13

Coins testate: 15 altcoin + BTC come filtro
"""

import requests
import time
import numpy as np
import pandas as pd
from datetime import datetime, timezone
import os
import pickle

CACHE_DIR = os.path.join(os.path.dirname(__file__), "mnt", "data", "kline_cache")
os.makedirs(CACHE_DIR, exist_ok=True)
CACHE_TTL_HOURS = 6   # riscadica se dati più vecchi di 6h

BASE = "https://api.bybit.com"

# ─── Parametri strategia (da ottimizzare solo sul TRAIN set) ────────────────
PARAMS = {
    "sl_atr":         2.0,
    "trail_atr":      3.0,
    "trail_start_r":  1.0,
    "rsi_max":        50.0,
    "vol_min":        1.2,
    "ext_cap_atr":    2.0,   # allargato: entry fino a 2 ATR sopra EMA20
    "near_floor_atr": 2.0,
}

RISK_PCT   = 0.0075   # rischio per trade (0.75%)
COMMISSION = 0.0006   # taker fee Bybit (0.06% per lato, 0.12% round trip)

TRAIN_END = pd.Timestamp("2025-07-01", tz="UTC")
TEST_END  = pd.Timestamp("2026-05-14", tz="UTC")

COINS = [
    # Large cap
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT", "ADAUSDT",
    "DOTUSDT", "AVAXUSDT", "LINKUSDT", "UNIUSDT", "NEARUSDT",
    # Mid cap
    "TONUSDT", "SUIUSDT", "TAOUSDT", "ONDOUSDT", "ENAUSDT",
    "1000PEPEUSDT", "AAVEUSDT", "LDOUSDT", "MKRUSDT", "COMPUSDT",
    # Alt
    "WLDUSDT", "INJUSDT", "TIAUSDT", "SEIUSDT", "FTMUSDT",
    "OPUSDT", "ARBUSDT", "STXUSDT", "APTUSDT", "FETUSDT",
    "RENDERUSDT", "JUPUSDT", "PYTHUSDT", "WIFUSDT", "BOMEUSDT",
    "NOTUSDT", "EIGENUSDT", "POLUSDT", "HYPEUSDT", "MOVEUSDT",
    # Extra universe (Run G)
    "LTCUSDT", "ATOMUSDT", "XLMUSDT", "VETUSDT", "FILUSDT",
    "ICPUSDT", "RUNEUSDT", "ARUSDT", "CRVUSDT", "SANDUSDT",
    "MANAUSDT", "AXSUSDT", "GALAUSDT", "SNXUSDT", "GRTUSDT",
    "APEUSDT", "GMTUSDT", "ORDIUSDT", "1000BONKUSDT", "CFXUSDT",
    "KASUSDT", "PENDLEUSDT", "1000SHIBUSDT", "BLURUSDT", "FLUXUSDT",
]

# ─── Fetch klines da Bybit ────────────────────────────────────────────────────
def fetch_klines(symbol: str, interval: str = "240", limit: int = 200, end_ms: int = None) -> list:
    params = {"category": "linear", "symbol": symbol, "interval": interval, "limit": limit}
    if end_ms:
        params["end"] = end_ms
    resp = requests.get(f"{BASE}/v5/market/kline", params=params, timeout=15)
    data = resp.json()
    if data.get("retCode") != 0:
        print(f"  [WARN] {symbol} kline error: {data.get('retMsg')}")
        return []
    return data["result"]["list"]


def fetch_all_klines(symbol: str, interval: str = "240", days: int = 900) -> pd.DataFrame:
    """Scarica kline con cache su disco. Ri-scarica solo se più vecchio di CACHE_TTL_HOURS."""
    cache_file = os.path.join(CACHE_DIR, f"{symbol}_{interval}_{days}.pkl")
    if os.path.exists(cache_file):
        age_hours = (time.time() - os.path.getmtime(cache_file)) / 3600
        if age_hours < CACHE_TTL_HOURS:
            print(f"[cache]", end=" ", flush=True)
            return pickle.load(open(cache_file, "rb"))

    """Scarica kline paginando all'indietro (end→start). Bybit ritorna in ordine DESC."""
    interval_ms     = int(interval) * 60 * 1000
    now_ms          = int(time.time() * 1000)
    target_start_ms = now_ms - days * 86_400_000

    all_bars = []
    end_ms   = now_ms
    MAX_ITER = 50    # cap di sicurezza

    for i in range(MAX_ITER):
        params = {
            "category": "linear",
            "symbol":   symbol,
            "interval": interval,
            "limit":    200,
            "end":      end_ms,
        }
        try:
            resp = requests.get(f"{BASE}/v5/market/kline", params=params, timeout=8)
            data = resp.json()
        except Exception as e:
            print(f"[err {e}]", end="", flush=True)
            break
        if data.get("retCode") != 0:
            break
        bars = data["result"]["list"]
        if not bars:
            break
        all_bars.extend(bars)
        print(".", end="", flush=True)
        # bars[0]=newest bars[-1]=oldest (ordine DESC)
        oldest_ms = int(bars[-1][0])
        if oldest_ms <= target_start_ms:
            break
        new_end = oldest_ms - interval_ms
        if new_end >= end_ms:
            break   # nessun progresso
        end_ms = new_end
        time.sleep(0.08)

    if not all_bars:
        return pd.DataFrame()

    df = pd.DataFrame(all_bars, columns=["ts", "O", "H", "L", "C", "V", "turnover"])
    df = df.astype({"ts": int, "O": float, "H": float, "L": float, "C": float, "V": float})
    df["dt"] = pd.to_datetime(df["ts"], unit="ms", utc=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    pickle.dump(df, open(cache_file, "wb"))
    return df


# ─── Indicatori ───────────────────────────────────────────────────────────────
def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    c = df["C"]
    df["ema20"]       = c.ewm(span=20,  adjust=False).mean()
    df["ema50"]       = c.ewm(span=50,  adjust=False).mean()
    df["ema200"]      = c.ewm(span=200, adjust=False).mean()
    df["ema20_slope"] = df["ema20"].diff(3)   # slope EMA20 della coin

    # ATR(14)
    tr = np.maximum(
        df["H"] - df["L"],
        np.maximum(
            (df["H"] - df["C"].shift(1)).abs(),
            (df["L"] - df["C"].shift(1)).abs()
        )
    )
    df["atr"] = tr.ewm(span=14, adjust=False).mean()

    # RSI(14)
    delta = c.diff()
    gain  = delta.clip(lower=0).ewm(span=14, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(span=14, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, np.nan))

    # Volume MA(20)
    df["vol_ma"] = df["V"].rolling(20).mean()

    # EMA200 slope (diff su 3 barre per smussare)
    df["ema200_slope"] = df["ema200"].diff(3)

    return df


# ─── Backtest engine ──────────────────────────────────────────────────────────
def run_backtest(df: pd.DataFrame, p: dict, label: str = "") -> list:
    """
    Esegue il backtest su un singolo DataFrame già filtrato (train o test).
    Restituisce lista di trade dict.
    """
    trades = []
    pos    = None   # posizione aperta

    for i in range(210, len(df)):
        prev = df.iloc[i - 1]
        row  = df.iloc[i]

        # ── Guard: indicatori validi ──────────────────────────────────────────
        if pd.isna(prev["ema200"]) or pd.isna(row["atr"]) or row["atr"] == 0:
            continue

        # ── Trend filter BTC (usa barra PRECEDENTE per no-lookahead) ─────────
        trend_up = bool(prev["btc_trend_up"])

        # ── Gestione posizione aperta ─────────────────────────────────────────
        if pos is not None:
            # Aggiorna massimo
            if row["H"] > pos["high"]:
                pos["high"] = row["H"]

            # Attiva trailing
            if not pos["trail_active"]:
                if row["H"] >= pos["entry"] + pos["trail_start_dist"]:
                    pos["trail_active"] = True
                    pos["sl"] = max(pos["sl"], pos["entry"])  # almeno breakeven

            # Aggiorna trailing SL
            if pos["trail_active"]:
                trail_sl = pos["high"] - p["trail_atr"] * row["atr"]
                if trail_sl > pos["sl"]:
                    pos["sl"] = trail_sl

            # SL colpito (usa il low della candela)
            if row["L"] <= pos["sl"]:
                exit_px   = pos["sl"]
                pnl_r     = (exit_px - pos["entry"]) / pos["sl_dist"]
                # Sottrai commissioni (0.12% round trip → in termini di R)
                comm_r    = (2 * COMMISSION * pos["entry"]) / pos["sl_dist"]
                pnl_r_net = pnl_r - comm_r

                trades.append({
                    "entry_dt": pos["entry_dt"],
                    "exit_dt":  row["dt"],
                    "entry":    pos["entry"],
                    "exit":     exit_px,
                    "sl_dist":  pos["sl_dist"],
                    "pnl_r":    pnl_r_net,
                    "win":      pnl_r_net > 0,
                    "bars":     i - pos["entry_bar"],
                })
                pos = None
                continue  # non aprire subito dopo chiusura

        # ── Cerca nuova entry ─────────────────────────────────────────────────
        if pos is None and trend_up:
            atr = prev["atr"]
            ema20 = prev["ema20"]

            near_ema20   = (row["C"] <= ema20 + p["ext_cap_atr"]   * atr)
            above_floor  = (row["C"] >= ema20 - p["near_floor_atr"] * atr)
            rsi_ok       = row["rsi"] < p["rsi_max"]
            vol_ok       = row["V"]   > p["vol_min"] * prev["vol_ma"] if not pd.isna(prev["vol_ma"]) else False
            # Micro-uptrend: EMA20 sopra EMA50 e in salita
            micro_up     = (prev["ema20"] > prev["ema50"]) and (prev["ema20_slope"] > 0)
            # Conferma inversione: la candela di entry deve chiudere sopra l'open (verde)
            bull_candle  = row["C"] > row["O"]

            if near_ema20 and above_floor and rsi_ok and vol_ok and micro_up and bull_candle:
                sl_dist = p["sl_atr"] * row["atr"]
                pos = {
                    "entry":             row["C"],
                    "sl":                row["C"] - sl_dist,
                    "sl_dist":           sl_dist,
                    "high":              row["H"],
                    "trail_active":      False,
                    "trail_start_dist":  p["trail_start_r"] * sl_dist,
                    "entry_bar":         i,
                    "entry_dt":          row["dt"],
                }

    return trades


# ─── Analisi risultati ────────────────────────────────────────────────────────
def analyze(trades: list, label: str = "") -> dict:
    if not trades:
        print(f"  {label}: 0 trade")
        return {}

    wins   = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]

    wr        = len(wins) / len(trades)
    avg_win   = np.mean([t["pnl_r"] for t in wins])   if wins   else 0.0
    avg_loss  = abs(np.mean([t["pnl_r"] for t in losses])) if losses else 0.0
    ratio     = avg_win / avg_loss if avg_loss > 0 else float("inf")
    exp       = wr * avg_win - (1 - wr) * avg_loss

    # Equity curve (composta, rischio fisso RISK_PCT per trade)
    eq = 1.0
    dd_peak = 1.0
    max_dd  = 0.0
    for t in trades:
        eq *= (1 + t["pnl_r"] * RISK_PCT)
        if eq > dd_peak:
            dd_peak = eq
        dd = (dd_peak - eq) / dd_peak
        if dd > max_dd:
            max_dd = dd

    avg_bars = np.mean([t["bars"] for t in trades])

    print(f"\n{'─'*60}")
    print(f"  {label}")
    print(f"{'─'*60}")
    print(f"  Trades : {len(trades):>4}  |  WR: {wr:.1%}  |  Ratio: {ratio:.2f}x")
    print(f"  Avg win: {avg_win:+.3f}R  |  Avg loss: {avg_loss:.3f}R")
    print(f"  Expectancy: {exp:+.4f}R/trade  |  Max DD: {max_dd:.1%}")
    print(f"  Equity finale: {eq:.3f}x  ({(eq-1)*100:+.1f}%)  |  Avg durata: {avg_bars:.1f} barre")

    return {
        "label":      label,
        "n":          len(trades),
        "wr":         wr,
        "ratio":      ratio,
        "expectancy": exp,
        "equity":     eq,
        "max_dd":     max_dd,
    }


# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    print("=" * 60)
    print("  BACKTEST v2 — EMA Trend Following LONG 4h")
    print("=" * 60)
    print(f"  Train: 2024-01-01 → 2025-06-30")
    print(f"  Test:  2025-07-01 → 2026-05-13")
    print(f"  Parametri: SL={PARAMS['sl_atr']}ATR | Trail={PARAMS['trail_atr']}ATR | RSI<{PARAMS['rsi_max']} | Vol>{PARAMS['vol_min']}x | EMA20>EMA50+slope ON | BullCandle ON")
    print()
    print("  >>> Sto scaricando dati e girando backtest, attendi risultati...")
    print()

    # Scarica BTC per il filtro trend
    print("Scarico BTC (filtro trend)...")
    btc_raw = fetch_all_klines("BTCUSDT", interval="240", days=900)
    if btc_raw.empty:
        print("ERRORE: impossibile scaricare BTC")
        return
    btc_raw = add_indicators(btc_raw)
    btc_raw = btc_raw.set_index("dt")

    all_train, all_test = [], []

    for symbol in COINS:
        print(f"Scarico {symbol}...", end=" ", flush=True)
        df = fetch_all_klines(symbol, interval="240", days=900)
        if df.empty:
            print("SKIP (no data)")
            continue
        df = add_indicators(df)

        # Applica filtro BTC su ogni barra
        df = df.set_index("dt")
        btc_ema200       = btc_raw["ema200"].reindex(df.index, method="ffill")
        btc_ema200_slope = btc_raw["ema200_slope"].reindex(df.index, method="ffill")
        btc_c            = btc_raw["C"].reindex(df.index, method="ffill")
        # Filtro BTC come colonna booleana (confronto BTC.C vs BTC.EMA200)
        df["btc_trend_up"] = (btc_c > btc_ema200)  # solo BTC > EMA200, no slope
        df = df.reset_index()

        # Split train/test
        df_train = df[df["dt"] <  TRAIN_END].copy().reset_index(drop=True)
        df_test  = df[(df["dt"] >= TRAIN_END) & (df["dt"] < TEST_END)].copy().reset_index(drop=True)

        t_train = run_backtest(df_train, PARAMS, symbol)
        t_test  = run_backtest(df_test,  PARAMS, symbol)

        all_train.extend(t_train)
        all_test.extend(t_test)

        wins_tr = sum(1 for t in t_train if t["win"])
        wins_te = sum(1 for t in t_test  if t["win"])
        print(f" {len(df)} barre | Train {len(t_train)}t ({wins_tr}W) | Test {len(t_test)}t ({wins_te}W)")

    print("\n" + "=" * 60)
    print("  RISULTATI AGGREGATI")
    print("=" * 60)

    r_train = analyze(all_train, "TRAIN (2024-01 → 2025-06)")
    r_test  = analyze(all_test,  "TEST  (2025-07 → 2026-05)")

    print()
    print("=" * 60)
    print("  SOGLIA LIVE: WR > 52%  |  Ratio > 1.3x  |  Exp > 0")
    if r_test:
        ok_wr    = r_test["wr"]    > 0.52
        ok_ratio = r_test["ratio"] > 1.3
        ok_exp   = r_test["expectancy"] > 0
        verdict  = "✓ PASSA" if (ok_wr and ok_ratio and ok_exp) else "✗ NON PASSA"
        print(f"  WR {r_test['wr']:.1%} {'✓' if ok_wr else '✗'}  |  "
              f"Ratio {r_test['ratio']:.2f}x {'✓' if ok_ratio else '✗'}  |  "
              f"Exp {r_test['expectancy']:+.4f}R {'✓' if ok_exp else '✗'}")
        print(f"  {verdict}")
    print("=" * 60)


if __name__ == "__main__":
    main()
