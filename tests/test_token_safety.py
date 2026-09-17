"""TokenSafety against hardcoded real-world rug patterns.

Every pattern here must be REJECTED: frozen mint, majority-holder
concentration, an unsellable honeypot, and a bundled/insider-controlled
launch. A fifth case (a clean token) must PASS everything, so we know
the checks aren't just rejecting unconditionally.
"""
from __future__ import annotations

import requests

from bot.models import Candidate, SignalSource, WalletBuyRecord
from bot.token_safety import TokenSafety
from conftest import RAYDIUM_AMM_V4, FakeJupiter, FakeQuoteResult, FakeRpc, make_pubkey

TOKEN_PROGRAM_ID = "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA"
TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"


class NoNetworkSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")


def _base_candidate(mint: str) -> Candidate:
    return Candidate(
        mint=mint,
        symbol="TEST",
        source=SignalSource.DEXSCREENER,
        liquidity_usd=50_000,
        pump_fun_graduated=False,  # skip LP-burn plumbing; not what these tests target
    )


def _clean_mint_account(mint_authority=None, freeze_authority=None, owner=TOKEN_PROGRAM_ID) -> dict:
    return {
        "owner": owner,
        "data": {"parsed": {"info": {"mintAuthority": mint_authority, "freezeAuthority": freeze_authority}}},
    }


def _pool_owned_holder_accounts(rpc: FakeRpc, mint: str, total_supply: int, n: int = 3) -> None:
    """Sets up top-N holders that are all pool vaults (should be excluded)."""
    accounts = []
    for i in range(n):
        addr = make_pubkey(10 + i)
        pool_authority = make_pubkey(200 + i)  # far from addr's range so n can safely grow
        accounts.append({"address": addr, "amount": str(total_supply // 10)})
        rpc.accounts[addr] = {"owner": TOKEN_PROGRAM_ID, "data": {"parsed": {"info": {"owner": pool_authority}}}}
        rpc.accounts[pool_authority] = {"owner": RAYDIUM_AMM_V4}
    rpc.largest_accounts[mint] = accounts
    rpc.token_supply[mint] = {"amount": str(total_supply)}


def _make_safety(rpc: FakeRpc, jupiter: FakeJupiter) -> TokenSafety:
    return TokenSafety(rpc, jupiter, rugcheck_session=NoNetworkSession())


def test_frozen_mint_rejected():
    mint = make_pubkey(1)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account(mint_authority=None, freeze_authority=make_pubkey(99))
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter()
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is False
    assert any("mint_freeze_authority" in r for r in verdict.rejection_reasons)


def test_majority_holder_concentration_rejected():
    mint = make_pubkey(3)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    total_supply = 1_000_000
    whale = make_pubkey(30)
    rpc.largest_accounts[mint] = [{"address": make_pubkey(40), "amount": str(int(total_supply * 0.60))}]
    rpc.accounts[make_pubkey(40)] = {"owner": TOKEN_PROGRAM_ID, "data": {"parsed": {"info": {"owner": whale}}}}
    rpc.accounts[whale] = {"owner": "11111111111111111111111111111111111111111"}  # not a known AMM program
    rpc.token_supply[mint] = {"amount": str(total_supply)}
    jupiter = FakeJupiter()
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is False
    assert any("holder_concentration" in r for r in verdict.rejection_reasons)


def test_unsellable_honeypot_rejected():
    mint = make_pubkey(5)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter(sellable=False, sell_detail="sell simulation reverted: InstructionError")
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is False
    assert any("honeypot" in r for r in verdict.rejection_reasons)


def test_bundled_launch_rejected_without_leader():
    mint = make_pubkey(6)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter()
    safety = _make_safety(rpc, jupiter)

    candidate = _base_candidate(mint)
    candidate.pool_creation_slot = 100
    first_buyers = [
        WalletBuyRecord(wallet=make_pubkey(70 + i), mint=mint, slot=100, amount_sol=0.1, price_usd=0.001)
        for i in range(5)
    ]

    verdict = safety.evaluate(
        candidate, position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2), first_buyers=first_buyers
    )

    assert verdict.passed is False
    assert any("bundled_launch" in r for r in verdict.rejection_reasons)


def test_bundled_launch_exempted_for_diversified_leader():
    mint = make_pubkey(7)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter()
    safety = _make_safety(rpc, jupiter)

    leader = make_pubkey(80)
    candidate = _base_candidate(mint)
    candidate.pool_creation_slot = 100
    candidate.leader_wallet = leader
    first_buyers = [
        WalletBuyRecord(wallet=make_pubkey(80 + i), mint=mint, slot=100, amount_sol=0.1, price_usd=0.001)
        for i in range(5)
    ]

    verdict = safety.evaluate(
        candidate,
        position_size_lamports=50_000_000,
        wallet_pubkey=make_pubkey(2),
        first_buyers=first_buyers,
        distinct_token_lookup=lambda w: 20 if w == leader else 0,
    )

    assert verdict.passed is True


def test_clean_token_passes_everything():
    mint = make_pubkey(9)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is True, verdict.rejection_reasons


def test_holder_concentration_batches_owner_lookups_instead_of_one_per_holder():
    """This is the RPC-budget-death-spiral fix: the old implementation made
    up to 2 individual getAccountInfo calls per top holder (owner lookup +
    pool-authority lookup), so 20 holders cost ~40 calls on this ONE check
    alone. The batched version costs exactly 2 getMultipleAccounts calls
    no matter how many holders there are."""
    mint = make_pubkey(60)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000, n=20)  # the worst case for the old per-holder approach
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is True, verdict.rejection_reasons
    assert rpc.call_log.count("getMultipleAccounts") == 2  # owners, then pool-authority-of-owners
    assert rpc.call_log.count("getAccountInfo") == 1  # only the mint account, shared with the freeze-authority check
    assert rpc.call_log.count("getTokenLargestAccounts") == 1
    assert rpc.call_log.count("getTokenSupply") == 1


def test_mint_account_fetched_once_and_shared_across_checks():
    """check_mint_freeze_authority and check_transfer_fee_extension both
    need the mint account; evaluate() must fetch it once and share it,
    not fetch it twice."""
    mint = make_pubkey(61)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    safety = _make_safety(rpc, jupiter)

    safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert rpc.call_log.count("getAccountInfo") == 1


def test_price_impact_over_ceiling_rejected():
    mint = make_pubkey(11)
    rpc = FakeRpc()
    rpc.accounts[mint] = _clean_mint_account()
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.20))  # 20% impact -- dust pool
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is False
    assert any("price_impact" in r for r in verdict.rejection_reasons)


def test_token2022_transfer_fee_rejected():
    mint = make_pubkey(13)
    rpc = FakeRpc()
    rpc.accounts[mint] = {
        "owner": TOKEN_2022_PROGRAM_ID,
        "data": {
            "parsed": {
                "info": {
                    "mintAuthority": None,
                    "freezeAuthority": None,
                    "extensions": [
                        {"extension": "transferFeeConfig", "state": {"newerTransferFee": {"transferFeeBasisPoints": 500}}}
                    ],
                }
            }
        },
    }
    _pool_owned_holder_accounts(rpc, mint, 1_000_000)
    jupiter = FakeJupiter()
    safety = _make_safety(rpc, jupiter)

    verdict = safety.evaluate(_base_candidate(mint), position_size_lamports=50_000_000, wallet_pubkey=make_pubkey(2))

    assert verdict.passed is False
    assert any("transfer_fee_tax" in r for r in verdict.rejection_reasons)
