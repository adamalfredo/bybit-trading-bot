from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from live_mean_reversion import get_mean_reversion_signal, load_live_config
from validate_strategy_v2 import fetch_klines

CONFIG_PATH = Path(__file__).with_name("best_strategy_v2.json")
STATE_PATH = Path(__file__).with_name("long_shadow_state.json")
SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "LINKUSDT", "AVAXUSDT",
    "ATOMUSDT", "DOTUSDT", "NEARUSDT", "UNIUSDT", "AAVEUSDT", "INJUSDT", "ZECUSDT", "SUIUSDT"
]


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"signals": [], "updated_utc": None, "scan_count": 0}


def save_state(state: dict) -> None:
    state["updated_utc"] = datetime.now(timezone.utc).isoformat()
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def scan_once(state: dict) -> None:
    config = load_live_config()
    state["scan_count"] = int(state.get("scan_count", 0)) + 1
    state["last_scan_started_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_error"] = None
    scanned_symbols = []

    for symbol in SYMBOLS:
        scanned_symbols.append(symbol)
        print(f"LONG shadow scan #{state['scan_count']} symbol={symbol}", flush=True)

        for item in state["signals"]:
            if item["status"] != "shadow_open" or item["symbol"] != symbol:
                continue
            latest = float(fetch_klines(symbol, "240", 20)["Close"].iloc[-1]) if fetch_klines(symbol, "240", 20) is not None else float(item["entry"])
            if latest <= item["stop"]:
                item["status"] = "stopped"
                item["exit"] = item["stop"]
                item["pnl_pct"] = (item["stop"] - item["entry"]) / item["entry"] * 100.0
            elif latest >= item["target"]:
                item["status"] = "target"
                item["exit"] = item["target"]
                item["pnl_pct"] = (item["target"] - item["entry"]) / item["entry"] * 100.0
            elif item["bars_open"] >= int(config.get("max_hold_bars", 12)):
                item["status"] = "time_stop"
                item["exit"] = latest
                item["pnl_pct"] = (latest - item["entry"]) / item["entry"] * 100.0
            item["bars_open"] += 1

        signal = get_mean_reversion_signal(symbol, "long", fetch_klines, config)
        if not signal:
            continue

        signal_id = f"{symbol}:{datetime.now(timezone.utc).strftime('%Y%m%d%H%M')}"
        if any(item["id"] == signal_id for item in state["signals"]):
            continue

        entry = float(signal["entry_price"])
        stop = float(signal["sl_price"])
        target = float(signal["tp_est"])
        state["signals"].append({
            "id": signal_id,
            "symbol": symbol,
            "signal_utc": datetime.now(timezone.utc).isoformat(),
            "entry": entry,
            "stop": stop,
            "target": target,
            "bars_open": 0,
            "status": "shadow_open",
        })
        print(f"SHADOW SIGNAL LONG {symbol} entry={entry} stop={stop} target={target}")

    state["last_scan_finished_utc"] = datetime.now(timezone.utc).isoformat()
    state["last_scan_symbols"] = scanned_symbols
    open_count = sum(1 for item in state["signals"] if item.get("status") == "shadow_open")
    closed_count = sum(1 for item in state["signals"] if item.get("status") != "shadow_open")
    print(f"LONG shadow scan #{state['scan_count']} completato: open={open_count} closed={closed_count} total={len(state['signals'])}", flush=True)


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
