"""Replay harness: a stored DexScreener snapshot and a synthetic leader tx
stream, both pushed through the full pipeline with zero network access.

This is the closest thing in this suite to an integration test: signal
discovery -> TokenSafety -> RiskManager -> paper execution -> Accounting,
for both an organic signal and an insider copy-trade.
"""
from __future__ import annotations

import json
import os
import time

from bot.accounting import Accounting
from bot.config import Config
from bot.execution_engine import PaperExecutionEngine
from bot.insider_radar import InsiderRadar
from bot.kill_switch import KillSwitch
from bot.models import Position, PositionStatus, SignalSource, WalletBuyRecord, WalletSellRecord
from bot.risk_manager import RiskManager
from bot.signal_engine import SignalEngine
from bot.token_safety import TokenSafety
from conftest import FakeJupiter, FakeQuoteResult, FakeRpc, make_pubkey

FIXTURE_PATH = os.path.join(os.path.dirname(__file__), "fixtures", "dexscreener_snapshot.json")
TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"


def _load_snapshot_candidates(engine: SignalEngine) -> list:
    with open(FIXTURE_PATH) as f:
        data = json.load(f)
    now = time.time()
    candidates = []
    for raw in data["pairs"]:
        if raw.get("chainId") != "solana":
            continue
        raw = dict(raw)
        raw["pairCreatedAt"] = (now - raw.pop("pairCreatedAtSecondsAgo")) * 1000.0
        candidate = engine.parse_dexscreener_pair(raw)
        ok, reason = engine.passes_filters(candidate)
        candidates.append((candidate, ok, reason))
    return candidates


def _clean_rpc_and_jupiter_for(mint: str) -> tuple[FakeRpc, FakeJupiter]:
    rpc = FakeRpc()
    rpc.accounts[mint] = {
        "owner": TOKEN_PROGRAM_ID,
        "data": {"parsed": {"info": {"mintAuthority": None, "freezeAuthority": None}}},
    }
    total_supply = 1_000_000
    pool_addr, pool_authority = make_pubkey(200), make_pubkey(201)
    rpc.largest_accounts[mint] = [{"address": pool_addr, "amount": str(total_supply)}]
    rpc.accounts[pool_addr] = {"owner": TOKEN_PROGRAM_ID, "data": {"parsed": {"info": {"owner": pool_authority}}}}
    rpc.accounts[pool_authority] = {"owner": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"}
    rpc.token_supply[mint] = {"amount": str(total_supply)}
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    return rpc, jupiter


def test_replay_dexscreener_snapshot_filters_correctly():
    engine = SignalEngine(
        min_pool_liquidity_usd=15_000,
        max_pool_liquidity_usd=400_000,
        max_pool_age_s=72 * 3600,
        min_volume_liquidity_ratio=0.20,
        min_buy_sell_ratio=1.5,
    )
    results = _load_snapshot_candidates(engine)
    by_symbol = {c.symbol: ok for c, ok, _ in results}

    assert by_symbol["GOOD"] is True       # healthy liquidity, volume, buy/sell ratio
    assert by_symbol["DUST"] is False      # liquidity below floor
    assert by_symbol["DUMP"] is False      # sells >> buys


def test_replay_organic_signal_through_full_paper_pipeline(tmp_path):
    engine = SignalEngine(
        min_pool_liquidity_usd=15_000,
        max_pool_liquidity_usd=400_000,
        max_pool_age_s=72 * 3600,
        min_volume_liquidity_ratio=0.20,
        min_buy_sell_ratio=1.5,
    )
    candidates = [c for c, ok, _ in _load_snapshot_candidates(engine) if ok]
    assert len(candidates) == 1
    candidate = candidates[0]
    candidate.pump_fun_graduated = False  # skip LP-burn plumbing; not what this test targets

    rpc, jupiter = _clean_rpc_and_jupiter_for(candidate.mint)
    safety = TokenSafety(rpc, jupiter, rugcheck_session=_NoNetworkSession())
    cfg = Config(db_path=":memory:")
    ks = KillSwitch(cfg.daily_loss_cap_sol, state_path=str(tmp_path / "ks.json"))
    risk = RiskManager(cfg, ks)
    accounting = Accounting(":memory:")
    execution = PaperExecutionEngine(jupiter, haircut_pct=cfg.paper_fill_haircut_pct)

    accounting.record_candidate(candidate)
    allowed, reason = risk.can_open_position(candidate.mint, [])
    assert allowed, reason

    verdict = safety.evaluate(candidate, position_size_lamports=int(cfg.position_size_sol * 1e9), wallet_pubkey=make_pubkey(2))
    accounting.record_safety_verdict(verdict)
    assert verdict.passed, verdict.rejection_reasons

    fill = execution.buy(candidate.mint, cfg.position_size_sol, token_decimals=6)
    risk.register_buy()
    position = Position(
        mint=candidate.mint, symbol=candidate.symbol, size_sol=cfg.position_size_sol,
        entry_price_usd=fill.fill_price_usd, tokens_held=fill.tokens, source=candidate.source,
    )
    fill.position_id = position.id
    accounting.record_fill(fill)
    accounting.record_position_opened(position)

    # Price doubles -> ladder take-profit fires for 50% of the position.
    current_price = position.entry_price_usd * 2.0
    decision = risk.evaluate_exit(position, current_price)
    assert decision is not None
    exit_reason, fraction = decision

    exit_fill = execution.sell(position, fraction, exit_reason, token_decimals=6)
    accounting.record_fill(exit_fill)
    risk.apply_ladder_fill(position, exit_reason, fraction)

    assert position.ladder.tp1_filled is True
    assert position.status == PositionStatus.OPEN  # only half sold
    assert exit_fill.tokens == fill.tokens * 0.5


def test_replay_leader_tx_stream_builds_conviction_then_copies():
    radar = InsiderRadar(conviction_min_trades=20, conviction_min_distinct_tokens=15)
    leader = "replay_leader"
    t0 = time.time() - 30 * 86400

    for i in range(20):
        radar.record_buy(
            WalletBuyRecord(wallet=leader, mint=f"Mint{i}", slot=i, amount_sol=0.1, price_usd=0.001, tokens=1000.0, timestamp=t0 + i * 3600)
        )
        radar.record_sell(
            WalletSellRecord(wallet=leader, mint=f"Mint{i}", slot=i + 1, amount_sol=0.15, price_usd=0.0015, tokens=1000.0, timestamp=t0 + i * 3600 + 1800)
        )

    assert radar.should_copy(leader) is True

    new_buy = WalletBuyRecord(wallet=leader, mint="FreshLaunch", slot=99999, amount_sol=0.2, price_usd=0.005)
    signal = radar.evaluate_copy_signal(new_buy)
    assert signal is not None

    valid, _ = radar.is_copy_still_valid(signal, current_price_usd=0.0052)  # +4%, within 10% gate
    assert valid is True
    valid, _ = radar.is_copy_still_valid(signal, current_price_usd=0.006)  # +20%, outside gate
    assert valid is False


class _NoNetworkSession:
    def get(self, *args, **kwargs):
        import requests

        raise requests.ConnectionError("network disabled in tests")
