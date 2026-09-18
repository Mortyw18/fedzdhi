"""RiskManager: the constitution. Enforced in code, not suggestions.

Every rule from the spec lives here as an actual gate a trade must pass,
not a comment saying it should. In particular: there is no method
anywhere in this class (or in ExecutionEngine) that adds to an existing
position. Averaging down and martingale sizing aren't discouraged, they
are architecturally absent.
"""
from __future__ import annotations

import logging
from typing import Optional

from bot.config import Config
from bot.kill_switch import KillSwitch
from bot.models import ExitReason, Position, PositionStatus


class RiskManager:
    def __init__(self, config: Config, kill_switch: KillSwitch, logger: Optional[logging.Logger] = None) -> None:
        self.config = config
        self.kill_switch = kill_switch
        self.logger = logger or logging.getLogger("memebot.risk_manager")

    # ------------------------------------------------------------------
    # entry gating
    # ------------------------------------------------------------------

    def position_size_sol(self) -> float:
        return self.config.position_size_sol

    def can_open_position(self, mint: str, open_positions: list[Position]) -> tuple[bool, str]:
        if self.kill_switch.is_halted():
            return False, f"kill switch halted: {self.kill_switch.halt_reason()}"

        open_for_this_mint = [p for p in open_positions if p.mint == mint and p.status == PositionStatus.OPEN]
        if open_for_this_mint:
            return False, "already holding this mint -- no averaging down, ever"

        open_count = sum(1 for p in open_positions if p.status == PositionStatus.OPEN)
        if open_count >= self.config.max_concurrent_positions:
            return False, f"max concurrent positions reached ({self.config.max_concurrent_positions})"

        if self.kill_switch.buys_today >= self.config.max_buys_per_day:
            return False, f"max buys/day reached ({self.config.max_buys_per_day})"

        return True, "ok"

    # ------------------------------------------------------------------
    # exit logic -- hard stop > ladder take-profit > trailing stop > time stop
    # ------------------------------------------------------------------

    @staticmethod
    def pct_change(entry_price: float, current_price: float) -> float:
        if entry_price <= 0:
            return 0.0
        return (current_price - entry_price) / entry_price

    def check_hard_stop(self, position: Position, current_price_usd: float) -> bool:
        return self.pct_change(position.entry_price_usd, current_price_usd) <= self.config.hard_stop_pct

    def check_ladder_tp1(self, position: Position, current_price_usd: float) -> bool:
        if position.ladder.tp1_filled:
            return False
        return self.pct_change(position.entry_price_usd, current_price_usd) >= self.config.ladder_tp1_trigger_pct

    def check_trail_stop(self, position: Position, current_price_usd: float) -> bool:
        if not position.ladder.trail_active:
            return False
        if current_price_usd > position.ladder.trail_high_price:
            position.ladder.trail_high_price = current_price_usd
        trigger_price = position.ladder.trail_high_price * (1 - self.config.trail_pct)
        return current_price_usd <= trigger_price

    def check_time_stop(self, position: Position, current_price_usd: float) -> bool:
        if position.ladder.tp1_filled:
            return False  # already took profit, time stop no longer applies
        if position.age_s < self.config.time_stop_minutes * 60.0:
            return False
        return self.pct_change(position.entry_price_usd, current_price_usd) < self.config.time_stop_min_gain_pct

    def evaluate_exit(self, position: Position, current_price_usd: float) -> Optional[tuple[ExitReason, float]]:
        """Returns (reason, fraction_of_CURRENT_holdings_to_sell) or None.

        Checked in priority order: a hard stop always wins even if a ladder
        trigger would also fire on the same tick.
        """
        if self.check_hard_stop(position, current_price_usd):
            return ExitReason.HARD_STOP, 1.0

        if self.check_ladder_tp1(position, current_price_usd):
            return ExitReason.LADDER_TP, self.config.ladder_tp1_sell_fraction

        if self.check_trail_stop(position, current_price_usd):
            return ExitReason.TRAIL_STOP, 1.0

        if self.check_time_stop(position, current_price_usd):
            return ExitReason.TIME_STOP, 1.0

        return None

    def apply_ladder_fill(self, position: Position, reason: ExitReason, fraction_sold: float) -> None:
        """Mutate position state after a partial/full exit fill is recorded."""
        position.remaining_fraction = max(0.0, position.remaining_fraction - fraction_sold * position.remaining_fraction)
        if reason == ExitReason.LADDER_TP:
            position.ladder.tp1_filled = True
            position.ladder.trail_active = True
            position.ladder.trail_high_price = position.entry_price_usd  # will be updated on next price tick
        if position.remaining_fraction <= 1e-6:
            position.status = PositionStatus.CLOSED

    # ------------------------------------------------------------------
    # daily-loss cap bookkeeping (kill switch owns the halt itself)
    # ------------------------------------------------------------------

    def register_realized_pnl(self, pnl_sol: float) -> bool:
        """Returns True only if this call newly tripped the kill switch
        (the daily loss cap), so callers know whether to alert. See
        KillSwitch.record_pnl."""
        return self.kill_switch.record_pnl(pnl_sol)

    def register_buy(self) -> None:
        self.kill_switch.record_buy()

    def register_execution_failure(self) -> bool:
        """Returns True only if this call newly tripped the kill switch
        (too many consecutive failures). See KillSwitch.record_execution_failure."""
        return self.kill_switch.record_execution_failure()

    def register_execution_success(self) -> None:
        self.kill_switch.record_execution_success()
