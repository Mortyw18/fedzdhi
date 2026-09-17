"""ExitMonitor: isolated from every other module by design.

Each position's check runs inside its own try/except boundary -- one
position's Jupiter call throwing an exception must never stop the other
positions from being checked, and must never kill the monitor loop
itself. This is deliberately the most paranoid module in the codebase:
if the laptop is running this and nothing else, stops still have to work.

Prices used here are always Jupiter's *executable* quote for the
position's actual remaining size, never a chart/ticker price -- a chart
price is what a keyboard-clicker sees, not what we could actually get
filled at with our size, in this pool, right now.

`caffeinate` (see scripts/run_paper.sh and README) is mandatory on macOS:
if the laptop sleeps, this loop stops running, and stops silently stop
existing.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable, Optional

from bot.execution_engine import ExecutionEngine, ExecutionFailed, LAMPORTS_PER_SOL
from bot.jupiter_client import JupiterClient, SOL_MINT
from bot.models import ExitReason, Position, PositionStatus, now_ts
from bot.risk_manager import RiskManager

TokenDecimalsLookup = Callable[[str], int]
OpenPositionsProvider = Callable[[], list[Position]]


class ExitMonitor:
    def __init__(
        self,
        jupiter: JupiterClient,
        risk_manager: RiskManager,
        execution: ExecutionEngine,
        accounting,  # bot.accounting.Accounting, kept loosely typed to avoid a hard import cycle in tests
        alerter,     # bot.alerter.Alerter
        token_decimals_lookup: TokenDecimalsLookup,
        poll_interval_s: float = 3.0,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.jupiter = jupiter
        self.risk_manager = risk_manager
        self.execution = execution
        self.accounting = accounting
        self.alerter = alerter
        self.token_decimals_lookup = token_decimals_lookup
        self.poll_interval_s = poll_interval_s
        self.logger = logger or logging.getLogger("memebot.exit_monitor")

    def get_executable_price(self, position: Position) -> Optional[float]:
        remaining_tokens = position.tokens_held * position.remaining_fraction
        if remaining_tokens <= 0:
            return None
        decimals = self.token_decimals_lookup(position.mint)
        amount_raw = int(remaining_tokens * (10 ** decimals))
        if amount_raw <= 0:
            return None
        try:
            quote = self.jupiter.quote(position.mint, SOL_MINT, amount_raw, slippage_bps=100)
        except Exception as exc:  # noqa: BLE001 - a bad quote must not crash the monitor
            self.logger.warning("could not price position %s (%s): %s", position.id, position.mint, exc)
            return None
        if quote.in_amount <= 0:
            return None
        return (quote.out_amount / LAMPORTS_PER_SOL) / remaining_tokens

    def check_position(self, position: Position) -> None:
        """One position, one error boundary."""
        try:
            self._check_position_inner(position)
        except Exception:  # noqa: BLE001 - this is the isolation boundary the module exists for
            self.logger.exception("exit_monitor: unhandled error checking position %s", position.id)

    def _check_position_inner(self, position: Position) -> None:
        if position.status != PositionStatus.OPEN:
            return

        price = self.get_executable_price(position)
        if price is None:
            return

        decision = self.risk_manager.evaluate_exit(position, price)
        if decision is None:
            return

        reason, fraction_of_remaining = decision
        decimals = self.token_decimals_lookup(position.mint)
        emergency = reason == ExitReason.HARD_STOP
        fraction_of_original_sold = fraction_of_remaining * position.remaining_fraction

        try:
            fill = self.execution.sell(position, fraction_of_remaining, reason, decimals, emergency=emergency)
        except ExecutionFailed as exc:
            newly_tripped = self.risk_manager.register_execution_failure()
            self.logger.error("exit execution failed for %s: %s", position.id, exc)
            self.alerter.notify(f"EXIT FAILED {position.symbol} ({reason.value}): {exc}")
            if newly_tripped:
                self.alerter.notify_kill_switch(self.risk_manager.kill_switch.halt_reason())
            return

        self.risk_manager.register_execution_success()
        self.accounting.record_fill(fill)

        cost_basis_this_fill = position.size_sol * fraction_of_original_sold
        pnl_this_fill = (fill.size_sol - fill.fee_sol) - cost_basis_this_fill
        position.realized_pnl_sol += pnl_this_fill

        self.risk_manager.apply_ladder_fill(position, reason, fraction_of_remaining)
        self.alerter.notify_exit(position, fill, reason, pnl_this_fill)

        if position.status == PositionStatus.CLOSED:
            position.closed_at = now_ts()
            self.accounting.record_position_closed(position)
            newly_tripped = self.risk_manager.register_realized_pnl(position.realized_pnl_sol)
            if newly_tripped:
                self.alerter.notify_kill_switch(self.risk_manager.kill_switch.halt_reason())

    async def run_forever(self, open_positions: OpenPositionsProvider, stop_event: Optional[asyncio.Event] = None) -> None:
        loop = asyncio.get_running_loop()
        while stop_event is None or not stop_event.is_set():
            positions = [p for p in open_positions() if p.status == PositionStatus.OPEN]
            for position in positions:
                await loop.run_in_executor(None, self.check_position, position)
            await asyncio.sleep(self.poll_interval_s)
