"""
backtest_v2.py — Backtest due strategie alternative su timeframe 4h.

STRATEGIA A: Mean Reversion
  - Coin scende ≥7% in 2 candele 4h
  - RSI 4h < 35 (ipervenduto)
  - Prezzo sopra EMA200 4h (trend strutturale up)
  - Volume spike ≥1.5× media 20 barre
  - Entry: candela successiva open
  - SL: minimo delle ultime 2 candele - 0.5×ATR
  - TP: +2R (fisso, senza trailing per ora)

STRATEGIA B: Breakout da Consolidamento
  - Coin lateralizza ≥6 candele 4h in range ≤3%
  - Breakout sopra il range con volume ≥2.5× media
  - RSI 4h tra 50-70 (momentum ma non overbought)
  - ADX 4h > 20 (trend nascente)
  - Entry: close della candela di breakout
  - SL: fondo del range di consolidamento
  - TP: +2.5R

Utilizzo:
  python backtest_v2.py [--days 90] [--strategy A|B|both]

Output:
  - Confronto le due strategie + il bot attuale (baseline)
  - Curva equity, WR, PF, max drawdown
"""

import argparse
import time
import requests
import pandas as pd
import numpy as np
from typing import Optional

from ta.volatility import AverageTrueRange
from ta.trend import EMAIndicator, ADXIndicator, SMAIndicator
from ta.momentum import RSIIndicator

# ─────────────────────────────────────────────
BYBIT_BASE       = "https://api.bybit.com"
DEFAULT_LEVERAGE = 10
FEES_PCT         = 0.00055   # 0.055% taker per lato
RISK_PCT         = 0.012     # 1.2% equity per trade
INITIAL_EQUITY   = 100.0

DEFAULT_SYMBOLS = [
    "SOLUSDT","ETHUSDT","ADAUSDT","XRPUSDT","DOGEUSDT",
    "LINKUSDT","AAVEUSDT","ORCAUSDT","HYPEUSDT","SUIUSDT",
    "ZECUSDT","TAOUSDT","1000PEPEUSDT","APEUSDT","ONDOUSDT",
    "BIOUSDT","BNBUSDT","AVAXUSDT","DOTUSDT","MATICUSDT",
    "LTCUSDT","UNIUSDT","ATOMUSDT","NEARUSDT","FTMUSDT",
]

# Cache BTC 4h context (caricata una volta sola)
_btc_df_cache: dict = {}

# ─── Parametri Strategia A (Mean Reversion) ──
MR_DROP_PCT      = 0.05    # calo minimo in 3 candele per segnale
MR_DROP_BARS     = 3       # numero di candele per misurare il calo
MR_RSI_MAX       = 38      # RSI massimo per considerare ipervenduto
MR_VOL_MULT      = 1.2     # spike volume minimo
MR_TP_R          = 1.8     # take profit in multipli di R
MR_SL_MULT       = 1.5     # SL = ATR × questo

# ─── Parametri Strategia B (Breakout Consolidamento) ──
BO_CONSOL_BARS   = 5       # candele minime di consolidamento
BO_RANGE_MAX_PCT = 0.04    # range massimo (4%) per considerare laterale
BO_VOL_MULT      = 2.0     # spike volume al breakout
BO_RSI_MIN       = 48      # RSI minimo
BO_RSI_MAX       = 72      # RSI massimo (non overbought)
BO_ADX_MIN       = 18      # ADX minimo per trend nascente
BO_TP_R          = 2.5     # take profit
BO_SL_MULT       = 1.5     # SL = ATR × questo


def fetch_ohlcv(symbol: str, interval: int, days: int) -> Optional[pd.DataFrame]:
    """Scarica OHLCV da Bybit con paginazione. Interval in minuti."""
    needed = days * 24 * 60 // interval + 300
    url = f"{BYBIT_BASE}/v5/market/kline"
    all_rows: list = []
    end_ts: Optional[int] = None

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
            print(f"  [WARN] fetch {symbol}: {e}")
            return None
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
        return None
    df = pd.DataFrame(all_rows, columns=["ts","Open","High","Low","Close","Volume","turnover"])
    for c in ("Open","High","Low","Close","Volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_numeric(df["ts"]) // 1000
    df.sort_values("ts", inplace=True)
    df.drop_duplicates(subset="ts", inplace=True)
    df.reset_index(drop=True, inplace=True)
    df.dropna(subset=["Close"], inplace=True)
    return df


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    close = df["Close"]
    high  = df["High"]
    low   = df["Low"]
    df["rsi"]    = RSIIndicator(close=close, window=14).rsi()
    df["ema200"] = EMAIndicator(close=close, window=200).ema_indicator()
    df["ema50"]  = EMAIndicator(close=close, window=50).ema_indicator()
    df["ema20"]  = EMAIndicator(close=close, window=20).ema_indicator()
    df["atr"]    = AverageTrueRange(high=high, low=low, close=close, window=14).average_true_range()
    df["adx"]    = ADXIndicator(high=high, low=low, close=close, window=14).adx()
    vol_s        = df["Volume"]
    df["vol_ma20"] = vol_s.rolling(20).mean()
    return df


def get_btc_context(ts: int, days: int) -> bool:
    """Restituisce True se BTC è in trend rialzista al timestamp dato.
    Condizione: EMA20 4h > EMA50 4h (mini golden cross su 4h).
    Usa cache globale per evitare download multipli."""
    global _btc_df_cache
    if "BTCUSDT" not in _btc_df_cache:
        df = fetch_ohlcv("BTCUSDT", interval=240, days=days + 30)
        if df is None:
            return True  # fallback: non bloccare
        df = add_indicators(df)
        _btc_df_cache["BTCUSDT"] = df

    btc = _btc_df_cache["BTCUSDT"]
    # Trova la riga più vicina al timestamp richiesto
    idx_arr = btc["ts"].searchsorted(ts, side="right") - 1
    if idx_arr < 0 or idx_arr >= len(btc):
        return True
    row = btc.iloc[idx_arr]
    ema20 = row.get("ema20", float("nan"))
    ema50 = row.get("ema50", float("nan"))
    if pd.isna(ema20) or pd.isna(ema50):
        return True
    return bool(ema20 > ema50)  # BTC in trend up se EMA20 > EMA50


def simulate_mean_reversion(symbol: str, df: pd.DataFrame, days: int) -> dict:
    """Simula la strategia Mean Reversion su dati 4h."""
    df = add_indicators(df.copy())

    # Taglia al periodo richiesto
    cutoff = df["ts"].iloc[-1] - days * 86400
    df = df[df["ts"] >= cutoff - 200 * 4 * 3600].copy().reset_index(drop=True)

    trades = []
    equity = INITIAL_EQUITY
    equity_curve = [equity]
    in_trade = False
    entry_price = sl = tp = pos_size = 0.0
    cooldown = 0

    start_idx = max(210, len(df) - days * 6 - 5)  # 6 candele 4h per giorno

    for i in range(start_idx, len(df) - 1):
        row = df.iloc[i]
        prev = df.iloc[i - 1]
        prev2 = df.iloc[i - 2] if i >= 2 else prev

        if in_trade:
            # Check SL/TP sulla candela corrente
            lo = row["Low"]
            hi = row["High"]
            cl = row["Close"]
            exit_price = None
            outcome = None
            if lo <= sl:
                exit_price = sl
                outcome = "loss"
            elif hi >= tp:
                exit_price = tp
                outcome = "win"
            elif i == len(df) - 2:
                exit_price = cl
                outcome = "win" if cl >= entry_price else "loss"

            if exit_price is not None:
                pnl = (exit_price - entry_price) * pos_size
                fee = (entry_price + exit_price) * pos_size * FEES_PCT
                net = pnl - fee
                equity += net
                trades.append({
                    "symbol": symbol,
                    "entry": entry_price, "exit": exit_price,
                    "pnl": net, "outcome": outcome
                })
                equity_curve.append(equity)
                in_trade = False
                cooldown = 2

        if cooldown > 0:
            cooldown -= 1
            continue

        if in_trade:
            continue

        # ── SEGNALE MEAN REVERSION ──
        atr_val  = row["atr"]
        rsi_val  = row["rsi"]
        ema50_val = row["ema50"]
        close    = row["Close"]
        vol      = row["Volume"]
        vol_ma   = row["vol_ma20"]
        ts_val   = int(row["ts"])

        if pd.isna(atr_val) or pd.isna(rsi_val):
            continue
        if atr_val / close > 0.10:
            continue

        # Filtro BTC context: salta se BTC non è in trend rialzista
        if not get_btc_context(ts_val, days):
            continue

        # Calo nelle ultime MR_DROP_BARS candele
        lookback = df.iloc[i - MR_DROP_BARS] if i >= MR_DROP_BARS else df.iloc[0]
        drop_nbar = (lookback["Close"] - close) / lookback["Close"] if lookback["Close"] > 0 else 0

        # Non in downtrend strutturale troppo violento (EMA200 non necessaria, ma no free-fall)
        ema50_val = row["ema50"]
        not_freefall = pd.isna(ema50_val) or close > ema50_val * 0.85

        signal = (
            drop_nbar >= MR_DROP_PCT and          # calo ≥5% in 3 candele
            rsi_val < MR_RSI_MAX and              # ipervenduto
            not_freefall and                      # non in free-fall (-15%+ da EMA50)
            vol_ma > 0 and vol >= vol_ma * MR_VOL_MULT  # volume spike
        )

        if signal:
            entry_price = df.iloc[i + 1]["Open"]  # entry alla prossima candela
            if entry_price <= 0:
                continue
            atr_entry = df.iloc[i + 1]["atr"] if not pd.isna(df.iloc[i + 1]["atr"]) else atr_val
            r_dist = atr_entry * MR_SL_MULT
            if r_dist <= 0:
                continue
            sl = entry_price - r_dist
            tp = entry_price + r_dist * MR_TP_R
            pos_size = (equity * RISK_PCT) / r_dist
            in_trade = True

    total_pnl  = sum(t["pnl"] for t in trades)
    wins       = [t for t in trades if t["outcome"] == "win"]
    losses     = [t for t in trades if t["outcome"] == "loss"]
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr         = len(wins) / len(trades) if trades else 0
    avg_win    = gross_win / len(wins) if wins else 0
    avg_loss   = gross_loss / len(losses) if losses else 0

    # Max drawdown
    peak = INITIAL_EQUITY
    max_dd = 0.0
    eq = INITIAL_EQUITY
    for t in trades:
        eq += t["pnl"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    return {
        "symbol": symbol, "strategy": "MeanReversion",
        "trades": len(trades), "wr": wr, "pf": pf,
        "pnl": total_pnl, "avg_win": avg_win, "avg_loss": avg_loss,
        "max_dd": max_dd
    }


def simulate_breakout(symbol: str, df: pd.DataFrame, days: int) -> dict:
    """Simula la strategia Breakout da Consolidamento su dati 4h."""
    df = add_indicators(df.copy())

    cutoff = df["ts"].iloc[-1] - days * 86400
    df = df[df["ts"] >= cutoff - 200 * 4 * 3600].copy().reset_index(drop=True)

    trades = []
    equity = INITIAL_EQUITY
    in_trade = False
    entry_price = sl = tp = pos_size = 0.0
    cooldown = 0

    start_idx = max(220, len(df) - days * 6 - 5)

    for i in range(start_idx, len(df) - 1):
        row = df.iloc[i]

        if in_trade:
            lo = row["Low"]
            hi = row["High"]
            cl = row["Close"]
            exit_price = None
            outcome = None
            if lo <= sl:
                exit_price = sl
                outcome = "loss"
            elif hi >= tp:
                exit_price = tp
                outcome = "win"
            elif i == len(df) - 2:
                exit_price = cl
                outcome = "win" if cl >= entry_price else "loss"

            if exit_price is not None:
                pnl = (exit_price - entry_price) * pos_size
                fee = (entry_price + exit_price) * pos_size * FEES_PCT
                net = pnl - fee
                equity += net
                trades.append({
                    "symbol": symbol,
                    "entry": entry_price, "exit": exit_price,
                    "pnl": net, "outcome": outcome
                })
                in_trade = False
                cooldown = 2

        if cooldown > 0:
            cooldown -= 1
            continue
        if in_trade:
            continue

        # ── SEGNALE BREAKOUT ──
        if i < BO_CONSOL_BARS + 1:
            continue

        atr_val = row["atr"]
        rsi_val = row["rsi"]
        adx_val = row["adx"]
        close   = row["Close"]
        vol     = row["Volume"]
        vol_ma  = row["vol_ma20"]
        ts_val  = int(row["ts"])

        if pd.isna(atr_val) or pd.isna(rsi_val) or pd.isna(adx_val):
            continue
        if atr_val / close > 0.10:
            continue

        # Filtro BTC context: salta se BTC non è in trend rialzista
        if not get_btc_context(ts_val, days):
            continue

        # Finestra di consolidamento: ultime BO_CONSOL_BARS candele (esclusa corrente)
        window = df.iloc[i - BO_CONSOL_BARS: i]
        w_high = window["High"].max()
        w_low  = window["Low"].min()
        w_close_last = window["Close"].iloc[-1]

        if w_low <= 0:
            continue
        range_pct = (w_high - w_low) / w_low

        # Condizioni breakout
        signal = (
            range_pct <= BO_RANGE_MAX_PCT and         # laterale ≤3%
            close > w_high and                         # breakout sopra range
            vol_ma > 0 and vol >= vol_ma * BO_VOL_MULT and  # volume spike
            BO_RSI_MIN <= rsi_val <= BO_RSI_MAX and   # RSI in zona buona
            adx_val > BO_ADX_MIN                       # trend nascente
        )

        if signal:
            entry_price = close  # entry sulla close di breakout
            r_dist = atr_val * BO_SL_MULT
            if r_dist <= 0:
                continue
            sl = w_low  # SL al fondo del range
            # Aggiusta r_dist al vero SL
            actual_r = entry_price - sl
            if actual_r <= 0:
                continue
            tp = entry_price + actual_r * BO_TP_R
            pos_size = (equity * RISK_PCT) / actual_r
            in_trade = True

    total_pnl  = sum(t["pnl"] for t in trades)
    wins       = [t for t in trades if t["outcome"] == "win"]
    losses     = [t for t in trades if t["outcome"] == "loss"]
    gross_win  = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    pf         = gross_win / gross_loss if gross_loss > 0 else float("inf")
    wr         = len(wins) / len(trades) if trades else 0
    avg_win    = gross_win / len(wins) if wins else 0
    avg_loss   = gross_loss / len(losses) if losses else 0

    peak = INITIAL_EQUITY
    max_dd = 0.0
    eq = INITIAL_EQUITY
    for t in trades:
        eq += t["pnl"]
        if eq > peak:
            peak = eq
        dd = (peak - eq) / peak
        if dd > max_dd:
            max_dd = dd

    return {
        "symbol": symbol, "strategy": "Breakout",
        "trades": len(trades), "wr": wr, "pf": pf,
        "pnl": total_pnl, "avg_win": avg_win, "avg_loss": avg_loss,
        "max_dd": max_dd
    }


def aggregate(results: list) -> dict:
    if not results:
        return {}
    total_trades = sum(r["trades"] for r in results)
    total_wins   = sum(int(r["wr"] * r["trades"]) for r in results)
    total_pnl    = sum(r["pnl"] for r in results)
    all_wins_val = sum(r["avg_win"] * int(r["wr"] * r["trades"]) for r in results)
    all_loss_val = sum(r["avg_loss"] * (r["trades"] - int(r["wr"] * r["trades"])) for r in results)
    pf           = all_wins_val / all_loss_val if all_loss_val > 0 else float("inf")
    wr           = total_wins / total_trades if total_trades > 0 else 0
    avg_win      = all_wins_val / total_wins if total_wins > 0 else 0
    avg_loss_n   = total_trades - total_wins
    avg_loss     = all_loss_val / avg_loss_n if avg_loss_n > 0 else 0
    max_dd       = max(r["max_dd"] for r in results) if results else 0
    return {
        "trades": total_trades, "wr": wr, "pf": pf,
        "pnl": total_pnl, "avg_win": avg_win, "avg_loss": avg_loss,
        "max_dd": max_dd
    }


def print_results(label: str, agg: dict):
    print(f"\n{'═'*60}")
    print(f"  {label}")
    print(f"{'═'*60}")
    if not agg:
        print("  Nessun trade generato.")
        return
    print(f"  Trade totali  : {agg['trades']}")
    print(f"  Win Rate      : {agg['wr']*100:.1f}%")
    print(f"  Profit Factor : {agg['pf']:.2f}")
    print(f"  PnL totale    : {agg['pnl']:+.4f} USDT  (su {INITIAL_EQUITY} USDT)")
    print(f"  PnL %         : {agg['pnl']/INITIAL_EQUITY*100:+.2f}%")
    print(f"  Avg vincita   : +{agg['avg_win']:.4f} USDT")
    print(f"  Avg perdita   : -{agg['avg_loss']:.4f} USDT")
    print(f"  R:R ratio     : {agg['avg_win']/agg['avg_loss']:.2f}" if agg['avg_loss'] > 0 else "  R:R ratio     : ∞")
    print(f"  Max Drawdown  : {agg['max_dd']*100:.1f}%")


def main():
    parser = argparse.ArgumentParser(description="Backtest V2 — Strategie Mean Reversion & Breakout 4h")
    parser.add_argument("--days",          type=int, default=60,    help="Giorni di backtest (default 60)")
    parser.add_argument("--strategy",      type=str, default="both", choices=["A","B","both"])
    parser.add_argument("--symbols",       type=str, default="",    help="Simboli separati da virgola")
    parser.add_argument("--no-btc-filter", action="store_true",     help="Disabilita filtro BTC context")
    args = parser.parse_args()

    symbols = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else DEFAULT_SYMBOLS
    days    = args.days

    print(f"\n{'═'*60}")
    print(f"  BACKTEST V2 — {days} giorni — {len(symbols)} simboli")
    print(f"  Strategia: {args.strategy} | BTC filter: {'OFF' if args.no_btc_filter else 'ON'}")
    print(f"{'═'*60}")

    # Pre-carica BTC context se richiesto
    if not args.no_btc_filter:
        print("  Carico BTC 4h per context filter...", end=" ", flush=True)
        btc_df = fetch_ohlcv("BTCUSDT", interval=240, days=days + 30)
        if btc_df is not None:
            _btc_df_cache["BTCUSDT"] = add_indicators(btc_df)
            print("ok")
        else:
            print("WARN: dati BTC non disponibili, filtro disabilitato")

    results_mr = []
    results_bo = []

    for sym in symbols:
        if sym == "BTCUSDT" and not args.no_btc_filter:
            # BTC già caricato nel context filter, ma simuliamolo comunque
            pass
        print(f"  {sym}...", end=" ", flush=True)
        # Scarica dati 4h (interval=240)
        df = fetch_ohlcv(sym, interval=240, days=days)
        if df is None or len(df) < 250:
            print("skip (dati insufficienti)")
            continue

        if args.strategy in ("A", "both"):
            r = simulate_mean_reversion(sym, df, days)
            results_mr.append(r)

        if args.strategy in ("B", "both"):
            r = simulate_breakout(sym, df, days)
            results_bo.append(r)

        print("ok")
        time.sleep(0.05)

    # ── RISULTATI ──
    if args.strategy in ("A", "both") and results_mr:
        agg = aggregate(results_mr)
        print_results("STRATEGIA A — Mean Reversion 4h", agg)

        # Breakdown per simbolo
        results_mr.sort(key=lambda x: x["pnl"], reverse=True)
        print(f"\n  {'Simbolo':<16} {'Trade':>6} {'WR':>7} {'PnL':>10} {'PF':>6}")
        print(f"  {'-'*50}")
        for r in results_mr:
            icon = "✅" if r["pnl"] >= 0 else "❌"
            print(f"  {icon} {r['symbol']:<14} {r['trades']:>6} {r['wr']*100:>6.0f}% {r['pnl']:>+9.4f} {r['pf']:>6.2f}")

    if args.strategy in ("B", "both") and results_bo:
        agg = aggregate(results_bo)
        print_results("STRATEGIA B — Breakout da Consolidamento 4h", agg)

        results_bo.sort(key=lambda x: x["pnl"], reverse=True)
        print(f"\n  {'Simbolo':<16} {'Trade':>6} {'WR':>7} {'PnL':>10} {'PF':>6}")
        print(f"  {'-'*50}")
        for r in results_bo:
            icon = "✅" if r["pnl"] >= 0 else "❌"
            print(f"  {icon} {r['symbol']:<14} {r['trades']:>6} {r['wr']*100:>6.0f}% {r['pnl']:>+9.4f} {r['pf']:>6.2f}")

    if args.strategy == "both" and results_mr and results_bo:
        print(f"\n{'═'*60}")
        print("  CONFRONTO FINALE")
        print(f"{'═'*60}")
        agg_mr = aggregate(results_mr)
        agg_bo = aggregate(results_bo)
        print(f"  {'':20} {'MeanRev':>10} {'Breakout':>10}")
        print(f"  {'Trade':20} {agg_mr['trades']:>10} {agg_bo['trades']:>10}")
        print(f"  {'Win Rate':20} {agg_mr['wr']*100:>9.1f}% {agg_bo['wr']*100:>9.1f}%")
        print(f"  {'Profit Factor':20} {agg_mr['pf']:>10.2f} {agg_bo['pf']:>10.2f}")
        print(f"  {'PnL %':20} {agg_mr['pnl']/INITIAL_EQUITY*100:>+9.2f}% {agg_bo['pnl']/INITIAL_EQUITY*100:>+9.2f}%")
        print(f"  {'Max Drawdown':20} {agg_mr['max_dd']*100:>9.1f}% {agg_bo['max_dd']*100:>9.1f}%")
        print(f"  {'R:R ratio':20} {agg_mr['avg_win']/max(agg_mr['avg_loss'],1e-9):>10.2f} {agg_bo['avg_win']/max(agg_bo['avg_loss'],1e-9):>10.2f}")
        print()

    print("\n  ✅ Backtest completato.")


if __name__ == "__main__":
    main()
