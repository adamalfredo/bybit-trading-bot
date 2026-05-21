"""
Backtest VETTORIZZATO — Trend Following 4h EMA20 Pullback.
Obiettivo: calibrare MIN_BODY_PCT, MIN_VOL_RATIO, MAX_DIST_EMA50_D.

Metodologia:
  - Entry: open della candela SUCCESSIVA al segnale (realistico)
  - Exit: SL fisso → -1R | Trailing 2×ATR attivo da 1.5R | Timeout 40 candele
  - P&L espresso in R (multipli del rischio)
  - Fee: 0.055% per lato × 2 = 0.11% round-trip (taker Bybit)
  - Leva 5× implicita nel sizing (1% equity / r_dist)
  - Survivorship bias: solo coin esistenti oggi (nota il limite)
"""
import time, requests, warnings
import pandas as pd
import numpy as np
from ta.momentum import RSIIndicator
from ta.volatility import AverageTrueRange
from itertools import product

warnings.filterwarnings("ignore")

BASE = "https://api.bybit.com"

COINS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT",
    "LINKUSDT", "ATOMUSDT", "DOTUSDT", "NEARUSDT", "AVAXUSDT",
    "UNIUSDT", "AAVEUSDT", "INJUSDT", "ZECUSDT", "SUIUSDT",
]

# ── DOWNLOAD ──────────────────────────────────────────────────────────────────
def download(symbol, interval_min, days=730):
    """Scarica `days` giorni di candele con paginazione."""
    interval = str(interval_min)
    end_ms   = int(time.time() * 1000)
    start_ms = end_ms - days * 86_400_000
    rows_all = []
    cur_end  = end_ms

    while True:
        try:
            r = requests.get(
                f"{BASE}/v5/market/kline",
                params={"category": "linear", "symbol": symbol,
                        "interval": interval, "limit": 200, "end": str(cur_end)},
                timeout=15,
            )
            d = r.json()
        except Exception:
            break
        if d.get("retCode") != 0:
            break
        rows = d["result"]["list"]
        if not rows:
            break
        rows_all.extend(rows)
        oldest = int(rows[-1][0])
        if oldest <= start_ms:
            break
        cur_end = oldest - 1
        time.sleep(0.05)

    if not rows_all:
        return None

    rows_all = [r for r in rows_all if int(r[0]) >= start_ms]
    rows_all.sort(key=lambda x: int(x[0]))
    df = pd.DataFrame(rows_all,
                      columns=["ts", "Open", "High", "Low", "Close", "Volume", "Turnover"])
    for c in ["Open", "High", "Low", "Close", "Volume"]:
        df[c] = df[c].astype(float)
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    return df.drop_duplicates("ts").reset_index(drop=True)


# ── PREPARA INDICATORI 4H (una volta sola) ────────────────────────────────────
def prepare_4h(df4):
    df = df4.copy()
    df["EMA20"]     = df["Close"].ewm(span=20, adjust=False).mean()
    df["RSI"]       = RSIIndicator(df["Close"], window=14).rsi()
    df["ATR"]       = AverageTrueRange(df["High"], df["Low"],
                                       df["Close"], window=14).average_true_range()
    # Volume ratio senza lookahead: usa la media delle 20 barre PRECEDENTI
    df["VolAvg20"]  = df["Volume"].shift(1).rolling(20).mean()
    df["VolRatio"]  = df["Volume"] / df["VolAvg20"]
    rng             = (df["High"] - df["Low"]).replace(0, np.nan)
    df["BodyPct"]   = (df["Close"] - df["Open"]).abs() / rng * 100
    # Swing low delle 3 barre precedenti (shift evita lookahead)
    df["SwingLow3"] = df["Low"].shift(1).rolling(3).min()
    df["SL_price"]  = df["SwingLow3"] - SL_BUFFER * df["ATR"]
    df["R_dist"]    = df["Close"] - df["SL_price"]
    df["SL_pct"]    = df["R_dist"] / df["Close"] * 100
    return df


# ── PRECOMPUTA DAILY OK (completamente vettorizzato) ──────────────────────────
def compute_daily_ok(df4, df_d):
    """
    Per ogni 4h candle trova la daily close piu recente (giorno precedente)
    e calcola uptrend + distanza EMA50 in modo vettoriale con merge_asof.
    Ritorna dict {max_dist: pd.Series(bool, index=range(len(df4)))}
    """
    dd = df_d[["ts", "Close"]].copy()
    dd["EMA50"]    = df_d["Close"].ewm(span=50, adjust=False).mean()
    dd["EMA50_5d"] = dd["EMA50"].shift(5)
    dd["day"]      = dd["ts"].dt.normalize()
    dd = dd.drop_duplicates("day").sort_values("day").reset_index(drop=True)

    # Per ogni 4h: usa la daily del GIORNO PRECEDENTE (closed)
    # Cast esplicito a ns per evitare mismatch us/ms nel merge_asof
    dd["day"] = dd["day"].astype("datetime64[ns]")
    prev_days = (df4["ts"].dt.normalize() - pd.Timedelta(days=1)).astype("datetime64[ns]")
    df4_work = pd.DataFrame({
        "pos": df4.index,
        "day": prev_days,
    }).sort_values("day")

    merged = pd.merge_asof(
        df4_work,
        dd[["day", "Close", "EMA50", "EMA50_5d"]],
        on="day",
        direction="backward",
    ).sort_values("pos").reset_index(drop=True)

    close    = merged["Close"]
    ema50    = merged["EMA50"]
    ema50_5d = merged["EMA50_5d"]
    valid    = close.notna() & ema50.notna() & ema50_5d.notna() & (ema50 > 0)
    uptrend  = valid & (close > ema50) & (ema50 > ema50_5d)
    dist     = (close - ema50) / ema50.replace(0, np.nan) * 100

    return {md: (uptrend & (dist <= md)) for md in [15, 20, 25, 30]}


# ── SIMULAZIONE SINGOLO TRADE ─────────────────────────────────────────────────
FEE_RT     = 0.0011   # 0.055% × 2 lati
SL_BUFFER  = 0.3
TRAIL_MULT = 2.0
TRAIL_R    = 1.5
MAX_HOLD   = 40       # candele max (~6.7 giorni 4h)


def simulate(df4, entry_idx, entry_price, sl_price, r_dist, atr0):
    if entry_idx >= len(df4):
        return np.nan
    trail_active, trail_sl, peak = False, sl_price, entry_price
    for i in range(entry_idx, min(entry_idx + MAX_HOLD, len(df4))):
        hi  = df4["High"].iat[i]
        lo  = df4["Low"].iat[i]
        atr = df4["ATR"].iat[i]
        if pd.isna(atr) or atr <= 0:
            atr = atr0
        if hi > peak:
            peak = hi
        if not trail_active and peak >= entry_price + TRAIL_R * r_dist:
            trail_active = True
        if trail_active:
            trail_sl = max(trail_sl, peak - TRAIL_MULT * atr)
        active_sl = trail_sl if trail_active else sl_price
        if lo <= active_sl:
            pnl_r = (active_sl - entry_price) / r_dist
            return pnl_r - FEE_RT / (r_dist / entry_price)
    ep = df4["Close"].iat[min(entry_idx + MAX_HOLD - 1, len(df4) - 1)]
    return (ep - entry_price) / r_dist - FEE_RT / (r_dist / entry_price)


# ── BACKTEST VETTORIZZATO PER UNA COMBINAZIONE ───────────────────────────────
def run_combo(coin_data, daily_ok_cache,
              body_pct, vol_ratio, max_dist50,
              rsi_min=30, rsi_max=65, ema_tol=0.012, max_dist_ema=3.0):
    trades = []
    for sym, df4 in coin_data.items():
        c    = df4["Close"]
        l    = df4["Low"]
        o    = df4["Open"]
        e20  = df4["EMA20"]
        rsi  = df4["RSI"]
        bp   = df4["BodyPct"]
        vr   = df4["VolRatio"]
        rd   = df4["R_dist"]
        slp  = df4["SL_pct"]
        slpr = df4["SL_price"]
        atr  = df4["ATR"]
        dema = (c - e20) / e20 * 100
        dok  = daily_ok_cache[sym][max_dist50]

        # Trova segnali con operazioni vettorizzate (no loop sulle candele)
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
        mask.iloc[:50] = False   # warmup indicatori
        mask.iloc[-2:] = False   # serve candela successiva per entry

        # Simula solo i trade trovati (decine, non migliaia)
        for idx in df4.index[mask]:
            i = int(idx)
            if i + 1 >= len(df4):
                continue
            entry = df4["Open"].iat[i + 1]
            sl    = float(slpr.iat[i])
            rd_v  = entry - sl
            if rd_v <= 0:
                continue
            pnl = simulate(df4, i + 1, entry, sl, rd_v, float(atr.iat[i]))
            if not pd.isna(pnl):
                trades.append(pnl)

    if len(trades) < 10:
        return None
    t  = np.array(trades)
    w  = t[t > 0]
    ls = t[t <= 0]
    return {
        "n":        len(t),
        "wr":       round(len(w) / len(t) * 100, 1),
        "pf":       round(w.sum() / abs(ls.sum()), 3) if ls.sum() != 0 else 999,
        "exp":      round(t.mean(), 3),
        "avg_win":  round(w.mean()  if len(w)  else 0, 3),
        "avg_loss": round(ls.mean() if len(ls) else 0, 3),
        "total_R":  round(t.sum(), 2),
    }


# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Scaricamento dati (730 giorni × 4h + daily)...")
    coin_data      = {}
    daily_ok_cache = {}

    for sym in COINS:
        print(f"  {sym}...", end=" ", flush=True)
        df4  = download(sym, 240, days=730)
        df_d = download(sym, "D",  days=790)
        if df4 is None or df_d is None or len(df4) < 200 or len(df_d) < 60:
            print("SKIP"); continue
        df4  = prepare_4h(df4)
        daily_ok_cache[sym] = compute_daily_ok(df4, df_d)
        coin_data[sym] = df4
        print(f"{len(df4)} candele 4h | daily_ok precomputato")
        time.sleep(0.2)

    print(f"\nCoin caricate: {len(coin_data)}")
    print("Grid search 120 combinazioni...\n")

    BODY_PCTS   = [20, 25, 30, 35, 40, 45]
    VOL_RATIOS  = [0.8, 1.0, 1.1, 1.2, 1.5]
    EMA50_DISTS = [15, 20, 25, 30]

    results = []
    total, done = len(BODY_PCTS) * len(VOL_RATIOS) * len(EMA50_DISTS), 0
    t0 = time.time()

    for body, vol, dist50 in product(BODY_PCTS, VOL_RATIOS, EMA50_DISTS):
        done += 1
        r = run_combo(coin_data, daily_ok_cache, body, vol, dist50)
        if r:
            r["body"] = body; r["vol"] = vol; r["dist50"] = dist50
            results.append(r)
        if done % 20 == 0:
            print(f"  {done}/{total} ({time.time()-t0:.0f}s)...")

    df_res = pd.DataFrame(results).sort_values("pf", ascending=False)

    print("\n" + "="*80)
    print("TOP 15 — Profit Factor")
    print("="*80)
    print(df_res.head(15).to_string(index=False))

    print("\n" + "="*80)
    print("TOP 15 — Expectancy (R medio per trade)")
    print("="*80)
    print(df_res.sort_values("exp", ascending=False).head(15).to_string(index=False))

    cur = df_res[(df_res["body"]==35) & (df_res["vol"]==1.1) & (df_res["dist50"]==20)]
    print("\n" + "="*80)
    print("PARAMETRI ATTUALI DEL BOT (body=35, vol=1.1, dist50=20)")
    print("="*80)
    print(cur.to_string(index=False) if len(cur) else "non trovato nel grid")

    df_res.to_csv("backtest_results.csv", index=False)
    print(f"\nSalvato: backtest_results.csv  ({time.time()-t0:.0f}s totali)")
