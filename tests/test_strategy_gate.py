import math

from strategy_gate import compute_trade_metrics, order_notional_for_risk, should_allow_live_trading


def test_order_notional_rejects_minimum_that_exceeds_risk():
    assert order_notional_for_risk(100.0, 10.0, 0.5, 6.0) is None


def test_order_notional_keeps_risk_sized_value_above_minimum():
    assert order_notional_for_risk(100.0, 5.0, 0.5, 6.0) == 10.0


def test_negative_expectancy_blocks_live_trading():
    trades = [-0.35, -0.30, 0.10, 0.12, 0.08, -0.42]
    allowed, report = should_allow_live_trading(trades)
    assert allowed is False
    assert report["expectancy"] < 0
    assert report["total_trades"] == 6


def test_positive_edge_allows_live_trading():
    trades = [
        0.18, 0.22, 0.16, 0.12, 0.14, 0.20, 0.13, 0.17, 0.15, 0.19,
        0.21, 0.18, 0.14, 0.16, 0.18, 0.20, 0.17, 0.13, 0.15, 0.19,
        0.22, 0.16, -0.08, -0.09, -0.10, -0.07, -0.11, -0.08, -0.09, -0.10
    ]
    allowed, report = should_allow_live_trading(trades)
    assert allowed is True
    assert report["expectancy"] > 0
    assert report["profit_factor"] >= 1.15


def test_compute_trade_metrics_handles_empty_input():
    metrics = compute_trade_metrics([])
    assert metrics["total_trades"] == 0
    assert math.isclose(metrics["expectancy"], 0.0)
    assert metrics["profit_factor"] == 0.0
