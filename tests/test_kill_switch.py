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


# ----------------------------------------------------------------------
# latching: each record_* returns True ONLY on the transition into halted,
# so callers (Orchestrator, ExitMonitor) know to alert once and then stay
# silent -- this is what stops the reported "re-trips and re-alerts every
# 2-3 seconds" behavior.
# ----------------------------------------------------------------------


def test_record_pnl_returns_true_only_on_first_trip(tmp_path):
    ks = _ks(tmp_path)
    assert ks.record_pnl(-0.02) is False  # under the cap, no trip
    assert ks.record_pnl(-0.03) is True  # this call crosses the cap -- newly tripped
    # Further losses while already halted must not report a new trip, even
    # though the underlying condition (being over the loss cap) still holds.
    assert ks.record_pnl(-0.01) is False
    assert ks.record_pnl(-0.01) is False


def test_record_execution_failure_returns_true_only_on_first_trip(tmp_path):
    ks = _ks(tmp_path, max_consecutive_failures=3)
    assert ks.record_execution_failure() is False
    assert ks.record_execution_failure() is False
    assert ks.record_execution_failure() is True  # 3rd failure crosses the threshold
    assert ks.record_execution_failure() is False  # still halted, not a new trip
    assert ks.record_execution_failure() is False


def test_set_rpc_outage_returns_true_only_on_first_trip(tmp_path):
    ks = _ks(tmp_path)
    assert ks.set_rpc_outage(True) is True
    # A flood of repeated RpcOutage exceptions (e.g. one per WebSocket
    # notification, arriving every few seconds) must each report "not a
    # new trip" once already halted -- this is the exact call pattern that
    # was spamming alerts.
    for _ in range(50):
        assert ks.set_rpc_outage(True) is False


def test_reset_then_new_trip_reports_true_again(tmp_path):
    """A reset is a real state change back to 'not halted', so the NEXT
    trip afterward is genuinely new and must alert again."""
    ks = _ks(tmp_path)
    assert ks.set_rpc_outage(True) is True
    assert ks.set_rpc_outage(True) is False
    ks.reset()
    assert ks.set_rpc_outage(True) is True


def test_different_trip_reasons_do_not_double_report_while_already_halted(tmp_path):
    """Once halted for one reason, a second, different condition tripping
    must not report as newly-tripped either -- there's only one halted
    state, and only its first entry is news."""
    ks = _ks(tmp_path, max_consecutive_failures=3)
    assert ks.record_pnl(-0.05) is True
    assert ks.record_execution_failure() is False
    assert ks.record_execution_failure() is False
    assert ks.record_execution_failure() is False  # would trip failures on its own, but already halted
