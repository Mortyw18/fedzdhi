"""TokenSafety.check_lp_or_graduation: pump.fun-origin mints (vanity suffix
"pump") get their bonding-curve/graduation state resolved from pump.fun's
own API, instead of being permanently rejected with "no LP mint known" --
Candidate.pump_fun_graduated is only ever set when SignalEngine discovers a
token via its OWN pump.fun poller, never when the same token is discovered
via DexScreener (parse_dexscreener_pair never sets it, and never sets
lp_mint either), so every pump.fun-origin token found via DexScreener's
token-profiles/boosts discovery used to fail this check unconditionally.

A lookup failure against pump.fun's API is deliberately NOT a reject --
it's "unknown right now," consistent with the rest of TokenSafety's
"insufficient data doesn't block a trade on its own" philosophy (see
check_bundled_launch's cold-start case).
"""
from __future__ import annotations

import requests

from bot.models import Candidate, SignalSource
from bot.token_safety import PUMPFUN_COIN_INFO_URL, TokenSafety
from conftest import FakeJupiter, FakeRpc, make_pubkey

PUMPFUN_MINT = "4PYVWqC358kpvKYDrwbniVAeUXUq6dULa27Z2TCVpump"
NON_PUMPFUN_MINT = make_pubkey(31)


class _NoNetworkSession:
    def get(self, *args, **kwargs):
        raise requests.ConnectionError("network disabled in tests")


class _FakeResponse:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} Server Error: for url: fake")

    def json(self):
        return self._payload


class _PumpfunCoinSession:
    def __init__(self, complete: bool = False, status_code: int = 200, exc: Exception | None = None):
        self.complete = complete
        self.status_code = status_code
        self.exc = exc
        self.requests: list[tuple] = []

    def get(self, url, headers=None, timeout=None):
        self.requests.append((url, headers))
        if self.exc is not None:
            raise self.exc
        return _FakeResponse(self.status_code, payload={"complete": self.complete})


def _candidate(mint: str, pump_fun_graduated=None) -> Candidate:
    return Candidate(
        mint=mint,
        symbol="TEST",
        source=SignalSource.DEXSCREENER,  # discovered via DexScreener -- pump_fun_graduated NOT set by SignalEngine
        liquidity_usd=50_000,
        pump_fun_graduated=pump_fun_graduated,
    )


def _safety(pumpfun_session=None, enable_pumpfun_lookups=True) -> TokenSafety:
    return TokenSafety(
        FakeRpc(), FakeJupiter(), rugcheck_session=_NoNetworkSession(),
        pumpfun_session=pumpfun_session, enable_pumpfun_lookups=enable_pumpfun_lookups,
    )


def test_pumpfun_mint_discovered_via_dexscreener_is_no_longer_a_blanket_reject():
    """This is the exact reported bug: a pump.fun-suffixed mint, discovered
    via DexScreener (so pump_fun_graduated was never set), used to reject
    with "no LP mint known and token is not an un-graduated pump.fun
    token" on every single check -- permanently, since lp_mint is never
    populated by DexScreener discovery either."""
    session = _PumpfunCoinSession(complete=False)
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT))

    assert result.passed is True
    assert "bonding curve" in result.detail
    assert session.requests  # actually asked pump.fun's API, not just guessed


def test_pumpfun_mint_confirmed_graduated_passes():
    session = _PumpfunCoinSession(complete=True)
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT))

    assert result.passed is True
    assert "graduated" in result.detail
    assert "migration burns the LP" in result.detail


def test_pumpfun_mint_already_flagged_graduated_by_signalengine_still_resolved_via_api():
    """Candidate.pump_fun_graduated=True (set by SignalEngine's own poller)
    must not be trusted blindly either -- it can be stale by the time
    TokenSafety runs. The API call is still made."""
    session = _PumpfunCoinSession(complete=True)
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT, pump_fun_graduated=True))

    assert result.passed is True
    assert session.requests


def test_pumpfun_signalengine_confirmed_ungraduated_skips_the_api_call_entirely():
    """The cheapest case: SignalEngine's OWN pump.fun poller already told
    us pump_fun_graduated=False this cycle -- no need to ask again."""
    session = _PumpfunCoinSession(complete=True)  # would say graduated if asked -- must NOT be asked
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT, pump_fun_graduated=False))

    assert result.passed is True
    assert "pre-graduation" in result.detail
    assert session.requests == []


def test_pumpfun_lookup_failure_is_unknown_not_a_reject():
    session = _PumpfunCoinSession(exc=requests.ConnectionError("pump.fun unreachable"))
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT))

    assert result.passed is True
    assert "unknown" in result.detail
    assert "pump.fun unreachable" in result.detail


def test_pumpfun_lookup_530_is_also_unknown_not_a_reject():
    session = _PumpfunCoinSession(status_code=530)
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT))

    assert result.passed is True
    assert "unknown" in result.detail


def test_pumpfun_request_sends_browser_like_headers():
    session = _PumpfunCoinSession(complete=False)
    safety = _safety(pumpfun_session=session)

    safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT))

    url, headers = session.requests[0]
    assert url == PUMPFUN_COIN_INFO_URL.format(mint=PUMPFUN_MINT)
    assert headers is not None
    assert "python-requests" not in headers["User-Agent"]


def test_pumpfun_lookups_disabled_via_config_rejects_cleanly_no_network():
    class _ExplodingSession:
        def get(self, *args, **kwargs):
            raise AssertionError("must not call pump.fun when lookups are disabled")

    safety = _safety(pumpfun_session=_ExplodingSession(), enable_pumpfun_lookups=False)

    result = safety.check_lp_or_graduation(_candidate(PUMPFUN_MINT))

    assert result.passed is False
    assert "disabled via config" in result.detail


def test_non_pumpfun_mint_with_no_lp_mint_still_rejected():
    """The general (non-pump.fun) case is unchanged: this bot has no way to
    resolve an arbitrary Raydium/Orca/Meteora pool's LP mint from just a
    pair address, so it fails closed rather than passing on uncertainty --
    unlike the pump.fun case, which has an authoritative API to ask."""
    session = _PumpfunCoinSession(complete=False)
    safety = _safety(pumpfun_session=session)

    result = safety.check_lp_or_graduation(_candidate(NON_PUMPFUN_MINT))

    assert result.passed is False
    assert "no pump.fun heritage" in result.detail
    assert session.requests == []  # never even asked pump.fun about a mint that isn't one
