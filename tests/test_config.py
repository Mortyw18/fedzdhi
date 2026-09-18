from __future__ import annotations

import os

import pytest

from bot.config import Config, ConfigError, load_config_from_env
from bot.models import Mode


def test_default_config_is_valid():
    Config().validate()  # must not raise


def test_default_max_buys_per_day_is_three():
    assert Config().max_buys_per_day == 3


def test_position_over_hard_ceiling_rejected():
    with pytest.raises(ConfigError):
        Config(position_size_sol=0.5).validate()


def test_heavy_sizing_without_flag_rejected():
    with pytest.raises(ConfigError):
        Config(position_size_sol=0.10).validate()


def test_heavy_sizing_with_flag_allowed():
    Config(position_size_sol=0.10, allow_heavy_sizing=True, max_concurrent_positions=2).validate()


def test_position_over_50pct_bankroll_rejected():
    with pytest.raises(ConfigError):
        Config(bankroll_sol=0.2, position_size_sol=0.10, allow_heavy_sizing=True, max_concurrent_positions=1, max_position_fraction_of_bankroll=0.25).validate()


def test_daily_loss_cap_over_half_bankroll_rejected():
    with pytest.raises(ConfigError):
        Config(bankroll_sol=0.2, daily_loss_cap_sol=0.15).validate()


def test_zero_daily_loss_cap_rejected():
    with pytest.raises(ConfigError):
        Config(daily_loss_cap_sol=0.0).validate()


def test_positive_hard_stop_rejected():
    with pytest.raises(ConfigError):
        Config(hard_stop_pct=0.10).validate()


def test_concurrent_positions_times_size_over_bankroll_rejected():
    with pytest.raises(ConfigError):
        Config(bankroll_sol=0.2, position_size_sol=0.05, max_concurrent_positions=10).validate()


def test_error_message_explains_the_problem():
    try:
        Config(position_size_sol=0.10).validate()
        assert False, "should have raised"
    except ConfigError as exc:
        assert "allow-heavy-sizing" in str(exc)


def test_ruin_table_contains_key_numbers():
    table = Config(bankroll_sol=0.2).ruin_table()
    assert "0.200" in table
    assert "0.050" in table
    assert "RUIN TABLE" in table


def test_observe_only_requires_rpc_configured():
    with pytest.raises(ConfigError):
        Config(observe_only=True).validate()


def test_observe_only_with_helius_key_is_valid():
    Config(observe_only=True, helius_api_key="test-key").validate()


def test_observe_only_with_rpc_url_is_valid():
    Config(observe_only=True, helius_rpc_url="https://example.invalid/rpc").validate()


def test_observe_only_cannot_combine_with_live():
    with pytest.raises(ConfigError):
        Config(mode=Mode.LIVE, observe_only=True, helius_api_key="test-key").validate()


# ----------------------------------------------------------------------
# ENABLE_PUMPFUN env var -- previously enable_pumpfun_source existed as a
# Config field but had no actual way to be set from .env, despite the
# pump.fun 530 circuit breaker/config-flag being documented as the fix.
# ----------------------------------------------------------------------


def _clear_env(monkeypatch, *names):
    for name in names:
        monkeypatch.delenv(name, raising=False)


def test_enable_pumpfun_defaults_true_when_unset(tmp_path, monkeypatch):
    _clear_env(monkeypatch, "ENABLE_PUMPFUN")
    cfg = load_config_from_env(str(tmp_path / "nonexistent.env"))
    assert cfg.enable_pumpfun_source is True


@pytest.mark.parametrize("value", ["false", "False", "FALSE", "0", "no", "No"])
def test_enable_pumpfun_false_values_disable_it(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ENABLE_PUMPFUN", value)
    cfg = load_config_from_env(str(tmp_path / "nonexistent.env"))
    assert cfg.enable_pumpfun_source is False


@pytest.mark.parametrize("value", ["true", "True", "1", "yes"])
def test_enable_pumpfun_true_like_values_keep_it_enabled(tmp_path, monkeypatch, value):
    monkeypatch.setenv("ENABLE_PUMPFUN", value)
    cfg = load_config_from_env(str(tmp_path / "nonexistent.env"))
    assert cfg.enable_pumpfun_source is True


def test_enable_pumpfun_from_dotenv_file(tmp_path, monkeypatch):
    _clear_env(monkeypatch, "ENABLE_PUMPFUN")
    env_file = tmp_path / ".env"
    env_file.write_text("ENABLE_PUMPFUN=false\n")
    cfg = load_config_from_env(str(env_file))
    assert cfg.enable_pumpfun_source is False
