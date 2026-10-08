from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Optional

import pandas as pd

from strategy_v2 import build_signal_frame, ema

DEFAULT_CONFIG_PATH = Path(__file__).with_name("best_strategy_v2.json")


def load_live_config(path: Path = DEFAULT_CONFIG_PATH) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    config = payload.get("config")
    if not isinstance(config, dict) or config.get("strategy_family") != "mean_reversion":
        raise ValueError("best_strategy_v2.json non contiene una config mean_reversion")
    return config


def _daily_regime_ok(daily: pd.DataFrame, direction: str) -> bool:
    return _daily_regime_rejection(daily, direction) is None


def _daily_regime_rejection(daily: pd.DataFrame, direction: str) -> Optional[str]:
    if daily is None or len(daily) < 55:
        return "daily_history_insufficient"
    close = daily["Close"]
    ema50 = ema(close, 50)
    last_close = float(close.iloc[-2])
    last_ema = float(ema50.iloc[-2])
    previous_ema = float(ema50.iloc[-7])
    if direction == "long":
        if last_close <= last_ema:
            return "daily_close_not_above_ema50"
        if last_ema <= previous_ema:
            return "daily_ema50_slope_not_up"
        return None
    elif direction == "short":
        if last_close >= last_ema:
            return "daily_close_not_below_ema50"
        if last_ema >= previous_ema:
            return "daily_ema50_slope_not_down"
        return None
    raise ValueError(f"direction non supportata: {direction}")


def _failed_mean_reversion_conditions(
    signals: pd.DataFrame,
    idx: int,
    direction: str,
    rsi_min: float,
    rsi_max: float,
) -> list[str]:
    close = float(signals["Close"].iloc[idx])
    previous_close = float(signals["Close"].iloc[idx - 1])
    ema20 = float(signals["ema20"].iloc[idx])
    rsi = float(signals["rsi14"].iloc[idx])
    atr_value = float(signals["atr14"].iloc[idx])
    stretch = float(signals["stretch_z"].iloc[idx])
    previous_stretch = float(signals["stretch_z"].iloc[idx - 1])
    ema50_slope = float(signals["ema50_slope5"].iloc[idx])

    if any(pd.isna(value) for value in (close, previous_close, ema20, rsi, atr_value, stretch, previous_stretch, ema50_slope)):
        return ["indicator_values_invalid"]

    failures = []
    if atr_value <= 0:
        failures.append("atr_not_positive")
    if direction == "long":
        if ema50_slope <= -0.5 * atr_value:
            failures.append("mean_reversion_slope_regime_failed")
        if previous_stretch > -1.25:
            failures.append("washout_not_deep_enough")
        if stretch <= previous_stretch:
            failures.append("washout_not_recovering")
        if close <= previous_close:
            failures.append("reversal_close_not_higher")
        if close >= ema20:
            failures.append("price_not_below_ema20")
    else:
        if ema50_slope >= 0.5 * atr_value:
            failures.append("mean_reversion_slope_regime_failed")
        if previous_stretch < 1.25:
            failures.append("blowoff_not_high_enough")
        if stretch >= previous_stretch:
            failures.append("blowoff_not_fading")
        if close >= previous_close:
            failures.append("rejection_close_not_lower")
        if close <= ema20:
            failures.append("price_not_above_ema20")
    if not rsi_min <= rsi <= rsi_max:
        failures.append("rsi_out_of_range")
    return failures or ["signal_predicate_not_met"]


def get_mean_reversion_signal(
    symbol: str,
    direction: str,
    fetch_klines: Callable[..., Optional[pd.DataFrame]],
    config: Optional[dict] = None,
    diagnostics: Optional[dict[str, object]] = None,
) -> Optional[dict]:
    if diagnostics is not None:
        diagnostics.clear()

    def reject(reason: str, failed_conditions: Optional[list[str]] = None) -> None:
        if diagnostics is not None:
            diagnostics["reason"] = reason
            if failed_conditions is not None:
                diagnostics["failed_conditions"] = failed_conditions

    config = config or load_live_config()
    candles = fetch_klines(symbol, interval="240", limit=120)
    daily = fetch_klines(symbol, interval="D", limit=100)
    if candles is None or len(candles) < 70:
        reject("four_hour_history_insufficient")
        return None
    if daily is None:
        reject("daily_history_unavailable")
        return None
    daily_rejection = _daily_regime_rejection(daily, direction)
    if daily_rejection:
        reject(daily_rejection)
        return None

    rsi_min = float(config[f"{direction}_rsi_min"])
    rsi_max = float(config[f"{direction}_rsi_max"])
    signals = build_signal_frame(
        candles[["Open", "High", "Low", "Close", "Volume"]],
        direction=direction,
        strategy_name="mean_reversion",
        rsi_min=rsi_min,
        rsi_max=rsi_max,
    )
    idx = len(signals) - 2
    if idx < 60:
        reject("four_hour_indicator_history_insufficient")
        return None
    if not bool(signals["signal"].iloc[idx]):
        failed_conditions = _failed_mean_reversion_conditions(
            signals, idx, direction, rsi_min, rsi_max
        )
        reject("mean_reversion_conditions_not_met", failed_conditions)
        return None

    entry_price = float(signals["Close"].iloc[idx])
    atr_value = float(signals["atr14"].iloc[idx])
    if entry_price <= 0 or atr_value <= 0:
        reject("entry_price_or_atr_invalid")
        return None

    lookback = 8
    if direction == "long":
        stop_price = float(signals["Low"].iloc[idx - lookback:idx + 1].min()) - float(config["stop_atr_buffer"]) * atr_value
        r_dist = entry_price - stop_price
    else:
        stop_price = float(signals["High"].iloc[idx - lookback:idx + 1].max()) + float(config["stop_atr_buffer"]) * atr_value
        r_dist = stop_price - entry_price
    if r_dist <= 0:
        reject("stop_distance_invalid")
        return None

    previous_close = float(signals["Close"].iloc[idx - 1])
    candle_range = float(signals["High"].iloc[idx] - signals["Low"].iloc[idx])
    volume_avg = float(signals["Volume"].iloc[max(0, idx - 20):idx].mean())
    # I bot live chiamano la colonna "timestamp", validate_strategy_v2 la chiama "ts".
    time_col = "ts" if "ts" in candles.columns else "timestamp"
    if diagnostics is not None:
        diagnostics["reason"] = "signal_confirmed"
        diagnostics["failed_conditions"] = []
    return {
        "candle_time": str(candles[time_col].iloc[idx]),
        "entry_price": entry_price,
        "sl_price": stop_price,
        "r_dist": r_dist,
        "atr": atr_value,
        "rsi": float(signals["rsi14"].iloc[idx]),
        "ema20_4h": float(signals["ema20"].iloc[idx]),
        "dist_ema": (entry_price - float(signals["ema20"].iloc[idx])) / float(signals["ema20"].iloc[idx]) * 100.0,
        "sl_pct": r_dist / entry_price * 100.0,
        "chg_1h": (entry_price / previous_close - 1.0) * 100.0,
        "chg_4h": (entry_price / float(signals["Close"].iloc[idx - 4]) - 1.0) * 100.0,
        "rvol": float(signals["Volume"].iloc[idx]) / volume_avg if volume_avg > 0 else 0.0,
        "min_rvol": 0.0,
        "base_range": candle_range / entry_price * 100.0 if entry_price > 0 else 0.0,
        "base_max": 0.0,
        "norm_z": 0.0,
        "rr_est": float(config["target_r_multiple"]),
        "tp_est": entry_price + float(config["target_r_multiple"]) * r_dist if direction == "long" else entry_price - float(config["target_r_multiple"]) * r_dist,
        "max_hold_bars": int(config["max_hold_bars"]),
        "strategy_mode": "mean_reversion",
    }
