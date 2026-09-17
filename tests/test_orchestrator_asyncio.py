"""Regression test for the "Task got a Future attached to a different loop"
crash: Orchestrator() is constructed synchronously in cli.main(), *before*
asyncio.run() creates the loop that actually drives the bot. Any asyncio
synchronization primitive built at construction time binds to whatever
implicit loop exists at that moment, which is not the loop it's later
awaited from -- so Orchestrator must not construct one until it's
actually running inside the target loop (see the comment on
Orchestrator.__init__'s self.stop_event and the top of Orchestrator.run()).
"""
from __future__ import annotations

import asyncio

import requests

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator


class _NoNetworkSession:
    """Fails every call instantly -- SignalEngine's own error handling
    (broadened for pump.fun, pre-existing for DexScreener) turns this into
    an empty candidate list rather than a raised exception, so the
    background polling tasks started by run() never touch the network."""

    def get(self, *args, **kwargs):
        raise requests.ConnectionError("network disabled in this test")


def _build_orchestrator(tmp_path) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",  # no helius_ws_url -> no WS indexing tasks
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        dexscreener_poll_interval_s=0.01,
    )
    cfg.validate()

    # Constructed here, synchronously, with no event loop running -- this is
    # exactly the call pattern bot/cli.py uses (Orchestrator(cfg) happens
    # before asyncio.run(orchestrator.run())), and exactly the pattern that
    # exposed the original bug.
    orch = Orchestrator(cfg)
    orch.signal_engine.session = _NoNetworkSession()
    return orch


def test_orchestrator_constructed_outside_loop_does_not_eagerly_create_stop_event(tmp_path):
    orch = _build_orchestrator(tmp_path)
    assert orch.stop_event is None


def test_orchestrator_run_and_stop_does_not_raise_loop_mismatch(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def drive() -> None:
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)  # let background loops spin up at least once
        assert orch.stop_event is not None  # now created, inside the running loop
        await orch._request_stop()
        await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(drive())  # must not raise RuntimeError: Task got a Future attached to a different loop


def test_orchestrator_run_works_when_a_prior_loop_already_ran_and_closed(tmp_path):
    """The specific failure mode reported: an Event bound to an earlier,
    now-closed loop breaks when awaited from a fresh asyncio.run() loop.
    Running the orchestrator twice in a row (two separate asyncio.run()
    calls, as would happen across two CLI invocations in the same
    process) must work both times."""
    orch = _build_orchestrator(tmp_path)

    async def drive(o: Orchestrator) -> None:
        run_task = asyncio.create_task(o.run())
        await asyncio.sleep(0.02)
        await o._request_stop()
        await asyncio.wait_for(run_task, timeout=5.0)

    # First run on its own loop.
    asyncio.run(drive(orch))

    # A second, independent Orchestrator + a second, independent loop --
    # simulates the loop asyncio.run() tears down and recreates each call.
    orch2 = _build_orchestrator(tmp_path)
    asyncio.run(drive(orch2))
