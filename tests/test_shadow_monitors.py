import json

import pandas as pd
import pytest

import shadow_long_monitor
import shadow_long_breakout_monitor
import shadow_short_monitor


@pytest.mark.parametrize(
    ("monitor", "direction"),
    [
        (shadow_long_monitor, "long"),
        (shadow_short_monitor, "short"),
    ],
)
def test_shadow_monitor_counts_signal_rejection(monkeypatch, monitor, direction):
    candles = pd.DataFrame({
        "ts": [pd.Timestamp("2026-10-08 00:00"), pd.Timestamp("2026-10-08 04:00")],
        "Open": [100.0, 100.0],
        "High": [101.0, 101.0],
        "Low": [99.0, 99.0],
        "Close": [100.0, 100.0],
        "Volume": [1000.0, 1000.0],
    })

    monkeypatch.setattr(monitor, "SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(monitor, "fetch_klines", lambda *args, **kwargs: candles)
    if direction == "short":
        monkeypatch.setattr(monitor, "load_live_config", lambda: {"max_hold_bars": 12})

    def rejected_signal(_symbol, _direction, _fetch, _config, diagnostics=None):
        diagnostics.update({
            "reason": "daily_close_not_below_ema50" if _direction == "short" else "daily_close_not_above_ema50",
            "failed_conditions": [],
        })
        return None

    monkeypatch.setattr(monitor, "get_mean_reversion_signal", rejected_signal)
    state = {"signals": [], "scan_count": 0}

    monitor.scan_once(state)

    expected = "daily_close_not_below_ema50" if direction == "short" else "daily_close_not_above_ema50"
    assert state["last_rejection_counts"] == {expected: 1}
    assert state["last_symbol_diagnostics"]["BTCUSDT"]["reason"] == expected


def test_short_shadow_migration_archives_old_strategy_state(monkeypatch, tmp_path):
    state_path = tmp_path / "short_shadow_state.json"
    legacy_signal = {"id": "BTCUSDT:old", "status": "shadow_open"}
    state_path.write_text(json.dumps({"schema_version": 2, "signals": [legacy_signal]}), encoding="utf-8")
    monkeypatch.setattr(shadow_short_monitor, "STATE_PATH", state_path)

    state = shadow_short_monitor.load_state()

    assert state["strategy_id"] == "mean_reversion_v2"
    assert state["signals"] == []
    assert state["legacy_signals"] == [legacy_signal]


@pytest.mark.parametrize(
    ("monitor", "direction", "stop"),
    [
        (shadow_long_monitor, "long", 90.0),
        (shadow_short_monitor, "short", 110.0),
    ],
)
def test_shadow_does_not_open_overlapping_signal(monkeypatch, monitor, direction, stop):
    candles = pd.DataFrame({
        "ts": [pd.Timestamp("2026-10-08 00:00"), pd.Timestamp("2026-10-08 04:00")],
        "Open": [100.0, 100.0],
        "High": [101.0, 101.0],
        "Low": [99.0, 99.0],
        "Close": [100.0, 100.0],
        "Volume": [1000.0, 1000.0],
    })
    monkeypatch.setattr(monitor, "SYMBOLS", ["BTCUSDT"])
    monkeypatch.setattr(monitor, "fetch_klines", lambda *args, **kwargs: candles)
    if direction == "short":
        monkeypatch.setattr(monitor, "load_live_config", lambda: {
            "max_hold_bars": 12, "target_r_multiple": 1.1,
        })

    def signal(_symbol, _direction, _fetch, _config, diagnostics=None):
        if diagnostics is not None:
            diagnostics.update({"reason": "signal_confirmed", "failed_conditions": []})
        return {"candle_time": "previous-bar", "entry_price": 100.0,
                "r_dist": 10.0, "sl_price": stop}

    monkeypatch.setattr(monitor, "get_mean_reversion_signal", signal)
    state = {"signals": [{
        "id": "BTCUSDT:older", "symbol": "BTCUSDT", "status": "shadow_open",
        "last_bar_time": str(candles["ts"].iloc[-2]), "bars_open": 0,
        "entry": 100.0, "stop": stop, "target": 110.0 if direction == "long" else 90.0,
    }], "scan_count": 0}

    monitor.scan_once(state, symbols=["BTCUSDT"])

    assert len(state["signals"]) == 1
    assert state["last_rejection_counts"] == {"symbol_shadow_position_already_open": 1}
    assert state["closed_shadow_metrics"]["closed_count"] == 0


def test_long_breakout_shadow_archives_mean_reversion_state(monkeypatch, tmp_path):
    state_path = tmp_path / "long_breakout_shadow_state.json"
    legacy_signal = {"id": "BTCUSDT:mean-reversion", "status": "shadow_open"}
    state_path.write_text(json.dumps({"schema_version": 2, "signals": [legacy_signal]}), encoding="utf-8")
    monkeypatch.setattr(shadow_long_breakout_monitor, "STATE_PATH", state_path)

    state = shadow_long_breakout_monitor.load_state()

    assert state["strategy_id"] == "long_breakout_atr_trail_v1"
    assert state["signals"] == []
    assert state["legacy_signals"] == [legacy_signal]


def test_long_breakout_shadow_trails_after_one_r_and_closes_next_bar(monkeypatch):
    base_time = pd.Timestamp("2026-01-01 00:00")
    candle_sets = []
    for offset in range(3):
        frame = pd.DataFrame({
            "ts": [base_time + pd.Timedelta(hours=4 * i) for i in range(120)],
            "Open": [100.0] * 120,
            "High": [101.0] * 120,
            "Low": [99.0] * 120,
            "Close": [100.0] * 120,
            "Volume": [1000.0] * 120,
        })
        frame["ts"] += pd.Timedelta(hours=4 * offset)
        candle_sets.append(frame)
    candle_sets[1].loc[118, ["High", "Close"]] = [104.0, 103.0]
    candle_sets[2].loc[118, ["Open", "High", "Low", "Close"]] = [103.0, 104.0, 101.5, 102.5]
    daily = pd.DataFrame({
        "ts": pd.date_range("2025-10-01", periods=80, freq="D"),
        "Open": [100.0] * 80,
        "High": [102.0 + i for i in range(80)],
        "Low": [99.0 + i for i in range(80)],
        "Close": [100.0 + i for i in range(80)],
        "Volume": [1000.0] * 80,
    })
    scan_index = 0

    def fetch(_symbol, interval, _limit):
        nonlocal scan_index
        if interval == "D":
            return daily
        result = candle_sets[min(scan_index, 2)]
        scan_index += 1
        return result

    signal_calls = 0

    def signal_builder(data, direction, strategy_name, rsi_min, rsi_max):
        nonlocal signal_calls
        result = data.copy()
        result["atr14"] = 1.0
        result["rsi14"] = 60.0
        result["breakout_high20"] = 99.0
        result["signal"] = False
        if signal_calls == 0:
            result.loc[len(result) - 2, "signal"] = True
        signal_calls += 1
        return result

    monkeypatch.setattr(shadow_long_breakout_monitor, "fetch_klines", fetch)
    monkeypatch.setattr(shadow_long_breakout_monitor, "build_signal_frame", signal_builder)
    state = {"signals": [], "scan_count": 0}

    shadow_long_breakout_monitor.scan_once(state, ["BTCUSDT"])
    assert len(state["signals"]) == 1
    assert state["signals"][0]["status"] == "shadow_open"

    shadow_long_breakout_monitor.scan_once(state, ["BTCUSDT"])
    assert state["signals"][0]["stop"] == pytest.approx(102.0)
    assert state["signals"][0]["status"] == "shadow_open"

    shadow_long_breakout_monitor.scan_once(state, ["BTCUSDT"])
    signal = state["signals"][0]
    assert signal["status"] == "stopped"
    assert signal["exit"] == pytest.approx(102.0)
    assert signal["pnl_pct"] == pytest.approx(1.89)