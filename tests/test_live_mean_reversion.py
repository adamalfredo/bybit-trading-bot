import pandas as pd

from live_mean_reversion import _failed_mean_reversion_conditions, get_mean_reversion_signal


def test_signal_diagnostics_report_daily_regime_rejection():
    candles = pd.DataFrame({
        "ts": range(80),
        "Open": [100.0] * 80,
        "High": [101.0] * 80,
        "Low": [99.0] * 80,
        "Close": [100.0] * 80,
        "Volume": [1000.0] * 80,
    })
    daily = pd.DataFrame({"Close": [100.0] * 60})

    def fetch(_symbol, interval, limit):
        return candles if interval == "240" else daily

    diagnostics = {}
    signal = get_mean_reversion_signal(
        "BTCUSDT",
        "long",
        fetch,
        config={"strategy_family": "mean_reversion"},
        diagnostics=diagnostics,
    )

    assert signal is None
    assert diagnostics["reason"] == "daily_close_not_above_ema50"


def test_diagnostics_identify_multiple_failed_long_conditions():
    signals = pd.DataFrame({
        "Close": [100.0, 99.0],
        "ema20": [100.0, 100.0],
        "rsi14": [40.0, 40.0],
        "atr14": [1.0, 1.0],
        "stretch_z": [-0.5, -1.0],
        "ema50_slope5": [0.0, 0.0],
    })

    failed = _failed_mean_reversion_conditions(
        signals, idx=1, direction="long", rsi_min=35.0, rsi_max=58.0
    )

    assert "washout_not_deep_enough" in failed
    assert "washout_not_recovering" in failed
    assert "reversal_close_not_higher" in failed