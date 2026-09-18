"""Orchestrator.evaluate_candidate is the ONLY path that feeds
KillSwitch.set_rpc_outage -- see test_orchestrator_rpc_budget.py for why
InsiderRadar's background indexing must never touch it. TokenSafety's
verdict here is the price-critical signal: an actual RPC call inside
evaluate() failing (TokenSafety's own "rpc error: " detail prefix) is what
proves we can't safely evaluate a buy right now, not an ordinary rejection
(holder concentration, no LP mint known, price impact too high, ...).

A prior overnight run tripped the kill switch from the indexing loop
within ~18s of startup while a plain curl to the same RPC endpoint
answered fine -- that coupling was the bug. Moving the signal here means
the kill switch only ever halts for a failure that would have blocked an
actual buy.
"""
from __future__ import annotations

from bot.config import Config
from bot.models import Candidate, Mode, SafetyCheckResult, SafetyVerdict, SignalSource
from bot.orchestrator import Orchestrator
from conftest import FakeRpc


class _ScriptedTokenSafety:
    """Returns verdicts fed to it one at a time via `verdicts` -- lets a
    test script an exact sequence of RPC-outage-flavored vs. ordinary
    verdicts across repeated evaluate_candidate calls."""

    def __init__(self, verdicts: list[SafetyVerdict]) -> None:
        self._verdicts = list(verdicts)
        self.calls = 0

    def evaluate(self, candidate, position_size_lamports, wallet_pubkey, first_buyers=None, distinct_token_lookup=None):
        self.calls += 1
        idx = min(self.calls - 1, len(self._verdicts) - 1)
        return self._verdicts[idx]


def _build(tmp_path, verdicts: list[SafetyVerdict]) -> tuple[Orchestrator, list[str]]:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        max_consecutive_rpc_outages=3,
    )
    cfg.validate()
    orch = Orchestrator(cfg)
    orch.rpc = FakeRpc()  # no real network for _get_token_decimals
    orch.token_safety = _ScriptedTokenSafety(verdicts)
    alerts: list[str] = []
    orch.alerter.notify_kill_switch = alerts.append
    return orch, alerts


def _rpc_error_verdict(mint: str = "Mint1") -> SafetyVerdict:
    return SafetyVerdict(
        mint=mint,
        passed=False,
        checks=[
            SafetyCheckResult("mint_freeze_authority", False, "rpc error: All RPC endpoints failed for getAccountInfo: timeout"),
            SafetyCheckResult("holder_concentration", False, "rpc error: All RPC endpoints failed for getTokenLargestAccounts: timeout"),
        ],
    )


def _ordinary_rejection_verdict(mint: str = "Mint1") -> SafetyVerdict:
    return SafetyVerdict(
        mint=mint,
        passed=False,
        checks=[
            SafetyCheckResult("mint_freeze_authority", True, "mint and freeze authority revoked"),
            SafetyCheckResult("lp_burned_or_graduated", False, "no LP mint known and token is not an un-graduated pump.fun token"),
            SafetyCheckResult("holder_concentration", False, "top-10 non-pool holders control 55% of supply (>= 30% limit)"),
        ],
    )


def _passing_verdict(mint: str = "Mint1") -> SafetyVerdict:
    return SafetyVerdict(mint=mint, passed=True, checks=[SafetyCheckResult("x", True, "ok")])


def _candidate(mint: str = "Mint1") -> Candidate:
    return Candidate(mint=mint, symbol="TST", source=SignalSource.DEXSCREENER, liquidity_usd=50_000)


def test_sustained_rpc_error_verdicts_trip_the_kill_switch(tmp_path):
    orch, alerts = _build(tmp_path, [_rpc_error_verdict()] * 3)
    for _ in range(3):
        orch.evaluate_candidate(_candidate())
    assert orch.kill_switch.is_halted() is True
    assert len(alerts) == 1  # latched -- one alert for the transition, not one per verdict


def test_a_single_rpc_error_verdict_never_trips_it_alone(tmp_path):
    orch, alerts = _build(tmp_path, [_rpc_error_verdict()])
    orch.evaluate_candidate(_candidate())
    assert orch.kill_switch.is_halted() is False
    assert alerts == []


def test_ordinary_rejection_resets_the_rpc_outage_streak(tmp_path):
    """Two RPC-error verdicts, then one ordinary rejection (proves the RPC
    just answered fine), then two more RPC-error verdicts -- must never
    halt, since the ordinary rejection resets the streak below threshold."""
    orch, alerts = _build(
        tmp_path,
        [
            _rpc_error_verdict(),
            _rpc_error_verdict(),
            _ordinary_rejection_verdict(),
            _rpc_error_verdict(),
            _rpc_error_verdict(),
        ],
    )
    for _ in range(5):
        orch.evaluate_candidate(_candidate())
    assert orch.kill_switch.is_halted() is False
    assert alerts == []


def test_a_passing_verdict_also_resets_the_streak(tmp_path):
    orch, alerts = _build(tmp_path, [_rpc_error_verdict(), _rpc_error_verdict(), _passing_verdict()])
    for _ in range(3):
        orch.evaluate_candidate(_candidate())
    assert orch.kill_switch.is_halted() is False
    assert alerts == []


def test_observe_only_mode_still_feeds_the_rpc_outage_signal(tmp_path):
    """The overnight incident this feature traces back to WAS an
    observe-only run -- the signal must not be gated behind live/paper
    trading actually happening, since evaluate_candidate runs TokenSafety
    unconditionally in observe-only mode too."""
    orch, alerts = _build(tmp_path, [_rpc_error_verdict()] * 3)
    assert orch.config.observe_only is True
    for _ in range(3):
        orch.evaluate_candidate(_candidate())
    assert orch.kill_switch.is_halted() is True
    assert len(alerts) == 1
