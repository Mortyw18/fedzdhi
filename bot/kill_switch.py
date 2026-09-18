"""KillSwitch: halts new buys. Manual reset only.

Trips on any of three conditions:
  - daily realized loss >= daily_loss_cap_sol
  - too many consecutive execution failures in a row (failed txs cost fees;
    a streak of them means something is structurally wrong, not bad luck)
  - a SUSTAINED RPC outage: max_consecutive_rpc_outages consecutive failures
    (default 3), never the first one. A single cold-start blip (DNS/TLS
    still warming up, one transient timeout) is not an outage -- an
    overnight run tripped this within ~2 seconds of startup while the same
    endpoint answered a plain curl just fine, which is exactly the false
    positive this threshold exists to prevent. Call set_rpc_outage(False)
    on any RPC success to reset the streak; only a real, sustained failure
    should ever halt anything.

    set_rpc_outage is fed ONLY by Orchestrator.evaluate_candidate, from
    TokenSafety's verdict -- the price-critical path an actual buy is gated
    on. A second overnight run tripped this from InsiderRadar's background
    on-chain indexing instead (a busy AMM program can fire many logsSubscribe
    notifications a second, each one a getTransaction call, so 3 consecutive
    failures there can happen in seconds even against a healthy endpoint).
    Indexing is best-effort learning, not a trade waiting on a price, so its
    RPC failures now degrade locally (log + a short self-throttle, see
    Orchestrator._index_program_loop) and never call this method at all.

State is persisted to disk so a halt survives a restart -- this is
deliberate, not an oversight: a halt exists specifically so a crash-loop
or repeated restart can't silently keep trading through a real problem.
`--reset-kill-switch` clears it from a separate process invocation
without the bot having to be running. Orchestrator logs loudly (and
alerts) at startup if it finds the switch already halted, so "persists
silently" is a logging gap to close, not a reason to auto-clear it.
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
    consecutive_rpc_outages: int = 0
    halted: bool = False
    halt_reason: str = ""
    rpc_outage: bool = False


class KillSwitch:
    def __init__(
        self,
        daily_loss_cap_sol: float,
        max_consecutive_failures: int = 3,
        max_consecutive_rpc_outages: int = 3,
        state_path: str = "data/kill_switch_state.json",
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.daily_loss_cap_sol = daily_loss_cap_sol
        self.max_consecutive_failures = max_consecutive_failures
        self.max_consecutive_rpc_outages = max_consecutive_rpc_outages
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

    def record_pnl(self, pnl_sol: float) -> bool:
        """Returns True only if THIS call newly tripped the halt (a state
        change), False if it didn't trip anything or the switch was already
        halted. Callers use this to decide whether to alert -- see
        Orchestrator's RpcOutage handler for why that distinction matters:
        without it, every subsequent event re-announces a halt that already
        happened, which is exactly the alert spam this return value exists
        to prevent."""
        self._roll_day_if_needed()
        self.state.daily_pnl_sol += pnl_sol
        newly_tripped = False
        if self.state.daily_pnl_sol <= -abs(self.daily_loss_cap_sol):
            newly_tripped = self._halt(
                f"daily loss cap breached: {self.state.daily_pnl_sol:.4f} SOL "
                f"<= -{self.daily_loss_cap_sol:.4f} SOL"
            )
        self._save()
        return newly_tripped

    def record_buy(self) -> None:
        self._roll_day_if_needed()
        self.state.buys_today += 1
        self._save()

    def record_execution_failure(self) -> bool:
        """Returns True only on the transition into halted -- see record_pnl."""
        self.state.consecutive_failures += 1
        newly_tripped = False
        if self.state.consecutive_failures >= self.max_consecutive_failures:
            newly_tripped = self._halt(f"{self.state.consecutive_failures} consecutive execution failures")
        self._save()
        return newly_tripped

    def record_execution_success(self) -> None:
        self.state.consecutive_failures = 0
        self._save()

    def set_rpc_outage(self, active: bool) -> bool:
        """Requires max_consecutive_rpc_outages consecutive True calls
        before actually halting -- a single blip must never trip this on
        its own. Call with active=False on any RPC success to reset the
        streak. Returns True only on the transition into halted -- see
        record_pnl."""
        if not active:
            self.state.rpc_outage = False
            self.state.consecutive_rpc_outages = 0
            self._save()
            return False

        self.state.rpc_outage = True
        self.state.consecutive_rpc_outages += 1
        newly_tripped = False
        if self.state.consecutive_rpc_outages >= self.max_consecutive_rpc_outages:
            newly_tripped = self._halt(
                f"RPC outage: {self.state.consecutive_rpc_outages} consecutive failures, "
                "can't see prices, can't safely manage exits"
            )
        self._save()
        return newly_tripped

    def _halt(self, reason: str) -> bool:
        """Returns True if this call is what newly tripped the halt."""
        newly_tripped = not self.state.halted
        if newly_tripped:
            self.logger.error("kill_switch_tripped", extra={"fields": {"reason": reason}})
        self.state.halted = True
        self.state.halt_reason = reason
        return newly_tripped

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

    @property
    def consecutive_rpc_outages(self) -> int:
        return self.state.consecutive_rpc_outages

    def reset(self) -> None:
        """Manual reset. Clears the halt and failure streaks; daily PnL history
        is left intact since it's a factual record of what happened today."""
        self.state.halted = False
        self.state.halt_reason = ""
        self.state.consecutive_failures = 0
        self.state.consecutive_rpc_outages = 0
        self.state.rpc_outage = False
        self._save()
        self.logger.info("kill_switch_reset")
