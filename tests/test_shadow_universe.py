import importlib.util
from pathlib import Path

import pytest


class FakeResponse:
    def json(self):
        return {
            "retCode": 0,
            "result": {"list": [
                {"symbol": "BTCUSDT", "turnover24h": "30000000", "lastPrice": "100", "price24hPcnt": "0.01"},
                {"symbol": "ETHUSDT", "turnover24h": "20000000", "lastPrice": "10", "price24hPcnt": "0.02"},
                {"symbol": "SOLUSDT", "turnover24h": "15000000", "lastPrice": "5", "price24hPcnt": "-0.01"},
                {"symbol": "DUSTUSDT", "turnover24h": "1000000", "lastPrice": "1", "price24hPcnt": "0"},
            ]},
        }


class FakeSession:
    def get(self, *args, **kwargs):
        return FakeResponse()


def load_bot(filename, module_name):
    path = Path(__file__).parents[1] / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("filename", "module_name"),
    [
        ("main-pullback.py", "long_bot_universe_test"),
        ("main-short-pullback.py", "short_bot_universe_test"),
    ],
)
def test_shadow_universe_includes_open_and_blocked_symbols(monkeypatch, filename, module_name):
    bot = load_bot(filename, module_name)
    monkeypatch.setattr(bot, "SESSION", FakeSession())
    monkeypatch.setattr(bot, "get_non_crypto_symbols", lambda: set())
    monkeypatch.setattr(bot, "open_positions", {"BTCUSDT"})
    monkeypatch.setattr(bot, "blocked_symbols", {"ETHUSDT"})
    monkeypatch.setattr(bot, "EXCLUDE_SYMBOLS", set())

    live_symbols = [item["symbol"] for item in bot.scan_universe()]
    shadow_symbols = [item["symbol"] for item in bot.scan_universe(
        include_open_positions=True, include_blocked=True
    )]

    assert live_symbols == ["SOLUSDT"]
    assert shadow_symbols == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]