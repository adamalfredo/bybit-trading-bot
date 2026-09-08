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
    if daily is None or len(daily) < 55:
        return False
    close = daily["Close"]
    ema50 = ema(close, 50)
    last_close = float(close.iloc[-2])
    last_ema = float(ema50.iloc[-2])
    previous_ema = float(ema50.iloc[-7])
    if direction == "long":
        return last_close > last_ema and last_ema > previous_ema
    if direction == "short":
        return last_close < last_ema and last_ema < previous_ema
    raise ValueError(f"direction non supportata: {direction}")


def get_mean_reversion_signal(
    symbol: str,
    direction: str,
    fetch_klines: Callable[..., Optional[pd.DataFrame]],
    config: Optional[dict] = None,
) -> Optional[dict]:
    config = config or load_live_config()
    candles = fetch_klines(symbol, interval="240", limit=120)
    daily = fetch_klines(symbol, interval="D", limit=100)
    if candles is None or daily is None or len(candles) < 70:
        return None
    if not _daily_regime_ok(daily, direction):
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
    if idx < 60 or not bool(signals["signal"].iloc[idx]):
        return None

    entry_price = float(signals["Close"].iloc[idx])
    atr_value = float(signals["atr14"].iloc[idx])
    if entry_price <= 0 or atr_value <= 0:
        return None

    lookback = 8
    if direction == "long":
        stop_price = float(signals["Low"].iloc[idx - lookback:idx + 1].min()) - float(config["stop_atr_buffer"]) * atr_value
        r_dist = entry_price - stop_price
    else:
        stop_price = float(signals["High"].iloc[idx - lookback:idx + 1].max()) + float(config["stop_atr_buffer"]) * atr_value
        r_dist = stop_price - entry_price
    if r_dist <= 0:
        return None

    previous_close = float(signals["Close"].iloc[idx - 1])
    candle_range = float(signals["High"].iloc[idx] - signals["Low"].iloc[idx])
    volume_avg = float(signals["Volume"].iloc[max(0, idx - 20):idx].mean())
    return {
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
