"""
Walk-Forward Validation — Trend Following 4h EMA20 Pullback
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Metodologia:
  TRAIN (giorni 1-365):  grid search → trova parametri vincitori in-sample
  TEST  (giorni 366-730): applica quei STESSI parametri senza ricalibrazione
  → Se PF_test > 1.0: edge probabilmente reale
  → Se PF_test ≈ 1.0 o < 1: era overfitting
  
Bonus: Monte Carlo bootstrap per stima incertezza sul PF_train.
"""
import time, requests, warnings
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from itertools import product

warnings.filterwarnings("ignore")
np.random.seed(42)

BASE = "https://api.bybit.com"
COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "LINKUSDT", "ATOMUSDT", "DOTUSDT", "NEARUSDT", "AVAXUSDT",
    "UNIUSDT", "AAVEUSDT", "INJUSDT", "ZECUSDT", "SUIUSDT",
]

FEE_RT     = 0.0011
SL_BUFFER  = 0.3
TRAIL_MULT = 2.0
TRAIL_R    = 1.5
MAX_HOLD   = 40

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def download(symbol, interval_min, days=730):
    interval = str(interval_min)
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows_all = []
    cur_end  = end_ms
    while True:
        try:
            r = requests.get(f"{BASE}/v5/market/kline",
                params={"category":"linear","symbol":symbol,"interval":interval,
                        "limit":200,"end":str(cur_end)}, timeout=15)
            d = r.json()
        except Exception:
            break
        if d.get("retCode") != 0: break
        rows = d["result"]["list"]
        if not rows: break
        rows_all.extend(rows)
        oldest = int(rows[-1][0])
        if oldest <= start_ms: break
        cur_end = oldest - 1
        time.sleep(0.05)
    if not rows_all: return None
    rows_all = [r for r in rows_all if int(r[0]) >= start_ms]
    rows_all.sort(key=lambda x: int(x[0]))
    df = pd.DataFrame(rows_all, columns=["ts","Open","High","Low","Close","Volume","Turnover"])
    for c in ["Open","High","Low","Close","Volume"]: df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    return df.drop_duplicates("ts").reset_index(drop=True)

# ── INDICATORI ────────────────────────────────────────────────────────────────
def prepare_4h(df4):
    df = df4.copy()
    df["EMA20"]    = df["Close"].ewm(span=20, adjust=False).mean()
    df["RSI"]      = RSIIndicator(df["Close"], window=14).rsi()
    df["ATR"]      = AverageTrueRange(df["High"],df["Low"],df["Close"],window=14).average_true_range()
    df["VolAvg20"] = df["Volume"].shift(1).rolling(20).mean()
    df["VolRatio"] = df["Volume"] / df["VolAvg20"]
    rng            = (df["High"] - df["Low"]).replace(0, np.nan)
    df["BodyPct"]  = (df["Close"] - df["Open"]).abs() / rng * 100
    df["SwingLow3"]= df["Low"].shift(1).rolling(3).min()
    df["SL_price"] = df["SwingLow3"] - SL_BUFFER * df["ATR"]
    df["R_dist"]   = df["Close"] - df["SL_price"]
    df["SL_pct"]   = df["R_dist"] / df["Close"] * 100
    return df

def compute_daily_ok(df4, df_d):
    dd = df_d[["ts","Close"]].copy()
    dd["EMA50"]    = df_d["Close"].ewm(span=50, adjust=False).mean()
    dd["EMA50_5d"] = dd["EMA50"].shift(5)
    dd["day"]      = dd["ts"].dt.normalize()
    dd = dd.drop_duplicates("day").sort_values("day").reset_index(drop=True)
    dd["day"] = dd["day"].astype("datetime64[ns]")
    prev_days = (df4["ts"].dt.normalize() - pd.Timedelta(days=1)).astype("datetime64[ns]")
    df4_work = pd.DataFrame({"pos": df4.index, "day": prev_days}).sort_values("day")
    merged = pd.merge_asof(df4_work, dd[["day","Close","EMA50","EMA50_5d"]],
                           on="day", direction="backward").sort_values("pos").reset_index(drop=True)
    close    = merged["Close"]
    ema50    = merged["EMA50"]
    ema50_5d = merged["EMA50_5d"]
    valid    = close.notna() & ema50.notna() & ema50_5d.notna() & (ema50 > 0)
    uptrend  = valid & (close > ema50) & (ema50 > ema50_5d)
    dist     = (close - ema50) / ema50.replace(0, np.nan) * 100
    return {md: (uptrend & (dist <= md)) for md in [15, 20, 25, 30]}

# ── SIMULAZIONE TRADE ─────────────────────────────────────────────────────────
def simulate(df4, entry_idx, entry_price, sl_price, r_dist, atr0):
    if entry_idx >= len(df4): return np.nan
    trail_active, trail_sl, peak = False, sl_price, entry_price
    for i in range(entry_idx, min(entry_idx + MAX_HOLD, len(df4))):
        hi = df4["High"].iat[i]; lo = df4["Low"].iat[i]
        atr = df4["ATR"].iat[i]
        if pd.isna(atr) or atr <= 0: atr = atr0
        if hi > peak: peak = hi
        if not trail_active and peak >= entry_price + TRAIL_R * r_dist:
            trail_active = True
        if trail_active:
            trail_sl = max(trail_sl, peak - TRAIL_MULT * atr)
        active_sl = trail_sl if trail_active else sl_price
        if lo <= active_sl:
            return (active_sl - entry_price) / r_dist - FEE_RT / (r_dist / entry_price)
    ep = df4["Close"].iat[min(entry_idx + MAX_HOLD - 1, len(df4) - 1)]
    return (ep - entry_price) / r_dist - FEE_RT / (r_dist / entry_price)

# ── BACKTEST SU FINESTRA TEMPORALE ────────────────────────────────────────────
def run_combo(coin_data, daily_ok_cache, body_pct, vol_ratio, max_dist50,
              date_from=None, date_to=None,
              rsi_min=30, rsi_max=65, ema_tol=0.012, max_dist_ema=3.0):
    trades = []
    for sym, df4 in coin_data.items():
        c   = df4["Close"]; l = df4["Low"]; o = df4["Open"]
        e20 = df4["EMA20"]; rsi = df4["RSI"]
        bp  = df4["BodyPct"]; vr = df4["VolRatio"]
        rd  = df4["R_dist"]; slp = df4["SL_pct"]
        slpr= df4["SL_price"]; atr = df4["ATR"]
        dema= (c - e20) / e20 * 100
        dok = daily_ok_cache[sym][max_dist50]

        mask = (
            (l <= e20 * (1 + ema_tol)) &
            (c >= e20) & (c > o) &
            rsi.between(rsi_min, rsi_max) &
            (dema <= max_dist_ema) &
            (bp >= body_pct) &
            (vr >= vol_ratio) &
            (rd > 0) & (slp <= 8.0) &
            dok
        )
        mask.iloc[:50]  = False
        mask.iloc[-2:]  = False
        # Filtro temporale (walk-forward)
        if date_from is not None: mask &= (df4["ts"] >= date_from)
        if date_to   is not None: mask &= (df4["ts"] <  date_to)

        for idx in df4.index[mask]:
            i = int(idx)
            if i + 1 >= len(df4): continue
            entry = df4["Open"].iat[i + 1]
            sl    = float(slpr.iat[i])
            rd_v  = entry - sl
            if rd_v <= 0: continue
            pnl = simulate(df4, i + 1, entry, sl, rd_v, float(atr.iat[i]))
            if not pd.isna(pnl): trades.append(pnl)

    if len(trades) < 5: return None
    t  = np.array(trades)
    w  = t[t > 0]; ls = t[t <= 0]
    return {
        "n":        len(t),
        "wr":       round(len(w) / len(t) * 100, 1),
        "pf":       round(w.sum() / abs(ls.sum()), 3) if ls.sum() != 0 else 999,
        "exp":      round(t.mean(), 4),
        "avg_win":  round(w.mean()  if len(w)  else 0, 3),
        "avg_loss": round(ls.mean() if len(ls) else 0, 3),
        "total_R":  round(t.sum(), 2),
        "trades":   t,  # per Monte Carlo
    }

# ── MONTE CARLO BOOTSTRAP ─────────────────────────────────────────────────────
def monte_carlo_pf(trades_arr, n_sim=5000):
    """Bootstrap: campiona i trade con reintroduzione, calcola PF distribuzione."""
    n = len(trades_arr)
    pfs = []
    for _ in range(n_sim):
        s  = np.random.choice(trades_arr, size=n, replace=True)
        w  = s[s > 0]; ls = s[s <= 0]
        if ls.sum() != 0:
            pfs.append(w.sum() / abs(ls.sum()))
    pfs = np.array(pfs)
    return {
        "mean":  round(float(np.mean(pfs)), 3),
        "p5":    round(float(np.percentile(pfs, 5)), 3),
        "p25":   round(float(np.percentile(pfs, 25)), 3),
        "p50":   round(float(np.percentile(pfs, 50)), 3),
        "p75":   round(float(np.percentile(pfs, 75)), 3),
        "p95":   round(float(np.percentile(pfs, 95)), 3),
        "pct_above_1": round(float(np.mean(pfs > 1.0)) * 100, 1),
    }

# ── EQUITY CURVE ──────────────────────────────────────────────────────────────
def equity_curve_stats(trades_arr):
    eq = np.cumsum(trades_arr)
    peak = np.maximum.accumulate(eq)
    dd   = eq - peak
    return {
        "max_dd_R":    round(float(np.min(dd)), 2),
        "avg_dd_R":    round(float(np.mean(dd[dd < 0])) if np.any(dd < 0) else 0, 2),
        "calmar":      round(float(eq[-1] / abs(np.min(dd))) if np.min(dd) < 0 else 999, 2),
        "sharpe":      round(float(np.mean(trades_arr) / (np.std(trades_arr) + 1e-9)), 3),
    }

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    t0 = time.time()
    print("="*70)
    print("  WALK-FORWARD VALIDATION — 4h EMA20 Pullback")
    print("  Train: anno 1 (giorni 1-365)  |  Test: anno 2 (giorni 366-730)")
    print("="*70)

    print("\nScaricamento dati (730 giorni)...")
    coin_data = {}; daily_ok_cache = {}

    for sym in COINS:
        print(f"  {sym}...", end=" ", flush=True)
        df4  = download(sym, 240, days=730)
        df_d = download(sym, "D",  days=790)
        if df4 is None or df_d is None or len(df4) < 200 or len(df_d) < 60:
            print("SKIP"); continue
        df4 = prepare_4h(df4)
        daily_ok_cache[sym] = compute_daily_ok(df4, df_d)
        coin_data[sym] = df4
        print(f"{len(df4)} barre 4h")
        time.sleep(0.2)

    # Data split
    # Prendi la data minima e massima comune a tutti i coin
    all_starts = [df4["ts"].min() for df4 in coin_data.values()]
    all_ends   = [df4["ts"].max() for df4 in coin_data.values()]
    global_start = max(all_starts)
    global_end   = min(all_ends)
    mid = global_start + (global_end - global_start) / 2
    mid = mid.replace(hour=0, minute=0, second=0, microsecond=0)

    print(f"\n  Range dati: {global_start.date()} → {global_end.date()}")
    print(f"  Split date: {mid.date()}")
    print(f"  TRAIN: {global_start.date()} → {mid.date()}")
    print(f"  TEST:  {mid.date()} → {global_end.date()}")
    print(f"\nCoin caricate: {len(coin_data)}")

    BODY_PCTS   = [20, 25, 30, 35, 40, 45]
    VOL_RATIOS  = [0.8, 1.0, 1.1, 1.2, 1.5]
    EMA50_DISTS = [15, 20, 25, 30]

    # ── FASE 1: GRID SEARCH SUL TRAIN ────────────────────────────────────────
    print("\n" + "─"*70)
    print("FASE 1 — Grid Search su TRAIN (anno 1)")
    print("─"*70)

    train_results = []
    total = len(BODY_PCTS) * len(VOL_RATIOS) * len(EMA50_DISTS)
    done  = 0

    for body, vol, dist50 in product(BODY_PCTS, VOL_RATIOS, EMA50_DISTS):
        done += 1
        r = run_combo(coin_data, daily_ok_cache, body, vol, dist50,
                      date_from=global_start, date_to=mid)
        if r:
            train_results.append({
                "body": body, "vol": vol, "dist50": dist50,
                **{k: v for k, v in r.items() if k != "trades"},
                "_trades": r["trades"],
            })
        if done % 30 == 0:
            print(f"  {done}/{total} completate...")

    df_train = pd.DataFrame(train_results).sort_values("pf", ascending=False)
    df_train_show = df_train.drop(columns=["_trades"])

    print(f"\nTOP 10 sul TRAIN:")
    cols = ["body","vol","dist50","n","wr","pf","exp","total_R"]
    print(df_train_show[cols].head(10).to_string(index=False))

    # Parametri attuali del bot
    bot_train = df_train[(df_train.body==40)&(df_train.vol==1.5)&(df_train.dist50==20)]
    print(f"\nParametri bot live (body=40, vol=1.5, dist50=20) sul TRAIN:")
    if len(bot_train):
        row = bot_train.iloc[0]
        print(f"  N={int(row.n)}  WR={row.wr}%  PF={row.pf}  EXP={row.exp}R  totalR={row.total_R}")
    else:
        print("  nessun trade nel periodo")

    # ── FASE 2: TEST OOS CON TOP 5 PARAMETRI TRAIN ───────────────────────────
    print("\n" + "─"*70)
    print("FASE 2 — Test OUT-OF-SAMPLE (anno 2) con i top parametri del train")
    print("─"*70)
    print(f"{'body':>5} {'vol':>5} {'dist50':>7} │ {'TRAIN n':>8} {'TRAIN PF':>10} │ "
          f"{'TEST n':>7} {'TEST PF':>9} {'TEST EXP':>9} {'TEST totalR':>12}")
    print("─"*70)

    test_details = []
    # Testa i top 10 train + parametri bot live
    top10 = list(df_train[["body","vol","dist50"]].head(10).itertuples(index=False))
    bot_params = (40, 1.5, 20)
    to_test = list(set(top10 + [bot_params]))

    for body, vol, dist50 in to_test:
        train_row = df_train[(df_train.body==body)&(df_train.vol==vol)&(df_train.dist50==dist50)]
        r_test = run_combo(coin_data, daily_ok_cache, body, vol, dist50,
                           date_from=mid, date_to=global_end)

        train_n  = int(train_row.iloc[0].n) if len(train_row) else 0
        train_pf = float(train_row.iloc[0].pf) if len(train_row) else 0
        test_n   = r_test["n"]      if r_test else 0
        test_pf  = r_test["pf"]     if r_test else 0
        test_exp = r_test["exp"]    if r_test else 0
        test_R   = r_test["total_R"]if r_test else 0

        flag = "✅" if test_pf >= 1.0 else "❌"
        is_bot = "◄ BOT LIVE" if (body, vol, dist50) == bot_params else ""
        print(f"{body:>5} {vol:>5} {dist50:>7} │ {train_n:>8} {train_pf:>10.3f} │ "
              f"{test_n:>7} {test_pf:>9.3f} {test_exp:>9.4f} {test_R:>12.2f}  {flag} {is_bot}")
        test_details.append({
            "body": body, "vol": vol, "dist50": dist50,
            "train_pf": train_pf, "test_pf": test_pf,
            "test_n": test_n, "test_exp": test_exp,
        })

    # ── FASE 3: MONTE CARLO SUL BOT LIVE (train) ─────────────────────────────
    print("\n" + "─"*70)
    print("FASE 3 — Monte Carlo bootstrap (5000 simulazioni) sui parametri bot live")
    print("─"*70)

    bot_row = df_train[(df_train.body==40)&(df_train.vol==1.5)&(df_train.dist50==20)]
    if len(bot_row):
        trades_arr = bot_row.iloc[0]["_trades"]
        mc = monte_carlo_pf(trades_arr)
        eq = equity_curve_stats(trades_arr)

        print(f"\n  TRAIN — Distribuzione PF (bootstrap 5000 campioni):")
        print(f"  P5={mc['p5']}  P25={mc['p25']}  P50={mc['p50']}  "
              f"P75={mc['p75']}  P95={mc['p95']}")
        print(f"  PF medio bootstrap: {mc['mean']}")
        print(f"  % simulazioni con PF > 1.0: {mc['pct_above_1']}%")
        print(f"\n  TRAIN — Equity curve:")
        print(f"  Max Drawdown: {eq['max_dd_R']}R  |  Calmar: {eq['calmar']}  |  Sharpe(R): {eq['sharpe']}")
    else:
        print("  Parametri bot live non trovati nel train!")

    # ── FASE 4: MONTE CARLO SUL BOT LIVE (test OOS) ──────────────────────────
    r_bot_test = run_combo(coin_data, daily_ok_cache, 40, 1.5, 20,
                           date_from=mid, date_to=global_end)
    if r_bot_test and r_bot_test["n"] >= 10:
        trades_test = r_bot_test["trades"]
        mc_t = monte_carlo_pf(trades_test)
        eq_t = equity_curve_stats(trades_test)

        print(f"\n  TEST OOS — Distribuzione PF (bootstrap 5000 campioni):")
        print(f"  P5={mc_t['p5']}  P25={mc_t['p25']}  P50={mc_t['p50']}  "
              f"P75={mc_t['p75']}  P95={mc_t['p95']}")
        print(f"  PF medio bootstrap: {mc_t['mean']}")
        print(f"  % simulazioni con PF > 1.0: {mc_t['pct_above_1']}%")
        print(f"\n  TEST OOS — Equity curve:")
        print(f"  Max Drawdown: {eq_t['max_dd_R']}R  |  Calmar: {eq_t['calmar']}  |  Sharpe(R): {eq_t['sharpe']}")
    else:
        print(f"\n  TEST OOS: troppo pochi trade ({r_bot_test['n'] if r_bot_test else 0}) per bootstrap")

    # ── VERDETTO FINALE ───────────────────────────────────────────────────────
    print("\n" + "="*70)
    print("VERDETTO FINALE")
    print("="*70)

    test_df = pd.DataFrame(test_details)
    n_oos_positive = int((test_df["test_pf"] >= 1.0).sum())
    n_tested = len(test_df)
    bot_test_pf = float(test_df[(test_df.body==40)&(test_df.vol==1.5)&(test_df.dist50==20)]["test_pf"].iloc[0]) \
        if len(test_df[(test_df.body==40)&(test_df.vol==1.5)&(test_df.dist50==20)]) else 0

    print(f"\n  Top {n_tested} combo dal train: {n_oos_positive}/{n_tested} profittevoli OOS")
    print(f"  Parametri bot live — OOS PF: {bot_test_pf:.3f}")
    print()

    if bot_test_pf >= 1.10:
        verdict = "EDGE ROBUSTO ✅ — I parametri funzionano anche out-of-sample. Continua."
    elif bot_test_pf >= 1.0:
        verdict = "EDGE MARGINALE ⚠️ — Funziona OOS ma appena. Monitora strettamente."
    elif bot_test_pf >= 0.95:
        verdict = "EDGE INCERTO ⚠️ — Quasi breakeven OOS. Probabilmente overfitting."
    else:
        verdict = "OVERFITTING ❌ — Non funziona OOS. Strategia da rivedere."

    print(f"  {verdict}")
    print(f"\n  Stima annuale realistica (da OOS):")
    if r_bot_test:
        trades_per_year_oos = r_bot_test["n"]  # già è 1 anno
        exp_oos = r_bot_test["exp"]
        print(f"  {trades_per_year_oos} trade/anno × {exp_oos:.4f}R = {trades_per_year_oos*exp_oos:.1f}R = "
              f"{trades_per_year_oos*exp_oos:.0f}% atteso (con RISK=1%)")

    print(f"\n  Durata totale: {time.time()-t0:.0f}s")
