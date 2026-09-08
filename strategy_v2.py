from __future__ import annotations

from typing import Iterable, List, Optional

import numpy as np
import pandas as pd
from ta.momentum import RSIIndicator


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def atr(high: pd.Series, low: pd.Series, close: pd.Series, span: int = 14) -> pd.Series:
    tr = pd.concat([
        (high - low).abs(),
        (high - close.shift(1)).abs(),
        (low - close.shift(1)).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(span=span, adjust=False).mean()


def _is_valid_long_setup(df: pd.DataFrame, idx: int, rsi_min: float = 45.0, rsi_max: float = 75.0) -> bool:
    if idx < 60:
        return False

    close = df["Close"].iloc[idx]
    prev_close = df["Close"].iloc[idx - 1]
    ema20 = df["ema20"].iloc[idx]
    ema50 = df["ema50"].iloc[idx]
    rsi = df["rsi14"].iloc[idx]
    atr_val = df["atr14"].iloc[idx]

    if pd.isna(close) or pd.isna(ema20) or pd.isna(ema50):
        return False

    trend_ok = close > ema20 > ema50
    pullback_ok = prev_close < ema20 and close > ema20
    momentum_ok = rsi_min <= rsi <= rsi_max
    volatility_ok = atr_val > 0
    return trend_ok and pullback_ok and momentum_ok and volatility_ok


def _is_valid_short_setup(df: pd.DataFrame, idx: int, rsi_min: float = 25.0, rsi_max: float = 55.0) -> bool:
    if idx < 60:
        return False

    close = df["Close"].iloc[idx]
    prev_close = df["Close"].iloc[idx - 1]
    ema20 = df["ema20"].iloc[idx]
    ema50 = df["ema50"].iloc[idx]
    rsi = df["rsi14"].iloc[idx]
    atr_val = df["atr14"].iloc[idx]

    if pd.isna(close) or pd.isna(ema20) or pd.isna(ema50):
        return False

    trend_ok = close < ema20 < ema50
    reclaim_ok = prev_close > ema20 and close < ema20
    momentum_ok = rsi_min <= rsi <= rsi_max
    volatility_ok = atr_val > 0
    return trend_ok and reclaim_ok and momentum_ok and volatility_ok


def _is_valid_long_breakout(df: pd.DataFrame, idx: int, rsi_min: float = 55.0, rsi_max: float = 78.0) -> bool:
    if idx < 60:
        return False

    close = df["Close"].iloc[idx]
    prev_close = df["Close"].iloc[idx - 1]
    ema20 = df["ema20"].iloc[idx]
    ema50 = df["ema50"].iloc[idx]
    rsi = df["rsi14"].iloc[idx]
    atr_val = df["atr14"].iloc[idx]
    breakout_level = df["breakout_high20"].iloc[idx]

    if pd.isna(close) or pd.isna(breakout_level) or pd.isna(ema20) or pd.isna(ema50):
        return False

    trend_ok = close > ema20 > ema50
    breakout_ok = prev_close <= breakout_level and close > breakout_level
    momentum_ok = rsi_min <= rsi <= rsi_max
    volatility_ok = atr_val > 0
    return trend_ok and breakout_ok and momentum_ok and volatility_ok


def _is_valid_short_breakout(df: pd.DataFrame, idx: int, rsi_min: float = 22.0, rsi_max: float = 45.0) -> bool:
    if idx < 60:
        return False

    close = df["Close"].iloc[idx]
    prev_close = df["Close"].iloc[idx - 1]
    ema20 = df["ema20"].iloc[idx]
    ema50 = df["ema50"].iloc[idx]
    rsi = df["rsi14"].iloc[idx]
    atr_val = df["atr14"].iloc[idx]
    breakout_level = df["breakout_low20"].iloc[idx]

    if pd.isna(close) or pd.isna(breakout_level) or pd.isna(ema20) or pd.isna(ema50):
        return False

    trend_ok = close < ema20 < ema50
    breakout_ok = prev_close >= breakout_level and close < breakout_level
    momentum_ok = rsi_min <= rsi <= rsi_max
    volatility_ok = atr_val > 0
    return trend_ok and breakout_ok and momentum_ok and volatility_ok


def _is_valid_long_mean_reversion(df: pd.DataFrame, idx: int, rsi_min: float = 35.0, rsi_max: float = 58.0) -> bool:
    if idx < 60:
        return False

    close = df["Close"].iloc[idx]
    prev_close = df["Close"].iloc[idx - 1]
    ema20 = df["ema20"].iloc[idx]
    ema50 = df["ema50"].iloc[idx]
    rsi = df["rsi14"].iloc[idx]
    atr_val = df["atr14"].iloc[idx]
    stretch = df["stretch_z"].iloc[idx]
    prev_stretch = df["stretch_z"].iloc[idx - 1]
    ema50_slope5 = df["ema50_slope5"].iloc[idx]

    if pd.isna(close) or pd.isna(ema20) or pd.isna(ema50) or pd.isna(stretch) or pd.isna(prev_stretch) or pd.isna(ema50_slope5):
        return False

    regime_ok = ema50_slope5 > (-0.5 * atr_val)
    washout_ok = prev_stretch <= -1.25 and stretch > prev_stretch and close > prev_close
    reversion_ok = close < ema20
    momentum_ok = rsi_min <= rsi <= rsi_max
    volatility_ok = atr_val > 0
    return regime_ok and washout_ok and reversion_ok and momentum_ok and volatility_ok


def _is_valid_short_mean_reversion(df: pd.DataFrame, idx: int, rsi_min: float = 45.0, rsi_max: float = 78.0) -> bool:
    if idx < 60:
        return False

    close = df["Close"].iloc[idx]
    prev_close = df["Close"].iloc[idx - 1]
    ema20 = df["ema20"].iloc[idx]
    ema50 = df["ema50"].iloc[idx]
    rsi = df["rsi14"].iloc[idx]
    atr_val = df["atr14"].iloc[idx]
    stretch = df["stretch_z"].iloc[idx]
    prev_stretch = df["stretch_z"].iloc[idx - 1]
    ema50_slope5 = df["ema50_slope5"].iloc[idx]

    if pd.isna(close) or pd.isna(ema20) or pd.isna(ema50) or pd.isna(stretch) or pd.isna(prev_stretch) or pd.isna(ema50_slope5):
        return False

    regime_ok = ema50_slope5 < (0.5 * atr_val)
    blowoff_ok = prev_stretch >= 1.25 and stretch < prev_stretch and close < prev_close
    reversion_ok = close > ema20
    momentum_ok = rsi_min <= rsi <= rsi_max
    volatility_ok = atr_val > 0
    return regime_ok and blowoff_ok and reversion_ok and momentum_ok and volatility_ok


def build_signal_frame(
    df: pd.DataFrame,
    direction: str = "long",
    strategy_name: str = "pullback",
    rsi_min: float = 45.0,
    rsi_max: float = 75.0,
) -> pd.DataFrame:
    out = df.copy()
    out["ema20"] = ema(out["Close"], 20)
    out["ema50"] = ema(out["Close"], 50)
    out["ema50_slope5"] = out["ema50"] - out["ema50"].shift(5)
    out["rsi14"] = RSIIndicator(out["Close"], window=14).rsi().fillna(50.0)
    out["atr14"] = atr(out["High"], out["Low"], out["Close"], 14)
    out["breakout_high20"] = out["High"].shift(1).rolling(20).max()
    out["breakout_low20"] = out["Low"].shift(1).rolling(20).min()
    out["stretch_z"] = (out["Close"] - out["ema20"]) / out["atr14"].replace(0, np.nan)
    out["signal"] = False
    signal_builders = {
        ("long", "pullback"): _is_valid_long_setup,
        ("short", "pullback"): _is_valid_short_setup,
        ("long", "breakout"): _is_valid_long_breakout,
        ("short", "breakout"): _is_valid_short_breakout,
        ("long", "mean_reversion"): _is_valid_long_mean_reversion,
        ("short", "mean_reversion"): _is_valid_short_mean_reversion,
    }
    signal_builder = signal_builders[(direction, strategy_name)]
    for i in range(len(out)):
        out.at[out.index[i], "signal"] = bool(signal_builder(out, i, rsi_min=rsi_min, rsi_max=rsi_max))
    return out


def _backtest_simple_directional(
    df: pd.DataFrame,
    direction: str,
    strategy_name: str,
    max_hold_bars: int = 30,
    stop_atr_buffer: float = 1.25,
    target_r_multiple: float = 2.0,
    rsi_min: float = 45.0,
    rsi_max: float = 75.0,
    regime_column: Optional[str] = None,
) -> dict:
    data = build_signal_frame(df, direction=direction, strategy_name=strategy_name, rsi_min=rsi_min, rsi_max=rsi_max)
    if regime_column is not None and regime_column in df.columns:
        data[regime_column] = df[regime_column].astype(bool).values
    trades: List[float] = []
    in_trade = False
    entry_price = 0.0
    stop_price = 0.0
    target_price = 0.0
    entry_index = -1

    for i in range(len(data)):
        regime_ok = True if regime_column is None else bool(data[regime_column].iloc[i])
        if not in_trade and regime_ok and data["signal"].iloc[i]:
            entry_price = float(data["Close"].iloc[i])
            if direction == "long":
                lookback = 20 if strategy_name == "breakout" else 8 if strategy_name == "mean_reversion" else 12
                stop_price = float(min(data["Low"].iloc[max(0, i - lookback): i + 1])) - stop_atr_buffer * float(data["atr14"].iloc[i])
                if stop_price >= entry_price:
                    continue
                target_price = entry_price + target_r_multiple * (entry_price - stop_price)
            else:
                lookback = 20 if strategy_name == "breakout" else 8 if strategy_name == "mean_reversion" else 12
                stop_price = float(max(data["High"].iloc[max(0, i - lookback): i + 1])) + stop_atr_buffer * float(data["atr14"].iloc[i])
                if stop_price <= entry_price:
                    continue
                target_price = entry_price - target_r_multiple * (stop_price - entry_price)
            in_trade = True
            entry_index = i
            continue

        if in_trade:
            close = float(data["Close"].iloc[i])
            high = float(data["High"].iloc[i])
            low = float(data["Low"].iloc[i])

            if direction == "long":
                if low <= stop_price:
                    pnl = ((stop_price - entry_price) / entry_price) * 100.0
                    trades.append(pnl)
                    in_trade = False
                    continue

                if high >= target_price:
                    pnl = ((target_price - entry_price) / entry_price) * 100.0
                    trades.append(pnl)
                    in_trade = False
                    continue

                if i - entry_index >= max_hold_bars or i == len(data) - 1:
                    pnl = ((close - entry_price) / entry_price) * 100.0
                    trades.append(pnl)
                    in_trade = False
                    continue
            else:
                if high >= stop_price:
                    pnl = ((entry_price - stop_price) / entry_price) * 100.0
                    trades.append(pnl)
                    in_trade = False
                    continue

                if low <= target_price:
                    pnl = ((entry_price - target_price) / entry_price) * 100.0
                    trades.append(pnl)
                    in_trade = False
                    continue

                if i - entry_index >= max_hold_bars or i == len(data) - 1:
                    pnl = ((entry_price - close) / entry_price) * 100.0
                    trades.append(pnl)
                    in_trade = False
                    continue

    return {
        "trades": trades,
        "trade_count": len(trades),
        "net": float(sum(trades)),
    }


def backtest_simple_long(
    df: pd.DataFrame,
    risk_pct: float = 0.005,
    max_hold_bars: int = 30,
    stop_atr_buffer: float = 1.25,
    target_r_multiple: float = 2.0,
    rsi_min: float = 45.0,
    rsi_max: float = 75.0,
    regime_column: Optional[str] = None,
) -> dict:
    return _backtest_simple_directional(
        df,
        direction="long",
        strategy_name="pullback",
        max_hold_bars=max_hold_bars,
        stop_atr_buffer=stop_atr_buffer,
        target_r_multiple=target_r_multiple,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        regime_column=regime_column,
    )


def backtest_simple_short(
    df: pd.DataFrame,
    risk_pct: float = 0.005,
    max_hold_bars: int = 30,
    stop_atr_buffer: float = 1.25,
    target_r_multiple: float = 2.0,
    rsi_min: float = 25.0,
    rsi_max: float = 55.0,
    regime_column: Optional[str] = None,
) -> dict:
    return _backtest_simple_directional(
        df,
        direction="short",
        strategy_name="pullback",
        max_hold_bars=max_hold_bars,
        stop_atr_buffer=stop_atr_buffer,
        target_r_multiple=target_r_multiple,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        regime_column=regime_column,
    )


def backtest_breakout_long(
    df: pd.DataFrame,
    risk_pct: float = 0.005,
    max_hold_bars: int = 24,
    stop_atr_buffer: float = 1.0,
    target_r_multiple: float = 1.75,
    rsi_min: float = 55.0,
    rsi_max: float = 78.0,
    regime_column: Optional[str] = None,
) -> dict:
    return _backtest_simple_directional(
        df,
        direction="long",
        strategy_name="breakout",
        max_hold_bars=max_hold_bars,
        stop_atr_buffer=stop_atr_buffer,
        target_r_multiple=target_r_multiple,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        regime_column=regime_column,
    )


def backtest_breakout_short(
    df: pd.DataFrame,
    risk_pct: float = 0.005,
    max_hold_bars: int = 24,
    stop_atr_buffer: float = 1.0,
    target_r_multiple: float = 1.75,
    rsi_min: float = 22.0,
    rsi_max: float = 45.0,
    regime_column: Optional[str] = None,
) -> dict:
    return _backtest_simple_directional(
        df,
        direction="short",
        strategy_name="breakout",
        max_hold_bars=max_hold_bars,
        stop_atr_buffer=stop_atr_buffer,
        target_r_multiple=target_r_multiple,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        regime_column=regime_column,
    )


def backtest_mean_reversion_long(
    df: pd.DataFrame,
    risk_pct: float = 0.005,
    max_hold_bars: int = 12,
    stop_atr_buffer: float = 0.8,
    target_r_multiple: float = 1.1,
    rsi_min: float = 35.0,
    rsi_max: float = 58.0,
    regime_column: Optional[str] = None,
) -> dict:
    return _backtest_simple_directional(
        df,
        direction="long",
        strategy_name="mean_reversion",
        max_hold_bars=max_hold_bars,
        stop_atr_buffer=stop_atr_buffer,
        target_r_multiple=target_r_multiple,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        regime_column=regime_column,
    )


def backtest_mean_reversion_short(
    df: pd.DataFrame,
    risk_pct: float = 0.005,
    max_hold_bars: int = 12,
    stop_atr_buffer: float = 0.8,
    target_r_multiple: float = 1.1,
    rsi_min: float = 45.0,
    rsi_max: float = 78.0,
    regime_column: Optional[str] = None,
) -> dict:
    return _backtest_simple_directional(
        df,
        direction="short",
        strategy_name="mean_reversion",
        max_hold_bars=max_hold_bars,
        stop_atr_buffer=stop_atr_buffer,
        target_r_multiple=target_r_multiple,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        regime_column=regime_column,
    )


def compute_trade_metrics(trades: Iterable[float]) -> dict:
    trade_list = [float(t) for t in trades]
    if not trade_list:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "net": 0.0,
        }

    wins = [t for t in trade_list if t > 0]
    losses = [abs(t) for t in trade_list if t < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    win_rate = len(wins) / len(trade_list)
    gross_profit = sum(wins)
    gross_loss = sum(losses)
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else 999.0 if gross_profit > 0 else 0.0
    expectancy = (win_rate * avg_win) - ((1.0 - win_rate) * avg_loss)
    return {
        "total_trades": len(trade_list),
        "wins": len(wins),
        "losses": len(trade_list) - len(wins),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "net": sum(trade_list),
    }


if __name__ == "__main__":
    dates = pd.date_range("2024-01-01", periods=220, freq="4h")
    rng = np.random.default_rng(7)
    base = 100.0
    drift = 0.0015
    series = []
    for i in range(len(dates)):
        base = base * (1 + drift + rng.normal(0, 0.004))
        series.append(base)

    df = pd.DataFrame({
        "Open": series,
        "High": [v * (1 + 0.01 + rng.random() * 0.02) for v in series],
        "Low": [v * (1 - 0.01 - rng.random() * 0.02) for v in series],
        "Close": series,
        "Volume": [1000 + rng.random() * 2000 for _ in series],
    })
    result = backtest_simple_long(df)
    print(result)
    print(compute_trade_metrics(result["trades"]))
