"""Shared test fixtures: fake RPC and Jupiter clients.

Every test in this suite runs with zero real network access. These fakes
implement exactly the subset of RpcGateway/JupiterClient's interface that
the modules under test actually call, with canned, configurable
responses -- no monkeypatching of `requests` needed.
"""
from __future__ import annotations

import base58
import pytest
from nacl.signing import SigningKey

BURN_ADDR = "1nc1nerator11111111111111111111111111111111"
SYSTEM_PROGRAM = base58.b58encode(bytes(32)).decode("ascii")
RAYDIUM_AMM_V4 = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"


def make_pubkey(seed_byte: int) -> str:
    """Deterministic, valid-looking base58 pubkey for test fixtures."""
    return base58.b58encode(bytes([seed_byte % 256]) * 32).decode("ascii")


class FakeRpc:
    """Stands in for RpcGateway. Populate `.accounts`, `.largest_accounts`,
    `.token_supply`, `.simulate_result` before exercising code under test.
    """

    def __init__(self) -> None:
        self.accounts: dict[str, dict] = {}
        self.largest_accounts: dict[str, list[dict]] = {}
        self.token_supply: dict[str, dict] = {}
        self.simulate_result: dict = {"value": {"err": None, "logs": []}}
        self.send_signature = "FAKE_SIG_123"
        self.confirm_result = True
        self.transactions: dict[str, dict] = {}
        self.prioritization_fees: list[dict] = [{"prioritizationFee": 5000}]
        # Every method call is logged here so tests can assert on RPC volume
        # (e.g. "holder concentration must not cost more than a handful of
        # calls") without needing a real RpcGateway's budget tracker.
        self.call_log: list[str] = []

    def get_account_info(self, pubkey, encoding="jsonParsed"):
        self.call_log.append("getAccountInfo")
        return self.accounts.get(pubkey)

    def get_multiple_accounts(self, pubkeys, encoding="jsonParsed"):
        self.call_log.append("getMultipleAccounts")
        return [self.accounts.get(pk) for pk in pubkeys]

    def get_token_supply(self, mint):
        self.call_log.append("getTokenSupply")
        return self.token_supply.get(mint)

    def get_token_largest_accounts(self, mint):
        self.call_log.append("getTokenLargestAccounts")
        return self.largest_accounts.get(mint, [])

    def simulate_transaction(self, tx_b64, sig_verify=False):
        return self.simulate_result

    def get_balance(self, pubkey):
        return 200_000_000

    def get_latest_blockhash(self):
        return {"value": {"blockhash": base58.b58encode(b"\x01" * 32).decode("ascii")}}

    def get_recent_prioritization_fees(self, accounts=None):
        return self.prioritization_fees

    def send_transaction(self, tx_b64, skip_preflight=False):
        return self.send_signature

    def confirm_signature(self, signature, timeout_s=45.0, poll_interval_s=1.5):
        return self.confirm_result

    def call(self, method, params=None):
        if method == "getTransaction":
            sig = params[0]
            return self.transactions.get(sig, {"meta": {"fee": 5000, "preBalances": [0], "postBalances": [0]}})
        raise NotImplementedError(method)


class FakeQuoteResult:
    def __init__(self, in_amount, out_amount, price_impact_pct, raw=None):
        self.in_amount = in_amount
        self.out_amount = out_amount
        self.price_impact_pct = price_impact_pct
        self.raw = raw or {}

    @property
    def effective_price(self):
        return self.out_amount / self.in_amount if self.in_amount else 0.0


class FakeJupiter:
    """Stands in for JupiterClient. `quote_fn(input_mint, output_mint, amount)
    -> FakeQuoteResult` is the one thing tests customize per-scenario.
    """

    def __init__(self, quote_fn=None, sellable=True, sell_detail="ok") -> None:
        self.quote_fn = quote_fn or (lambda i, o, a: FakeQuoteResult(a, a, 0.01))
        self.sellable = sellable
        self.sell_detail = sell_detail
        self.built_transactions: list[dict] = []

    def quote(self, input_mint, output_mint, amount, slippage_bps=50, max_accounts=None):
        return self.quote_fn(input_mint, output_mint, amount)

    def build_swap_transaction(self, quote, user_pubkey, priority_fee_lamports=None, wrap_and_unwrap_sol=True):
        tx = {"swapTransaction": "ZmFrZQ==", "lastValidBlockHeight": 1}
        self.built_transactions.append(tx)
        return tx

    def check_sell_route(self, token_mint, token_amount_raw):
        quote = self.quote(token_mint, "SOL", token_amount_raw)
        return self.sellable, self.sell_detail, quote


@pytest.fixture
def fake_rpc():
    return FakeRpc()


@pytest.fixture
def fake_jupiter():
    return FakeJupiter()


@pytest.fixture
def test_wallet():
    from bot.solana_wallet import Wallet

    sk = SigningKey.generate()
    return Wallet(_signing_key=sk, pubkey_bytes=bytes(sk.verify_key))
