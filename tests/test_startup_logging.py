"""Orchestrator.run(): the WS subscription pool_events (and InsiderRadar
indexing) rides on must be loud about whether it's even attempted, and
every background task must survive -- and log -- an unhandled exception
instead of silently dying.

A ~1h production run showed zero pool_creation_detected / heartbeat lines
at all, with no log evidence either way of whether the subscription was
ever attempted or whether something crashed. These tests are the
regression coverage for closing that gap.
"""
from __future__ import annotations

import asyncio
import logging

import requests

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator


class _NoNetworkSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("network disabled in this test")


class _FakeWsNoNotifications:
    """Never yields anything -- just needs to exist so run() takes the
    "self._ws is not None" branch without any real network I/O."""

    def get_ws_stats(self):
        return {"active_connections": 0, "total_reconnects": 0, "last_drop_at": None}

    async def logs_subscribe(self, program_id, prefilter=None):
        return
        yield  # pragma: no cover -- makes this an async generator; never reached


def _build_orchestrator(tmp_path, with_ws: bool) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        helius_ws_url="wss://example.invalid/ws" if with_ws else "",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        dexscreener_poll_interval_s=0.01,
    )
    cfg.validate()
    orch = Orchestrator(cfg)
    orch.signal_engine.session = _NoNetworkSession()
    if with_ws:
        orch._ws = _FakeWsNoNotifications()
    return orch


def _capture_records(orch: Orchestrator) -> list[logging.LogRecord]:
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    orch.logger.addHandler(handler)
    return records


def test_subscribing_log_fires_when_ws_is_configured(tmp_path):
    orch = _build_orchestrator(tmp_path, with_ws=True)
    records = _capture_records(orch)

    async def drive():
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        await orch._request_stop()
        await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(drive())

    matches = [r for r in records if r.getMessage() == "pool_events_subscribing"]
    assert len(matches) == 1
    fields = matches[0].fields
    assert "raydium_amm_v4" in fields["programs"]
    assert "pumpfun_bonding_curve" in fields["programs"]
    assert fields["event_driven_discovery"] is True


def test_inactive_warning_fires_when_no_ws_configured(tmp_path):
    orch = _build_orchestrator(tmp_path, with_ws=False)
    records = _capture_records(orch)
    assert orch._ws is None

    async def drive():
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        await orch._request_stop()
        await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(drive())

    matches = [r for r in records if r.getMessage() == "pool_events_inactive_no_ws"]
    assert len(matches) == 1
    assert matches[0].levelname == "WARNING"
    assert "HELIUS_WS_URL not configured" in matches[0].fields["reason"]

    # And the positive log must NOT have fired.
    assert [r for r in records if r.getMessage() == "pool_events_subscribing"] == []


def test_a_crashing_background_task_is_logged_loudly_not_silently(tmp_path):
    """The general fix: ANY background task raising is now visible in the
    logs, not just the WS-dependent ones -- this drives it with the
    heartbeat loop specifically, standing in for "whatever task actually
    crashed in production without a trace.\""""
    orch = _build_orchestrator(tmp_path, with_ws=False)
    records = _capture_records(orch)

    async def _explode():
        raise RuntimeError("simulated crash")

    orch._heartbeat_loop = _explode

    async def drive():
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        await orch._request_stop()
        await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(drive())  # must not raise -- the crash is contained and logged, not propagated

    matches = [r for r in records if r.getMessage() == "background_task_crashed"]
    assert len(matches) == 1
    assert matches[0].fields["task"] == "heartbeat_loop"
    assert matches[0].levelname == "ERROR"


def test_a_crashing_task_does_not_take_down_other_tasks(tmp_path):
    """dexscreener polling (and everything else) must keep running even
    though heartbeat crashed -- background tasks are independent."""
    orch = _build_orchestrator(tmp_path, with_ws=False)

    async def _explode():
        raise RuntimeError("simulated crash")

    orch._heartbeat_loop = _explode

    async def drive():
        run_task = asyncio.create_task(orch.run())
        await asyncio.sleep(0.05)
        assert orch.signal_engine.dexscreener_polls_done > 0  # still polling despite heartbeat's crash
        await orch._request_stop()
        await asyncio.wait_for(run_task, timeout=5.0)

    asyncio.run(drive())
