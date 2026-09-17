from __future__ import annotations

import pytest

from bot.config import Config, ConfigError
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
