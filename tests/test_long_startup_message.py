import importlib.util
from pathlib import Path


def load_long_bot():
    path = Path(__file__).parents[1] / "main-pullback.py"
    spec = importlib.util.spec_from_file_location("long_startup_message_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_mean_reversion_startup_message_matches_live_exits_and_pause(monkeypatch):
    bot = load_long_bot()
    monkeypatch.setattr(bot, "MEAN_REV_MODE", True)
    monkeypatch.setattr(bot, "LONG_ENTRIES_ENABLED", False)
    monkeypatch.setattr(bot, "load_live_config", lambda: {
        "target_r_multiple": 1.1,
        "max_hold_bars": 12,
    })

    message = bot.startup_status_message(31.5)

    assert "TP limit reduce-only a 1.1R" in message
    assert "time-stop 12 barre (48h)" in message
    assert "no ratchet/partial TP" in message
    assert "Nuovi ingressi: IN PAUSA" in message
    assert "Ratchet floor fissi" not in message
    assert "Partial 50%" not in message