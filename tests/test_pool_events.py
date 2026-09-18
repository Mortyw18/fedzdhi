"""bot.pool_events: pool-creation/token-launch detection from raw
getTransaction results, using synthetic transaction fixtures built to
match the exact shape Solana's jsonParsed getTransaction encoding
returns. These are the highest-value tests in the event-driven discovery
feature -- the detection logic is pure and deterministic, unlike the live
on-chain byte layouts it's modeling, which can't be verified without
network access (see pool_events.py's module docstring for the confidence
levels this is built on).
"""
from __future__ import annotations

import base58

from bot.jupiter_client import SOL_MINT
from bot.pool_events import (
    PUMPFUN_BONDING_CURVE_PROGRAM_ID,
    PUMPFUN_CREATE_DISCRIMINATOR,
    RAYDIUM_AMM_V4_PROGRAM_ID,
    RAYDIUM_INITIALIZE2_DISCRIMINATOR,
    anchor_discriminator,
    detect_pool_creation,
    resolve_new_mint,
)

PAYER = "PayerWa11et111111111111111111111111111111"
BASE_MINT = "BaseTokenMint111111111111111111111111111111"
LP_MINT = "LpTokenMint1111111111111111111111111111111"
COIN_VAULT = "CoinVaultAccount1111111111111111111111111111"
PC_VAULT = "PcVaultAccount111111111111111111111111111111"
USER_LP_ACCOUNT = "UserLpAccount1111111111111111111111111111111"
USER_BASE_ACCOUNT = "UserBaseAccount11111111111111111111111111111"  # already existed pre-tx


def _ix(program_id_index: int, data_bytes: bytes) -> dict:
    return {"programIdIndex": program_id_index, "accounts": [], "data": base58.b58encode(data_bytes).decode()}


def _init_mint_parsed(mint: str) -> dict:
    return {"program": "spl-token", "parsed": {"type": "initializeMint2", "info": {"mint": mint}}}


def _token_balance(account_index: int, mint: str) -> dict:
    return {"accountIndex": account_index, "mint": mint, "owner": "SomeOwner", "uiTokenAmount": {"uiAmount": 1.0}}


def _raydium_initialize2_tx(payer_has_base_token_already: bool = True) -> dict:
    """Models: Raydium Initialize2 pooling an EXISTING base token against
    SOL. New accounts funded by this tx: coin vault (base mint), pc vault
    (SOL), user's LP token account (LP mint, freshly initialized here).
    The user's own base-token account already existed (has a pre-balance)
    -- exactly why _newly_appeared_mints alone can't distinguish base
    from LP without the initializeMint exclusion.
    """
    account_keys = [PAYER, RAYDIUM_AMM_V4_PROGRAM_ID, COIN_VAULT, PC_VAULT, USER_LP_ACCOUNT, USER_BASE_ACCOUNT]
    pre = []
    if payer_has_base_token_already:
        pre.append(_token_balance(5, BASE_MINT))  # USER_BASE_ACCOUNT already held some
    post = [
        _token_balance(2, BASE_MINT),   # coin vault -- newly funded
        _token_balance(3, SOL_MINT),    # pc vault -- newly funded
        _token_balance(4, LP_MINT),     # user's new LP tokens
    ]
    if payer_has_base_token_already:
        post.append({**_token_balance(5, BASE_MINT), "uiTokenAmount": {"uiAmount": 0.5}})  # balance decreased, still present
    return {
        "slot": 12345,
        "blockTime": 1_700_000_000,
        "transaction": {
            "message": {
                "accountKeys": account_keys,
                "instructions": [_ix(1, RAYDIUM_INITIALIZE2_DISCRIMINATOR + b"\x00\x01")],
            }
        },
        "meta": {
            "preTokenBalances": pre,
            "postTokenBalances": post,
            "innerInstructions": [{"index": 0, "instructions": [_init_mint_parsed(LP_MINT)]}],
        },
    }


def _pumpfun_create_tx() -> dict:
    """Models: pump.fun "create" -- mints a brand-new token and funds its
    bonding-curve token account, all in this same transaction."""
    account_keys = [PAYER, PUMPFUN_BONDING_CURVE_PROGRAM_ID, "BondingCurveTokenAccount1111111111111111111111"]
    return {
        "slot": 54321,
        "blockTime": 1_700_000_500,
        "transaction": {
            "message": {
                "accountKeys": account_keys,
                "instructions": [_ix(1, PUMPFUN_CREATE_DISCRIMINATOR + b"\x02\x03")],
            }
        },
        "meta": {
            "preTokenBalances": [],
            "postTokenBalances": [_token_balance(2, BASE_MINT)],
            "innerInstructions": [{"index": 0, "instructions": [_init_mint_parsed(BASE_MINT)]}],
        },
    }


def _ordinary_swap_tx(program_id: str) -> dict:
    """A routine swap -- targets the same program, but NOT the creation
    discriminator. Must never be mistaken for a launch."""
    account_keys = [PAYER, program_id]
    return {
        "slot": 999,
        "blockTime": 1_700_000_100,
        "transaction": {
            "message": {
                "accountKeys": account_keys,
                "instructions": [_ix(1, bytes([9]) + b"\x00" * 8)],  # SwapBaseIn-ish, not the creation discriminator
            }
        },
        "meta": {"preTokenBalances": [], "postTokenBalances": [], "innerInstructions": []},
    }


# ----------------------------------------------------------------------
# discriminators
# ----------------------------------------------------------------------


def test_anchor_discriminator_is_deterministic_sha256():
    import hashlib

    expected = hashlib.sha256(b"global:create").digest()[:8]
    assert anchor_discriminator("create") == expected
    assert PUMPFUN_CREATE_DISCRIMINATOR == expected


def test_raydium_discriminator_is_a_single_byte():
    assert RAYDIUM_INITIALIZE2_DISCRIMINATOR == bytes([1])


# ----------------------------------------------------------------------
# resolve_new_mint
# ----------------------------------------------------------------------


def test_raydium_resolves_the_base_mint_not_the_lp_mint():
    tx = _raydium_initialize2_tx()
    assert resolve_new_mint(tx, RAYDIUM_AMM_V4_PROGRAM_ID) == BASE_MINT


def test_raydium_still_resolves_base_mint_when_user_had_no_prior_balance():
    """Even if the user's base-token account also looks "new" in this
    tx (e.g. their first-ever holding of it), the LP-mint exclusion via
    initializeMint still disambiguates correctly as long as the base
    mint itself isn't ALSO initialized in this tx (it never is -- the
    base token was created earlier, by its deployer)."""
    tx = _raydium_initialize2_tx(payer_has_base_token_already=False)
    assert resolve_new_mint(tx, RAYDIUM_AMM_V4_PROGRAM_ID) == BASE_MINT


def test_pumpfun_resolves_the_newly_initialized_mint():
    tx = _pumpfun_create_tx()
    assert resolve_new_mint(tx, PUMPFUN_BONDING_CURVE_PROGRAM_ID) == BASE_MINT


def test_pumpfun_falls_back_to_the_initialized_mint_if_balances_dont_line_up():
    """A dev-buy-less launch might not show the expected postTokenBalances
    entry (e.g. zero initial supply moved) -- the mint being initialized
    in this tx is still unambiguous on its own."""
    tx = _pumpfun_create_tx()
    tx["meta"]["postTokenBalances"] = []  # balances didn't line up
    assert resolve_new_mint(tx, PUMPFUN_BONDING_CURVE_PROGRAM_ID) == BASE_MINT


def test_unresolvable_when_no_mint_initialized_at_all():
    tx = _raydium_initialize2_tx()
    tx["meta"]["innerInstructions"] = []  # no initializeMint anywhere -- can't distinguish LP from base
    assert resolve_new_mint(tx, RAYDIUM_AMM_V4_PROGRAM_ID) is None


def test_unresolvable_when_multiple_ambiguous_candidates():
    tx = _raydium_initialize2_tx()
    tx["meta"]["postTokenBalances"].append(_token_balance(6, "SomeOtherNewMint111111111111111111111111111"))
    assert resolve_new_mint(tx, RAYDIUM_AMM_V4_PROGRAM_ID) is None


def test_unknown_program_id_resolves_nothing():
    tx = _raydium_initialize2_tx()
    assert resolve_new_mint(tx, "SomeUnrelatedProgram1111111111111111111111111") is None


# ----------------------------------------------------------------------
# detect_pool_creation
# ----------------------------------------------------------------------


def test_detects_raydium_pool_creation():
    tx = _raydium_initialize2_tx()
    event = detect_pool_creation(tx, "sig123", RAYDIUM_AMM_V4_PROGRAM_ID)
    assert event is not None
    assert event.mint == BASE_MINT
    assert event.program_id == RAYDIUM_AMM_V4_PROGRAM_ID
    assert event.signature == "sig123"
    assert event.slot == 12345
    assert event.block_time == 1_700_000_000


def test_detects_pumpfun_create():
    tx = _pumpfun_create_tx()
    event = detect_pool_creation(tx, "sig456", PUMPFUN_BONDING_CURVE_PROGRAM_ID)
    assert event is not None
    assert event.mint == BASE_MINT
    assert event.program_id == PUMPFUN_BONDING_CURVE_PROGRAM_ID


def test_ordinary_swap_never_detected_as_a_creation():
    tx = _ordinary_swap_tx(RAYDIUM_AMM_V4_PROGRAM_ID)
    assert detect_pool_creation(tx, "sig789", RAYDIUM_AMM_V4_PROGRAM_ID) is None

    tx2 = _ordinary_swap_tx(PUMPFUN_BONDING_CURVE_PROGRAM_ID)
    assert detect_pool_creation(tx2, "sig790", PUMPFUN_BONDING_CURVE_PROGRAM_ID) is None


def test_creation_instruction_for_a_different_program_in_the_same_tx_is_ignored():
    """The discriminator byte matches, but the instruction targets a
    DIFFERENT program than the one we're checking for -- must not match."""
    tx = _raydium_initialize2_tx()
    assert detect_pool_creation(tx, "sig", "SomeOtherProgram111111111111111111111111111") is None


def test_malformed_transaction_never_raises_returns_none():
    for bad_tx in [{}, {"transaction": {}}, {"meta": None}, {"transaction": {"message": {}}}]:
        assert detect_pool_creation(bad_tx, "sig", RAYDIUM_AMM_V4_PROGRAM_ID) is None


def test_inner_instruction_creation_call_is_not_detected():
    """Documented, accepted gap: only top-level instructions are checked.
    A creation instruction wrapped inside an aggregator/router as an
    inner instruction is missed, not falsely matched as something else."""
    tx = _raydium_initialize2_tx()
    inner_ix = tx["transaction"]["message"]["instructions"].pop()
    tx["meta"]["innerInstructions"].append({"index": 0, "instructions": [inner_ix]})
    assert detect_pool_creation(tx, "sig", RAYDIUM_AMM_V4_PROGRAM_ID) is None
