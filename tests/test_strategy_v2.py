import pandas as pd

from strategy_v2 import backtest_breakout_long, backtest_breakout_short, backtest_mean_reversion_long, backtest_mean_reversion_short, backtest_simple_long, backtest_simple_short, build_signal_frame, compute_trade_metrics


def test_signal_frame_creates_long_entries():
    values = []
    base = 100.0
    for i in range(180):
        if i < 60:
            base += 0.3
        elif i < 90:
            base += 0.5
        elif i < 115:
            base += 2.5
        elif i < 140:
            base -= 0.8
        else:
            base += 0.7
        values.append(base)

    data = []
    for idx, price in enumerate(values):
        high = price * (1.01 + 0.002 * (idx % 6))
        low = price * (0.99 - 0.002 * (idx % 5))
        data.append({
            "Open": price * 0.998,
            "High": high,
            "Low": low,
            "Close": price,
            "Volume": 1200 + idx,
        })

    df = pd.DataFrame(data)
    out = build_signal_frame(df)
    assert out["signal"].sum() > 0


def test_backtest_closes_generated_trade():
    values = []
    base = 100.0
    for i in range(180):
        if i < 60:
            base += 0.3
        elif i < 90:
            base += 0.5
        elif i < 115:
            base += 2.5
        elif i < 140:
            base -= 0.8
        else:
            base += 0.7
        values.append(base)

    df = pd.DataFrame({
        "Open": [price * 0.998 for price in values],
        "High": [price * (1.01 + 0.002 * (idx % 6)) for idx, price in enumerate(values)],
        "Low": [price * (0.99 - 0.002 * (idx % 5)) for idx, price in enumerate(values)],
        "Close": values,
        "Volume": [1200 + idx for idx in range(len(values))],
    })

    result = backtest_simple_long(df)
    assert result["trade_count"] > 0


def test_short_backtest_closes_generated_trade():
    values = []
    base = 200.0
    for i in range(180):
        if i < 60:
            base -= 0.35
        elif i < 90:
            base -= 0.55
        elif i < 115:
            base -= 2.2
        elif i < 140:
            base += 0.9
        else:
            base -= 0.8
        values.append(base)

    df = pd.DataFrame({
        "Open": [price * 1.002 for price in values],
        "High": [price * (1.01 + 0.002 * (idx % 5)) for idx, price in enumerate(values)],
        "Low": [price * (0.99 - 0.002 * (idx % 4)) for idx, price in enumerate(values)],
        "Close": values,
        "Volume": [1400 + idx for idx in range(len(values))],
    })

    signals = build_signal_frame(df, direction="short", rsi_min=25.0, rsi_max=55.0)
    assert signals["signal"].sum() > 0

    result = backtest_simple_short(df)
    assert result["trade_count"] > 0


def test_breakout_backtests_close_generated_trades():
    long_values = []
    long_base = 100.0
    for i in range(180):
        if i < 70:
            long_base += 0.04
        elif i < 110:
            long_base += (0.12 if i % 2 == 0 else -0.11)
        else:
            long_base += (0.42 if i < 125 else 0.22)
        long_values.append(long_base)

    long_df = pd.DataFrame({
        "Open": [price * 0.999 for price in long_values],
        "High": [price * (1.0015 + 0.0005 * (idx % 2)) for idx, price in enumerate(long_values)],
        "Low": [price * (0.9985 - 0.0004 * (idx % 2)) for idx, price in enumerate(long_values)],
        "Close": long_values,
        "Volume": [1500 + idx for idx in range(len(long_values))],
    })
    long_signals = build_signal_frame(long_df, direction="long", strategy_name="breakout", rsi_min=55.0, rsi_max=78.0)
    assert long_signals["signal"].sum() > 0
    assert backtest_breakout_long(long_df)["trade_count"] > 0

    short_values = []
    short_base = 200.0
    for i in range(180):
        if i < 70:
            short_base -= 0.04
        elif i < 110:
            short_base += (0.11 if i % 2 == 0 else -0.12)
        else:
            short_base -= (0.48 if i < 125 else 0.26)
        short_values.append(short_base)

    short_df = pd.DataFrame({
        "Open": [price * 1.001 for price in short_values],
        "High": [price * (1.0015 + 0.0005 * (idx % 2)) for idx, price in enumerate(short_values)],
        "Low": [price * (0.9985 - 0.0004 * (idx % 2)) for idx, price in enumerate(short_values)],
        "Close": short_values,
        "Volume": [1500 + idx for idx in range(len(short_values))],
    })
    short_signals = build_signal_frame(short_df, direction="short", strategy_name="breakout", rsi_min=22.0, rsi_max=45.0)
    assert short_signals["signal"].sum() > 0
    assert backtest_breakout_short(short_df)["trade_count"] > 0


def test_mean_reversion_backtests_close_generated_trades():
    long_values = []
    long_base = 100.0
    for i in range(180):
        if i < 80:
            long_base += 0.22
        elif i < 95:
            long_base -= 0.65
        elif i < 110:
            long_base += 0.45
        else:
            long_base += 0.15
        long_values.append(long_base)

    long_df = pd.DataFrame({
        "Open": [price * 0.999 for price in long_values],
        "High": [price * (1.004 + 0.001 * (idx % 3)) for idx, price in enumerate(long_values)],
        "Low": [price * (0.996 - 0.002 * (idx % 2)) for idx, price in enumerate(long_values)],
        "Close": long_values,
        "Volume": [1600 + idx for idx in range(len(long_values))],
    })
    long_signals = build_signal_frame(long_df, direction="long", strategy_name="mean_reversion", rsi_min=35.0, rsi_max=58.0)
    assert long_signals["signal"].sum() > 0
    assert backtest_mean_reversion_long(long_df)["trade_count"] > 0

    short_values = []
    short_base = 180.0
    for i in range(180):
        if i < 80:
            short_base -= 0.24
        elif i < 95:
            short_base += 0.7
        elif i < 110:
            short_base -= 0.5
        else:
            short_base -= 0.18
        short_values.append(short_base)

    short_df = pd.DataFrame({
        "Open": [price * 1.001 for price in short_values],
        "High": [price * (1.004 + 0.001 * (idx % 3)) for idx, price in enumerate(short_values)],
        "Low": [price * (0.996 - 0.002 * (idx % 2)) for idx, price in enumerate(short_values)],
        "Close": short_values,
        "Volume": [1600 + idx for idx in range(len(short_values))],
    })
    short_signals = build_signal_frame(short_df, direction="short", strategy_name="mean_reversion", rsi_min=45.0, rsi_max=78.0)
    assert short_signals["signal"].sum() > 0
    assert backtest_mean_reversion_short(short_df)["trade_count"] > 0


def test_metrics_negative_result_is_detected():
    trades = [0.08, 0.06, -0.16, 0.05, -0.12, 0.04, -0.15]
    metrics = compute_trade_metrics(trades)
    assert metrics["expectancy"] < 0
    assert metrics["net"] < 0
