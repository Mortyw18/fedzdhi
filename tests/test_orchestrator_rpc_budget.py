"""Orchestrator's indexing throttles: RPC budget priority, rate cap,
calls/min cap, and exponential backoff, all GLOBAL/SHARED across every
concurrent _index_program_loop task (Orchestrator runs one per entry in
INDEXED_PROGRAM_IDS -- currently Raydium AMM v4 and pump.fun's bonding
curve, running concurrently).

That "global" property is load-bearing, not incidental: an earlier
version kept this state per-instance but the actual pause (an
await-sleep) only affected the ONE task that triggered it. With two
programs' loops running concurrently, one backing off did nothing to stop
the OTHER program's loop from continuing to fail on its own schedule at
the same time -- from the logs, that looked exactly like "no backoff at
all, fixed-interval retries every 1-3s," which is exactly what was
reported. test_two_concurrent_loops_share_one_backoff_window below is the
regression test for that specific bug.

Indexing's own RPC failures must also degrade gracefully and NEVER touch
the kill switch -- that's fed exclusively by TokenSafety's verdicts in
evaluate_candidate (see test_evaluate_candidate_rpc_outage.py), the
price-critical path a buy is actually gated on.
"""
from __future__ import annotations

import asyncio
from unittest import mock

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator
from bot.rpc_gateway import RpcMethodDisabled, RpcOutage


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
        self.calls: list[tuple] = []  # (method, max_retries) per call, for asserting the max_retries=1 override
        self.params_log: list = []  # raw params per call, for asserting the exact outgoing request shape
        self.budget = _FakeBudget(usage_pct=0.0)
        self._disabled: set[str] = set()
        self.max_supported_transaction_version = 1

    def call(self, method, params=None, max_retries=None, allow_method_disable=True):
        self.call_count += 1
        self.calls.append((method, max_retries, allow_method_disable))
        self.params_log.append(params)
        raise RpcOutage("simulated outage")

    def is_method_disabled(self, method):
        return method in self._disabled


class _AlwaysSucceedsRpc:
    """Every getTransaction call succeeds with an empty-but-well-formed tx
    -- isolates the rate cap from the failure/backoff path entirely."""

    def __init__(self) -> None:
        self.call_count = 0
        self.budget = _FakeBudget(usage_pct=0.0)
        self._disabled: set[str] = set()
        self.max_supported_transaction_version = 1

    def call(self, method, params=None, max_retries=None, allow_method_disable=True):
        self.call_count += 1
        return {"meta": {"preTokenBalances": [], "postTokenBalances": []}, "slot": 1, "transaction": {"message": {"accountKeys": []}}}

    def is_method_disabled(self, method):
        return method in self._disabled


class _AlwaysMethodDisabledRpc:
    """Every call raises RpcMethodDisabled -- simulates a method RpcGateway
    already detected as permanently rejected (403 / plan-gated)."""

    def __init__(self) -> None:
        self.call_count = 0
        self.budget = _FakeBudget(usage_pct=0.0)
        self.max_supported_transaction_version = 1

    def call(self, method, params=None, max_retries=None, allow_method_disable=True):
        self.call_count += 1
        raise RpcMethodDisabled(f"{method} permanently disabled this run")

    def is_method_disabled(self, method):
        return True


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
        indexing_min_call_interval_s=0.0,  # isolated per-test below where the rate cap itself is what's tested
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


def test_indexing_skipped_when_method_already_disabled(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysMethodDisabledRpc()
    reason = orch._indexing_skip_reason()
    assert reason is not None
    assert "permanently disabled" in reason


def test_indexing_skipped_during_an_active_backoff_window(tmp_path):
    import time

    orch = _build_orchestrator(tmp_path)
    orch._indexing_backoff_until = time.monotonic() + 30.0
    reason = orch._indexing_skip_reason()
    assert reason is not None
    assert "backing off" in reason


def test_indexing_skipped_when_calls_per_minute_cap_reached(tmp_path):
    import time

    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_max_calls_per_minute = 3
    now = time.monotonic()
    orch._indexing_call_timestamps.extend([now, now, now])
    reason = orch._indexing_skip_reason()
    assert reason is not None
    assert "calls/min" in reason


def test_stale_call_timestamps_age_out_of_the_per_minute_window(tmp_path):
    import time

    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_max_calls_per_minute = 3
    orch._indexing_call_timestamps.extend([time.monotonic() - 90.0] * 3)  # 90s ago -- outside the 60s window
    assert orch._indexing_skip_reason() is None


# ----------------------------------------------------------------------
# _index_program_loop: end to end
# ----------------------------------------------------------------------


def test_index_loop_never_touches_kill_switch_on_sustained_rpc_outage(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(20))

    alerts: list[str] = []
    orch.alerter.notify_kill_switch = alerts.append

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert orch.kill_switch.is_halted() is False
    assert alerts == []


def test_index_loop_calls_getTransaction_with_max_retries_one(tmp_path):
    """RpcGateway's own internal retry loop (up to 3 attempts, with sleeps
    between) running on top of indexing's own backoff was what actually
    produced a "fixed ~1-3s interval, no backoff" pattern in the logs --
    not an absence of backoff. Indexing must ask for exactly one attempt
    per call so its own backoff is the only thing controlling retry
    timing."""
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(1))

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert orch.rpc.calls == [("getTransaction", 1, False)]


def test_index_loop_getTransaction_always_requests_versioned_tx_support(tmp_path):
    """Regression coverage for a specific misdiagnosis: on a free RPC tier,
    a versioned transaction fetched with maxSupportedTransactionVersion set
    too LOW for that transaction's actual version errors with JSON-RPC code
    -32015 and a message containing the substring "not supported" -- which
    _looks_like_method_unavailable (rpc_gateway.py) would otherwise treat
    as "this method looks plan-gated" if that code weren't handled upstream
    (see _TRANSACTION_VERSION_NOT_SUPPORTED_CODE), eventually accumulating
    method_disable_threshold sustained rejections and permanently disabling
    getTransaction -- exactly the symptom a silent pool_events funnel
    showed in production, root-caused to Config.rpc_max_supported_transaction_version
    (default 1) being too low for transactions the chain had moved past.
    Confirms indexing's actual outgoing request reads the live value off
    self.rpc rather than a hardcoded literal, so an auto-bump (see
    RpcGateway._post_with_auto_version_bump) takes effect immediately."""
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch.rpc.max_supported_transaction_version = 1
    orch._ws = _FakeWs(_notifications(1))

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert len(orch.rpc.params_log) == 1
    signature, config = orch.rpc.params_log[0]
    assert config["maxSupportedTransactionVersion"] == 1


def test_index_loop_picks_up_a_bumped_max_supported_transaction_version(tmp_path):
    """If RpcGateway's live max_supported_transaction_version has already
    been auto-bumped (e.g. by an earlier -32015 on a different call), the
    NEXT indexing call must use the bumped value immediately, not the
    original config default -- this is what makes the auto-bump actually
    fix every subsequent call, not just retry the one that triggered it."""
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch.rpc.max_supported_transaction_version = 7  # simulates a prior auto-bump
    orch._ws = _FakeWs(_notifications(1))

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    signature, config = orch.rpc.params_log[0]
    assert config["maxSupportedTransactionVersion"] == 7


def test_index_loop_never_lets_getTransaction_be_permanently_disabled(tmp_path):
    """A single spurious 403 permanently disabling getTransaction went
    completely silent (indexing AND event-driven discovery both ride on
    this exact call) for a full production run before allow_method_disable
    existed. Confirms indexing always asks for the exemption."""
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(3))
    orch._indexing_skip_reason = lambda: None  # bypass the backoff gate -- isolates the per-call kwarg, not throttling

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert orch.rpc.calls == [("getTransaction", 1, False)] * 3


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
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    failure_records = [r for r in records if r.getMessage() == "indexing_rpc_failure"]
    assert len(failure_records) == 1
    fields = failure_records[0].fields
    assert fields["method"] == "getTransaction"
    assert "simulated outage" in fields["error"]


def test_index_loop_backs_off_exponentially_from_the_first_failure(tmp_path):
    """No grace threshold -- backoff starts on failure #1 and doubles each
    consecutive failure after that (base=2s here: 2, 4, 8, 16, ...),
    capped. _indexing_skip_reason is bypassed here (always returns None)
    so every one of the 4 notifications actually reaches rpc.call() --
    this isolates the backoff MATH from "does the shared skip gate
    correctly stop further attempts," which is covered separately by
    test_indexing_skipped_during_an_active_backoff_window and
    test_two_concurrent_loops_share_one_backoff_window."""
    import logging

    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_rpc_failure_backoff_base_s = 2.0
    orch.config.indexing_rpc_failure_backoff_max_s = 60.0
    orch.rpc = _AlwaysOutageRpc()
    orch._ws = _FakeWs(_notifications(4))
    orch._indexing_skip_reason = lambda: None

    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    orch.logger.addHandler(handler)

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    assert orch.rpc.call_count == 4
    assert orch._indexing_rpc_consecutive_failures == 4
    backoffs = [r.fields["backoff_s"] for r in records if r.getMessage() == "indexing_backing_off"]
    assert backoffs == [2.0, 4.0, 8.0, 16.0]


def test_two_concurrent_loops_share_one_backoff_window(tmp_path):
    """The regression test for the actual reported bug: two programs'
    indexing loops running concurrently (Orchestrator runs one per entry
    in INDEXED_PROGRAM_IDS, sharing one Orchestrator instance -- and
    therefore one _indexing_backoff_until). Program A's loop failing and
    setting the shared backoff must immediately stop program B's loop from
    attempting further calls too -- not just pause the loop that happened
    to fail, which is what an earlier, per-loop-state version got wrong
    (each loop backed off independently, so one loop's pause never
    stopped the other from continuing to fail on its own schedule)."""
    orch = _build_orchestrator(tmp_path)
    orch.config.indexing_rpc_failure_backoff_base_s = 5.0
    orch.rpc = _AlwaysOutageRpc()

    # Program A's loop: one failing notification sets the shared backoff.
    orch._ws = _FakeWs(_notifications(1))

    async def run_a() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("ProgramA")

    asyncio.run(run_a())
    assert orch.rpc.call_count == 1
    assert orch._indexing_backoff_until > 0.0

    # Program B's loop, a separate task in production but the SAME
    # Orchestrator instance: every one of its 10 notifications must be
    # skipped by the backoff A just set, without ever calling rpc.call().
    calls_before_b = orch.rpc.call_count
    orch._ws = _FakeWs(_notifications(10))

    async def run_b() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("ProgramB")

    asyncio.run(run_b())

    assert orch.rpc.call_count == calls_before_b  # B made ZERO calls
    assert orch._indexing_skipped_count == 10


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


def test_index_loop_stops_calling_a_permanently_disabled_method(tmp_path):
    orch = _build_orchestrator(tmp_path)
    orch.rpc = _AlwaysMethodDisabledRpc()
    orch._ws = _FakeWs(_notifications(10))

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        await orch._index_program_loop("SomeProgram")

    asyncio.run(drive())

    # is_method_disabled() returns True from the very first check, so
    # _indexing_skip_reason() drops every notification before ever calling
    # rpc.call() at all.
    assert orch.rpc.call_count == 0
    assert orch._indexing_skipped_count == 10


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
