from __future__ import annotations

import json
import time

import pandas as pd
import requests

from strategy_gate import should_allow_live_trading
from strategy_v2 import backtest_breakout_long, backtest_breakout_short, backtest_mean_reversion_long, backtest_mean_reversion_short, backtest_simple_long, backtest_simple_short, compute_trade_metrics, ema

BASE_URL = "https://api.bybit.com/v5/market/kline"
SYMBOLS = [
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "BNBUSDT",
    "XRPUSDT",
    "LINKUSDT",
    "AVAXUSDT",
]
VALIDATION_MIN_TRADES = 10
TRAIN_BARS = 720
TEST_BARS = 240
STEP_BARS = 240
RESULTS_CSV = "validation_results_v2.csv"
BEST_CONFIG_JSON = "best_strategy_v2.json"


def fetch_klines(symbol: str, interval: str, limit: int) -> pd.DataFrame:
    end_ms = int(time.time() * 1000)
    rows: list[list[str]] = []
    rate_limit_retries = 0
    while len(rows) < limit:
        response = requests.get(
            BASE_URL,
            params={
                "category": "linear",
                "symbol": symbol,
                "interval": interval,
                "limit": min(200, limit - len(rows)),
                "end": str(end_ms),
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("retCode") == 10006:
            rate_limit_retries += 1
            if rate_limit_retries > 6:
                raise RuntimeError(f"Bybit rate limit for {symbol}: {payload}")
            time.sleep(min(2.0 * rate_limit_retries, 10.0))
            continue
        rate_limit_retries = 0
        if payload.get("retCode") != 0:
            raise RuntimeError(f"Bybit error for {symbol}: {payload}")

        chunk = payload.get("result", {}).get("list", [])
        if not chunk:
            break
        rows.extend(chunk)
        end_ms = int(chunk[-1][0]) - 1
        time.sleep(0.06)

    df = pd.DataFrame(rows, columns=["ts", "Open", "High", "Low", "Close", "Volume", "Turnover"])
    if df.empty:
        return pd.DataFrame(columns=["ts", "Open", "High", "Low", "Close", "Volume"])

    for column in ["Open", "High", "Low", "Close", "Volume"]:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df["ts"] = pd.to_datetime(df["ts"].astype(int), unit="ms")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    return df[["ts", "Open", "High", "Low", "Close", "Volume"]]


def attach_daily_regime(df4: pd.DataFrame, dfd: pd.DataFrame) -> pd.DataFrame:
    out = df4.copy()
    daily = dfd[["ts", "Close"]].copy()
    daily["ema50d"] = ema(daily["Close"], 50)
    daily["ema50d_prev5"] = daily["ema50d"].shift(5)
    daily["day"] = daily["ts"].dt.normalize().astype("datetime64[ns]")
    daily = daily[["day", "Close", "ema50d", "ema50d_prev5"]].drop_duplicates("day").sort_values("day").reset_index(drop=True)

    probe = pd.DataFrame({
        "pos": out.index,
        "day": (out["ts"].dt.normalize() - pd.Timedelta(days=1)).astype("datetime64[ns]"),
    }).sort_values("day")
    merged = pd.merge_asof(probe, daily, on="day", direction="backward").sort_values("pos").reset_index(drop=True)

    out["daily_long_ok"] = (
        merged["Close"].notna()
        & merged["ema50d"].notna()
        & merged["ema50d_prev5"].notna()
        & (merged["Close"] > merged["ema50d"])
        & (merged["ema50d"] > merged["ema50d_prev5"])
    )
    out["daily_short_ok"] = (
        merged["Close"].notna()
        & merged["ema50d"].notna()
        & merged["ema50d_prev5"].notna()
        & (merged["Close"] < merged["ema50d"])
        & (merged["ema50d"] < merged["ema50d_prev5"])
    )
    return out


def iter_walkforward_slices(length: int) -> list[tuple[int, int, int]]:
    windows: list[tuple[int, int, int]] = []
    start = 0
    while start + TRAIN_BARS + TEST_BARS <= length:
        train_end = start + TRAIN_BARS
        test_end = train_end + TEST_BARS
        windows.append((start, train_end, test_end))
        start += STEP_BARS
    if not windows and length >= 200:
        split_index = int(length * 0.6)
        if split_index >= 120 and length - split_index >= 80:
            windows.append((0, split_index, length))
    return windows


def evaluate_config(symbol_data: dict[str, pd.DataFrame], config: dict[str, float | int]) -> dict:
    train_trades: list[float] = []
    test_trades: list[float] = []
    symbol_rows: list[dict] = []
    family_runners = {
        "pullback": (backtest_simple_long, backtest_simple_short),
        "breakout": (backtest_breakout_long, backtest_breakout_short),
        "mean_reversion": (backtest_mean_reversion_long, backtest_mean_reversion_short),
    }
    long_runner, short_runner = family_runners[str(config["strategy_family"])]

    for symbol, df in symbol_data.items():
        windows = iter_walkforward_slices(len(df))
        if not windows:
            continue

        symbol_train_trades: list[float] = []
        symbol_test_long_trades: list[float] = []
        symbol_test_short_trades: list[float] = []

        for start, train_end, test_end in windows:
            train_df = df.iloc[start:train_end].reset_index(drop=True)
            test_df = df.iloc[train_end:test_end].reset_index(drop=True)

            train_long_result = long_runner(
                train_df[["Open", "High", "Low", "Close", "Volume", "daily_long_ok"]],
                max_hold_bars=int(config["max_hold_bars"]),
                stop_atr_buffer=float(config["stop_atr_buffer"]),
                target_r_multiple=float(config["target_r_multiple"]),
                rsi_min=float(config["long_rsi_min"]),
                rsi_max=float(config["long_rsi_max"]),
                regime_column="daily_long_ok",
            )
            train_short_result = short_runner(
                train_df[["Open", "High", "Low", "Close", "Volume", "daily_short_ok"]],
                max_hold_bars=int(config["max_hold_bars"]),
                stop_atr_buffer=float(config["stop_atr_buffer"]),
                target_r_multiple=float(config["target_r_multiple"]),
                rsi_min=float(config["short_rsi_min"]),
                rsi_max=float(config["short_rsi_max"]),
                regime_column="daily_short_ok",
            )
            test_long_result = long_runner(
                test_df[["Open", "High", "Low", "Close", "Volume", "daily_long_ok"]],
                max_hold_bars=int(config["max_hold_bars"]),
                stop_atr_buffer=float(config["stop_atr_buffer"]),
                target_r_multiple=float(config["target_r_multiple"]),
                rsi_min=float(config["long_rsi_min"]),
                rsi_max=float(config["long_rsi_max"]),
                regime_column="daily_long_ok",
            )
            test_short_result = short_runner(
                test_df[["Open", "High", "Low", "Close", "Volume", "daily_short_ok"]],
                max_hold_bars=int(config["max_hold_bars"]),
                stop_atr_buffer=float(config["stop_atr_buffer"]),
                target_r_multiple=float(config["target_r_multiple"]),
                rsi_min=float(config["short_rsi_min"]),
                rsi_max=float(config["short_rsi_max"]),
                regime_column="daily_short_ok",
            )
            symbol_train_trades.extend(train_long_result["trades"])
            symbol_train_trades.extend(train_short_result["trades"])
            symbol_test_long_trades.extend(test_long_result["trades"])
            symbol_test_short_trades.extend(test_short_result["trades"])

        train_trades.extend(symbol_train_trades)
        test_trades.extend(symbol_test_long_trades)
        test_trades.extend(symbol_test_short_trades)
        long_test_metrics = compute_trade_metrics(symbol_test_long_trades)
        short_test_metrics = compute_trade_metrics(symbol_test_short_trades)
        combined_test_metrics = compute_trade_metrics(symbol_test_long_trades + symbol_test_short_trades)
        symbol_rows.append(
            {
                "symbol": symbol,
                "test_trades": long_test_metrics["total_trades"] + short_test_metrics["total_trades"],
                "long_expectancy": long_test_metrics["expectancy"],
                "short_expectancy": short_test_metrics["expectancy"],
                "test_expectancy": combined_test_metrics["expectancy"],
                "test_pf": combined_test_metrics["profit_factor"],
                "test_net": combined_test_metrics["net"],
                "windows": len(windows),
            }
        )

    train_gate, train_metrics = should_allow_live_trading(train_trades, min_trades=VALIDATION_MIN_TRADES)
    test_gate, test_metrics = should_allow_live_trading(test_trades, min_trades=VALIDATION_MIN_TRADES)
    return {
        "config": config,
        "train_gate": train_gate,
        "test_gate": test_gate,
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "per_symbol": symbol_rows,
    }


def _flatten_result_row(result: dict) -> dict:
    cfg = result["config"]
    train = result["train_metrics"]
    test = result["test_metrics"]
    return {
        "strategy_family": cfg["strategy_family"],
        "max_hold_bars": cfg["max_hold_bars"],
        "stop_atr_buffer": cfg["stop_atr_buffer"],
        "target_r_multiple": cfg["target_r_multiple"],
        "long_rsi_min": cfg["long_rsi_min"],
        "long_rsi_max": cfg["long_rsi_max"],
        "short_rsi_min": cfg["short_rsi_min"],
        "short_rsi_max": cfg["short_rsi_max"],
        "train_gate": result["train_gate"],
        "test_gate": result["test_gate"],
        "train_trades": train["total_trades"],
        "train_expectancy": train["expectancy"],
        "train_pf": train["profit_factor"],
        "train_net": train["net"],
        "test_trades": test["total_trades"],
        "test_expectancy": test["expectancy"],
        "test_pf": test["profit_factor"],
        "test_net": test["net"],
    }


def main() -> None:
    print("Downloading Bybit history...")
    symbol_data = {}
    for symbol in SYMBOLS:
        df4 = fetch_klines(symbol, interval="240", limit=1800)
        dfd = fetch_klines(symbol, interval="D", limit=400)
        symbol_data[symbol] = attach_daily_regime(df4, dfd)

    configs = [
        {
            "strategy_family": "pullback",
            "max_hold_bars": 18,
            "stop_atr_buffer": 1.0,
            "target_r_multiple": 1.25,
            "long_rsi_min": 45.0,
            "long_rsi_max": 72.0,
            "short_rsi_min": 25.0,
            "short_rsi_max": 50.0,
        },
        {
            "strategy_family": "breakout",
            "max_hold_bars": 18,
            "stop_atr_buffer": 1.25,
            "target_r_multiple": 1.25,
            "long_rsi_min": 50.0,
            "long_rsi_max": 78.0,
            "short_rsi_min": 22.0,
            "short_rsi_max": 40.0,
        },
        {
            "strategy_family": "breakout",
            "max_hold_bars": 24,
            "stop_atr_buffer": 1.25,
            "target_r_multiple": 1.25,
            "long_rsi_min": 55.0,
            "long_rsi_max": 78.0,
            "short_rsi_min": 22.0,
            "short_rsi_max": 45.0,
        },
        {
            "strategy_family": "mean_reversion",
            "max_hold_bars": 10,
            "stop_atr_buffer": 0.8,
            "target_r_multiple": 1.0,
            "long_rsi_min": 35.0,
            "long_rsi_max": 55.0,
            "short_rsi_min": 45.0,
            "short_rsi_max": 75.0,
        },
        {
            "strategy_family": "mean_reversion",
            "max_hold_bars": 12,
            "stop_atr_buffer": 0.8,
            "target_r_multiple": 1.1,
            "long_rsi_min": 35.0,
            "long_rsi_max": 58.0,
            "short_rsi_min": 45.0,
            "short_rsi_max": 78.0,
        },
        {
            "strategy_family": "mean_reversion",
            "max_hold_bars": 12,
            "stop_atr_buffer": 1.0,
            "target_r_multiple": 1.25,
            "long_rsi_min": 32.0,
            "long_rsi_max": 58.0,
            "short_rsi_min": 45.0,
            "short_rsi_max": 78.0,
        },
    ]

    results = [evaluate_config(symbol_data, config) for config in configs]
    results.sort(
        key=lambda row: (
            row["test_gate"],
            row["test_metrics"]["expectancy"],
            row["test_metrics"]["profit_factor"],
            row["test_metrics"]["net"],
        ),
        reverse=True,
    )

    rows = [_flatten_result_row(result) for result in results]
    pd.DataFrame(rows).to_csv(RESULTS_CSV, index=False)

    winner = results[0]
    winner_payload = {
        "config": winner["config"],
        "train_gate": winner["train_gate"],
        "test_gate": winner["test_gate"],
        "train_metrics": winner["train_metrics"],
        "test_metrics": winner["test_metrics"],
        "notes": "Selected by rolling out-of-sample ranking in validate_strategy_v2.py",
    }
    with open(BEST_CONFIG_JSON, "w", encoding="utf-8") as handle:
        json.dump(winner_payload, handle, indent=2)

    print("\nTop configurations by rolling out-of-sample metrics:\n")
    for rank, result in enumerate(results[:5], start=1):
        config = result["config"]
        train_metrics = result["train_metrics"]
        test_metrics = result["test_metrics"]
        print(f"#{rank} config={config}")
        print(
            "  TRAIN gate={0} trades={1} expectancy={2:.4f} PF={3:.2f} net={4:.4f}".format(
                result["train_gate"],
                train_metrics["total_trades"],
                train_metrics["expectancy"],
                train_metrics["profit_factor"],
                train_metrics["net"],
            )
        )
        print(
            "  TEST  gate={0} trades={1} expectancy={2:.4f} PF={3:.2f} net={4:.4f}".format(
                result["test_gate"],
                test_metrics["total_trades"],
                test_metrics["expectancy"],
                test_metrics["profit_factor"],
                test_metrics["net"],
            )
        )
        print("  TEST symbols:")
        for row in result["per_symbol"]:
            print(
                "    {0}: windows={1} trades={2} long_exp={3:.4f} short_exp={4:.4f} total_exp={5:.4f} PF={6:.2f} net={7:.4f}".format(
                    row["symbol"],
                    row["windows"],
                    row["test_trades"],
                    row["long_expectancy"],
                    row["short_expectancy"],
                    row["test_expectancy"],
                    row["test_pf"],
                    row["test_net"],
                )
            )
        print()

    print("Saved ranking:", RESULTS_CSV)
    print("Saved winner:", BEST_CONFIG_JSON)
    print(
        "Winner summary: family={0} test_gate={1} trades={2} expectancy={3:.4f} PF={4:.2f} net={5:.4f}".format(
            winner["config"]["strategy_family"],
            winner["test_gate"],
            winner["test_metrics"]["total_trades"],
            winner["test_metrics"]["expectancy"],
            winner["test_metrics"]["profit_factor"],
            winner["test_metrics"]["net"],
        )
    )


if __name__ == "__main__":
    main()