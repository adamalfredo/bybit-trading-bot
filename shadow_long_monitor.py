from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from live_mean_reversion import get_mean_reversion_signal, load_live_config
from validate_strategy_v2 import fetch_klines

CONFIG_PATH = Path(__file__).with_name("best_strategy_v2.json")
STATE_PATH = Path(os.getenv("SHADOW_STATE_DIR", str(Path(__file__).parent))) / "long_shadow_state.json"
STRATEGY_ID = "mean_reversion_v2_diagnostics"
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT",
    "ATOMUSDT", "DOTUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "INJUSDT", "ZECUSDT", "SUIUSDT"
]


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("strategy_id") != STRATEGY_ID:
            old_signals = state.get("signals", [])
            archived = state.get("legacy_signals", [])
            state["legacy_signals"] = (archived + old_signals)[-2000:]
            state["signals"] = []
        state["schema_version"] = 3
        state["strategy_id"] = STRATEGY_ID
        state.setdefault("signals", [])
        state.setdefault("scan_count", 0)
        return state
    return {
        "schema_version": 3,
        "strategy_id": STRATEGY_ID,
        "signals": [],
        "legacy_signals": [],
        "updated_utc": None,
        "scan_count": 0,
    }


def save_state(state: dict) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def scan_once(state: dict, symbols: list[str] | None = None) -> None:
    config = load_live_config()
    state["scan_count"] = int(state.get("scan_count", 0)) + 1
    state["last_scan_started_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_error"] = None
    scanned_symbols = []
    rejection_counts = {}
    condition_counts = {}
    symbol_diagnostics = {}

    def count(target: dict, key: str) -> None:
        target[key] = target.get(key, 0) + 1

    active_symbols = symbols if symbols is not None else SYMBOLS
    for symbol in active_symbols:
        scanned_symbols.append(symbol)
        print(f"LONG shadow scan #{state['scan_count']} symbol={symbol}", flush=True)

        candles = fetch_klines(symbol, "240", 120)
        if candles is None or len(candles) < 2:
            continue
        closed_bar = candles.iloc[-2]
        closed_time = str(closed_bar["ts"])
        for item in state["signals"]:
            if item["status"] != "shadow_open" or item["symbol"] != symbol:
                continue
            if closed_time <= item.get("last_bar_time", closed_time):
                item["last_bar_time"] = closed_time
                continue
            item["last_bar_time"] = closed_time
            item["bars_open"] += 1
            latest = float(closed_bar["Close"])
            if float(closed_bar["Low"]) <= item["stop"]:
                item["status"] = "stopped"
                item["exit"] = item["stop"]
            elif float(closed_bar["High"]) >= item["target"]:
                item["status"] = "target"
                item["exit"] = item["target"]
            elif item["bars_open"] >= int(config["max_hold_bars"]):
                item["status"] = "time_stop"
                item["exit"] = latest
            if item["status"] != "shadow_open":
                item["pnl_pct"] = (item["exit"] - item["entry"]) / item["entry"] * 100.0 - 0.11

        diagnostics = {}
        signal = get_mean_reversion_signal(
            symbol, "long", fetch_klines, config, diagnostics=diagnostics
        )
        if not signal:
            reason = str(diagnostics.get("reason", "signal_unavailable"))
            count(rejection_counts, reason)
            for condition in diagnostics.get("failed_conditions", []):
                count(condition_counts, str(condition))
            symbol_diagnostics[symbol] = diagnostics
            continue

        signal_id = f"{symbol}:{signal['candle_time']}"
        if any(item["id"] == signal_id for item in state["signals"]):
            count(rejection_counts, "signal_already_recorded")
            continue
        if any(item.get("symbol") == symbol and item.get("status") == "shadow_open" for item in state["signals"]):
            count(rejection_counts, "symbol_shadow_position_already_open")
            continue

        entry = float(candles["Close"].iloc[-1])
        stop = float(signal["sl_price"])
        if entry <= stop:
            count(rejection_counts, "stop_not_below_entry")
            symbol_diagnostics[symbol] = {"reason": "stop_not_below_entry"}
            continue
        if abs(entry - float(signal["entry_price"])) > 0.25 * float(signal["r_dist"]):
            count(rejection_counts, "entry_drift_exceeded_0_25R")
            symbol_diagnostics[symbol] = {"reason": "entry_drift_exceeded_0_25R"}
            continue
        target = entry + float(config["target_r_multiple"]) * (entry - stop)
        state["signals"].append({
            "id": signal_id,
            "symbol": symbol,
            "signal_utc": datetime.now(timezone.utc).isoformat(),
            "candle_time": signal["candle_time"],
            "last_bar_time": closed_time,
            "entry": entry,
            "stop": stop,
            "target": target,
            "bars_open": 0,
            "status": "shadow_open",
        })
        print(f"SHADOW SIGNAL LONG {symbol} entry={entry} stop={stop} target={target}")

    state["last_scan_finished_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_scan_symbols"] = scanned_symbols
    state["live_universe_size"] = len(active_symbols)
    state["last_rejection_counts"] = rejection_counts
    state["last_failed_condition_counts"] = condition_counts
    state["last_symbol_diagnostics"] = symbol_diagnostics
    open_count = sum(1 for item in state["signals"] if item.get("status") == "shadow_open")
    closed_count = sum(1 for item in state["signals"] if item.get("status") != "shadow_open")
    closed = [item for item in state["signals"] if item.get("status") != "shadow_open"]
    pnl_values = [float(item.get("pnl_pct", 0.0)) for item in closed]
    positive = sum(pnl for pnl in pnl_values if pnl > 0)
    negative = -sum(pnl for pnl in pnl_values if pnl < 0)
    metrics = {
        "closed_count": len(closed),
        "net_pnl_pct": sum(pnl_values),
        "expectancy_pct": sum(pnl_values) / len(pnl_values) if pnl_values else 0.0,
        "profit_factor": positive / negative if negative else (999.0 if positive else 0.0),
    }
    state["closed_shadow_metrics"] = metrics
    print(f"LONG shadow scan #{state['scan_count']} completato: open={open_count} closed={closed_count} total={len(state['signals'])}", flush=True)
    print(
        f"LONG shadow rejects={json.dumps(rejection_counts, sort_keys=True)} "
        f"failed_conditions={json.dumps(condition_counts, sort_keys=True)} "
        f"closed_metrics={json.dumps(metrics, sort_keys=True)}",
        flush=True,
    )


def print_status(state: dict) -> None:
    open_count = sum(1 for item in state["signals"] if item.get("status") == "shadow_open")
    closed = [item for item in state["signals"] if item.get("status") != "shadow_open"]
    pnl_values = [float(item.get("pnl_pct", 0.0)) for item in closed]
    wins = [value for value in pnl_values if value > 0]
    losses = [abs(value) for value in pnl_values if value < 0]
    profit_factor = (sum(wins) / sum(losses)) if losses else (999.0 if wins else 0.0)
    expectancy = (sum(pnl_values) / len(pnl_values)) if pnl_values else 0.0
    print(json.dumps({
        "updated_utc": state.get("updated_utc"),
        "last_scan_finished_utc": state.get("last_scan_finished_utc"),
        "scan_count": state.get("scan_count", 0),
        "last_error": state.get("last_error"),
        "signals_total": len(state["signals"]),
        "signals_open": open_count,
        "signals_closed": len(closed),
        "closed_profit_factor": profit_factor,
        "closed_expectancy": expectancy,
        "strategy_id": state.get("strategy_id", "mean_reversion_v2"),
        "last_rejection_counts": state.get("last_rejection_counts", {}),
        "last_failed_condition_counts": state.get("last_failed_condition_counts", {}),
        "live_universe_size": state.get("live_universe_size", 0),
        "closed_shadow_metrics": state.get("closed_shadow_metrics", {}),
    }, indent=2))


def main() -> None:
    state = load_state()
    if "--status" in sys.argv:
        print_status(state)
        return
    if "--once" in sys.argv:
        try:
            scan_once(state)
        except Exception as exc:
            state["last_error"] = str(exc)
            print(f"shadow scan error: {exc}")
            raise
        finally:
            save_state(state)
        print_status(state)
        return
    print("LONG shadow monitor avviato; nessun ordine reale verrà inviato.")
    while True:
        try:
            scan_once(state)
        except Exception as exc:
            state["last_error"] = str(exc)
            print(f"shadow scan error: {exc}")
        finally:
            save_state(state)
        time.sleep(1800)


if __name__ == "__main__":
    main()
