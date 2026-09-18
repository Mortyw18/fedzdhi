"""The kill switch persisting across restarts is deliberate (see its
module docstring) -- but it must never be silent about it. Orchestrator
must log loudly and alert at startup if it finds the switch already
halted from a previous run.
"""
from __future__ import annotations

from unittest import mock

from bot.alerter import Alerter
from bot.config import Config
from bot.kill_switch import KillSwitch
from bot.models import Mode
from bot.orchestrator import Orchestrator


def _pre_halt_state_file(tmp_path) -> str:
    state_path = str(tmp_path / "kill_switch_state.json")
    ks = KillSwitch(daily_loss_cap_sol=0.04, state_path=state_path)
    ks.record_pnl(-1.0)  # force a halt
    assert ks.is_halted() is True
    return state_path


def _build_cfg(tmp_path, state_path: str) -> Config:
    return Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=state_path,
    )


def test_startup_logs_loudly_when_already_halted(tmp_path, capsys):
    # The "memebot" logger has propagate=False (see logging_setup.py) and
    # writes to its own console StreamHandler, so this checks what an
    # operator watching the terminal actually sees rather than relying on
    # pytest's caplog (which attaches to the root logger).
    state_path = _pre_halt_state_file(tmp_path)
    cfg = _build_cfg(tmp_path, state_path)

    Orchestrator(cfg)

    err = capsys.readouterr().err
    assert "startup_kill_switch_already_halted" in err


def test_startup_alerts_when_already_halted(tmp_path):
    state_path = _pre_halt_state_file(tmp_path)
    cfg = _build_cfg(tmp_path, state_path)

    with mock.patch.object(Alerter, "notify") as notify:
        Orchestrator(cfg)

    messages = [c.args[0] for c in notify.call_args_list]
    assert any("ALREADY HALTED" in m for m in messages)
    assert any("--reset-kill-switch" in m for m in messages)


def test_startup_silent_when_not_halted(tmp_path, capsys):
    state_path = str(tmp_path / "kill_switch_state.json")  # never touched -- fresh, not halted
    cfg = _build_cfg(tmp_path, state_path)

    Orchestrator(cfg)

    err = capsys.readouterr().err
    assert "startup_kill_switch_already_halted" not in err
