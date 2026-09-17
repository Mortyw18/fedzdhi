from __future__ import annotations

import pytest

from bot.execution_engine import ExecutionFailed, LiveExecutionEngine, PaperExecutionEngine
from bot.jupiter_client import slippage_bps_for_impact
from bot.models import ExitReason, Position
from conftest import FakeJupiter, FakeQuoteResult, FakeRpc, make_pubkey


def test_slippage_doctrine_formula():
    assert slippage_bps_for_impact(0.01) == round((0.01 * 2 + 0.01) * 10_000)  # 300 bps
    assert slippage_bps_for_impact(0.001) == round((0.001 * 2 + 0.01) * 10_000)


def test_slippage_doctrine_hard_cap():
    bps = slippage_bps_for_impact(10.0, hard_cap_pct=0.10)  # absurd impact
    assert bps == 1000  # capped at 10%


def test_slippage_doctrine_emergency_cap_is_higher():
    normal = slippage_bps_for_impact(10.0, hard_cap_pct=0.10, emergency=False)
    emergency = slippage_bps_for_impact(10.0, emergency_cap_pct=0.15, emergency=True)
    assert emergency > normal
    assert emergency == 1500


def test_paper_buy_applies_haircut():
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))  # 1:1 raw ratio
    engine = PaperExecutionEngine(jupiter, haircut_pct=0.02)

    fill = engine.buy("MintA", size_sol=0.05, token_decimals=6)

    quote_tokens = (0.05 * 1_000_000_000) / (10 ** 6)
    expected_tokens = quote_tokens * 0.98
    assert fill.tokens == pytest.approx(expected_tokens)
    assert fill.side == "buy"
    assert fill.tx_sig.startswith("PAPER-")
    # worse execution than quote -> fill price is higher than quote price
    assert fill.fill_price_usd > fill.quote_price_usd


def test_paper_sell_applies_haircut():
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    engine = PaperExecutionEngine(jupiter, haircut_pct=0.02)
    position = Position(mint="MintA", symbol="TST", size_sol=0.05, entry_price_usd=1.0, tokens_held=1000.0)

    fill = engine.sell(position, fraction=1.0, reason=ExitReason.HARD_STOP, token_decimals=6)

    raw_amount = int(1000.0 * 10 ** 6)
    quote_sol = raw_amount / 1_000_000_000
    assert fill.size_sol == pytest.approx(quote_sol * 0.98)
    assert fill.reason == "hard_stop"


def test_paper_partial_ladder_sell_uses_remaining_fraction():
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    engine = PaperExecutionEngine(jupiter, haircut_pct=0.0)
    position = Position(mint="MintA", symbol="TST", size_sol=0.05, entry_price_usd=1.0, tokens_held=1000.0)
    position.remaining_fraction = 1.0

    fill = engine.sell(position, fraction=0.5, reason=ExitReason.LADDER_TP, token_decimals=6)

    assert fill.tokens == pytest.approx(500.0)


class _FailingRpc(FakeRpc):
    def __init__(self, fail_times: int):
        super().__init__()
        self.fail_times = fail_times
        self.attempts = 0

    def send_transaction(self, tx_b64, skip_preflight=False):
        self.attempts += 1
        if self.attempts <= self.fail_times:
            raise Exception("simulated network error")
        return "SIG_OK"


def test_live_execution_retries_then_succeeds(test_wallet):
    rpc = _FailingRpc(fail_times=1)
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    engine = LiveExecutionEngine(rpc, jupiter, test_wallet, max_retries=2)

    fill = engine.buy("MintA", size_sol=0.05, token_decimals=6)
    assert fill.tx_sig == "SIG_OK"
    assert rpc.attempts == 2


def test_live_execution_fails_after_max_retries(test_wallet):
    rpc = _FailingRpc(fail_times=99)
    jupiter = FakeJupiter(quote_fn=lambda i, o, a: FakeQuoteResult(a, a, 0.01))
    engine = LiveExecutionEngine(rpc, jupiter, test_wallet, max_retries=2)

    with pytest.raises(ExecutionFailed):
        engine.buy("MintA", size_sol=0.05, token_decimals=6)
    assert rpc.attempts == 3  # initial attempt + 2 retries


def test_price_impact_over_5pct_rejected_before_safety():
    """Doctrine: reject outright above 5% impact, independent of TokenSafety."""
    from bot.jupiter_client import JupiterClient, LiquidityTooThin

    class DummySession:
        def get(self, url, params=None, timeout=None):
            class R:
                def raise_for_status(self):
                    pass

                def json(self):
                    return {"inAmount": str(params["amount"]), "outAmount": str(params["amount"]), "priceImpactPct": "0.06"}

            return R()

    client = JupiterClient(session=DummySession())
    with pytest.raises(LiquidityTooThin):
        client.quote_buy_with_impact_gate("MintA", 50_000_000, max_acceptable_impact_pct=0.05)
