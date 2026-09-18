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
from typing import Any, Callable, Optional

import requests

from bot.jupiter_client import JupiterClient, JupiterError, SOL_MINT
from bot.models import Candidate, SafetyCheckResult, SafetyVerdict, SignalSource, WalletBuyRecord
from bot.rpc_gateway import RpcGateway, RpcError

TOKEN_2022_PROGRAM_ID = "TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb"

# pump.fun mints are vanity-searched to end in this suffix -- a cheap,
# zero-RPC heuristic for "this token's origin is pump.fun," independent of
# whether SignalEngine happened to discover it via pump.fun's own poller
# (which sets Candidate.pump_fun_graduated) or via DexScreener (which never
# does -- see check_lp_or_graduation).
PUMPFUN_MINT_SUFFIX = "pump"
PUMPFUN_COIN_INFO_URL = "https://frontend-api.pump.fun/coins/{mint}"
# Kept in sync manually with signal_engine.py's PUMPFUN_NEW_COINS_URL
# headers -- frontend-api.pump.fun sits behind Cloudflare and has been
# observed 530ing scripted clients with no browser-like User-Agent.
PUMPFUN_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://pump.fun/",
    "Origin": "https://pump.fun",
}

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

# Sentinel meaning "no pre-fetched value given -- fetch it yourself." Plain
# None is a valid, meaningful value here (mint account genuinely not found),
# so it can't double as "not provided."
_UNFETCHED = object()


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
        pumpfun_session: Optional[requests.Session] = None,
        enable_pumpfun_lookups: bool = True,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.rpc = rpc
        self.jupiter = jupiter
        self.max_top10_holder_pct = max_top10_holder_pct
        self.max_acceptable_price_impact_pct = max_acceptable_price_impact_pct
        self.bundled_launch_min_distinct_tokens = bundled_launch_min_distinct_tokens
        self.bundle_cluster_min_wallets = bundle_cluster_min_wallets
        self.rugcheck_session = rugcheck_session or requests.Session()
        # Same on/off switch as SignalEngine's enable_pumpfun_source: pump.fun's
        # API is the same unofficial, occasionally-530ing endpoint either way,
        # so one config flag disables both call sites at once.
        self.pumpfun_session = pumpfun_session or requests.Session()
        self.enable_pumpfun_lookups = enable_pumpfun_lookups
        self.logger = logger or logging.getLogger("memebot.token_safety")

        # Burn addresses and AMM program IDs never change, so once we've
        # resolved a token account's owner or an owner's controlling program,
        # that answer is good for the rest of the process. This -- combined
        # with batching lookups via get_multiple_accounts below -- is what
        # keeps a single evaluate() call from costing dozens of RPC requests.
        self._owner_cache: dict[str, Optional[str]] = {}
        self._pool_authority_cache: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # individual checks -- each returns a SafetyCheckResult, never raises
    # ------------------------------------------------------------------

    def _fetch_mint_info(self, mint: str) -> Optional[dict]:
        """Fetched once per evaluate() and shared between the mint/freeze
        and transfer-fee checks -- they used to each fetch this account
        independently, doubling that RPC cost for no reason."""
        return self.rpc.get_account_info(mint, encoding="jsonParsed")

    def check_mint_freeze_authority(self, candidate: Candidate, mint_info: Any = _UNFETCHED) -> SafetyCheckResult:
        try:
            info = self._fetch_mint_info(candidate.mint) if mint_info is _UNFETCHED else mint_info
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
            # Already confirmed un-graduated by SignalEngine's own pump.fun
            # poller this cycle -- cheapest possible case, no extra lookup.
            return SafetyCheckResult(
                "lp_burned_or_graduated", True, "pump.fun bonding curve (pre-graduation), no separate LP to rug"
            )

        if candidate.mint.endswith(PUMPFUN_MINT_SUFFIX):
            # Covers BOTH "discovered via DexScreener, so
            # Candidate.pump_fun_graduated was never set at all" (the gap
            # that made every pump.fun-origin token found via DexScreener's
            # token-profiles/boosts discovery permanently unrejectable --
            # lp_mint is never populated by parse_dexscreener_pair either)
            # AND "SignalEngine's poller already said pump_fun_graduated is
            # True." Either way, pump.fun's own API is the authoritative,
            # current source for bonding-curve/graduation state for its own
            # tokens -- ask it directly instead of rejecting a token that IS
            # pump.fun-origin just because we didn't happen to learn that
            # from SignalEngine's separate pump.fun poller this cycle.
            return self._check_pumpfun_graduation(candidate.mint)

        if not candidate.lp_mint:
            # Not pump.fun-origin (or at least doesn't look it) and we have
            # no LP mint to check burn status on. This bot currently has no
            # way to resolve a generic Raydium/Orca/Meteora pool's LP mint
            # from just a pair/pool address without decoding that DEX's own
            # binary account layout (each one is different, and none of
            # them are JSON-parseable via getAccountInfo) -- see HONESTY.md.
            # Fails closed: an unverifiable non-pump.fun launch is rejected,
            # not passed through on uncertainty, unlike the pump.fun lookup
            # above (which has an authoritative source to ask).
            return SafetyCheckResult(
                "lp_burned_or_graduated", False,
                "no LP mint known and no pump.fun heritage to resolve graduation from -- cannot "
                "currently verify LP burn status for a non-pump.fun DEX-native launch",
            )
        try:
            largest = self.rpc.get_token_largest_accounts(candidate.lp_mint)
            supply = self.rpc.get_token_supply(candidate.lp_mint)
        except RpcError as exc:
            return SafetyCheckResult("lp_burned_or_graduated", False, f"rpc error: {exc}")
        total = float(supply["amount"]) if supply else 0.0
        if total <= 0:
            return SafetyCheckResult("lp_burned_or_graduated", False, "LP mint has zero supply (unexpected)")

        owners_by_account = self._resolve_owners_batch([acct["address"] for acct in largest])
        burned_or_locked = sum(
            float(acct["amount"]) for acct in largest if owners_by_account.get(acct["address"]) in BURN_ADDRESSES
        )
        locked_pct = burned_or_locked / total
        if locked_pct < 0.80:
            return SafetyCheckResult(
                "lp_burned_or_graduated", False, f"only {locked_pct:.1%} of LP burned/locked (need >= 80%)"
            )
        return SafetyCheckResult("lp_burned_or_graduated", True, f"{locked_pct:.1%} of LP burned/locked")

    def _check_pumpfun_graduation(self, mint: str) -> SafetyCheckResult:
        """Ask pump.fun's own (unofficial) API whether this specific mint
        has graduated. Authoritative for pump.fun-origin tokens, and much
        cheaper than decoding a Raydium pool account's binary layout to
        answer the same question generically (see check_lp_or_graduation's
        non-pump.fun branch, which can't do that).

        A lookup failure is deliberately NOT a reject: the API is
        unofficial and known to 530 under Cloudflare for stretches (see
        SignalEngine's own pump.fun handling), and pump.fun is not a source
        an attacker can reliably force offline just to slip a bad token
        past this specific check -- every other check (holder
        concentration, mint/freeze authority, honeypot route, price
        impact, ...) still applies regardless. A failure here means
        "unknown right now," not "assume it's fine forever": the verdict
        cache's TTL (see Orchestrator) means this candidate gets a fresh
        attempt on a later cycle rather than being permanently blocked by
        one bad request.
        """
        if not self.enable_pumpfun_lookups:
            return SafetyCheckResult(
                "lp_burned_or_graduated", False,
                "pump.fun lookups disabled via config (enable_pumpfun_source=False) -- cannot resolve graduation",
            )
        try:
            resp = self.pumpfun_session.get(
                PUMPFUN_COIN_INFO_URL.format(mint=mint), headers=PUMPFUN_BROWSER_HEADERS, timeout=5.0
            )
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            return SafetyCheckResult(
                "lp_burned_or_graduated", True,
                f"pump.fun graduation lookup failed ({exc}) -- treating as unknown, not a reject; "
                "will re-check on a later cycle",
            )
        if not bool(data.get("complete", False)):
            return SafetyCheckResult(
                "lp_burned_or_graduated", True,
                "pump.fun bonding curve (pre-graduation, confirmed via API), no separate LP to rug",
            )
        return SafetyCheckResult(
            "lp_burned_or_graduated", True,
            "pump.fun graduated to Raydium (confirmed via API) -- pump.fun's migration burns the LP automatically",
        )

    def _resolve_owners_batch(self, token_account_addresses: list[str]) -> dict[str, Optional[str]]:
        """{token_account_address: owner_or_None}, in ONE getMultipleAccounts
        call for whatever isn't already cached -- see __init__'s comment on
        why this is safe to cache for the life of the process."""
        to_fetch = [a for a in dict.fromkeys(token_account_addresses) if a not in self._owner_cache]
        if to_fetch:
            try:
                infos = self.rpc.get_multiple_accounts(to_fetch)
            except RpcError:
                infos = [None] * len(to_fetch)
            for addr, info in zip(to_fetch, infos):
                owner = info.get("data", {}).get("parsed", {}).get("info", {}).get("owner") if info else None
                self._owner_cache[addr] = owner
        return {addr: self._owner_cache.get(addr) for addr in token_account_addresses}

    def _resolve_pool_authorities_batch(self, owner_addresses: list[str]) -> dict[str, bool]:
        """{owner_address: is_a_known_amm_pool_authority}, batched the same way."""
        unique = list(dict.fromkeys(owner_addresses))
        to_fetch = [a for a in unique if a not in self._pool_authority_cache]
        if to_fetch:
            try:
                infos = self.rpc.get_multiple_accounts(to_fetch)
            except RpcError:
                infos = [None] * len(to_fetch)
            for addr, info in zip(to_fetch, infos):
                self._pool_authority_cache[addr] = bool(info and info.get("owner") in KNOWN_AMM_PROGRAM_IDS)
        return {addr: self._pool_authority_cache.get(addr, False) for addr in unique}

    def check_holder_concentration(self, candidate: Candidate) -> SafetyCheckResult:
        try:
            largest = self.rpc.get_token_largest_accounts(candidate.mint)
            supply = self.rpc.get_token_supply(candidate.mint)
        except RpcError as exc:
            return SafetyCheckResult("holder_concentration", False, f"rpc error: {exc}")
        total = float(supply["amount"]) if supply else 0.0
        if total <= 0:
            return SafetyCheckResult("holder_concentration", False, "token has zero supply (unexpected)")

        # Two batched calls total (owners, then pool-authority-of-owners)
        # instead of up to ~40 individual getAccountInfo calls -- this is
        # the single biggest RPC cost in the whole safety pipeline, and the
        # main reason a handful of candidates could blow the Helius free
        # tier's budget in seconds.
        owners_by_account = self._resolve_owners_batch([acct["address"] for acct in largest])
        candidate_owners = [o for o in owners_by_account.values() if o is not None and o not in BURN_ADDRESSES]
        pool_flags = self._resolve_pool_authorities_batch(candidate_owners)

        counted = 0.0
        kept = 0
        for acct in largest:
            owner = owners_by_account.get(acct["address"])
            if owner is None or owner in BURN_ADDRESSES:
                continue
            if pool_flags.get(owner, False):
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

    def check_transfer_fee_extension(self, candidate: Candidate, mint_info: Any = _UNFETCHED) -> SafetyCheckResult:
        try:
            info = self._fetch_mint_info(candidate.mint) if mint_info is _UNFETCHED else mint_info
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

    def check_honeypot(self, candidate: Candidate, expected_tokens_out: int, mint_info: Any = _UNFETCHED) -> SafetyCheckResult:
        """A proxy for "can this actually be sold," not a true pre-buy
        simulation -- see JupiterClient.check_sell_route's docstring for
        why the old simulateTransaction-based check could never pass on
        ANY token (it needs a funded token account we don't have before
        buying). Three cheap, free-tier-affordable signals, in order:

          1. Jupiter quotes a sell-side route with positive SOL out
             (check_sell_route -- a price lookup, doesn't require holding
             the token).
          2. When the discovery source reports trade activity (DexScreener
             only -- pump.fun's coin feed doesn't expose 5m buy/sell
             counts), at least one real sell happened in the last 5
             minutes. All-buys-no-sells inside a window is exactly what a
             sell-blocking honeypot looks like from the outside.
          3. No active Token-2022 transferHook extension on the mint.
             Arbitrary hook logic on every transfer is the most common
             real honeypot mechanism on current Solana token launches --
             it can silently revert a sell that a quote alone would never
             catch, since a quote never actually invokes the hook. Reuses
             the same mint_info evaluate() already fetched for the
             mint/freeze-authority and transfer-fee checks, so this costs
             zero extra RPC calls.

        None of this proves a token is safe to sell -- it proves the
        cheapest signals available on a free RPC tier didn't fire. See
        HONESTY.md for what this trades away versus a true simulation.
        """
        ok, detail, _ = self.jupiter.check_sell_route(candidate.mint, expected_tokens_out)
        if not ok:
            return SafetyCheckResult("honeypot", False, detail)

        if candidate.source == SignalSource.DEXSCREENER and candidate.sells_5m <= 0:
            return SafetyCheckResult(
                "honeypot", False,
                "sell route exists but zero observed sells in the last 5m (DexScreener) -- "
                "indistinguishable from a sell-blocking honeypot from here",
            )

        try:
            info = self._fetch_mint_info(candidate.mint) if mint_info is _UNFETCHED else mint_info
        except RpcError:
            info = None
        if info and self._has_active_transfer_hook(info):
            return SafetyCheckResult(
                "honeypot", False, "Token-2022 transferHook extension active -- can silently block sells"
            )

        volume_note = (
            f"{candidate.sells_5m} sells/5m" if candidate.source == SignalSource.DEXSCREENER
            else "sell-volume data unavailable (pump.fun)"
        )
        return SafetyCheckResult("honeypot", True, f"{detail}, {volume_note}")

    @staticmethod
    def _has_active_transfer_hook(mint_info: dict) -> bool:
        if mint_info.get("owner") != TOKEN_2022_PROGRAM_ID:
            return False  # transferHook is a Token-2022 extension; plain SPL Token can't have one
        parsed = mint_info.get("data", {}).get("parsed", {}).get("info", {})
        for ext in parsed.get("extensions", []):
            if ext.get("extension") != "transferHook":
                continue
            program_id = ext.get("state", {}).get("programId")
            if program_id and program_id != "11111111111111111111111111111111111111111":
                return True
        return False

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
        # wallet_pubkey is currently unused: check_honeypot no longer builds
        # a real swap transaction (see its docstring), which was the only
        # check that needed a pubkey. Kept in the signature since it's part
        # of Orchestrator's call contract and a future check that DOES need
        # a real wallet (e.g. a live-only, already-holding-the-token
        # simulation) would want it without another signature change.
        checks: list[SafetyCheckResult] = []

        # Fetched once and shared between the three checks that all need the
        # mint account -- see _fetch_mint_info's docstring.
        try:
            mint_info = self._fetch_mint_info(candidate.mint)
        except RpcError:
            mint_info = None

        checks.append(self.check_mint_freeze_authority(candidate, mint_info=mint_info))
        checks.append(self.check_lp_or_graduation(candidate))
        checks.append(self.check_holder_concentration(candidate))
        checks.append(self.check_transfer_fee_extension(candidate, mint_info=mint_info))

        price_impact_check, quote = self.check_price_impact(candidate, position_size_lamports)
        checks.append(price_impact_check)

        if quote is not None and quote.out_amount > 0:
            checks.append(self.check_honeypot(candidate, quote.out_amount, mint_info=mint_info))
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
