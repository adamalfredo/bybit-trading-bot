from __future__ import annotations

import argparse
import json
from pathlib import Path
from datetime import datetime, timezone

from strategy_gate import should_allow_live_trading
from strategy_v2 import (
    backtest_breakout_long,
    backtest_breakout_short,
    backtest_mean_reversion_long,
    backtest_mean_reversion_short,
    backtest_simple_long,
    backtest_simple_short,
    compute_trade_metrics,
)
from validate_strategy_v2 import BEST_CONFIG_JSON, SYMBOLS, attach_daily_regime, fetch_klines


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dry run operativo usando la migliore config salvata in best_strategy_v2.json"
    )
    parser.add_argument(
        "--config",
        default=BEST_CONFIG_JSON,
        help="Percorso al file JSON con la config vincente",
    )
    parser.add_argument(
        "--symbols",
        nargs="*",
        default=SYMBOLS,
        help="Lista simboli da valutare (default: universo del validatore)",
    )
    parser.add_argument(
        "--bars-4h",
        type=int,
        default=1200,
        help="Numero candele 4h da scaricare per simbolo",
    )
    parser.add_argument(
        "--bars-d",
        type=int,
        default=300,
        help="Numero candele daily da scaricare per simbolo",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=30,
        help="Soglia minima trade per il gate GO/NO-GO",
    )
    parser.add_argument(
        "--report-out",
        default="validation_v2_latest.txt",
        help="File output report testuale",
    )
    parser.add_argument(
        "--status-out",
        default="live_gate_status_v2.json",
        help="File output stato macchina-legibile GO/NO-GO",
    )
    return parser.parse_args()


def _load_best_config(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if "config" not in payload:
        raise ValueError("File config invalido: campo 'config' mancante")
    return payload


def _family_runners(family: str):
    mapping = {
        "pullback": (backtest_simple_long, backtest_simple_short),
        "breakout": (backtest_breakout_long, backtest_breakout_short),
        "mean_reversion": (backtest_mean_reversion_long, backtest_mean_reversion_short),
    }
    if family not in mapping:
        raise ValueError(f"Strategy family non supportata: {family}")
    return mapping[family]


def main() -> int:
    args = _parse_args()
    config_path = Path(args.config)
    payload = _load_best_config(config_path)
    config = payload["config"]

    long_runner, short_runner = _family_runners(str(config["strategy_family"]))

    all_long_trades: list[float] = []
    all_short_trades: list[float] = []
    lines: list[str] = []

    lines.append("=== BEST STRATEGY V2 DRY RUN ===")
    lines.append(f"Config file: {config_path}")
    lines.append(f"Family: {config['strategy_family']}")
    lines.append(f"Symbols: {', '.join(args.symbols)}")
    lines.append("")

    for symbol in args.symbols:
        df4 = fetch_klines(symbol, interval="240", limit=args.bars_4h)
        dfd = fetch_klines(symbol, interval="D", limit=args.bars_d)
        if df4.empty or dfd.empty:
            lines.append(f"{symbol}: SKIP (dati insufficienti)")
            continue

        merged = attach_daily_regime(df4, dfd)

        long_result = long_runner(
            merged[["Open", "High", "Low", "Close", "Volume", "daily_long_ok"]],
            max_hold_bars=int(config["max_hold_bars"]),
            stop_atr_buffer=float(config["stop_atr_buffer"]),
            target_r_multiple=float(config["target_r_multiple"]),
            rsi_min=float(config["long_rsi_min"]),
            rsi_max=float(config["long_rsi_max"]),
            regime_column="daily_long_ok",
        )
        short_result = short_runner(
            merged[["Open", "High", "Low", "Close", "Volume", "daily_short_ok"]],
            max_hold_bars=int(config["max_hold_bars"]),
            stop_atr_buffer=float(config["stop_atr_buffer"]),
            target_r_multiple=float(config["target_r_multiple"]),
            rsi_min=float(config["short_rsi_min"]),
            rsi_max=float(config["short_rsi_max"]),
            regime_column="daily_short_ok",
        )

        sym_long = compute_trade_metrics(long_result["trades"])
        sym_short = compute_trade_metrics(short_result["trades"])
        sym_total = compute_trade_metrics(long_result["trades"] + short_result["trades"])

        all_long_trades.extend(long_result["trades"])
        all_short_trades.extend(short_result["trades"])

        lines.append(
            "{0}: trades={1} long_exp={2:.4f} short_exp={3:.4f} total_exp={4:.4f} PF={5:.2f} net={6:.4f}".format(
                symbol,
                sym_total["total_trades"],
                sym_long["expectancy"],
                sym_short["expectancy"],
                sym_total["expectancy"],
                sym_total["profit_factor"],
                sym_total["net"],
            )
        )

    all_trades = all_long_trades + all_short_trades
    allowed, gate_metrics = should_allow_live_trading(all_trades, min_trades=args.min_trades)
    long_metrics = compute_trade_metrics(all_long_trades)
    short_metrics = compute_trade_metrics(all_short_trades)

    lines.append("")
    lines.append("=== AGGREGATO ===")
    lines.append(
        "LONG  trades={0} exp={1:.4f} PF={2:.2f} net={3:.4f}".format(
            long_metrics["total_trades"],
            long_metrics["expectancy"],
            long_metrics["profit_factor"],
            long_metrics["net"],
        )
    )
    lines.append(
        "SHORT trades={0} exp={1:.4f} PF={2:.2f} net={3:.4f}".format(
            short_metrics["total_trades"],
            short_metrics["expectancy"],
            short_metrics["profit_factor"],
            short_metrics["net"],
        )
    )
    lines.append(
        "TOTAL trades={0} exp={1:.4f} PF={2:.2f} net={3:.4f}".format(
            gate_metrics["total_trades"],
            gate_metrics["expectancy"],
            gate_metrics["profit_factor"],
            gate_metrics["net"],
        )
    )
    lines.append(f"LIVE GATE: {'GO' if allowed else 'NO-GO'} (min_trades={args.min_trades})")

    report_text = "\n".join(lines)
    Path(args.report_out).write_text(report_text + "\n", encoding="utf-8")

    status_payload = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "decision": "GO" if allowed else "NO-GO",
        "min_trades": int(args.min_trades),
        "config_file": str(config_path),
        "config": config,
        "symbols": list(args.symbols),
        "metrics": {
            "long": long_metrics,
            "short": short_metrics,
            "total": gate_metrics,
        },
    }
    Path(args.status_out).write_text(json.dumps(status_payload, indent=2) + "\n", encoding="utf-8")

    print(report_text)
    print(f"\nReport salvato in: {args.report_out}")
    print(f"Stato salvato in: {args.status_out}")
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
