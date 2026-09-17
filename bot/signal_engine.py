"""SignalEngine: our own trend-following candidate discovery.

Two sources, budget-aware polling:
  - DexScreener: liquidity/volume/velocity screening on Raydium/Orca/Meteora
    pairs that have already established some trading history.
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
from typing import Awaitable, Callable, Optional, Union

import requests

from bot.models import Candidate, SignalSource

DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search"
DEXSCREENER_TOKENS_URL = "https://api.dexscreener.com/latest/dex/tokens/{mint}"
PUMPFUN_NEW_COINS_URL = "https://frontend-api.pump.fun/coins"

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
        dexscreener_query: str = "solana",
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
        self.dexscreener_query = dexscreener_query
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.signal_engine")
        self._seen_mints: set[str] = set()

        # pump.fun's API is unofficial, unauthenticated, and has been known
        # to sit behind Cloudflare returning 5xx (530 = origin unreachable)
        # for scripted clients for extended periods. A pump.fun outage must
        # never take down DexScreener polling, InsiderRadar, or the rest of
        # the bot -- so after enough consecutive failures we stop polling it
        # for this run (one clear log line) instead of hammering a dead
        # endpoint on every cycle forever.
        self.pumpfun_consecutive_failures = 0
        self.pumpfun_disabled = False

    # ------------------------------------------------------------------
    # DexScreener
    # ------------------------------------------------------------------

    def fetch_dexscreener_pairs(self) -> list[dict]:
        resp = self.session.get(
            DEXSCREENER_SEARCH_URL, params={"q": self.dexscreener_query}, timeout=10.0
        )
        resp.raise_for_status()
        data = resp.json()
        pairs = data.get("pairs") or []
        return [p for p in pairs if p.get("chainId") == "solana"]

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
        if candidate.pool_age_s > self.max_pool_age_s:
            return False, f"pool age {candidate.pool_age_s / 3600:.1f}h exceeds {self.max_pool_age_s / 3600:.0f}h"
        if not (self.min_pool_liquidity_usd <= candidate.liquidity_usd <= self.max_pool_liquidity_usd):
            return False, (
                f"liquidity ${candidate.liquidity_usd:,.0f} outside "
                f"[${self.min_pool_liquidity_usd:,.0f}, ${self.max_pool_liquidity_usd:,.0f}]"
            )
        if candidate.liquidity_usd <= 0:
            return False, "zero liquidity"
        vol_liq_ratio = candidate.volume_5m_usd / candidate.liquidity_usd
        if vol_liq_ratio < self.min_volume_liquidity_ratio:
            return False, f"5m volume/liquidity ratio {vol_liq_ratio:.2f} below {self.min_volume_liquidity_ratio:.2f}"
        if candidate.buy_sell_ratio < self.min_buy_sell_ratio:
            return False, f"buy/sell ratio {candidate.buy_sell_ratio:.2f} below {self.min_buy_sell_ratio:.2f}"
        return True, "passes trend filters"

    def fetch_candidate_by_mint(self, mint: str) -> Optional[Candidate]:
        """Fresh single-token lookup, used for pricing an insider copy-trade signal."""
        try:
            resp = self.session.get(DEXSCREENER_TOKENS_URL.format(mint=mint), timeout=10.0)
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

    def poll_dexscreener_once(self) -> list[Candidate]:
        try:
            raw_pairs = self.fetch_dexscreener_pairs()
        except (requests.RequestException, ValueError) as exc:
            self.logger.warning("dexscreener poll failed: %s", exc)
            return []
        out: list[Candidate] = []
        for raw in raw_pairs:
            candidate = self.parse_dexscreener_pair(raw)
            if candidate is None:
                continue
            ok, reason = self.passes_filters(candidate)
            if ok:
                out.append(candidate)
            else:
                self.logger.debug("dexscreener candidate filtered: %s (%s)", candidate.mint, reason)
        return out

    # ------------------------------------------------------------------
    # pump.fun
    # ------------------------------------------------------------------

    def fetch_pumpfun_new_coins(self, limit: int = 50) -> list[dict]:
        resp = self.session.get(
            PUMPFUN_NEW_COINS_URL,
            params={"offset": 0, "limit": limit, "sort": "created_timestamp", "order": "DESC"},
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
        result = callback(candidate)
        if asyncio.iscoroutine(result):
            await result

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
