from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
from ta.momentum import RSIIndicator

from strategy_gate import should_allow_live_trading
from strategy_v2 import atr, ema
from validate_strategy_v2 import SYMBOLS, attach_daily_regime, fetch_klines, iter_walkforward_slices

RESULTS_PATH = Path("research_volatility_expansion_results.json")
ROUND_TRIP_COST_PCT = 0.11


def features(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out["Close"], 20)
    out["ema50"] = ema(out["Close"], 50)
    out["atr14"] = atr(out["High"], out["Low"], out["Close"], 14)
    out["atr_pct"] = out["atr14"] / out["Close"]
    out["atr_pct_rank"] = out["atr_pct"].shift(1).rolling(72).rank(pct=True)
    out["rsi14"] = RSIIndicator(out["Close"], window=14).rsi()
    out["volume_mean"] = out["Volume"].shift(1).rolling(20).mean()
    out["range_high"] = out["High"].shift(1).rolling(8).max()
    out["range_low"] = out["Low"].shift(1).rolling(8).min()
    return out


def is_signal(data: pd.DataFrame, i: int, direction: str, config: dict) -> bool:
    if i < 80:
        return False
    row = data.iloc[i]
    required = ["ema20", "ema50", "atr14", "atr_pct_rank", "rsi14", "volume_mean", "range_high", "range_low"]
    if row[required].isna().any():
        return False
    close = float(row["Close"])
    open_price = float(row["Open"])
    ema20 = float(row["ema20"])
    ema50 = float(row["ema50"])
    atrv = float(row["atr14"])
    rsi = float(row["rsi14"])
    volume_ratio = float(row["Volume"]) / float(row["volume_mean"]) if float(row["volume_mean"]) > 0 else 0.0
    expansion = float(row["atr_pct_rank"]) >= float(config["expansion_percentile"])
    volume_ok = volume_ratio >= float(config["min_volume_ratio"])
    if direction == "long":
        return (close > ema20 > ema50 and close > float(row["range_high"]) and close > open_price
                and float(config["long_rsi_min"]) <= rsi <= float(config["long_rsi_max"])
                and expansion and volume_ok)
    return (close < ema20 < ema50 and close < float(row["range_low"]) and close < open_price
            and float(config["short_rsi_min"]) <= rsi <= float(config["short_rsi_max"])
            and expansion and volume_ok)


def backtest(data: pd.DataFrame, direction: str, regime: str, config: dict) -> list[float]:
    data = features(data)
    trades = []
    position = None
    for i in range(len(data)):
        row = data.iloc[i]
        if position is None:
            if not bool(row[regime]) or not is_signal(data, i, direction, config):
                continue
            if i + 1 >= len(data):
                continue
            entry = float(data["Open"].iloc[i + 1])
            atrv = float(row["atr14"])
            lookback = int(config["stop_lookback_bars"])
            if direction == "long":
                stop = float(data["Low"].iloc[max(0, i-lookback):i+1].min()) - float(config["stop_atr_buffer"]) * atrv
                risk = entry - stop
                target = entry + float(config["target_r_multiple"]) * risk
            else:
                stop = float(data["High"].iloc[max(0, i-lookback):i+1].max()) + float(config["stop_atr_buffer"]) * atrv
                risk = stop - entry
                target = entry - float(config["target_r_multiple"]) * risk
            if risk > 0:
                position = {"entry": entry, "stop": stop, "target": target, "index": i}
            continue
        entry = position["entry"]
        stop = position["stop"]
        target = position["target"]
        high, low, close = float(row["High"]), float(row["Low"]), float(row["Close"])
        pnl = None
        if direction == "long":
            if low <= stop: pnl = (stop-entry)/entry*100 - ROUND_TRIP_COST_PCT
            elif high >= target: pnl = (target-entry)/entry*100 - ROUND_TRIP_COST_PCT
            elif i-position["index"] >= int(config["max_hold_bars"]): pnl = (close-entry)/entry*100 - ROUND_TRIP_COST_PCT
        else:
            if high >= stop: pnl = (entry-stop)/entry*100 - ROUND_TRIP_COST_PCT
            elif low <= target: pnl = (entry-target)/entry*100 - ROUND_TRIP_COST_PCT
            elif i-position["index"] >= int(config["max_hold_bars"]): pnl = (entry-close)/entry*100 - ROUND_TRIP_COST_PCT
        if pnl is not None:
            trades.append(pnl)
            position = None
    return trades


def evaluate(config: dict) -> dict:
    train = {"long": [], "short": []}
    test = {"long": [], "short": []}
    for symbol in SYMBOLS:
        data = attach_daily_regime(fetch_klines(symbol, "240", 1800), fetch_klines(symbol, "D", 400))
        for start, train_end, test_end in iter_walkforward_slices(len(data)):
            a = data.iloc[start:train_end].reset_index(drop=True)
            b = data.iloc[train_end:test_end].reset_index(drop=True)
            train["long"].extend(backtest(a, "long", "daily_long_ok", config))
            train["short"].extend(backtest(a, "short", "daily_short_ok", config))
            test["long"].extend(backtest(b, "long", "daily_long_ok", config))
            test["short"].extend(backtest(b, "short", "daily_short_ok", config))
    result = {"config": config}
    for direction in ("long", "short"):
        tg, tm = should_allow_live_trading(train[direction], min_trades=10)
        vg, vm = should_allow_live_trading(test[direction], min_trades=10)
        result[direction] = {"train_gate": tg, "test_gate": vg, "train": tm, "test": vm}
    return result


def main() -> None:
    configs = [
        {"name":"expansion_tight","expansion_percentile":0.75,"min_volume_ratio":1.1,"stop_lookback_bars":8,"stop_atr_buffer":0.9,"target_r_multiple":1.25,"max_hold_bars":18,"long_rsi_min":55,"long_rsi_max":78,"short_rsi_min":22,"short_rsi_max":45},
        {"name":"expansion_balanced","expansion_percentile":0.60,"min_volume_ratio":1.0,"stop_lookback_bars":8,"stop_atr_buffer":1.0,"target_r_multiple":1.5,"max_hold_bars":24,"long_rsi_min":52,"long_rsi_max":80,"short_rsi_min":20,"short_rsi_max":48},
    ]
    results = [evaluate(c) for c in configs]
    RESULTS_PATH.write_text(json.dumps(results, indent=2), encoding="utf-8")
    for r in results:
        print(r["config"]["name"], "LONG", r["long"]["train_gate"], r["long"]["test_gate"], r["long"]["test"]["total_trades"], round(r["long"]["test"]["profit_factor"],2), round(r["long"]["test"]["expectancy"],4))
        print(r["config"]["name"], "SHORT", r["short"]["train_gate"], r["short"]["test_gate"], r["short"]["test"]["total_trades"], round(r["short"]["test"]["profit_factor"],2), round(r["short"]["test"]["expectancy"],4))


if __name__ == "__main__":
    main()
