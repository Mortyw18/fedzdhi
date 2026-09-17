"""KillSwitch: halts new buys. Manual reset only.

Trips on any of three conditions:
  - daily realized loss >= daily_loss_cap_sol
  - too many consecutive execution failures in a row (failed txs cost fees;
    a streak of them means something is structurally wrong, not bad luck)
  - an RPC outage (if we can't see prices, we can't safely manage exits,
    so we stop opening new positions -- ExitMonitor keeps trying on
    existing ones independently)

State is persisted to disk so a halt survives a restart, and so
`--reset-kill-switch` can clear it from a separate process invocation
without the bot having to be running.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional


def _today_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@dataclass
class _KillSwitchState:
    day: str = field(default_factory=_today_utc)
    daily_pnl_sol: float = 0.0
    buys_today: int = 0
    consecutive_failures: int = 0
    halted: bool = False
    halt_reason: str = ""
    rpc_outage: bool = False


class KillSwitch:
    def __init__(
        self,
        daily_loss_cap_sol: float,
        max_consecutive_failures: int = 3,
        state_path: str = "data/kill_switch_state.json",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.daily_loss_cap_sol = daily_loss_cap_sol
        self.max_consecutive_failures = max_consecutive_failures
        self.state_path = state_path
        self.logger = logger or logging.getLogger("memebot.kill_switch")
        self.state = self._load()
        self._roll_day_if_needed()

    def _load(self) -> _KillSwitchState:
        if os.path.exists(self.state_path):
            try:
                with open(self.state_path) as f:
                    data = json.load(f)
                return _KillSwitchState(**data)
            except (json.JSONDecodeError, TypeError, OSError):
                self.logger.warning("corrupt kill switch state at %s, starting fresh", self.state_path)
        return _KillSwitchState()

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
        with open(self.state_path, "w") as f:
            json.dump(asdict(self.state), f, indent=2)

    def _roll_day_if_needed(self) -> None:
        today = _today_utc()
        if self.state.day != today:
            self.state.day = today
            self.state.daily_pnl_sol = 0.0
            self.state.buys_today = 0
            # A halt from a prior day (loss cap, failures) does NOT auto-clear;
            # only an explicit reset() does. RPC outage also persists.
            self._save()

    # ------------------------------------------------------------------
    # recording
    # ------------------------------------------------------------------

    def record_pnl(self, pnl_sol: float) -> None:
        self._roll_day_if_needed()
        self.state.daily_pnl_sol += pnl_sol
        if self.state.daily_pnl_sol <= -abs(self.daily_loss_cap_sol):
            self._halt(
                f"daily loss cap breached: {self.state.daily_pnl_sol:.4f} SOL "
                f"<= -{self.daily_loss_cap_sol:.4f} SOL"
            )
        self._save()

    def record_buy(self) -> None:
        self._roll_day_if_needed()
        self.state.buys_today += 1
        self._save()

    def record_execution_failure(self) -> None:
        self.state.consecutive_failures += 1
        if self.state.consecutive_failures >= self.max_consecutive_failures:
            self._halt(f"{self.state.consecutive_failures} consecutive execution failures")
        self._save()

    def record_execution_success(self) -> None:
        self.state.consecutive_failures = 0
        self._save()

    def set_rpc_outage(self, active: bool) -> None:
        self.state.rpc_outage = active
        if active:
            self._halt("RPC outage: can't see prices, can't safely manage exits")
        self._save()

    def _halt(self, reason: str) -> None:
        if not self.state.halted:
            self.logger.error("kill_switch_tripped", extra={"fields": {"reason": reason}})
        self.state.halted = True
        self.state.halt_reason = reason

    # ------------------------------------------------------------------
    # queries / manual reset
    # ------------------------------------------------------------------

    def is_halted(self) -> bool:
        self._roll_day_if_needed()
        return self.state.halted

    def halt_reason(self) -> str:
        return self.state.halt_reason

    @property
    def buys_today(self) -> int:
        self._roll_day_if_needed()
        return self.state.buys_today

    @property
    def daily_pnl_sol(self) -> float:
        self._roll_day_if_needed()
        return self.state.daily_pnl_sol

    def reset(self) -> None:
        """Manual reset. Clears the halt and failure streak; daily PnL history
        is left intact since it's a factual record of what happened today."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.consecutive_failures = 0
        self.state.rpc_outage = False
        self._save()
        self.logger.info("kill_switch_reset")
