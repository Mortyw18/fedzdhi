"""Orchestrator wiring for the two independent pump.fun flags.

enable_pumpfun_source (discovery: SignalEngine's pump.fun poller) and
enable_pumpfun_graduation_lookup (TokenSafety's graduation/LP-burn lookup)
used to be the same Config field, so turning off the dead discovery
endpoint also silently broke the unrelated graduation lookup, false-
rejecting every pump.fun-origin candidate. This is the regression coverage
for keeping them wired to separate flags in orchestrator.py.
"""
from __future__ import annotations

from bot.config import Config
from bot.models import Mode
from bot.orchestrator import Orchestrator


def _build_orchestrator(tmp_path, enable_pumpfun_source: bool, enable_pumpfun_graduation_lookup: bool) -> Orchestrator:
    cfg = Config(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        helius_ws_url="",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        enable_pumpfun_source=enable_pumpfun_source,
        enable_pumpfun_graduation_lookup=enable_pumpfun_graduation_lookup,
    )
    cfg.validate()
    return Orchestrator(cfg)


def test_discovery_off_does_not_disable_the_graduation_lookup(tmp_path):
    """The exact bug this decoupling fixes."""
    orch = _build_orchestrator(tmp_path, enable_pumpfun_source=False, enable_pumpfun_graduation_lookup=True)
    assert orch.signal_engine.pumpfun_disabled is True
    assert orch.token_safety.enable_pumpfun_lookups is True


def test_graduation_lookup_off_does_not_disable_discovery(tmp_path):
    orch = _build_orchestrator(tmp_path, enable_pumpfun_source=True, enable_pumpfun_graduation_lookup=False)
    assert orch.signal_engine.pumpfun_disabled is False
    assert orch.token_safety.enable_pumpfun_lookups is False


def test_both_off_together(tmp_path):
    orch = _build_orchestrator(tmp_path, enable_pumpfun_source=False, enable_pumpfun_graduation_lookup=False)
    assert orch.signal_engine.pumpfun_disabled is True
    assert orch.token_safety.enable_pumpfun_lookups is False
