from __future__ import annotations

from bot.kill_switch import KillSwitch


def _ks(tmp_path, **kw) -> KillSwitch:
    return KillSwitch(daily_loss_cap_sol=0.04, state_path=str(tmp_path / "ks.json"), **kw)


def test_daily_loss_cap_trips(tmp_path):
    ks = _ks(tmp_path)
    ks.record_pnl(-0.02)
    assert ks.is_halted() is False
    ks.record_pnl(-0.03)
    assert ks.is_halted() is True
    assert "daily loss cap" in ks.halt_reason()


def test_consecutive_failures_trip(tmp_path):
    ks = _ks(tmp_path, max_consecutive_failures=3)
    ks.record_execution_failure()
    ks.record_execution_failure()
    assert ks.is_halted() is False
    ks.record_execution_failure()
    assert ks.is_halted() is True


def test_success_resets_failure_streak(tmp_path):
    ks = _ks(tmp_path, max_consecutive_failures=3)
    ks.record_execution_failure()
    ks.record_execution_failure()
    ks.record_execution_success()
    ks.record_execution_failure()
    ks.record_execution_failure()
    assert ks.is_halted() is False  # streak was reset, only at 2 again


def test_rpc_outage_halts(tmp_path):
    ks = _ks(tmp_path)
    ks.set_rpc_outage(True)
    assert ks.is_halted() is True
    assert "RPC outage" in ks.halt_reason()


def test_manual_reset_required(tmp_path):
    ks = _ks(tmp_path)
    ks.record_pnl(-0.05)
    assert ks.is_halted() is True
    ks.record_pnl(0.10)  # even a big win doesn't auto-clear the halt
    assert ks.is_halted() is True
    ks.reset()
    assert ks.is_halted() is False


def test_state_persists_across_instances(tmp_path):
    path = str(tmp_path / "ks.json")
    ks1 = KillSwitch(daily_loss_cap_sol=0.04, state_path=path)
    ks1.record_pnl(-0.05)
    assert ks1.is_halted() is True

    ks2 = KillSwitch(daily_loss_cap_sol=0.04, state_path=path)
    assert ks2.is_halted() is True
    ks2.reset()

    ks3 = KillSwitch(daily_loss_cap_sol=0.04, state_path=path)
    assert ks3.is_halted() is False


def test_buys_today_tracked(tmp_path):
    ks = _ks(tmp_path)
    assert ks.buys_today == 0
    ks.record_buy()
    ks.record_buy()
    assert ks.buys_today == 2
