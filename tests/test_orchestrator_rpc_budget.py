"""Orchestrator: the indexing pipeline must be the first thing to back off
when RPC budget is tight (it's background learning, not a trade waiting on
a price). Its own RpcOutage failures must degrade gracefully (log + a
local self-throttle) and must NEVER touch the kill switch -- an overnight
run tripped the kill switch from this exact loop within ~18s of startup
while a plain curl to the same endpoint worked fine. The kill switch's
RPC-outage counter is now fed exclusively by TokenSafety's verdicts in
evaluate_candidate (see test_evaluate_candidate_rpc_outage.py), the
price-critical path a buy is actually gated on.
"""
from __future__ import annotations

import asyncio
from unittest import mock

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


class _AlwaysSucceedsRpc:
    """Every getTransaction call succeeds with an empty-but-well-formed tx
    -- isolates the rate cap from the failure/backoff path entirely."""

    def __init__(self) -> None:
        self.call_count = 0
        self.budget = _FakeBudget(usage_pct=0.0)

    def call(self, method, params=None):
        self.call_count += 1
        return {"meta": {"preTokenBalances": [], "postTokenBalances": []}, "slot": 1, "transaction": {"message": {"accountKeys": []}}}


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
    for _ in range(orch.kill_switch.max_consecutive_rpc_outages):
        orch.kill_switch.set_rpc_outage(True)  # sustained -- see KillSwitch's own tests for the single-blip case
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


def test_index_loop_never_touches_kill_switch_on_sustained_rpc_outage(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(20))

    alerts: list[str] = []
    orch.alerter.notify_kill_switch = alerts.append

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        with mock.patch("bot.orchestrator.asyncio.sleep", new=mock.AsyncMock()):
            await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    # Every single notification is attempted (no skip kicks in, since the
    # kill switch never halts and the fake budget stays at 0) -- indexing's
    # own RPC failures degrade locally and never alert or halt anything.
    assert orch.rpc.call_count == 20
    assert orch.kill_switch.is_halted() is False
    assert alerts == []


def test_index_loop_logs_method_and_error_on_each_rpc_failure(tmp_path):
    import logging

    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(1))

    # memebot's logger has propagate=False (see logging_setup.py), so a
    # plain handler attached directly to it is the reliable way to capture
    # records in a test -- caplog's root-logger handler never sees them.
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    orch.logger.addHandler(handler)

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        with mock.patch("bot.orchestrator.asyncio.sleep", new=mock.AsyncMock()):
            await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    failure_records = [r for r in records if r.getMessage() == "indexing_rpc_failure"]
    assert len(failure_records) == 1
    fields = failure_records[0].fields
    assert fields["method"] == "getTransaction"
    assert "simulated outage" in fields["error"]


def test_index_loop_backs_off_exponentially_after_threshold_then_self_heals(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_max_consecutive_rpc_failures = 3
    orch.config.indexing_min_call_interval_s = 0.0  # isolate backoff from the separate rate-cap test below
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(6))  # exactly two full thresholds' worth

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        with mock.patch("bot.orchestrator.asyncio.sleep", new=_fake_sleep):
            await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    # Every notification is still attempted (degrading gracefully means
    # pausing between bursts, not giving up) -- but every 3rd consecutive
    # failure triggers a backoff sleep and resets the failure counter, so a
    # sustained outage self-throttles instead of hammering the endpoint.
    # The backoff itself grows each time the threshold is crossed again
    # without an intervening success (base=5s -> 5, 10, 20, ...) instead of
    # retrying on the same fixed schedule forever.
    assert orch.rpc.call_count == 6
    assert sleep_calls == [5.0, 10.0]
    assert orch._indexing_rpc_consecutive_failures == 0
    assert orch._indexing_rpc_backoff_level == 2


def test_index_loop_backoff_level_resets_on_a_success(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_max_consecutive_rpc_failures = 2
    orch.config.indexing_min_call_interval_s = 0.0

    calls = {"n": 0}

    class _FailTwiceThenSucceed:
        budget = _FakeBudget(usage_pct=0.0)

        def call(self, method, params=None):
            calls["n"] += 1
            if calls["n"] <= 2:
                raise RpcOutage("simulated outage")
            return {"meta": {"preTokenBalances": [], "postTokenBalances": []}, "slot": 1, "transaction": {"message": {"accountKeys": []}}}

    orch.rpc = _FailTwiceThenSucceed()
    # 2 failures (crosses the threshold=2, backoff_level -> 1) then 2 more
    # notifications that succeed and reset the backoff level back to 0.
    orch._ws = _FakeWs(_notifications(4))

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        with mock.patch("bot.orchestrator.asyncio.sleep", new=_fake_sleep):
            await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert sleep_calls == [orch.config.indexing_rpc_failure_backoff_base_s]  # only the one, level-1 backoff
    assert orch._indexing_rpc_backoff_level == 0  # reset by the successes that followed


def test_index_loop_rate_caps_successive_calls_regardless_of_failure_state(tmp_path):
    """The rate cap is a hard floor on call spacing, independent of whether
    calls are succeeding or failing -- this isolates it with an RPC that
    always succeeds, so nothing here is about the backoff path at all."""
    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_min_call_interval_s = 0.3
    orch.rpc = _AlwaysSucceedsRpc()
    orch._ws = _FakeWs(_notifications(3))

    sleep_calls: list[float] = []

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        with mock.patch("bot.orchestrator.asyncio.sleep", new=_fake_sleep):
            await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert orch.rpc.call_count == 3
    # The first call never waits (no prior call this run), but back-to-back
    # notifications processed in the same tight loop each have to wait
    # nearly a full interval -- this is exactly what stops "several
    # notifications a second" from becoming "several RPC calls a second."
    assert len(sleep_calls) == 2
    assert all(0 < s <= orch.config.indexing_min_call_interval_s for s in sleep_calls)


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
