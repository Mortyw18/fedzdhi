"""Orchestrator's verdict cache: the same mint gets rediscovered by
SignalEngine every poll cycle (DexScreener always returns still-active
pools; that's the point of a trend-following search), and without a cache,
evaluate_candidate() re-ran the full RPC/Jupiter/rugcheck/pump.fun-lookup
safety pipeline on the SAME mint every single cycle, forever -- exactly
the "quota" complaint this exists to fix.
"""
from __future__ import annotations

from bot.config import Config
from bot.models import Candidate, Mode, SafetyCheckResult, SafetyVerdict, SignalSource
from bot.orchestrator import Orchestrator
from conftest import FakeRpc


class _CountingTokenSafety:
    def __init__(self, passed: bool = True, rpc_error: bool = False) -> None:
        self.calls = 0
        self.passed = passed
        self.rpc_error = rpc_error

    def evaluate(self, candidate, position_size_lamports, wallet_pubkey, first_buyers=None, distinct_token_lookup=None):
        self.calls += 1
        if self.rpc_error:
            checks = [SafetyCheckResult("holder_concentration", False, "rpc error: simulated outage")]
        else:
            checks = [SafetyCheckResult("x", self.passed, "ok" if self.passed else "rejected")]
        return SafetyVerdict(mint=candidate.mint, passed=self.passed, checks=checks)


def _orch(tmp_path, passed: bool = True, rpc_error: bool = False, **cfg_overrides) -> tuple[Orchestrator, _CountingTokenSafety]:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        **cfg_overrides,
    )
    cfg.validate()
    orch = Orchestrator(cfg)
    orch.rpc = FakeRpc()  # no real network for _get_token_decimals
    safety = _CountingTokenSafety(passed=passed, rpc_error=rpc_error)
    orch.token_safety = safety
    return orch, safety


def _candidate(mint: str = "Mint1", liquidity_usd: float = 50_000) -> Candidate:
    return Candidate(mint=mint, symbol="TST", source=SignalSource.DEXSCREENER, liquidity_usd=liquidity_usd)


def test_rediscovering_the_same_mint_does_not_re_run_the_safety_pipeline(tmp_path):
    orch, safety = _orch(tmp_path)
    for _ in range(5):
        orch.evaluate_candidate(_candidate())
    assert safety.calls == 1


def test_different_mints_each_get_their_own_evaluation(tmp_path):
    orch, safety = _orch(tmp_path)
    orch.evaluate_candidate(_candidate(mint="MintA"))
    orch.evaluate_candidate(_candidate(mint="MintB"))
    orch.evaluate_candidate(_candidate(mint="MintA"))  # cached
    assert safety.calls == 2


def test_cached_rediscovery_still_counts_as_a_signal_in_accounting(tmp_path):
    """The cache skips the expensive safety pipeline, not visibility into
    how often a mint gets rediscovered -- record_candidate always runs."""
    orch, safety = _orch(tmp_path)
    for _ in range(4):
        orch.evaluate_candidate(_candidate())
    report = orch.accounting.daily_report()
    assert report["signals"] == 4
    assert report["safety_checks"] == 1


def test_cache_expires_after_ttl(tmp_path):
    orch, safety = _orch(tmp_path, verdict_cache_ttl_s=0.05)
    orch.evaluate_candidate(_candidate())
    assert safety.calls == 1

    import time
    time.sleep(0.1)

    orch.evaluate_candidate(_candidate())
    assert safety.calls == 2


def test_large_liquidity_swing_bypasses_the_cache_before_ttl_expires(tmp_path):
    orch, safety = _orch(tmp_path, verdict_cache_ttl_s=1200.0, verdict_cache_liquidity_change_pct=0.20)
    orch.evaluate_candidate(_candidate(liquidity_usd=50_000))
    assert safety.calls == 1

    # Liquidity halved -- well past the 20% change threshold, even though
    # the TTL (20 min default here) is nowhere close to expiring.
    orch.evaluate_candidate(_candidate(liquidity_usd=25_000))
    assert safety.calls == 2


def test_small_liquidity_change_stays_cached(tmp_path):
    orch, safety = _orch(tmp_path, verdict_cache_ttl_s=1200.0, verdict_cache_liquidity_change_pct=0.20)
    orch.evaluate_candidate(_candidate(liquidity_usd=50_000))
    assert safety.calls == 1

    orch.evaluate_candidate(_candidate(liquidity_usd=52_000))  # +4%, well under the 20% bar
    assert safety.calls == 1


def test_rpc_error_verdicts_are_never_cached_so_a_retry_happens_next_cycle(tmp_path):
    """An "unknown, RPC failed" verdict is not a real pass/fail -- caching
    it would lock a candidate out of re-evaluation for the full TTL right
    when a fresh attempt (once the RPC recovers) is most wanted. It would
    also mask a genuinely sustained outage from KillSwitch.set_rpc_outage's
    consecutive-failure counter, which depends on repeated rediscoveries
    of (typically different, but here deliberately the same) mints each
    still reaching TokenSafety.evaluate()."""
    orch, safety = _orch(tmp_path, rpc_error=True)
    for _ in range(3):
        orch.evaluate_candidate(_candidate())
    assert safety.calls == 3


def test_a_passing_verdict_is_cached_and_skips_reopening_the_position(tmp_path):
    """In live/paper mode a passed verdict buys immediately; the cache
    existing must not cause a second buy attempt on rediscovery -- it's
    already redundant with RiskManager's own "already holding this mint"
    gate, but confirms the two don't fight each other."""
    cfg = Config(
        mode=Mode.PAPER,
        helius_rpc_url="https://example.invalid/rpc",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
    )
    cfg.validate()
    orch = Orchestrator(cfg)
    orch.rpc = FakeRpc()
    safety = _CountingTokenSafety(passed=False)  # reject -- isolates the cache without needing a real buy path
    orch.token_safety = safety

    for _ in range(3):
        orch.evaluate_candidate(_candidate())
    assert safety.calls == 1
