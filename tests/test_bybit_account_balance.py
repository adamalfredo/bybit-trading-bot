import importlib.util
from pathlib import Path

import pytest

class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


def load_bot(filename: str, module_name: str):
    path = Path(__file__).parents[1] / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("filename", "module_name"),
    [
        ("main-pullback.py", "long_balance_test"),
        ("main-short-pullback.py", "short_balance_test"),
    ],
)
def test_bot_equity_falls_back_to_usdt_coin_equity(monkeypatch, filename, module_name):
    bot = load_bot(filename, module_name)
    response = FakeResponse({"retCode": 0, "result": {"list": [{
        "totalEquity": "0",
        "totalAvailableBalance": "0",
        "coin": [{"coin": "USDT", "equity": "35.0361", "availableToWithdraw": "0"}],
    }]}})
    monkeypatch.setattr(bot, "_bybit_signed_get", lambda *_args, **_kwargs: response)

    assert bot.get_total_equity() == pytest.approx(35.0361)


@pytest.mark.parametrize(
    ("filename", "module_name"),
    [
        ("main-pullback.py", "long_available_test"),
        ("main-short-pullback.py", "short_available_test"),
    ],
)
def test_bot_uses_account_available_balance_not_withdrawable(monkeypatch, filename, module_name):
    bot = load_bot(filename, module_name)
    response = FakeResponse({"retCode": 0, "result": {"list": [{
        "totalAvailableBalance": "22.5",
        "coin": [{"coin": "USDT", "walletBalance": "35.0361", "availableToWithdraw": "0"}],
    }]}})
    monkeypatch.setattr(bot, "_bybit_signed_get", lambda *_args, **_kwargs: response)

    assert bot.get_usdt_balance() == pytest.approx(22.5)


@pytest.mark.parametrize(
    ("filename", "module_name", "market_function"),
    [
        ("main-pullback.py", "long_zero_margin_test", "market_long"),
        ("main-short-pullback.py", "short_zero_margin_test", "market_short"),
    ],
)
def test_bot_rejects_order_when_trading_margin_is_zero(
    monkeypatch, filename, module_name, market_function
):
    bot = load_bot(filename, module_name)
    monkeypatch.setattr(bot, "get_last_price", lambda _symbol: 100.0)
    monkeypatch.setattr(bot, "get_instrument_info", lambda _symbol: {
        "qty_step": 0.01, "min_qty": 0.01, "min_order_amt": 5.0,
    })
    monkeypatch.setattr(bot, "get_usdt_balance", lambda: 0.0)

    assert getattr(bot, market_function)("BTCUSDT", 10.0) is None
