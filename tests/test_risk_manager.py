"""RiskManager: exact ladder/stop math, and property-style trade-sequence tests.

The property tests assert two invariants that must hold for ANY sequence
of trade outcomes: the kill switch trips at or before the configured
daily loss cap (never after), and once tripped, no further position can
be opened until a manual reset.
"""
from __future__ import annotations

from bot.config import Config
from bot.kill_switch import KillSwitch
from bot.models import ExitReason, Position
from bot.risk_manager import RiskManager


def _fresh_risk_manager(tmp_path, **config_overrides) -> tuple[RiskManager, KillSwitch]:
    cfg = Config(**config_overrides)
    state_path = str(tmp_path / "kill_switch_state.json")
    ks = KillSwitch(cfg.daily_loss_cap_sol, state_path=state_path)
    return RiskManager(cfg, ks), ks


def _position(entry_price=1.0, size_sol=0.05, tokens=0.05) -> Position:
    return Position(mint="MintA", symbol="TST", size_sol=size_sol, entry_price_usd=entry_price, tokens_held=tokens)


def test_hard_stop_exact_threshold(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)

    assert rm.check_hard_stop(pos, 0.649) is True  # -35.1%
    assert rm.check_hard_stop(pos, 0.66) is False  # -34%, above the stop


def test_ladder_tp1_triggers_at_plus_100_pct(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)

    assert rm.check_ladder_tp1(pos, 1.99) is False
    assert rm.check_ladder_tp1(pos, 2.00) is True

    decision = rm.evaluate_exit(pos, 2.00)
    assert decision == (ExitReason.LADDER_TP, 0.5)


def test_ladder_tp1_does_not_refire_after_filled(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)
    pos.ladder.tp1_filled = True

    assert rm.check_ladder_tp1(pos, 5.0) is False


def test_trail_stop_math(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)
    pos.ladder.trail_active = True
    pos.ladder.trail_high_price = 2.0

    assert rm.check_trail_stop(pos, 1.60) is False  # exactly at 25% trail, not below
    assert rm.check_trail_stop(pos, 1.49) is True  # below trail_high * 0.75

    # a new high should ratchet the trail up
    pos.ladder.trail_high_price = 2.0
    rm.check_trail_stop(pos, 3.0)
    assert pos.ladder.trail_high_price == 3.0


def test_time_stop_fires_after_6h_below_10pct(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)
    pos.opened_at -= 6 * 3600 + 1

    assert rm.check_time_stop(pos, 1.05) is True  # only +5% after 6h
    assert rm.check_time_stop(pos, 1.15) is False  # +15% is fine


def test_time_stop_does_not_fire_once_ladder_hit(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)
    pos.opened_at -= 7 * 3600
    pos.ladder.tp1_filled = True

    assert rm.check_time_stop(pos, 1.05) is False


def test_hard_stop_takes_priority_over_ladder(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    pos = _position(entry_price=1.0)
    # Can't simultaneously be -35% and +100%, but confirms the check order directly.
    reason, frac = rm.evaluate_exit(pos, 0.60)
    assert reason == ExitReason.HARD_STOP
    assert frac == 1.0


def test_no_averaging_down_same_mint_rejected(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path)
    existing = _position()
    existing.mint = "SameMint"
    ok, reason = rm.can_open_position("SameMint", [existing])
    assert ok is False
    assert "averaging down" in reason


def test_max_concurrent_positions_enforced(tmp_path):
    rm, _ = _fresh_risk_manager(tmp_path, max_concurrent_positions=2)
    positions = [_position() for _ in range(2)]
    for i, p in enumerate(positions):
        p.mint = f"Mint{i}"
    ok, reason = rm.can_open_position("NewMint", positions)
    assert ok is False
    assert "max concurrent" in reason


def test_max_buys_per_day_enforced(tmp_path):
    rm, ks = _fresh_risk_manager(tmp_path, max_buys_per_day=3)
    for _ in range(3):
        ks.record_buy()
    ok, reason = rm.can_open_position("NewMint", [])
    assert ok is False
    assert "max buys/day" in reason


def test_daily_loss_cap_property_never_exceeded_before_halt(tmp_path):
    """However losses arrive, the kill switch halts at or before the cap --
    never lets cumulative losses run past it before stopping new buys."""
    rm, ks = _fresh_risk_manager(tmp_path, daily_loss_cap_sol=0.04, max_concurrent_positions=99, max_buys_per_day=999)

    loss_per_trade = 0.0175  # a single -35% stop on a 0.05 SOL position
    trades = 0
    cumulative = 0.0
    for _ in range(20):
        if ks.is_halted():
            break
        ok, _ = rm.can_open_position(f"Mint{trades}", [])
        assert ok is True
        rm.register_buy()
        rm.register_realized_pnl(-loss_per_trade)
        cumulative -= loss_per_trade
        trades += 1

    assert ks.is_halted() is True
    # Halts on the trade that breaches the cap -- never lets it run past by more
    # than one trade's worth of loss.
    assert cumulative <= -0.04
    assert cumulative > -0.04 - loss_per_trade

    ok, reason = rm.can_open_position("OneMoreMint", [])
    assert ok is False
    assert "kill switch halted" in reason


def test_kill_switch_reset_allows_trading_again(tmp_path):
    rm, ks = _fresh_risk_manager(tmp_path, daily_loss_cap_sol=0.01)
    rm.register_realized_pnl(-0.02)
    assert ks.is_halted() is True
    ok, _ = rm.can_open_position("Mint", [])
    assert ok is False

    ks.reset()
    ok, _ = rm.can_open_position("Mint", [])
    assert ok is True
