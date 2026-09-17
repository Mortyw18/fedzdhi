"""TokenSafety: the core module. Runs on EVERY candidate before any buy.

This includes candidates surfaced by our own SignalEngine AND candidates
surfaced by InsiderRadar's copy-trade logic. There are no exceptions: a
conviction leader's buy still has to clear every check here, because
copying a leader on THEIR OWN illiquid or bundled token means becoming
their exit liquidity, not sharing in their edge.

Rejection stats out of `evaluate()` are the product: if the daily report
shows we're passing almost everything, the thresholds are wrong.
"""
from __future__ import annotations

import logging
from typing import Callable, Optional

import requests

from bot.jupiter_client import JupiterClient, JupiterError, SOL_MINT
from bot.models import Candidate, SafetyCheckResult, SafetyVerdict, WalletBuyRecord
from bot.rpc_gateway import RpcGateway, RpcError

TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

BURN_ADDRESSES = {
    "1nc1nerator11111111111111111111111111111111",
    "11111111111111111111111111111111111111111",
}

# Programs whose PDAs commonly hold pool/vault balances. A top-10 holder
# whose authority is owned by one of these is a pool, not a whale, and is
# excluded from the concentration count.
KNOWN_AMM_PROGRAM_IDS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "Raydium AMM V4",
    "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK": "Raydium CLMM",
    "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc": "Orca Whirlpool",
    "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo": "Meteora DLMM",
    "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA": "pump.fun AMM",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pump.fun bonding curve",
}

RUGCHECK_URL = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"

# Type alias: given a wallet address, return how many distinct tokens it has
# realized PnL on. Supplied by InsiderRadar at call time -- TokenSafety must
# not import InsiderRadar (InsiderRadar imports TokenSafety for copy checks).
DistinctTokenLookup = Callable[[str], int]


class TokenSafety:
    def __init__(
        self,
        rpc: RpcGateway,
        jupiter: JupiterClient,
        max_top10_holder_pct: float = 0.30,
        max_acceptable_price_impact_pct: float = 0.05,
        bundled_launch_min_distinct_tokens: int = 15,
        bundle_cluster_min_wallets: int = 3,
        rugcheck_session: Optional[requests.Session] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.rpc = rpc
        self.jupiter = jupiter
        self.max_top10_holder_pct = max_top10_holder_pct
        self.max_acceptable_price_impact_pct = max_acceptable_price_impact_pct
        self.bundled_launch_min_distinct_tokens = bundled_launch_min_distinct_tokens
        self.bundle_cluster_min_wallets = bundle_cluster_min_wallets
        self.rugcheck_session = rugcheck_session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.token_safety")

    # ------------------------------------------------------------------
    # individual checks -- each returns a SafetyCheckResult, never raises
    # ------------------------------------------------------------------

    def check_mint_freeze_authority(self, candidate: Candidate) -> SafetyCheckResult:
        try:
            info = self.rpc.get_account_info(candidate.mint, encoding="jsonParsed")
        except RpcError as exc:
            return SafetyCheckResult("mint_freeze_authority", False, f"rpc error: {exc}")
        if not info:
            return SafetyCheckResult("mint_freeze_authority", False, "mint account not found")
        parsed = info.get("data", {}).get("parsed", {}).get("info", {})
        mint_authority = parsed.get("mintAuthority")
        freeze_authority = parsed.get("freezeAuthority")
        if mint_authority is not None or freeze_authority is not None:
            return SafetyCheckResult(
                "mint_freeze_authority",
                False,
                f"authority not revoked (mint={mint_authority}, freeze={freeze_authority})",
                data=parsed,
            )
        return SafetyCheckResult("mint_freeze_authority", True, "mint and freeze authority revoked")

    def check_lp_or_graduation(self, candidate: Candidate) -> SafetyCheckResult:
        if candidate.pump_fun_graduated is False:
            return SafetyCheckResult(
                "lp_burned_or_graduated", True, "pump.fun bonding curve (pre-graduation), no separate LP to rug"
            )
        if not candidate.lp_mint:
            return SafetyCheckResult(
                "lp_burned_or_graduated", False, "no LP mint known and token is not an un-graduated pump.fun token"
            )
        try:
            largest = self.rpc.get_token_largest_accounts(candidate.lp_mint)
            supply = self.rpc.get_token_supply(candidate.lp_mint)
        except RpcError as exc:
            return SafetyCheckResult("lp_burned_or_graduated", False, f"rpc error: {exc}")
        total = float(supply["amount"]) if supply else 0.0
        if total <= 0:
            return SafetyCheckResult("lp_burned_or_graduated", False, "LP mint has zero supply (unexpected)")
        burned_or_locked = 0.0
        for acct in largest:
            owner = self._resolve_token_account_owner(acct["address"])
            if owner in BURN_ADDRESSES:
                burned_or_locked += float(acct["amount"])
        locked_pct = burned_or_locked / total
        if locked_pct < 0.80:
            return SafetyCheckResult(
                "lp_burned_or_graduated", False, f"only {locked_pct:.1%} of LP burned/locked (need >= 80%)"
            )
        return SafetyCheckResult("lp_burned_or_graduated", True, f"{locked_pct:.1%} of LP burned/locked")

    def _resolve_token_account_owner(self, token_account_address: str) -> Optional[str]:
        try:
            info = self.rpc.get_account_info(token_account_address, encoding="jsonParsed")
        except RpcError:
            return None
        if not info:
            return None
        return info.get("data", {}).get("parsed", {}).get("info", {}).get("owner")

    def _owner_is_pool_authority(self, owner_address: str) -> bool:
        try:
            owner_account = self.rpc.get_account_info(owner_address, encoding="jsonParsed")
        except RpcError:
            return False
        if not owner_account:
            return False
        return owner_account.get("owner") in KNOWN_AMM_PROGRAM_IDS

    def check_holder_concentration(self, candidate: Candidate) -> SafetyCheckResult:
        try:
            largest = self.rpc.get_token_largest_accounts(candidate.mint)
            supply = self.rpc.get_token_supply(candidate.mint)
        except RpcError as exc:
            return SafetyCheckResult("holder_concentration", False, f"rpc error: {exc}")
        total = float(supply["amount"]) if supply else 0.0
        if total <= 0:
            return SafetyCheckResult("holder_concentration", False, "token has zero supply (unexpected)")

        counted = 0.0
        kept = 0
        for acct in largest:
            owner = self._resolve_token_account_owner(acct["address"])
            if owner is None:
                continue
            if owner in BURN_ADDRESSES:
                continue
            if self._owner_is_pool_authority(owner):
                continue
            counted += float(acct["amount"])
            kept += 1
            if kept >= 10:
                break

        pct = counted / total
        if pct >= self.max_top10_holder_pct:
            return SafetyCheckResult(
                "holder_concentration",
                False,
                f"top-10 non-pool holders control {pct:.1%} of supply (>= {self.max_top10_holder_pct:.0%} limit)",
            )
        return SafetyCheckResult("holder_concentration", True, f"top-10 non-pool holders control {pct:.1%} of supply")

    def check_transfer_fee_extension(self, candidate: Candidate) -> SafetyCheckResult:
        try:
            info = self.rpc.get_account_info(candidate.mint, encoding="jsonParsed")
        except RpcError as exc:
            return SafetyCheckResult("transfer_fee_tax", False, f"rpc error: {exc}")
        if not info:
            return SafetyCheckResult("transfer_fee_tax", False, "mint account not found")
        if info.get("owner") != TOKEN_2022_PROGRAM_ID:
            return SafetyCheckResult("transfer_fee_tax", True, "SPL Token (not Token-2022), no transfer-fee extension possible")
        parsed = info.get("data", {}).get("parsed", {}).get("info", {})
        extensions = parsed.get("extensions", [])
        for ext in extensions:
            if ext.get("extension") == "transferFeeConfig":
                newer = ext.get("state", {}).get("newerTransferFee", {})
                bps = int(newer.get("transferFeeBasisPoints", 0))
                if bps > 0:
                    return SafetyCheckResult(
                        "transfer_fee_tax", False, f"Token-2022 transfer fee extension active at {bps / 100:.2f}%"
                    )
        return SafetyCheckResult("transfer_fee_tax", True, "no active transfer-fee extension")

    def check_honeypot(self, candidate: Candidate, expected_tokens_out: int, wallet_pubkey: str) -> SafetyCheckResult:
        sellable, detail, _ = self.jupiter.simulate_sell(self.rpc, candidate.mint, expected_tokens_out, wallet_pubkey)
        return SafetyCheckResult("honeypot", sellable, detail)

    def check_price_impact(self, candidate: Candidate, position_size_lamports: int):
        """Returns (SafetyCheckResult, QuoteResult|None). Also feeds honeypot check's amount."""
        try:
            quote = self.jupiter.quote(SOL_MINT, candidate.mint, position_size_lamports, slippage_bps=100)
        except (JupiterError, requests.RequestException) as exc:
            return SafetyCheckResult("price_impact", False, f"no buy route found: {exc}"), None
        if quote.price_impact_pct > self.max_acceptable_price_impact_pct:
            return (
                SafetyCheckResult(
                    "price_impact",
                    False,
                    f"price impact {quote.price_impact_pct:.2%} exceeds "
                    f"{self.max_acceptable_price_impact_pct:.2%} ceiling -- pool is dust",
                ),
                quote,
            )
        return SafetyCheckResult("price_impact", True, f"price impact {quote.price_impact_pct:.2%}"), quote

    def check_bundled_launch(
        self,
        candidate: Candidate,
        first_buyers: Optional[list[WalletBuyRecord]],
        distinct_token_lookup: Optional[DistinctTokenLookup],
    ) -> SafetyCheckResult:
        if not first_buyers:
            return SafetyCheckResult(
                "bundled_launch", True, "no first-buyer data indexed yet for this pool (InsiderRadar cold start)"
            )
        creation_slot = candidate.pool_creation_slot
        if creation_slot is None:
            creation_slot = min(b.slot for b in first_buyers)
        same_slot_wallets = {b.wallet for b in first_buyers if b.slot == creation_slot}
        if len(same_slot_wallets) < self.bundle_cluster_min_wallets:
            return SafetyCheckResult("bundled_launch", True, "no same-slot buyer cluster detected at launch")

        # Bundled launch detected. Only acceptable if this is a copy-trade of a
        # wallet with a real, diversified track record -- otherwise we'd be
        # buying an insider's own token and becoming their exit liquidity.
        if not candidate.leader_wallet:
            return SafetyCheckResult(
                "bundled_launch",
                False,
                f"{len(same_slot_wallets)} wallets bought in the launch slot -- insider-controlled token, "
                "not a leader copy-trade",
            )
        distinct_count = distinct_token_lookup(candidate.leader_wallet) if distinct_token_lookup else 0
        if distinct_count < self.bundled_launch_min_distinct_tokens:
            return SafetyCheckResult(
                "bundled_launch",
                False,
                f"bundled launch ({len(same_slot_wallets)} wallets) and leader {candidate.leader_wallet} has only "
                f"{distinct_count} distinct-token history (need >= {self.bundled_launch_min_distinct_tokens})",
            )
        return SafetyCheckResult(
            "bundled_launch",
            True,
            f"bundled launch but leader has {distinct_count} distinct-token history -- treating as exempt",
        )

    def check_rugcheck_secondary(self, candidate: Candidate) -> SafetyCheckResult:
        """RugCheck score is informational only -- it never blocks a trade on its own."""
        try:
            resp = self.rugcheck_session.get(RUGCHECK_URL.format(mint=candidate.mint), timeout=5.0)
            resp.raise_for_status()
            data = resp.json()
            score = data.get("score")
            risks = [r.get("name") for r in data.get("risks", [])]
            return SafetyCheckResult("rugcheck_secondary", True, f"score={score} risks={risks}", data=data)
        except Exception as exc:  # noqa: BLE001 - purely informational, never blocks
            return SafetyCheckResult("rugcheck_secondary", True, f"rugcheck unavailable: {exc}")

    # ------------------------------------------------------------------
    # orchestration
    # ------------------------------------------------------------------

    def evaluate(
        self,
        candidate: Candidate,
        position_size_lamports: int,
        wallet_pubkey: str,
        first_buyers: Optional[list[WalletBuyRecord]] = None,
        distinct_token_lookup: Optional[DistinctTokenLookup] = None,
    ) -> SafetyVerdict:
        checks: list[SafetyCheckResult] = []

        checks.append(self.check_mint_freeze_authority(candidate))
        checks.append(self.check_lp_or_graduation(candidate))
        checks.append(self.check_holder_concentration(candidate))
        checks.append(self.check_transfer_fee_extension(candidate))

        price_impact_check, quote = self.check_price_impact(candidate, position_size_lamports)
        checks.append(price_impact_check)

        if quote is not None and quote.out_amount > 0:
            checks.append(self.check_honeypot(candidate, quote.out_amount, wallet_pubkey))
        else:
            checks.append(SafetyCheckResult("honeypot", False, "skipped: no valid buy quote to size the sell test"))

        checks.append(self.check_bundled_launch(candidate, first_buyers, distinct_token_lookup))
        checks.append(self.check_rugcheck_secondary(candidate))

        passed = all(c.passed for c in checks)
        verdict = SafetyVerdict(mint=candidate.mint, passed=passed, checks=checks)

        if not passed:
            self.logger.info(
                "safety_reject",
                extra={"fields": {"mint": candidate.mint, "reasons": verdict.rejection_reasons}},
            )
        return verdict
