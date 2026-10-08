import json

import pandas as pd
import pytest

import shadow_long_monitor
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