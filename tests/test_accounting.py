from __future__ import annotations

import time

from bot.accounting import Accounting
from bot.models import (
    Candidate,
    Fill,
    Position,
    PositionStatus,
    SafetyCheckResult,
    SafetyVerdict,
    SignalSource,
)


def _acc() -> Accounting:
    return Accounting(":memory:")


def test_record_and_daily_report_pass_rate_warning():
    acc = _acc()
    today = time.strftime("%Y-%m-%d", time.gmtime())

    for i in range(10):
        c = Candidate(mint=f"Mint{i}", symbol="T", source=SignalSource.DEXSCREENER, liquidity_usd=50_000)
        acc.record_candidate(c)
        passed = i < 9  # 90% pass rate -- should trigger the "too loose" warning
        checks = [SafetyCheckResult("mint_freeze_authority", passed, "ok" if passed else "authority not revoked")]
        acc.record_safety_verdict(SafetyVerdict(mint=c.mint, passed=passed, checks=checks))

    report = acc.daily_report(today)
    assert report["signals"] == 10
    assert report["safety_checks"] == 10
    assert report["safety_pass_rate"] == 0.9
    assert any("too loose" in w or "70%" in w for w in report["warnings"])


def test_daily_report_no_warning_when_pass_rate_healthy():
    acc = _acc()
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for i in range(10):
        passed = i < 3  # 30% pass rate
        acc.record_safety_verdict(
            SafetyVerdict(mint=f"Mint{i}", passed=passed, checks=[SafetyCheckResult("x", passed, "detail")])
        )
    report = acc.daily_report(today)
    assert report["safety_pass_rate"] == 0.3
    assert report["warnings"] == []


def test_position_lifecycle_and_leader_pnl():
    acc = _acc()
    position = Position(
        mint="MintA",
        symbol="TST",
        size_sol=0.05,
        entry_price_usd=1.0,
        tokens_held=1000.0,
        source=SignalSource.INSIDER_COPY,
        leader_wallet="leader_1",
    )
    acc.record_position_opened(position)

    fill = Fill(
        position_id=position.id,
        mint="MintA",
        side="sell",
        quote_price_usd=1.5,
        fill_price_usd=1.5,
        size_sol=0.075,
        tokens=1000.0,
        fee_sol=0.000005,
        slippage_bps=10,
        tx_sig="sig1",
    )
    acc.record_fill(fill)

    position.status = PositionStatus.CLOSED
    position.closed_at = time.time()
    position.realized_pnl_sol = 0.075 - 0.05 - fill.fee_sol
    acc.record_position_closed(position)

    per_leader = acc.per_leader_pnl()
    assert len(per_leader) == 1
    assert per_leader[0]["leader_wallet"] == "leader_1"
    assert per_leader[0]["pnl_sol"] == position.realized_pnl_sol

    today = time.strftime("%Y-%m-%d", time.gmtime())
    report = acc.daily_report(today)
    assert report["trades_closed"] == 1
    assert report["pnl_after_fees_sol"] == round(position.realized_pnl_sol, 6)


def test_render_daily_report_is_human_readable():
    acc = _acc()
    text = acc.render_daily_report()
    assert "Daily Report" in text
    assert "Trades closed" in text
