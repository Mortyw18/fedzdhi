"""SignalEngine: our own trend-following candidate discovery.

Two sources, budget-aware polling:
  - DexScreener: liquidity/volume/velocity screening on Raydium/Orca/Meteora
    pairs. Discovery itself is now THREE DexScreener endpoints, not one --
    see poll_dexscreener_once's docstring for why /search alone silently
    starved the bot of every real signal.
  - pump.fun: brand-new bonding-curve launches, before they'd even show up
    on DexScreener. pump.fun's public API is unofficial and can change
    without notice -- every call here is wrapped so a schema change or
    outage degrades to "skip this cycle, log it" rather than crashing.

Every threshold is a Config field. If the daily report later shows we
reject almost nothing, these are the numbers to tighten.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import Counter
from typing import Awaitable, Callable, Optional, Union

import requests

from bot.models import Candidate, SignalSource

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{mints}"
# Newest token listings and currently-boosted (paid-promotion) tokens across
# every chain DexScreener indexes -- unlike /search (a keyword text match),
# these two actually surface freshly launched / currently-trending pools.
# Neither carries pool/liquidity data itself; DEXSCREENER_TOKENS_URL resolves
# the addresses they return to real pairs.
DEXSCREENER_TOKEN_PROFILES_URL = "https://api.dexscreener.com/token-profiles/latest/v1"
DEXSCREENER_TOKEN_BOOSTS_URL = "https://api.dexscreener.com/token-boosts/latest/v1"
PUMPFUN_NEW_COINS_URL = "https://frontend-api.pump.fun/coins"

DEFAULT_DEXSCREENER_SEARCH_QUERIES = ("SOL", "pump", "bonk", "meme")

CandidateCallback = Callable[[Candidate], Union[None, Awaitable[None]]]


class SignalEngine:
    def __init__(
        self,
        min_pool_liquidity_usd: float,
        max_pool_liquidity_usd: float,
        max_pool_age_s: float,
        min_volume_liquidity_ratio: float,
        min_buy_sell_ratio: float,
        dexscreener_poll_interval_s: float = 45.0,
        pumpfun_poll_interval_s: float = 20.0,
        pumpfun_max_consecutive_failures: int = 5,
        # /search is a keyword text search over token name/symbol/address,
        # NOT a chain filter, and -- the second bug found after fixing the
        # first -- it also only really indexes pairs that already have
        # trading history and relevance ranking behind them. An overnight
        # run querying "SOL" logged "solana_pairs": 15 every cycle but
        # "passed_filters": 0, 100% rejected by pool_age -- every single
        # result /search returned was already older than the 72h freshness
        # window. A brand-new pool essentially never ranks in a text search
        # yet. /search is kept here as a supplementary trend signal (several
        # queries, not one), but token-profiles/token-boosts below are now
        # the primary discovery path specifically because they list pools
        # by recency/promotion, not by search relevance.
        dexscreener_search_queries: Optional[list[str]] = None,
        enable_pumpfun_source: bool = True,
        session: Optional[requests.Session] = None,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.min_pool_liquidity_usd = min_pool_liquidity_usd
        self.max_pool_liquidity_usd = max_pool_liquidity_usd
        self.max_pool_age_s = max_pool_age_s
        self.min_volume_liquidity_ratio = min_volume_liquidity_ratio
        self.min_buy_sell_ratio = min_buy_sell_ratio
        self.dexscreener_poll_interval_s = dexscreener_poll_interval_s
        self.pumpfun_poll_interval_s = pumpfun_poll_interval_s
        self.pumpfun_max_consecutive_failures = pumpfun_max_consecutive_failures
        self.dexscreener_search_queries: list[str] = (
            list(dexscreener_search_queries) if dexscreener_search_queries else list(DEFAULT_DEXSCREENER_SEARCH_QUERIES)
        )
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.signal_engine")
        self._seen_mints: set[str] = set()
        # Lifetime poll counts, purely for the heartbeat log -- "0 signals
        # for 10 hours" looked identical to "the bot is quietly idle" until
        # these existed; now polls_done=0 in the heartbeat is a completely
        # different, immediately diagnosable signal from polls_done=1200.
        self.dexscreener_polls_done = 0
        self.pumpfun_polls_done = 0

        # pump.fun's API is unofficial, unauthenticated, and has been known
        # to sit behind Cloudflare returning 5xx (530 = origin unreachable)
        # for scripted clients for extended periods. A pump.fun outage must
        # never take down DexScreener polling, InsiderRadar, or the rest of
        # the bot -- so after enough consecutive failures we stop polling it
        # for this run (one clear log line) instead of hammering a dead
        # endpoint on every cycle forever.
        self.pumpfun_consecutive_failures = 0
        # enable_pumpfun_source=False and pumpfun_disabled=True look similar
        # but mean different things: the former is an operator's own choice
        # (config), logged once at startup; the latter is this engine giving
        # up on its own after real failures. Both result in a no-op poll.
        self.pumpfun_disabled = not enable_pumpfun_source
        if not enable_pumpfun_source:
            self.logger.warning("pump.fun source disabled via config (enable_pumpfun_source=False)")

    # ------------------------------------------------------------------
    # DexScreener
    # ------------------------------------------------------------------

    def fetch_dexscreener_token_profiles(self) -> list[dict]:
        """The newest token listings DexScreener knows about, across every
        chain it indexes -- this is the actual "what just launched" feed;
        /search's relevance ranking essentially never surfaces a pool this
        young (see poll_dexscreener_once). Each item carries chainId +
        tokenAddress but no pool/liquidity data of its own."""
        resp = self.session.get(DEXSCREENER_TOKEN_PROFILES_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def fetch_dexscreener_token_boosts(self) -> list[dict]:
        """Tokens currently paying for DexScreener's boost/promotion --
        a cheap "trending right now" signal, independent of how new the
        pool is. Same shape as token-profiles (chainId + tokenAddress,
        no pool data)."""
        resp = self.session.get(DEXSCREENER_TOKEN_BOOSTS_URL, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else []

    def fetch_dexscreener_pairs_for_tokens(self, mints: list[str]) -> list[dict]:
        """Resolve token-profile/token-boost addresses to their actual
        trading pairs (liquidity, volume, age) -- up to 30 addresses per
        request, chunked."""
        out: list[dict] = []
        for i in range(0, len(mints), 30):
            chunk = mints[i:i + 30]
            resp = self.session.get(DEXSCREENER_TOKENS_URL.format(mints=",".join(chunk)), timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
            out.extend(data.get("pairs") or [])
        return out

    def fetch_dexscreener_search_pairs(self, query: str) -> list[dict]:
        """Keyword text search -- a supplementary trend signal (several
        queries, see dexscreener_search_queries), not the primary discovery
        path. /search ranks by relevance/volume, so it reliably returns
        pairs that already have trading history; it is NOT how brand-new
        pools get found (see poll_dexscreener_once's docstring for the
        incident that established this)."""
        resp = self.session.get(DEXSCREENER_SEARCH_URL, params={"q": query}, timeout=10.0)
        resp.raise_for_status()
        data = resp.json()
        return data.get("pairs") or []

    @staticmethod
    def parse_dexscreener_pair(raw: dict) -> Optional[Candidate]:
        base = raw.get("baseToken") or {}
        mint = base.get("address")
        if not mint:
            return None
        liquidity = raw.get("liquidity") or {}
        volume = raw.get("volume") or {}
        txns = raw.get("txns") or {}
        m5 = txns.get("m5") or {}
        created_at_ms = raw.get("pairCreatedAt")
        return Candidate(
            mint=mint,
            symbol=base.get("symbol", "?"),
            source=SignalSource.DEXSCREENER,
            pair_address=raw.get("pairAddress"),
            dex=raw.get("dexId"),
            liquidity_usd=float(liquidity.get("usd") or 0.0),
            volume_5m_usd=float(volume.get("m5") or 0.0),
            volume_1h_usd=float(volume.get("h1") or 0.0),
            buys_5m=int(m5.get("buys") or 0),
            sells_5m=int(m5.get("sells") or 0),
            price_usd=float(raw.get("priceUsd") or 0.0),
            pool_created_at=(created_at_ms / 1000.0) if created_at_ms else None,
        )

    def passes_filters(self, candidate: Candidate) -> tuple[bool, str]:
        """Returns (passed, reason). `reason` always starts with a stable,
        colon-delimited bucket code (e.g. "pool_age:", "liquidity_range:")
        followed by the human-readable detail, so poll_dexscreener_once's
        funnel log can count rejections by bucket without re-parsing
        free-form English."""
        if candidate.pool_age_s > self.max_pool_age_s:
            return False, f"pool_age: {candidate.pool_age_s / 3600:.1f}h exceeds {self.max_pool_age_s / 3600:.0f}h"
        if candidate.liquidity_usd <= 0:
            return False, "zero_liquidity: no liquidity reported"
        if not (self.min_pool_liquidity_usd <= candidate.liquidity_usd <= self.max_pool_liquidity_usd):
            return False, (
                f"liquidity_range: ${candidate.liquidity_usd:,.0f} outside "
                f"[${self.min_pool_liquidity_usd:,.0f}, ${self.max_pool_liquidity_usd:,.0f}]"
            )
        vol_liq_ratio = candidate.volume_5m_usd / candidate.liquidity_usd
        if vol_liq_ratio < self.min_volume_liquidity_ratio:
            return False, f"volume_liquidity_ratio: {vol_liq_ratio:.2f} below {self.min_volume_liquidity_ratio:.2f}"
        if candidate.buy_sell_ratio < self.min_buy_sell_ratio:
            return False, f"buy_sell_ratio: {candidate.buy_sell_ratio:.2f} below {self.min_buy_sell_ratio:.2f}"
        return True, "ok: passes trend filters"

    def fetch_candidate_by_mint(self, mint: str) -> Optional[Candidate]:
        """Fresh single-token lookup, used for pricing an insider copy-trade signal."""
        try:
            resp = self.session.get(DEXSCREENER_TOKENS_URL.format(mints=mint), timeout=10.0)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            self.logger.warning("dexscreener token lookup failed for %s: %s", mint, exc)
            return None
        pairs = [p for p in (data.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pairs:
            return None
        best = max(pairs, key=lambda p: float((p.get("liquidity") or {}).get("usd") or 0.0))
        return self.parse_dexscreener_pair(best)

    def _fetch_source(self, label: str, errors: dict[str, str], fn, *args) -> list:
        """Run one DexScreener source fetch; on failure, record it in
        `errors` and return [] instead of raising -- one dead endpoint
        (say, token-boosts having an outage) must never take down the
        other sources in the same poll cycle."""
        try:
            return fn(*args)
        except (requests.RequestException, ValueError) as exc:
            errors[label] = str(exc)
            return []

    def poll_dexscreener_once(self) -> list[Candidate]:
        """Discovery is three DexScreener sources, not one:

          1. token-profiles/latest + token-boosts/latest -- the newest and
             currently-trending listings, resolved to real pairs via the
             tokens/{addresses} endpoint. This is the PRIMARY discovery
             path: it lists by recency/promotion, so a pool minutes old
             actually shows up here.
          2. /search, run over several configured query terms -- a
             supplementary trend signal. /search ranks by relevance and
             essentially never returns a pool young enough to pass the
             pool_age filter (an overnight run confirmed this: 15
             solana_pairs every cycle, 0 passed_filters, 100% rejected by
             pool_age -- every result was already stale by the time
             /search surfaced it).

        Every cycle still ends with one INFO-level funnel log (raw pairs ->
        on-chain pairs -> parseable -> pass/fail per threshold bucket), so
        "0 candidates" stays diagnosable from the logs alone -- the gap
        that let a bad discovery strategy run for 10 hours unnoticed."""
        self.dexscreener_polls_done += 1
        errors: dict[str, str] = {}

        profiles = self._fetch_source("profiles", errors, self.fetch_dexscreener_token_profiles)
        boosts = self._fetch_source("boosts", errors, self.fetch_dexscreener_token_boosts)
        profile_mints = {p.get("tokenAddress") for p in profiles if p.get("chainId") == "solana" and p.get("tokenAddress")}
        boost_mints = {b.get("tokenAddress") for b in boosts if b.get("chainId") == "solana" and b.get("tokenAddress")}
        new_token_mints = sorted(profile_mints | boost_mints)

        resolved_pairs = (
            self._fetch_source("resolve", errors, self.fetch_dexscreener_pairs_for_tokens, new_token_mints)
            if new_token_mints
            else []
        )

        search_pairs: list[dict] = []
        for query in self.dexscreener_search_queries:
            search_pairs.extend(self._fetch_source(f"search:{query}", errors, self.fetch_dexscreener_search_pairs, query))

        # Dedupe across all three sources by pair address (falling back to
        # the base mint) -- the same pool can easily show up via both a
        # boosted-token resolve AND a search hit.
        solana_pairs_by_key: dict[str, dict] = {}
        for raw in resolved_pairs + search_pairs:
            if raw.get("chainId") != "solana":
                continue
            key = raw.get("pairAddress") or (raw.get("baseToken") or {}).get("address")
            if key:
                solana_pairs_by_key[key] = raw
        solana_pairs = list(solana_pairs_by_key.values())

        out: list[Candidate] = []
        unparseable = 0
        rejection_buckets: Counter[str] = Counter()
        for raw in solana_pairs:
            candidate = self.parse_dexscreener_pair(raw)
            if candidate is None:
                unparseable += 1
                continue
            ok, reason = self.passes_filters(candidate)
            if ok:
                out.append(candidate)
            else:
                bucket = reason.split(":", 1)[0]
                rejection_buckets[bucket] += 1
                self.logger.debug("dexscreener candidate filtered: %s (%s)", candidate.mint, reason)

        self.logger.info(
            "dexscreener_poll_summary",
            extra={
                "fields": {
                    "queries": list(self.dexscreener_search_queries),
                    "profiles_seen": len(profiles),
                    "profiles_solana": len(profile_mints),
                    "boosts_seen": len(boosts),
                    "boosts_solana": len(boost_mints),
                    "resolved_tokens": len(new_token_mints),
                    "resolved_pairs": len(resolved_pairs),
                    "search_pairs": len(search_pairs),
                    "raw_pairs": len(resolved_pairs) + len(search_pairs),
                    "solana_pairs": len(solana_pairs),
                    "unparseable": unparseable,
                    "passed_filters": len(out),
                    "rejected_by": dict(rejection_buckets),
                    **({"errors": errors} if errors else {}),
                }
            },
        )
        if errors:
            self.logger.warning("dexscreener poll had %d source failure(s) this cycle: %s", len(errors), errors)
        return out

    # ------------------------------------------------------------------
    # pump.fun
    # ------------------------------------------------------------------

    def fetch_pumpfun_new_coins(self, limit: int = 50) -> list[dict]:
        # frontend-api.pump.fun sits behind Cloudflare and has been observed
        # returning 530 (origin unreachable) for requests with no browser-like
        # headers -- a default `requests` User-Agent is an easy tell for a
        # scripted client. These headers are a best effort, not a guarantee:
        # a genuine origin outage (which 530 usually means) isn't fixable by
        # spoofing a browser, and enable_pumpfun_source=False is the reliable
        # fallback if this keeps failing (see poll_pumpfun_once's own
        # consecutive-failure circuit breaker for the automatic version).
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://pump.fun/",
            "Origin": "https://pump.fun",
        }
        resp = self.session.get(
            PUMPFUN_NEW_COINS_URL,
            params={"offset": 0, "limit": limit, "sort": "created_timestamp", "order": "DESC"},
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        data = resp.json()
        return data if isinstance(data, list) else data.get("coins", [])

    @staticmethod
    def parse_pumpfun_coin(raw: dict) -> Optional[Candidate]:
        mint = raw.get("mint")
        if not mint:
            return None
        created_ms = raw.get("created_timestamp")
        virtual_sol_reserves = float(raw.get("virtual_sol_reserves") or 0.0) / 1e9
        # Rough USD liquidity proxy: bonding-curve SOL reserves both sides is
        # what actually backs the curve; this is intentionally approximate.
        return Candidate(
            mint=mint,
            symbol=raw.get("symbol", "?"),
            source=SignalSource.PUMPFUN,
            dex="pumpfun",
            liquidity_usd=virtual_sol_reserves * float(raw.get("sol_price_usd") or 0.0) * 2,
            price_usd=float(raw.get("usd_market_cap") or 0.0) / max(float(raw.get("total_supply") or 1.0), 1.0),
            pool_created_at=(created_ms / 1000.0) if created_ms else time.time(),
            pump_fun_graduated=bool(raw.get("complete", False)),
        )

    def poll_pumpfun_once(self) -> list[Candidate]:
        if self.pumpfun_disabled:
            return []
        self.pumpfun_polls_done += 1
        try:
            raw_coins = self.fetch_pumpfun_new_coins()
        except Exception as exc:  # noqa: BLE001 - unofficial API, any failure shape must degrade, never crash
            self.pumpfun_consecutive_failures += 1
            self.logger.warning(
                "pump.fun poll failed (%d/%d consecutive failures): %s",
                self.pumpfun_consecutive_failures,
                self.pumpfun_max_consecutive_failures,
                exc,
            )
            if self.pumpfun_consecutive_failures >= self.pumpfun_max_consecutive_failures:
                self.pumpfun_disabled = True
                self.logger.error(
                    "pump.fun source disabled for the rest of this run after %d consecutive failures -- "
                    "DexScreener signals and InsiderRadar indexing are unaffected. Restart the bot to retry.",
                    self.pumpfun_consecutive_failures,
                )
            return []
        self.pumpfun_consecutive_failures = 0
        out: list[Candidate] = []
        for raw in raw_coins:
            candidate = self.parse_pumpfun_coin(raw)
            if candidate is None or candidate.mint in self._seen_mints:
                continue
            self._seen_mints.add(candidate.mint)
            out.append(candidate)
        return out

    # ------------------------------------------------------------------
    # orchestration
    # ------------------------------------------------------------------

    async def _dispatch(self, callback: CandidateCallback, candidate: Candidate) -> None:
        """A sync callback (Orchestrator._on_candidate, in practice) runs in
        the default executor, NOT directly on this event loop.

        evaluate_candidate() makes several blocking `requests` calls
        (TokenSafety's RPC/Jupiter/rugcheck/pump.fun-graduation lookups) --
        calling it straight from here would block the entire event loop for
        however long that takes, which starves EVERYTHING else sharing it,
        including RpcWebSocket's ping/pong keepalive and its next
        logsSubscribe frame read. That starvation is the leading suspect
        for a WebSocket dropping with close code 1011 (a server-side
        "you stopped responding" timeout) under load: it looks like Helius's
        side, but it can just as easily be ours. Running the callback off
        the loop keeps candidate evaluation from ever being able to cause
        that, regardless of how slow a single evaluate() call is.

        A coroutine-function callback (none currently exist, but the type
        alias has always allowed one) is awaited directly instead, since it
        cooperates with the loop by construction and forcing it into a
        thread would be pointless.
        """
        if asyncio.iscoroutinefunction(callback):
            await callback(candidate)
        else:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, callback, candidate)

    async def run_dexscreener_loop(self, callback: CandidateCallback, stop_event: Optional[asyncio.Event] = None) -> None:
        loop = asyncio.get_running_loop()
        while stop_event is None or not stop_event.is_set():
            candidates = await loop.run_in_executor(None, self.poll_dexscreener_once)
            for c in candidates:
                await self._dispatch(callback, c)
            await asyncio.sleep(self.dexscreener_poll_interval_s)

    async def run_pumpfun_loop(self, callback: CandidateCallback, stop_event: Optional[asyncio.Event] = None) -> None:
        loop = asyncio.get_running_loop()
        while stop_event is None or not stop_event.is_set():
            candidates = await loop.run_in_executor(None, self.poll_pumpfun_once)
            for c in candidates:
                await self._dispatch(callback, c)
            await asyncio.sleep(self.pumpfun_poll_interval_s)
