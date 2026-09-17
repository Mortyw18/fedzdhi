"""Observe-only (M2) mode: candidates and safety verdicts get logged
against live data, but ExecutionEngine.buy must never be called -- not
even a paper fill. This is the property that actually matters for M2,
so it's asserted directly against a spy rather than inferred from the
absence of a Position.
"""
from __future__ import annotations

import pytest

from bot.config import Config, ConfigError
from bot.models import Candidate, Mode, SafetyCheckResult, SafetyVerdict, SignalSource
from bot.orchestrator import Orchestrator
from conftest import FakeRpc


class _SpyExecution:
    def __init__(self) -> None:
        self.buy_calls: list[tuple] = []
        self.sell_calls: list[tuple] = []

    def buy(self, mint, size_sol, token_decimals):
        self.buy_calls.append((mint, size_sol, token_decimals))
        raise AssertionError("ExecutionEngine.buy must never be called in observe-only mode")

    def sell(self, *args, **kwargs):
        self.sell_calls.append((args, kwargs))
        raise AssertionError("ExecutionEngine.sell must never be called in observe-only mode")


class _StubTokenSafety:
    def __init__(self, passed: bool) -> None:
        self.passed = passed
        self.calls = 0

    def evaluate(self, candidate, position_size_lamports, wallet_pubkey, first_buyers=None, distinct_token_lookup=None):
        self.calls += 1
        detail = "ok" if self.passed else "rejected for test"
        return SafetyVerdict(mint=candidate.mint, passed=self.passed, checks=[SafetyCheckResult("x", self.passed, detail)])


def _observe_only_orchestrator(tmp_path, safety_passed: bool = True) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
    )
    cfg.validate()  # must not raise: paper + observe_only + an RPC url is a valid combination
    orch = Orchestrator(cfg)

    # Swap the real RPC/TokenSafety/ExecutionEngine for fakes/spies so this
    # test proves the orchestration logic without touching the network.
    fake_rpc = FakeRpc()
    fake_rpc.token_supply["MintA"] = {"amount": "1000000", "decimals": 6}
    fake_rpc.token_supply["MintB"] = {"amount": "1000000", "decimals": 6}
    orch.rpc = fake_rpc
    orch.token_safety = _StubTokenSafety(safety_passed)
    orch.execution = _SpyExecution()
    return orch


def test_observe_only_never_buys_even_when_safety_passes(tmp_path):
    orch = _observe_only_orchestrator(tmp_path, safety_passed=True)
    candidate = Candidate(mint="MintA", symbol="TST", source=SignalSource.DEXSCREENER, liquidity_usd=50_000)

    orch.evaluate_candidate(candidate)

    assert orch.execution.buy_calls == []
    assert orch.open_positions == {}
    assert orch.token_safety.calls == 1


def test_observe_only_records_verdict_even_on_rejection(tmp_path):
    orch = _observe_only_orchestrator(tmp_path, safety_passed=False)
    candidate = Candidate(mint="MintB", symbol="TST", source=SignalSource.DEXSCREENER, liquidity_usd=50_000)

    orch.evaluate_candidate(candidate)

    report = orch.accounting.daily_report()
    assert report["signals"] == 1
    assert report["safety_checks"] == 1
    assert orch.execution.buy_calls == []


def test_observe_only_bypasses_risk_gate_but_not_execution(tmp_path):
    """Even a risk-gate-blocking state (kill switch halted) must not matter --
    observe-only never reaches ExecutionEngine regardless of risk gate state."""
    orch = _observe_only_orchestrator(tmp_path, safety_passed=True)
    orch.kill_switch.record_pnl(-10.0)  # force a halt
    assert orch.kill_switch.is_halted() is True

    candidate = Candidate(mint="MintA", symbol="TST", source=SignalSource.DEXSCREENER, liquidity_usd=50_000)
    orch.evaluate_candidate(candidate)

    assert orch.token_safety.calls == 1  # still evaluated despite the halt
    assert orch.execution.buy_calls == []


def test_observe_only_status_text_mentions_no_trades(tmp_path):
    orch = _observe_only_orchestrator(tmp_path)
    assert "no trades placed" in orch.status_text()


def test_observe_only_rejected_when_combined_with_live():
    with pytest.raises(ConfigError):
        Config(mode=Mode.LIVE, observe_only=True, helius_api_key="x").validate()


def test_observe_only_requires_rpc_configured():
    with pytest.raises(ConfigError):
        Config(observe_only=True).validate()


def test_observe_only_with_rpc_and_paper_is_valid():
    Config(observe_only=True, helius_api_key="test-key").validate()
