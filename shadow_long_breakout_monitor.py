from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from strategy_v2 import build_signal_frame
from validate_strategy_v2 import attach_daily_regime, fetch_klines

STATE_PATH = Path(os.getenv("SHADOW_STATE_DIR", str(Path(__file__).parent))) / "long_breakout_shadow_state.json"
STRATEGY_ID = "long_breakout_atr_trail_v1"
RSI_MIN = 55.0
RSI_MAX = 78.0
STOP_LOOKBACK = 20
STOP_ATR_BUFFER = 1.0
TRAIL_TRIGGER_R = 1.0
TRAIL_ATR_MULTIPLE = 2.0
MAX_HOLD_BARS = 24
ROUND_TRIP_COST_PCT = 0.11


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("strategy_id") != STRATEGY_ID:
            state["legacy_signals"] = (state.get("legacy_signals", []) + state.get("signals", []))[-2000:]
            state["signals"] = []
        state["schema_version"] = 1
        state["strategy_id"] = STRATEGY_ID
        state.setdefault("signals", [])
        state.setdefault("scan_count", 0)
        return state
    return {
        "schema_version": 1,
        "strategy_id": STRATEGY_ID,
        "signals": [],
        "legacy_signals": [],
        "scan_count": 0,
        "updated_utc": None,
    }


def save_state(state: dict) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def _close_signal(signal: dict, reason: str, exit_price: float) -> None:
    signal["status"] = reason
    signal["exit"] = exit_price
    signal["pnl_pct"] = (exit_price - signal["entry"]) / signal["entry"] * 100.0 - ROUND_TRIP_COST_PCT
    signal["closed_utc"] = datetime.now(timezone.utc).isoformat()


def _closed_metrics(signals: list[dict]) -> dict:
    closed = [item for item in signals if item.get("status") != "shadow_open"]
    pnls = [float(item.get("pnl_pct", 0.0)) for item in closed]
    gains = sum(value for value in pnls if value > 0)
    losses = -sum(value for value in pnls if value < 0)
    return {
        "closed_count": len(closed),
        "wins": sum(value > 0 for value in pnls),
        "net_pnl_pct": sum(pnls),
        "expectancy_pct": sum(pnls) / len(pnls) if pnls else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "exit_reasons": {
            reason: sum(item.get("status") == reason for item in closed)
            for reason in ("stopped", "target_trail", "time_stop")
        },
    }


def scan_once(state: dict, symbols: list[str]) -> None:
    state["scan_count"] = int(state.get("scan_count", 0)) + 1
    state["last_scan_started_utc"] = datetime.now(timezone.utc).isoformat()
    rejection_counts = {}
    condition_counts = {}
    scanned_symbols = []

    def reject(symbol: str, reason: str) -> None:
        rejection_counts[reason] = rejection_counts.get(reason, 0) + 1
        state.setdefault("last_symbol_diagnostics", {})[symbol] = {"reason": reason}

    for symbol in symbols:
        scanned_symbols.append(symbol)
        candles = fetch_klines(symbol, "240", 120)
        daily = fetch_klines(symbol, "D", 100)
        if candles is None or daily is None or len(candles) < 60:
            reject(symbol, "history_unavailable")
            continue

        data = attach_daily_regime(candles, daily)
        signals = build_signal_frame(
            data[["Open", "High", "Low", "Close", "Volume"]],
            direction="long",
            strategy_name="breakout",
            rsi_min=RSI_MIN,
            rsi_max=RSI_MAX,
        )
        index = len(signals) - 2
        if index < 60:
            reject(symbol, "indicator_history_insufficient")
            continue

        closed_bar = data.iloc[index]
        closed_time = str(closed_bar["ts"])
        atr_value = float(signals["atr14"].iloc[index])
        for item in state["signals"]:
            if item.get("status") != "shadow_open" or item.get("symbol") != symbol:
                continue
            if closed_time <= item.get("last_bar_time", closed_time):
                continue
            item["last_bar_time"] = closed_time
            if float(closed_bar["Open"]) <= item["stop"]:
                _close_signal(item, "stopped", float(closed_bar["Open"]))
                continue
            if float(closed_bar["Low"]) <= item["stop"]:
                _close_signal(item, "stopped", item["stop"])
                continue

            item["bars_open"] += 1
            item["high_water"] = max(item["high_water"], float(closed_bar["High"]))
            if float(closed_bar["Close"]) - item["entry"] >= TRAIL_TRIGGER_R * item["initial_risk"]:
                item["stop"] = max(
                    item["stop"], item["high_water"] - TRAIL_ATR_MULTIPLE * atr_value
                )
            if item["bars_open"] >= MAX_HOLD_BARS:
                _close_signal(item, "time_stop", float(closed_bar["Close"]))

        if not bool(data["daily_long_ok"].iloc[index]):
            reject(symbol, "daily_long_regime_failed")
            continue
        if not bool(signals["signal"].iloc[index]):
            reason = "breakout_not_confirmed"
            if float(signals["Close"].iloc[index]) <= float(signals["breakout_high20"].iloc[index]):
                reason = "close_below_20bar_high"
            elif not RSI_MIN <= float(signals["rsi14"].iloc[index]) <= RSI_MAX:
                reason = "rsi_out_of_range"
            reject(symbol, reason)
            continue

        signal_time = str(data["ts"].iloc[index])
        signal_id = f"{symbol}:{signal_time}"
        if any(item.get("id") == signal_id for item in state["signals"]):
            reject(symbol, "signal_already_recorded")
            continue
        if any(item.get("symbol") == symbol and item.get("status") == "shadow_open" for item in state["signals"]):
            reject(symbol, "symbol_shadow_position_already_open")
            continue

        entry = float(candles["Close"].iloc[-1])
        stop = float(data["Low"].iloc[max(0, index - STOP_LOOKBACK + 1):index + 1].min()) - STOP_ATR_BUFFER * atr_value
        initial_risk = entry - stop
        signal_close = float(data["Close"].iloc[index])
        if initial_risk <= 0:
            reject(symbol, "stop_not_below_entry")
            continue
        if abs(entry - signal_close) > 0.25 * initial_risk:
            reject(symbol, "entry_drift_exceeded_0_25R")
            continue

        state["signals"].append({
            "id": signal_id,
            "symbol": symbol,
            "strategy": STRATEGY_ID,
            "signal_utc": datetime.now(timezone.utc).isoformat(),
            "candle_time": signal_time,
            "last_bar_time": closed_time,
            "entry": entry,
            "stop": stop,
            "initial_risk": initial_risk,
            "high_water": entry,
            "bars_open": 0,
            "status": "shadow_open",
        })

    state["last_scan_finished_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_scan_symbols"] = scanned_symbols
    state["live_universe_size"] = len(symbols)
    state["last_rejection_counts"] = rejection_counts
    state["last_failed_condition_counts"] = condition_counts
    open_signals = [item for item in state["signals"] if item.get("status") == "shadow_open"]
    metrics = _closed_metrics(state["signals"])
    state["closed_shadow_metrics"] = metrics
    print(
        f"LONG breakout shadow scan #{state['scan_count']} universe={len(symbols)} "
        f"open={len(open_signals)} metrics={json.dumps(metrics, sort_keys=True)} "
        f"rejects={json.dumps(rejection_counts, sort_keys=True)}",
        flush=True,
    )


def print_status(state: dict) -> None:
    print(json.dumps({
        "strategy_id": state.get("strategy_id"),
        "scan_count": state.get("scan_count", 0),
        "live_universe_size": state.get("live_universe_size", 0),
        "last_scan_finished_utc": state.get("last_scan_finished_utc"),
        "last_rejection_counts": state.get("last_rejection_counts", {}),
        "closed_shadow_metrics": state.get("closed_shadow_metrics", {}),
        "open_signals": sum(item.get("status") == "shadow_open" for item in state.get("signals", [])),
    }, indent=2))


def main() -> None:
    state = load_state()
    if "--status" in sys.argv:
        print_status(state)
        return
    raise RuntimeError("Questo monitor va avviato dal worker LONG del bot, non come servizio separato")


if __name__ == "__main__":
    main()