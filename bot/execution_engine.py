"""ExecutionEngine: turns a decision into an on-chain fill (or a paper one).

Two implementations share one interface (`buy` / `sell` returning a
`Fill`) so RiskManager, Accounting, and ExitMonitor never need to know
which mode they're running in:

  - LiveExecutionEngine: real Jupiter quote -> swap -> sign -> send ->
    confirm, with the slippage doctrine applied on every trade, priority
    fee sized off recent network congestion, max 2 retries (each failed
    tx still costs a fee, so retries are counted and reported), and the
    true fill vs. quote slippage recorded from the confirmed transaction.
  - PaperExecutionEngine: identical decision pipeline, fills simulated
    from the same live Jupiter quotes with a 2% haircut baked in, because
    a quote is always a slightly optimistic view of what you'd actually
    get.
"""
from __future__ import annotations

import logging
import statistics
import uuid
from typing import Optional, Protocol

from bot.jupiter_client import JupiterClient, SOL_MINT, slippage_bps_for_impact
from bot.models import ExitReason, Fill, Position
from bot.rpc_gateway import RpcGateway, RpcOutage
from bot.solana_wallet import Wallet

LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_PRIORITY_FEE_LAMPORTS = 10_000
FALLBACK_NETWORK_FEE_SOL = 0.000_005


class ExecutionFailed(Exception):
    pass


class ExecutionEngine(Protocol):
    def buy(self, mint: str, size_sol: float, token_decimals: int) -> Fill: ...

    def sell(
        self, position: Position, fraction: float, reason: ExitReason, token_decimals: int, emergency: bool = False
    ) -> Fill: ...


class LiveExecutionEngine:
    def __init__(
        self,
        rpc: RpcGateway,
        jupiter: JupiterClient,
        wallet: Wallet,
        slippage_multiplier: float = 2.0,
        slippage_flat_addon_pct: float = 0.01,
        slippage_hard_cap_pct: float = 0.10,
        slippage_emergency_cap_pct: float = 0.15,
        max_retries: int = 2,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.rpc = rpc
        self.jupiter = jupiter
        self.wallet = wallet
        self.slippage_multiplier = slippage_multiplier
        self.slippage_flat_addon_pct = slippage_flat_addon_pct
        self.slippage_hard_cap_pct = slippage_hard_cap_pct
        self.slippage_emergency_cap_pct = slippage_emergency_cap_pct
        self.max_retries = max_retries
        self.logger = logger or logging.getLogger("memebot.execution")

    def _priority_fee_lamports(self) -> int:
        try:
            fees = self.rpc.get_recent_prioritization_fees()
        except RpcOutage:
            return DEFAULT_PRIORITY_FEE_LAMPORTS
        nonzero = [f["prioritizationFee"] for f in fees if f.get("prioritizationFee", 0) > 0]
        if not nonzero:
            return DEFAULT_PRIORITY_FEE_LAMPORTS
        return int(statistics.median(nonzero))

    def _quote_with_doctrine_slippage(
        self, input_mint: str, output_mint: str, amount: int, emergency: bool
    ):
        probe = self.jupiter.quote(input_mint, output_mint, amount, slippage_bps=50)
        real_bps = slippage_bps_for_impact(
            probe.price_impact_pct,
            multiplier=self.slippage_multiplier,
            flat_addon_pct=self.slippage_flat_addon_pct,
            hard_cap_pct=self.slippage_hard_cap_pct,
            emergency=emergency,
            emergency_cap_pct=self.slippage_emergency_cap_pct,
        )
        return self.jupiter.quote(input_mint, output_mint, amount, slippage_bps=real_bps)

    def _execute_swap(self, quote, side: str, emergency: bool) -> tuple[str, dict]:
        last_exc: Optional[Exception] = None
        for attempt in range(self.max_retries + 1):
            try:
                built = self.jupiter.build_swap_transaction(
                    quote, self.wallet.pubkey_base58, priority_fee_lamports=self._priority_fee_lamports()
                )
                signed_b64 = self.wallet.sign_versioned_transaction_b64(built["swapTransaction"])
                sig = self.rpc.send_transaction(signed_b64)
                confirmed = self.rpc.confirm_signature(sig, timeout_s=45.0)
                if not confirmed:
                    raise ExecutionFailed(f"{side} tx {sig} did not confirm")
                tx = self.rpc.call(
                    "getTransaction",
                    # Read live off self.rpc -- see the matching comment on
                    # Orchestrator's indexing call site. RpcGateway bumps
                    # this in place on a -32015 "transaction version not
                    # supported" response.
                    [sig, {
                        "encoding": "jsonParsed", "commitment": "confirmed",
                        "maxSupportedTransactionVersion": self.rpc.max_supported_transaction_version,
                    }],
                )
                return sig, (tx or {})
            except Exception as exc:  # noqa: BLE001 - any failure here costs a fee and counts as an attempt
                last_exc = exc
                self.logger.warning("%s attempt %d/%d failed: %s", side, attempt + 1, self.max_retries + 1, exc)
        raise ExecutionFailed(f"{side} failed after {self.max_retries + 1} attempts: {last_exc}")

    def _extract_sol_delta(self, tx: dict) -> float:
        meta = tx.get("meta") or {}
        pre = meta.get("preBalances") or []
        post = meta.get("postBalances") or []
        if not pre or not post:
            return 0.0
        return (post[0] - pre[0]) / LAMPORTS_PER_SOL  # fee payer is account index 0

    def _extract_token_delta(self, tx: dict, mint: str) -> float:
        meta = tx.get("meta") or {}
        owner = self.wallet.pubkey_base58

        def amount_for(balances: list[dict]) -> float:
            for b in balances:
                if b.get("mint") == mint and b.get("owner") == owner:
                    return float(b.get("uiTokenAmount", {}).get("uiAmount") or 0.0)
            return 0.0

        pre = amount_for(meta.get("preTokenBalances") or [])
        post = amount_for(meta.get("postTokenBalances") or [])
        return post - pre

    def _network_fee_sol(self, tx: dict) -> float:
        meta = tx.get("meta") or {}
        fee_lamports = meta.get("fee")
        if fee_lamports is None:
            return FALLBACK_NETWORK_FEE_SOL
        return fee_lamports / LAMPORTS_PER_SOL

    def buy(self, mint: str, size_sol: float, token_decimals: int) -> Fill:
        amount_lamports = int(size_sol * LAMPORTS_PER_SOL)
        quote = self._quote_with_doctrine_slippage(SOL_MINT, mint, amount_lamports, emergency=False)
        sig, tx = self._execute_swap(quote, side="buy", emergency=False)

        token_delta = self._extract_token_delta(tx, mint)
        tokens = token_delta if token_delta > 0 else quote.out_amount / (10 ** token_decimals)
        fee_sol = self._network_fee_sol(tx)
        quote_price = size_sol / (quote.out_amount / (10 ** token_decimals)) if quote.out_amount else 0.0
        fill_price = size_sol / tokens if tokens else 0.0
        slippage_bps = ((fill_price - quote_price) / quote_price * 10_000) if quote_price else 0.0

        return Fill(
            position_id="",
            mint=mint,
            side="buy",
            quote_price_usd=quote_price,
            fill_price_usd=fill_price,
            size_sol=size_sol,
            tokens=tokens,
            fee_sol=fee_sol,
            slippage_bps=slippage_bps,
            tx_sig=sig,
        )

    def sell(
        self, position: Position, fraction: float, reason: ExitReason, token_decimals: int, emergency: bool = False
    ) -> Fill:
        tokens_to_sell_ui = position.tokens_held * position.remaining_fraction * fraction
        amount_raw = int(tokens_to_sell_ui * (10 ** token_decimals))
        quote = self._quote_with_doctrine_slippage(position.mint, SOL_MINT, amount_raw, emergency=emergency)
        sig, tx = self._execute_swap(quote, side="sell", emergency=emergency)

        sol_delta = self._extract_sol_delta(tx)
        fee_sol = self._network_fee_sol(tx)
        sol_received = sol_delta + fee_sol if sol_delta > 0 else quote.out_amount / LAMPORTS_PER_SOL
        quote_price = (quote.out_amount / LAMPORTS_PER_SOL) / tokens_to_sell_ui if tokens_to_sell_ui else 0.0
        fill_price = sol_received / tokens_to_sell_ui if tokens_to_sell_ui else 0.0
        slippage_bps = ((quote_price - fill_price) / quote_price * 10_000) if quote_price else 0.0

        return Fill(
            position_id=position.id,
            mint=position.mint,
            side="sell",
            quote_price_usd=quote_price,
            fill_price_usd=fill_price,
            size_sol=sol_received,
            tokens=tokens_to_sell_ui,
            fee_sol=fee_sol,
            slippage_bps=slippage_bps,
            tx_sig=sig,
            reason=reason.value,
        )


class PaperExecutionEngine:
    """Same interface, fills simulated off real Jupiter quotes with a 2% haircut.

    No wallet, no signing, no RPC send. `jupiter` is still hit for live
    quotes because a paper mode that doesn't see real market prices isn't
    testing anything -- only execution is fake.
    """

    def __init__(
        self,
        jupiter: JupiterClient,
        haircut_pct: float = 0.02,
        base_fee_sol: float = FALLBACK_NETWORK_FEE_SOL,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.jupiter = jupiter
        self.haircut_pct = haircut_pct
        self.base_fee_sol = base_fee_sol
        self.logger = logger or logging.getLogger("memebot.paper_execution")

    def _fake_sig(self) -> str:
        return "PAPER-" + uuid.uuid4().hex

    def buy(self, mint: str, size_sol: float, token_decimals: int) -> Fill:
        amount_lamports = int(size_sol * LAMPORTS_PER_SOL)
        quote = self.jupiter.quote(SOL_MINT, mint, amount_lamports, slippage_bps=100)
        quote_tokens = quote.out_amount / (10 ** token_decimals)
        actual_tokens = quote_tokens * (1 - self.haircut_pct)
        quote_price = size_sol / quote_tokens if quote_tokens else 0.0
        fill_price = size_sol / actual_tokens if actual_tokens else 0.0
        slippage_bps = ((fill_price - quote_price) / quote_price * 10_000) if quote_price else 0.0

        return Fill(
            position_id="",
            mint=mint,
            side="buy",
            quote_price_usd=quote_price,
            fill_price_usd=fill_price,
            size_sol=size_sol,
            tokens=actual_tokens,
            fee_sol=self.base_fee_sol,
            slippage_bps=slippage_bps,
            tx_sig=self._fake_sig(),
        )

    def sell(
        self, position: Position, fraction: float, reason: ExitReason, token_decimals: int, emergency: bool = False
    ) -> Fill:
        tokens_to_sell = position.tokens_held * position.remaining_fraction * fraction
        amount_raw = int(tokens_to_sell * (10 ** token_decimals))
        quote = self.jupiter.quote(position.mint, SOL_MINT, amount_raw, slippage_bps=100)
        quote_sol = quote.out_amount / LAMPORTS_PER_SOL
        actual_sol = quote_sol * (1 - self.haircut_pct)
        quote_price = quote_sol / tokens_to_sell if tokens_to_sell else 0.0
        fill_price = actual_sol / tokens_to_sell if tokens_to_sell else 0.0
        slippage_bps = ((quote_price - fill_price) / quote_price * 10_000) if quote_price else 0.0

        return Fill(
            position_id=position.id,
            mint=position.mint,
            side="sell",
            quote_price_usd=quote_price,
            fill_price_usd=fill_price,
            size_sol=actual_sol,
            tokens=tokens_to_sell,
            fee_sol=self.base_fee_sol,
            slippage_bps=slippage_bps,
            tx_sig=self._fake_sig(),
            reason=reason.value,
        )
