from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from research_volatility_expansion import backtest, features, is_signal
from strategy_v2 import atr, ema
from validate_strategy_v2 import attach_daily_regime, fetch_klines

CONFIG_PATH = Path(__file__).with_name("short_shadow_config.json")
STATE_PATH = Path(os.getenv("SHADOW_STATE_DIR", str(Path(__file__).parent))) / "short_shadow_state.json"
SYMBOLS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT", "ATOMUSDT", "DOTUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "INJUSDT", "ZECUSDT", "SUIUSDT"]


def load_state() -> dict:
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if state.get("schema_version") != 2:
            state["legacy_signals"] = state.pop("signals", [])
            state["signals"] = []
            state["schema_version"] = 2
        return state
    return {"schema_version": 2, "signals": [], "updated_utc": None, "scan_count": 0}


def save_state(state: dict) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def scan_once(state: dict) -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))["config"]
    state["scan_count"] = int(state.get("scan_count", 0)) + 1
    state["last_scan_started_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_error"] = None
    scanned_symbols = []
    for symbol in SYMBOLS:
        scanned_symbols.append(symbol)
        print(f"SHORT shadow scan #{state['scan_count']} symbol={symbol}", flush=True)
        data = attach_daily_regime(fetch_klines(symbol, "240", 120), fetch_klines(symbol, "D", 100))
        prepared = features(data)
        index = len(prepared) - 2
        closed_time = str(prepared["ts"].iloc[index])
        for item in state["signals"]:
            if item["status"] != "shadow_open" or item["symbol"] != symbol:
                continue
            if closed_time <= item.get("last_bar_time", closed_time):
                item["last_bar_time"] = closed_time
                continue
            item["last_bar_time"] = closed_time
            item["bars_open"] += 1
            latest = float(prepared["Close"].iloc[index])
            latest_high = float(prepared["High"].iloc[index])
            latest_low = float(prepared["Low"].iloc[index])
            if latest_high >= item["stop"]:
                item["status"] = "stopped"
                item["exit"] = item["stop"]
            elif latest_low <= item["target"]:
                item["status"] = "target"
                item["exit"] = item["target"]
            elif item["bars_open"] >= config["max_hold_bars"]:
                item["status"] = "time_stop"
                item["exit"] = latest
            if item["status"] != "shadow_open":
                item["pnl_pct"] = (item["entry"] - item["exit"]) / item["entry"] * 100.0 - 0.11
        if index < 80 or not bool(prepared["daily_short_ok"].iloc[index]):
            continue
        if not is_signal(prepared, index, "short", config):
            continue
        signal_id = f"{symbol}:{closed_time}"
        if any(item["id"] == signal_id for item in state["signals"]):
            continue
        atr_value = float(prepared["atr14"].iloc[index])
        entry = float(prepared["Close"].iloc[index + 1])
        stop = float(prepared["High"].iloc[max(0, index - 8):index + 1].max()) + float(config["stop_atr_buffer"]) * atr_value
        risk = stop - entry
        if risk <= 0:
            continue
        target = entry - float(config["target_r_multiple"]) * risk
        state["signals"].append({
            "id": signal_id,
            "symbol": symbol,
            "signal_utc": datetime.now(timezone.utc).isoformat(),
            "candle_time": closed_time,
            "last_bar_time": closed_time,
            "entry": entry,
            "stop": stop,
            "target": target,
            "bars_open": 0,
            "status": "shadow_open",
        })
        print(f"SHADOW SIGNAL SHORT {symbol} entry={state['signals'][-1]['entry']}")
    state["last_scan_finished_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_scan_symbols"] = scanned_symbols
    open_count = sum(1 for item in state["signals"] if item.get("status") == "shadow_open")
    closed_count = sum(1 for item in state["signals"] if item.get("status") != "shadow_open")
    print(f"SHORT shadow scan #{state['scan_count']} completato: open={open_count} closed={closed_count} total={len(state['signals'])}", flush=True)


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
    print("SHORT shadow monitor avviato; nessun ordine reale verrà inviato.")
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
