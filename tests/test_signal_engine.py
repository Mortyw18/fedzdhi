"""SignalEngine: pump.fun must be a gracefully optional source.

pump.fun's API is unofficial, unauthenticated, and known to sit behind
Cloudflare returning 5xx (530 = origin unreachable) for scripted clients
for extended stretches. None of that -- an HTTP error, a malformed
response, anything -- may ever crash the bot or block DexScreener /
InsiderRadar. After enough consecutive failures, polling it should stop
altogether rather than hammering a dead endpoint forever.
"""
from __future__ import annotations

import time

import requests

from bot.models import SignalSource
from bot.signal_engine import SignalEngine


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Server Error: for url: fake")

    def json(self):
        return self._payload


class _FlakySession:
    """Simulates a pump.fun outage: every .get() raises/returns an error
    until `succeeds_after` calls, then returns healthy JSON."""

    def __init__(self, status_code: int = 530, succeeds_after: int | None = None):
        self.status_code = status_code
        self.succeeds_after = succeeds_after
        self.call_count = 0

    def get(self, url, params=None, headers=None, timeout=None):
        self.call_count += 1
        if self.succeeds_after is not None and self.call_count > self.succeeds_after:
            return _FakeResponse(200, payload=[])
        return _FakeResponse(self.status_code)


def _engine(session, max_failures: int = 5) -> SignalEngine:
    return SignalEngine(
        min_pool_liquidity_usd=15_000,
        max_pool_liquidity_usd=400_000,
        max_pool_age_s=72 * 3600,
        min_volume_liquidity_ratio=0.20,
        min_buy_sell_ratio=1.5,
        pumpfun_max_consecutive_failures=max_failures,
        session=session,
    )


def test_pumpfun_530_does_not_raise():
    engine = _engine(_FlakySession(status_code=530))
    candidates = engine.poll_pumpfun_once()  # must not raise
    assert candidates == []
    assert engine.pumpfun_consecutive_failures == 1
    assert engine.pumpfun_disabled is False


def test_pumpfun_generic_exception_does_not_raise():
    class _ExplodingSession:
        def get(self, *args, **kwargs):
            raise RuntimeError("something pump.fun's API never documented")

    engine = _engine(_ExplodingSession())
    candidates = engine.poll_pumpfun_once()  # must not raise, even for an unanticipated exception type
    assert candidates == []
    assert engine.pumpfun_consecutive_failures == 1


def test_pumpfun_disables_after_max_consecutive_failures():
    session = _FlakySession(status_code=530)
    engine = _engine(session, max_failures=3)

    for _ in range(3):
        assert engine.poll_pumpfun_once() == []

    assert engine.pumpfun_disabled is True
    assert session.call_count == 3

    # Further polls must not hit the network at all once disabled.
    engine.poll_pumpfun_once()
    engine.poll_pumpfun_once()
    assert session.call_count == 3


def test_pumpfun_recovers_before_disable_threshold():
    session = _FlakySession(status_code=530, succeeds_after=2)
    engine = _engine(session, max_failures=5)

    engine.poll_pumpfun_once()  # fails (1)
    engine.poll_pumpfun_once()  # fails (2)
    assert engine.pumpfun_consecutive_failures == 2

    engine.poll_pumpfun_once()  # succeeds -> resets counter
    assert engine.pumpfun_consecutive_failures == 0
    assert engine.pumpfun_disabled is False


def test_dexscreener_unaffected_by_pumpfun_outage():
    """pump.fun failing (even permanently disabled) must not touch DexScreener."""
    engine = _engine(_FlakySession(status_code=530), max_failures=1)
    engine.poll_pumpfun_once()
    assert engine.pumpfun_disabled is True

    # A completely independent DexScreener call still works normally.
    class _DexSession:
        def get(self, url, params=None, timeout=None):
            return _FakeResponse(200, payload={"pairs": []})

    engine.session = _DexSession()
    result = engine.poll_dexscreener_once()
    assert result == []  # no candidates, but crucially: no exception


def test_enable_pumpfun_source_false_skips_entirely_no_network():
    class _ExplodingSession:
        def get(self, *args, **kwargs):
            raise AssertionError("pump.fun must not be called when the source is disabled via config")

    engine = SignalEngine(
        min_pool_liquidity_usd=15_000, max_pool_liquidity_usd=400_000, max_pool_age_s=72 * 3600,
        min_volume_liquidity_ratio=0.20, min_buy_sell_ratio=1.5,
        enable_pumpfun_source=False, session=_ExplodingSession(),
    )
    assert engine.pumpfun_disabled is True
    assert engine.poll_pumpfun_once() == []


def test_pumpfun_request_sends_browser_like_headers():
    captured = {}

    class _CapturingSession:
        def get(self, url, params=None, headers=None, timeout=None):
            captured["headers"] = headers
            return _FakeResponse(200, payload=[])

    engine = _engine(_CapturingSession())
    engine.poll_pumpfun_once()

    assert captured["headers"] is not None
    assert "User-Agent" in captured["headers"]
    assert "python-requests" not in captured["headers"]["User-Agent"]


def test_poll_counters_increment_for_the_heartbeat_log():
    """These are what the heartbeat log reports as polls_done -- "0" must
    mean something different from "1200" even when both produced zero
    candidates."""
    session = _FlakySession(status_code=530)  # pump.fun always fails, still counts as a poll
    engine = _engine(session)
    assert engine.dexscreener_polls_done == 0
    assert engine.pumpfun_polls_done == 0

    engine.poll_pumpfun_once()
    engine.poll_pumpfun_once()
    assert engine.pumpfun_polls_done == 2

    class _DexSession:
        def get(self, url, params=None, timeout=None):
            return _FakeResponse(200, payload={"pairs": []})

    engine.session = _DexSession()
    engine.poll_dexscreener_once()
    engine.poll_dexscreener_once()
    engine.poll_dexscreener_once()
    assert engine.dexscreener_polls_done == 3


def _pair(mint: str, symbol: str, liquidity=50_000, vol5m=20_000, buys=10, sells=2, age_s=300):
    return {
        "chainId": "solana",
        "pairAddress": f"pair-{mint}",
        "dexId": "raydium",
        "baseToken": {"address": mint, "symbol": symbol},
        "liquidity": {"usd": liquidity},
        "volume": {"m5": vol5m, "h1": vol5m * 4},
        "txns": {"m5": {"buys": buys, "sells": sells}},
        "priceUsd": "0.001",
        "pairCreatedAt": (time.time() - age_s) * 1000.0,
    }


class _MultiSourceSession:
    """Routes by URL shape to the right fake payload for each of the three
    DexScreener discovery sources (token-profiles, token-boosts, search),
    plus the tokens/{addresses} resolve endpoint -- lets tests target one
    source at a time without a real HTTP mock library."""

    def __init__(self, profiles=None, boosts=None, tokens_by_addr=None, search_by_query=None, raise_on=None):
        self.profiles = profiles if profiles is not None else []
        self.boosts = boosts if boosts is not None else []
        self.tokens_by_addr = tokens_by_addr or {}
        self.search_by_query = search_by_query or {}
        self.raise_on = raise_on or set()
        self.calls: list[tuple] = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        if "token-profiles" in url:
            if "profiles" in self.raise_on:
                raise requests.ConnectionError("profiles endpoint down")
            return _FakeResponse(200, payload=self.profiles)
        if "token-boosts" in url:
            if "boosts" in self.raise_on:
                raise requests.ConnectionError("boosts endpoint down")
            return _FakeResponse(200, payload=self.boosts)
        if "/latest/dex/tokens/" in url:
            if "resolve" in self.raise_on:
                raise requests.ConnectionError("resolve endpoint down")
            addrs = url.rsplit("/", 1)[-1]
            return _FakeResponse(200, payload={"pairs": self.tokens_by_addr.get(addrs, [])})
        if "/latest/dex/search" in url:
            query = params.get("q") if params else None
            if f"search:{query}" in self.raise_on:
                raise requests.ConnectionError(f"search endpoint down for {query}")
            return _FakeResponse(200, payload={"pairs": self.search_by_query.get(query, [])})
        raise AssertionError(f"unexpected DexScreener URL in test: {url}")


def test_token_profiles_and_boosts_are_the_primary_discovery_path():
    """The scenario /search alone could never satisfy: a pool ~5 minutes
    old, well within the freshness window, discovered via token-profiles
    instead of a text search that essentially never ranks a pool this
    young (see poll_dexscreener_once's docstring for the overnight run that
    proved this)."""
    mint = "FreshMint111"
    session = _MultiSourceSession(
        profiles=[{"chainId": "solana", "tokenAddress": mint}],
        tokens_by_addr={mint: [_pair(mint, "FRESH", age_s=300)]},
    )
    engine = _engine(session)
    candidates = engine.poll_dexscreener_once()
    assert [c.mint for c in candidates] == [mint]


def test_token_boosts_also_feed_discovery():
    mint = "BoostedMint222"
    session = _MultiSourceSession(
        boosts=[{"chainId": "solana", "tokenAddress": mint}],
        tokens_by_addr={mint: [_pair(mint, "BOOST", age_s=120)]},
    )
    engine = _engine(session)
    candidates = engine.poll_dexscreener_once()
    assert [c.mint for c in candidates] == [mint]


def test_non_solana_profiles_and_boosts_are_never_resolved():
    session = _MultiSourceSession(
        profiles=[{"chainId": "base", "tokenAddress": "0xNotSolana"}],
        boosts=[{"chainId": "ethereum", "tokenAddress": "0xAlsoNotSolana"}],
    )
    engine = _engine(session)
    candidates = engine.poll_dexscreener_once()
    assert candidates == []
    # Never even attempted to resolve an off-chain address via tokens/{addr}.
    assert not any("/latest/dex/tokens/" in url for url, _ in session.calls)


def test_search_queries_are_multiple_by_default_not_one_hardcoded_term():
    engine = _engine(_MultiSourceSession())
    assert len(engine.dexscreener_search_queries) > 1


def test_every_configured_search_query_is_actually_requested():
    session = _MultiSourceSession()
    engine = SignalEngine(
        min_pool_liquidity_usd=15_000, max_pool_liquidity_usd=400_000, max_pool_age_s=72 * 3600,
        min_volume_liquidity_ratio=0.20, min_buy_sell_ratio=1.5,
        dexscreener_search_queries=["alpha", "beta", "gamma"],
        session=session,
    )
    engine.poll_dexscreener_once()
    searched = {params.get("q") for url, params in session.calls if "/latest/dex/search" in url}
    assert searched == {"alpha", "beta", "gamma"}


def test_one_dead_dexscreener_source_does_not_block_the_others():
    mint = "BoostedMint333"
    session = _MultiSourceSession(
        boosts=[{"chainId": "solana", "tokenAddress": mint}],
        tokens_by_addr={mint: [_pair(mint, "SURVIVOR", age_s=90)]},
        raise_on={"profiles"},
    )
    engine = _engine(session)
    candidates = engine.poll_dexscreener_once()  # must not raise despite profiles being down
    assert [c.mint for c in candidates] == [mint]


def test_all_dexscreener_sources_down_returns_empty_not_an_exception():
    session = _MultiSourceSession(raise_on={"profiles", "boosts", "search:SOL", "search:pump", "search:bonk", "search:meme"})
    engine = _engine(session)
    assert engine.poll_dexscreener_once() == []


def test_dedupes_the_same_pool_seen_via_both_boosts_and_search():
    mint = "DupeMint444"
    pair = _pair(mint, "DUPE", age_s=200)
    session = _MultiSourceSession(
        boosts=[{"chainId": "solana", "tokenAddress": mint}],
        tokens_by_addr={mint: [pair]},
        search_by_query={"SOL": [pair]},
    )
    engine = SignalEngine(
        min_pool_liquidity_usd=15_000, max_pool_liquidity_usd=400_000, max_pool_age_s=72 * 3600,
        min_volume_liquidity_ratio=0.20, min_buy_sell_ratio=1.5,
        dexscreener_search_queries=["SOL"],
        session=session,
    )
    candidates = engine.poll_dexscreener_once()
    assert len(candidates) == 1


def test_pumpfun_poll_counter_frozen_once_disabled():
    """Once the circuit breaker disables the source, further poll calls
    are no-ops and must not keep incrementing polls_done -- a frozen
    counter in the heartbeat is itself the signal that it's disabled."""
    engine = _engine(_FlakySession(status_code=530), max_failures=2)
    engine.poll_pumpfun_once()
    engine.poll_pumpfun_once()
    assert engine.pumpfun_disabled is True
    frozen_at = engine.pumpfun_polls_done

    engine.poll_pumpfun_once()
    engine.poll_pumpfun_once()
    assert engine.pumpfun_polls_done == frozen_at
