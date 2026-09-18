"""Ctrl+C must shut down cleanly: cancel every task and let it finish,
not let KeyboardInterrupt skip past run()'s cleanup and leave tasks for
asyncio.run() to tear down mid-flight (the "Task was destroyed but it is
pending" spam).
"""
from __future__ import annotations

import asyncio
import os
import signal

import requests

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator


class _NoNetworkSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("network disabled in this test")


def _build_orchestrator(tmp_path) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        dexscreener_poll_interval_s=0.01,
    )
    cfg.validate()
    orch = Orchestrator(cfg)
    orch.signal_engine.session = _NoNetworkSession()
    return orch


def test_sigint_triggers_clean_shutdown_not_an_exception(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def drive() -> None:
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)  # let the signal handler actually get registered
        os.kill(os.getpid(), signal.SIGINT)
        # If the handler works, stop_event is set and run() returns cleanly.
        # If it doesn't, the default SIGINT behavior raises KeyboardInterrupt
        # here instead, which this await would propagate as a test failure.
        await asyncio.wait_for(run_task, timeout=5.0)
        assert run_task.done()
        assert run_task.exception() is None

    asyncio.run(drive())


def test_sigterm_also_triggers_clean_shutdown(tmp_path):
    orch = _build_orchestrator(tmp_path)

    async def drive() -> None:
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(run_task, timeout=5.0)
        assert run_task.exception() is None

    asyncio.run(drive())


def test_shutdown_leaves_no_pending_tasks(tmp_path):
    """The actual symptom reported: tasks abandoned mid-flight print
    "Task was destroyed but it is pending" when the loop closes. After a
    clean shutdown, every task this orchestrator created must be done."""
    orch = _build_orchestrator(tmp_path)
    created_tasks: list[asyncio.Task] = []

    async def drive() -> None:
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        # Snapshot all non-done tasks on this loop right before signaling,
        # so we can confirm they're all finished afterward.
        created_tasks.extend(t for t in asyncio.all_tasks() if t is not asyncio.current_task())
        os.kill(os.getpid(), signal.SIGINT)
        await asyncio.wait_for(run_task, timeout=5.0)
        await asyncio.sleep(0)  # let cancellation fully propagate
        for t in created_tasks:
            assert t.done(), f"{t} was left pending"

    asyncio.run(drive())
