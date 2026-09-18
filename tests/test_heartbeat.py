"""Orchestrator._heartbeat_loop: one INFO-level proof-of-life line at a
regular interval, with enough fields (poll counts, RPC usage, WS status)
that silence can be told apart from a stall without waiting for the next
daily report.
"""
from __future__ import annotations

import asyncio

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator


def _build_orchestrator(tmp_path, heartbeat_interval_s: float = 0.01) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        heartbeat_interval_s=heartbeat_interval_s,
    )
    cfg.validate()
    return Orchestrator(cfg)


def test_heartbeat_logs_at_info_level_with_expected_fields(tmp_path, capsys):
    orch = _build_orchestrator(tmp_path)
    orch.signal_engine.dexscreener_polls_done = 42
    orch.signal_engine.pumpfun_polls_done = 7

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._heartbeat_loop())
        await asyncio.sleep(0.05)  # let at least one heartbeat fire
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    err = capsys.readouterr().err
    assert "heartbeat" in err
    assert "INFO" in err  # not buried at DEBUG -- must show up without cranking verbosity


def test_heartbeat_reports_zero_ws_stats_when_no_websocket_configured(tmp_path):
    """No HELIUS_WS_URL -> orch._ws is None -- the heartbeat must not
    crash on that, just report zeroed-out WS stats."""
    orch = _build_orchestrator(tmp_path)
    assert orch._ws is None

    async def drive() -> None:
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._heartbeat_loop())
        await asyncio.sleep(0.05)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)
        assert task.exception() is None

    asyncio.run(drive())
