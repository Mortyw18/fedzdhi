"""Orchestrator: the indexing pipeline must be the first thing to back off
when RPC budget is tight (it's background learning, not a trade waiting on
a price), and repeated RpcOutage failures from that pipeline must alert
the operator ONCE, not on every dropped notification.
"""
from __future__ import annotations

import asyncio

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator
from bot.rpc_gateway import RpcOutage


class _FakeBudget:
    def __init__(self, usage_pct: float = 0.0) -> None:
        self.usage_pct = usage_pct

    def current_usage_pct(self) -> float:
        return self.usage_pct


class _AlwaysOutageRpc:
    """Every getTransaction call raises RpcOutage -- simulates a sustained
    RPC failure the way a real death spiral would."""

    def __init__(self) -> None:
        self.call_count = 0
        self.budget = _FakeBudget(usage_pct=0.0)

    def call(self, method, params=None):
        self.call_count += 1
        raise RpcOutage("simulated outage")


class _FakeWs:
    def __init__(self, notifications: list[dict]) -> None:
        self._notifications = notifications

    async def logs_subscribe(self, program_id):
        for n in self._notifications:
            yield n


def _build_orchestrator(tmp_path) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        indexing_max_rpc_budget_pct=0.50,
    )
    cfg.validate()
    return Orchestrator(cfg)


def _notifications(n: int) -> list[dict]:
    return [{"value": {"signature": f"sig{i}"}} for i in range(n)]


# ----------------------------------------------------------------------
# _indexing_skip_reason: the pure, synchronous throttle decision
# ----------------------------------------------------------------------


def test_indexing_not_skipped_when_healthy(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc.budget = _FakeBudget(usage_pct=0.1)
    assert orch._indexing_skip_reason() is None


def test_indexing_skipped_when_kill_switch_halted(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.kill_switch.set_rpc_outage(True)
    reason = orch._indexing_skip_reason()
    assert reason is not None
    assert "kill switch halted" in reason


def test_indexing_skipped_when_over_its_own_rpc_budget_ceiling(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc.budget = _FakeBudget(usage_pct=0.75)  # above the 0.50 ceiling
    reason = orch._indexing_skip_reason()
    assert reason is not None
    assert "budget" in reason


def test_indexing_not_skipped_below_its_ceiling_even_if_nonzero(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc.budget = _FakeBudget(usage_pct=0.49)
    assert orch._indexing_skip_reason() is None


# ----------------------------------------------------------------------
# _index_program_loop: latched alert + self-throttle end to end
# ----------------------------------------------------------------------


def test_index_loop_alerts_once_despite_many_failing_notifications(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(20))

    alerts: list[str] = []
    orch.alerter.notify_kill_switch = alerts.append

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    # After the very first RpcOutage, the kill switch is halted, and every
    # notification after that is skipped by _indexing_skip_reason() before
    # ever reaching rpc.call again -- so only one call is attempted, and
    # only one alert is ever sent, despite 20 notifications arriving.
    assert orch.rpc.call_count == 1
    assert len(alerts) == 1


def test_index_loop_skips_all_notifications_when_budget_is_tight(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch.rpc.budget = _FakeBudget(usage_pct=0.99)  # already over the ceiling
    orch._ws = _FakeWs(_notifications(10))

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert orch.rpc.call_count == 0  # never even attempted -- budget-priority backoff
    assert orch._indexing_skipped_count == 10
