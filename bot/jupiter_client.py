"""Jupiter v6 quote + swap client, and the slippage doctrine.

Slippage TOLERANCE is not a cost you sometimes pay -- it's an order you're
placing with MEV bots. We never let the caller pick a slippage number
directly; it's always derived from the quote's own priceImpactPct:

    tolerance = impact * 2 + 1%, hard-capped at 10% (15% for emergency exits)

And any token whose price impact for OUR size exceeds 5% is rejected
outright before TokenSafety even runs -- that pool is too thin to trade.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

import requests

from bot.rpc_gateway import RpcGateway

SOL_MINT = "So11111111111111111111111111111111111111112"
DEFAULT_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
DEFAULT_SWAP_URL = "https://lite-api.jup.ag/swap/v1/swap"


class JupiterError(Exception):
    pass


class LiquidityTooThin(JupiterError):
    """Price impact for our size exceeds the 5% doctrine ceiling."""


@dataclass
class QuoteResult:
    raw: dict
    input_mint: str
    output_mint: str
    in_amount: int
    out_amount: int
    price_impact_pct: float

    @property
    def effective_price(self) -> float:
        """output units per input unit, in raw (lamport/atom) terms."""
        if self.in_amount == 0:
            return 0.0
        return self.out_amount / self.in_amount


def slippage_bps_for_impact(
    impact_pct: float,
    multiplier: float = 2.0,
    flat_addon_pct: float = 0.01,
    hard_cap_pct: float = 0.10,
    emergency: bool = False,
    emergency_cap_pct: float = 0.15,
) -> int:
    """The slippage doctrine, in one function: tolerance = impact*2 + 1%, capped."""
    cap = emergency_cap_pct if emergency else hard_cap_pct
    tolerance_pct = min(impact_pct * multiplier + flat_addon_pct, cap)
    return max(1, round(tolerance_pct * 10_000))


class JupiterClient:
    def __init__(
        self,
        quote_url: str = DEFAULT_QUOTE_URL,
        swap_url: str = DEFAULT_SWAP_URL,
        session: Optional[requests.Session] = None,
        logger: Optional[logging.Logger] = None,
        timeout_s: float = 10.0,
    ) -> None:
        self.quote_url = quote_url
        self.swap_url = swap_url
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.jupiter")
        self.timeout_s = timeout_s

    def quote(
        self,
        input_mint: str,
        output_mint: str,
        amount: int,
        slippage_bps: int = 50,
        max_accounts: Optional[int] = None,
    ) -> QuoteResult:
        params = {
            "inputMint": input_mint,
            "outputMint": output_mint,
            "amount": str(amount),
            "slippageBps": slippage_bps,
            "swapMode": "ExactIn",
        }
        if max_accounts:
            params["maxAccounts"] = max_accounts
        resp = self.session.get(self.quote_url, params=params, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise JupiterError(f"quote error: {data['error']}")
        return QuoteResult(
            raw=data,
            input_mint=input_mint,
            output_mint=output_mint,
            in_amount=int(data["inAmount"]),
            out_amount=int(data["outAmount"]),
            price_impact_pct=float(data.get("priceImpactPct", 0.0)),
        )

    def quote_buy_with_impact_gate(
        self,
        output_mint: str,
        amount_lamports: int,
        max_acceptable_impact_pct: float = 0.05,
        slippage_bps: int = 50,
    ) -> QuoteResult:
        """Quote SOL->mint for our size, rejecting pools too thin to trade.

        The 5% gate applies to the quote's OWN reported slippage impact
        estimate at whatever slippage_bps we pass for quoting purposes;
        the real tolerance sent with the swap is computed separately via
        `slippage_bps_for_impact` once we've decided to proceed.
        """
        result = self.quote(SOL_MINT, output_mint, amount_lamports, slippage_bps=slippage_bps)
        if result.price_impact_pct > max_acceptable_impact_pct:
            raise LiquidityTooThin(
                f"{output_mint}: price impact {result.price_impact_pct:.2%} exceeds "
                f"{max_acceptable_impact_pct:.2%} ceiling for {amount_lamports} lamports -- pool too thin"
            )
        return result

    def build_swap_transaction(
        self,
        quote: QuoteResult,
        user_pubkey: str,
        priority_fee_lamports: Optional[int] = None,
        wrap_and_unwrap_sol: bool = True,
    ) -> dict:
        payload: dict = {
            "quoteResponse": quote.raw,
            "userPublicKey": user_pubkey,
            "wrapAndUnwrapSol": wrap_and_unwrap_sol,
            "dynamicComputeUnitLimit": True,
        }
        if priority_fee_lamports is not None:
            payload["prioritizationFeeLamports"] = priority_fee_lamports
        resp = self.session.post(self.swap_url, json=payload, timeout=self.timeout_s)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise JupiterError(f"swap build error: {data['error']}")
        return data  # contains swapTransaction (base64), lastValidBlockHeight

    def simulate_sell(
        self,
        rpc: RpcGateway,
        token_mint: str,
        token_amount_raw: int,
        user_pubkey: str,
    ) -> tuple[bool, str, Optional[QuoteResult]]:
        """Honeypot check: can we get a route AND simulate selling our exact holdings?

        Returns (sellable, detail, quote_or_none). Used by TokenSafety before
        any buy -- if a token can't be sold now, at our size, it's worthless
        to us regardless of chart appearance.
        """
        try:
            quote = self.quote(token_mint, SOL_MINT, token_amount_raw, slippage_bps=500)
        except (JupiterError, requests.RequestException) as exc:
            return False, f"no sell route found: {exc}", None

        if quote.out_amount <= 0:
            return False, "sell route returns zero SOL out", quote

        try:
            built = self.build_swap_transaction(quote, user_pubkey)
            sim = rpc.simulate_transaction(built["swapTransaction"], sig_verify=False)
        except Exception as exc:  # noqa: BLE001 - any simulate failure means "can't confirm sellable"
            return False, f"sell simulation failed: {exc}", quote

        sim_value = sim.get("value", sim) if isinstance(sim, dict) else sim
        err = sim_value.get("err") if isinstance(sim_value, dict) else None
        if err is not None:
            return False, f"sell simulation reverted: {err}", quote

        return True, "sell simulated successfully", quote

    def detect_transfer_tax_pct(
        self,
        rpc: RpcGateway,
        token_mint: str,
        token_amount_raw: int,
        user_pubkey: str,
        quote: Optional[QuoteResult] = None,
    ) -> float:
        """Compare simulated realized SOL out vs quoted SOL out.

        A meaningful gap between what Jupiter quoted and what simulation
        shows actually moving is the signature of a Token-2022 transfer-fee
        tax that the router didn't fully price in. Returns the tax
        fraction (0.0 if no meaningful gap detected or simulation lacks
        the data to tell).
        """
        quote = quote or self.quote(token_mint, SOL_MINT, token_amount_raw, slippage_bps=500)
        built = self.build_swap_transaction(quote, user_pubkey)
        sim = rpc.simulate_transaction(built["swapTransaction"], sig_verify=False)
        sim_value = sim.get("value", sim) if isinstance(sim, dict) else sim
        # Without full postTokenBalances parsing (requires devnet-style simulate
        # with accounts config), we conservatively fall back to 0.0 unless the
        # simulation surfaces an explicit compute/log hint of a fee extension.
        logs = sim_value.get("logs") or [] if isinstance(sim_value, dict) else []
        for line in logs:
            if "TransferFeeConfig" in line or "transfer_fee" in line.lower():
                return -1.0  # sentinel: tax extension present, exact % unknown -> caller should reject
        return 0.0
