"""TokenSafety.check_honeypot: replaced a simulateTransaction-based check
that could never pass on ANY token (it required a funded token account we
never have before buying, so every real candidate failed with
AccountNotFound regardless of whether it was actually a honeypot -- see
jupiter_client.py's JupiterClient.check_sell_route docstring).

The proxy is three cheap signals: a Jupiter sell-side route quote, real
observed sell volume in the last 5 minutes (DexScreener-sourced candidates
only -- pump.fun's coin feed doesn't expose this), and no active Token-2022
transferHook extension on the mint.
"""
from __future__ import annotations

from bot.models import Candidate, SignalSource
from bot.token_safety import TokenSafety
from conftest import FakeJupiter, FakeQuoteResult, FakeRpc, make_pubkey

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class _NoNetworkSession:
    def get(self, *args, **kwargs):
        import requests

        raise requests.ConnectionError("network disabled in tests")


def _clean_mint_account(owner: str = TOKEN_PROGRAM_ID, extensions=None) -> dict:
    info = {"mintAuthority": None, "freezeAuthority": None}
    if extensions is not None:
        info["extensions"] = extensions
    return {"owner": owner, "data": {"parsed": {"info": info}}}


def _candidate(mint: str, source=SignalSource.DEXSCREENER, sells_5m: int = 5) -> Candidate:
    return Candidate(
        mint=mint,
        symbol="TEST",
        source=source,
        liquidity_usd=50_000,
        sells_5m=sells_5m,
        pump_fun_graduated=False,  # keep check_lp_or_graduation out of the way; not what these tests target
    )


def _safety(rpc: FakeRpc, jupiter: FakeJupiter) -> TokenSafety:
    return TokenSafety(rpc, jupiter, rugcheck_session=_NoNetworkSession())


def test_no_sell_route_rejected():
    mint = make_pubkey(21)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    jupiter = FakeJupiter(sellable=False, sell_detail="no sell route found: no route")
    safety = _safety(rpc, jupiter)

    result = safety.check_honeypot(_candidate(mint), expected_tokens_out=1000)

    assert result.passed is False
    assert "no sell route found" in result.detail


def test_sell_route_exists_but_zero_observed_sells_dexscreener_rejected():
    mint = make_pubkey(22)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    jupiter = FakeJupiter(sellable=True, sell_detail="sell route exists (impact 1.00%)")
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.DEXSCREENER, sells_5m=0)
    result = safety.check_honeypot(candidate, expected_tokens_out=1000)

    assert result.passed is False
    assert "zero observed sells" in result.detail


def test_sell_route_exists_and_dexscreener_reports_real_sells_passes():
    mint = make_pubkey(23)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    jupiter = FakeJupiter(sellable=True, sell_detail="sell route exists (impact 1.00%)")
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.DEXSCREENER, sells_5m=7)
    result = safety.check_honeypot(candidate, expected_tokens_out=1000)

    assert result.passed is True
    assert "7 sells/5m" in result.detail


def test_pumpfun_source_skips_the_sell_volume_requirement():
    """pump.fun's coin feed never populates sells_5m (stays at the
    Candidate default of 0) -- treating that the same as DexScreener's
    "zero observed sells" would reject every single pump.fun candidate
    regardless of actual sellability, since the data just isn't there."""
    mint = make_pubkey(24)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    jupiter = FakeJupiter(sellable=True, sell_detail="sell route exists (impact 1.00%)")
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.PUMPFUN, sells_5m=0)
    result = safety.check_honeypot(candidate, expected_tokens_out=1000)

    assert result.passed is True
    assert "sell-volume data unavailable (pump.fun)" in result.detail


def test_active_transfer_hook_rejected_even_with_a_good_route_and_sells():
    mint = make_pubkey(25)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account(
        owner=TOKEN_2022_PROGRAM_ID,
        extensions=[{"extension": "transferHook", "state": {"programId": make_pubkey(99)}}],
    )
    jupiter = FakeJupiter(sellable=True, sell_detail="sell route exists (impact 1.00%)")
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.DEXSCREENER, sells_5m=10)
    result = safety.check_honeypot(candidate, expected_tokens_out=1000)

    assert result.passed is False
    assert "transferHook" in result.detail


def test_transfer_hook_with_null_program_id_is_not_flagged():
    """A transferHook extension entry with no real program attached
    (disabled/never configured) must not be treated as an active hook."""
    mint = make_pubkey(26)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account(
        owner=TOKEN_2022_PROGRAM_ID,
        extensions=[{"extension": "transferHook", "state": {"programId": None}}],
    )
    jupiter = FakeJupiter(sellable=True, sell_detail="sell route exists (impact 1.00%)")
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.DEXSCREENER, sells_5m=10)
    result = safety.check_honeypot(candidate, expected_tokens_out=1000)

    assert result.passed is True


def test_plain_spl_token_never_checked_for_transfer_hook():
    """transferHook is a Token-2022-only extension -- a plain SPL Token
    mint_info has no "extensions" key at all; must not raise or false-flag."""
    mint = make_pubkey(27)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account(owner=TOKEN_PROGRAM_ID)
    jupiter = FakeJupiter(sellable=True, sell_detail="sell route exists (impact 1.00%)")
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.DEXSCREENER, sells_5m=10)
    result = safety.check_honeypot(candidate, expected_tokens_out=1000)

    assert result.passed is True


def test_full_evaluate_pipeline_can_now_actually_pass_a_clean_dexscreener_token():
    """End-to-end regression for the reported bug: the OLD honeypot check
    (simulateTransaction against an unheld token) rejected every single
    candidate with AccountNotFound, so verdict.passed could never be True
    for anything discovered pre-buy. This is the same full evaluate() path
    Orchestrator.evaluate_candidate calls."""
    mint = make_pubkey(28)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    total_supply = 1_000_000
    pool_addr, pool_authority = make_pubkey(228), make_pubkey(229)
    rpc.largest_accounts[mint] = [{"address": pool_addr, "amount": str(total_supply // 10)}]
    rpc.accounts[pool_addr] = {"owner": TOKEN_PROGRAM_ID, "data": {"parsed": {"info": {"owner": pool_authority}}}}
    rpc.accounts[pool_authority] = {"owner": "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"}
    rpc.token_supply[mint] = {"amount": str(total_supply)}
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    safety = _safety(rpc, jupiter)

    candidate = _candidate(mint, source=SignalSource.DEXSCREENER, sells_5m=12)
    verdict = safety.evaluate(candidate, position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is True, verdict.rejection_reasons
