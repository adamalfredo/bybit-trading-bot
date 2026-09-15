from __future__ import annotations

from typing import Iterable, List, Optional, Tuple


def order_notional_for_risk(
    entry_price: float,
    stop_distance: float,
    risk_usdt: float,
    min_notional: float,
) -> Optional[float]:
    if entry_price <= 0 or stop_distance <= 0 or risk_usdt <= 0 or min_notional <= 0:
        return None
    notional = (risk_usdt / stop_distance) * entry_price
    return notional if notional >= min_notional else None


def compute_trade_metrics(trades: Iterable[float]) -> dict:
    trade_list = [float(t) for t in trades]
    total = len(trade_list)

    if total == 0:
        return {
            "total_trades": 0,
            "wins": 0,
            "losses": 0,
            "win_rate": 0.0,
            "avg_win": 0.0,
            "avg_loss": 0.0,
            "expectancy": 0.0,
            "profit_factor": 0.0,
            "net": 0.0,
        }

    wins = [t for t in trade_list if t > 0]
    losses = [t for t in trade_list if t < 0]

    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = abs(sum(losses) / len(losses)) if losses else 0.0
    win_rate = len(wins) / total
    net = sum(trade_list)
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else (999.0 if gross_profit > 0 else 0.0)
    expectancy = (win_rate * avg_win) - ((1 - win_rate) * avg_loss)

    return {
        "total_trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": win_rate,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        "expectancy": expectancy,
        "profit_factor": profit_factor,
        "net": net,
    }


def should_allow_live_trading(trades: Iterable[float], min_trades: int = 30) -> Tuple[bool, dict]:
    trade_list = [float(t) for t in trades]
    metrics = compute_trade_metrics(trade_list)
    metrics["min_trades_required"] = min_trades

    if metrics["total_trades"] < min_trades:
        return False, metrics

    if metrics["expectancy"] <= 0:
        return False, metrics
    if metrics["profit_factor"] < 1.15:
        return False, metrics
    if metrics["avg_loss"] > 2.2 * metrics["avg_win"] and metrics["avg_win"] > 0:
        return False, metrics

    return True, metrics
