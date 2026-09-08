import hashlib
import hmac
import os
import sys
import time
from pathlib import Path

import requests
from dotenv import load_dotenv

from strategy_gate import should_allow_live_trading

_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path)

API_KEY = os.getenv("BYBIT_API_KEY", "")
API_SECRET = os.getenv("BYBIT_API_SECRET", "")
TESTNET = os.getenv("BYBIT_TESTNET", "0") == "1"
BASE_URL = "https://api-testnet.bybit.com" if TESTNET else "https://api.bybit.com"


def signed_get(path: str, params: dict) -> dict:
    ts = str(int(time.time() * 1000))
    recv_window = "30000"
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    sig = hmac.new(API_SECRET.encode(), f"{ts}{API_KEY}{recv_window}{query}".encode(), hashlib.sha256).hexdigest()
    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-SIGN": sig,
        "X-BAPI-TIMESTAMP": ts,
        "X-BAPI-RECV-WINDOW": recv_window,
    }
    resp = requests.get(f"{BASE_URL}{path}", params=params, headers=headers, timeout=15)
    try:
        return resp.json()
    except Exception:
        return {"retCode": -1, "retMsg": resp.text[:200]}


def fetch_recent_closed_pnl(limit: int = 100) -> list[float]:
    data = signed_get("/v5/position/closed-pnl", {"category": "linear", "limit": str(limit)})
    if data.get("retCode") != 0:
        raise RuntimeError(f"Bybit API error: {data.get('retMsg')}")

    values = []
    for trade in data.get("result", {}).get("list", []):
        pnl = float(trade.get("closedPnl") or 0.0)
        if pnl != 0:
            values.append(pnl)
    return values


def make_report(trades: list[float]) -> dict:
    allowed, report = should_allow_live_trading(trades, min_trades=30)
    report["allowed"] = allowed
    return report


def print_report(report: dict) -> None:
    print("=== GO / NO-GO CHECK ===")
    print(f"Total trades: {report['total_trades']}")
    print(f"Wins: {report['wins']} | Losses: {report['losses']}")
    print(f"Win rate: {report['win_rate'] * 100:.1f}%")
    print(f"Avg win: {report['avg_win']:.4f}")
    print(f"Avg loss: {report['avg_loss']:.4f}")
    print(f"Expectancy: {report['expectancy']:.4f}")
    print(f"Profit factor: {report['profit_factor']:.2f}")
    print(f"Net: {report['net']:.4f}")
    print(f"Decision: {'GO' if report['allowed'] else 'NO-GO'}")


def main() -> int:
    try:
        if not API_KEY or not API_SECRET:
            raise RuntimeError("Missing BYBIT_API_KEY or BYBIT_API_SECRET in environment")
        trades = fetch_recent_closed_pnl(limit=100)
        report = make_report(trades)
        print_report(report)
        return 0 if report["allowed"] else 1
    except Exception as exc:
        print(f"[ERROR] {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
