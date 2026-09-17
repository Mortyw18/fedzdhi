"""SignalEngine: pump.fun must be a gracefully optional source.

pump.fun's API is unofficial, unauthenticated, and known to sit behind
Cloudflare returning 5xx (530 = origin unreachable) for scripted clients
for extended stretches. None of that -- an HTTP error, a malformed
response, anything -- may ever crash the bot or block DexScreener /
InsiderRadar. After enough consecutive failures, polling it should stop
altogether rather than hammering a dead endpoint forever.
"""
from __future__ import annotations

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

    def get(self, url, params=None, timeout=None):
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
