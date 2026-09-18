"""RpcGateway: the 429 backoff must never retry into a rate limit, and the
budget-threshold alert must latch (fire once, stay silent through jitter,
only re-arm once usage genuinely recovers).
"""
from __future__ import annotations

from unittest import mock

import pytest
import requests

from bot.rpc_gateway import RateBudget, RpcGateway, RpcMethodDisabled, RpcOutage


def _ok_body(result="pong"):
    return {"jsonrpc": "2.0", "id": 1, "result": result}


class _FakeResp:
    def __init__(self, status_code: int, payload=None):
        self.status_code = status_code
        self.headers: dict = {}
        self._payload = payload if payload is not None else _ok_body()

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.exceptions.HTTPError(f"{self.status_code} error")

    def json(self):
        return self._payload


class _ScriptedSession:
    """Each entry is ("429",), ("ok", payload), or ("error",)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        kind, *rest = self.script.pop(0)
        if kind == "429":
            return _FakeResp(429)
        if kind == "403":
            return _FakeResp(403)
        if kind == "ok":
            return _FakeResp(200, rest[0] if rest else _ok_body())
        if kind == "jsonrpc_error":
            return _FakeResp(200, {"jsonrpc": "2.0", "id": 1, "error": rest[0]})
        if kind == "error":
            raise requests.ConnectionError("simulated network error")
        raise AssertionError(f"unknown script kind {kind}")


# ----------------------------------------------------------------------
# 429 backoff: never retry immediately into a rate limit
# ----------------------------------------------------------------------


@mock.patch("bot.rpc_gateway.time.sleep")
def test_429_waits_out_a_growing_cooldown_before_retrying(mock_sleep):
    session = _ScriptedSession([("429",), ("429",), ("ok", _ok_body("pong"))])
    gw = RpcGateway(
        "http://primary.invalid", session=session, max_retries=5,
        rate_limit_base_backoff_s=1.0, rate_limit_max_backoff_s=30.0,
    )

    result = gw.call("ping")

    assert result == "pong"
    assert session.calls == 3
    # A cooldown wait happened before each retry, not an instant retry.
    waited = [c.args[0] for c in mock_sleep.call_args_list if c.args]
    assert len(waited) == 2
    assert all(w > 0 for w in waited)
    # And it grew: the second 429 backs off longer than the first.
    assert waited[1] > waited[0]


@mock.patch("bot.rpc_gateway.time.sleep")
def test_429_backoff_resets_after_a_success(mock_sleep):
    session = _ScriptedSession([("429",), ("ok", _ok_body("a")), ("429",), ("ok", _ok_body("b"))])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5, rate_limit_base_backoff_s=2.0)

    gw.call("methodA")
    assert gw._rate_limit_backoff_level == 0
    assert gw._cooldown_until == 0.0

    gw.call("methodB")
    # Second 429 backs off at the BASE level again, not escalated further --
    # proof the first success actually cleared the cooldown state, not just
    # the counter, so an unrelated later call isn't stuck waiting out a
    # cooldown that already served its purpose.
    waited = [c.args[0] for c in mock_sleep.call_args_list if c.args]
    assert len(waited) == 2
    assert waited[0] == pytest.approx(waited[1], rel=0.05)


@mock.patch("bot.rpc_gateway.time.sleep")
def test_429_exhausting_retries_raises_rpc_outage_not_infinite_loop(mock_sleep):
    session = _ScriptedSession([("429",)] * 10)
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=3, rate_limit_base_backoff_s=0.01)

    with pytest.raises(RpcOutage):
        gw.call("ping")
    assert session.calls == 3  # exactly max_retries attempts, no runaway retrying


@mock.patch("bot.rpc_gateway.time.sleep")
def test_ordinary_transient_errors_still_use_the_short_fixed_backoff(mock_sleep):
    """A plain connection error (not a 429) shouldn't trip the long
    rate-limit cooldown -- that's reserved for actual 429s."""
    session = _ScriptedSession([("error",), ("ok", _ok_body("pong"))])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=3)

    result = gw.call("ping")

    assert result == "pong"
    assert gw._cooldown_until == 0.0  # never touched by a non-429 failure


# ----------------------------------------------------------------------
# call-stats audit
# ----------------------------------------------------------------------


def test_call_stats_track_per_method_and_per_minute():
    session = _ScriptedSession([("ok", _ok_body())] * 3)
    gw = RpcGateway("http://primary.invalid", session=session)

    gw.call("getAccountInfo")
    gw.call("getAccountInfo")
    gw.call("getTokenSupply")

    stats = gw.get_call_stats()
    assert stats["total_calls"] == 3
    assert stats["top_methods"]["getAccountInfo"] == 2
    assert stats["top_methods"]["getTokenSupply"] == 1
    assert stats["calls_per_minute"] == 3
    assert stats["rate_limit_per_10s"] == gw.budget.limit_per_window


@mock.patch("bot.rpc_gateway.time.sleep")
def test_retried_attempts_all_count_against_the_budget(mock_sleep):
    """A retried request still cost the provider one real HTTP call --
    the audit must reflect that, not just logical call() invocations."""
    session = _ScriptedSession([("error",), ("error",), ("ok", _ok_body())])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)

    gw.call("getBalance")

    assert gw.get_call_stats()["total_calls"] == 3


# ----------------------------------------------------------------------
# get_multiple_accounts
# ----------------------------------------------------------------------


def test_get_multiple_accounts_batches_into_one_call():
    captured = {}

    class _Session:
        def post(self, url, json=None, timeout=None):
            captured["payload"] = json
            value = [{"owner": "X"}, None, {"owner": "Y"}]
            return _FakeResp(200, {"jsonrpc": "2.0", "id": 1, "result": {"value": value}})

    gw = RpcGateway("http://primary.invalid", session=_Session())
    result = gw.get_multiple_accounts(["a", "b", "c"])

    assert result == [{"owner": "X"}, None, {"owner": "Y"}]
    assert captured["payload"]["method"] == "getMultipleAccounts"
    assert captured["payload"]["params"][0] == ["a", "b", "c"]


def test_get_multiple_accounts_empty_list_makes_no_call():
    class _ExplodingSession:
        def post(self, *a, **kw):
            raise AssertionError("should not be called for an empty pubkey list")

    gw = RpcGateway("http://primary.invalid", session=_ExplodingSession())
    assert gw.get_multiple_accounts([]) == []


# ----------------------------------------------------------------------
# RateBudget hysteresis latch
# ----------------------------------------------------------------------


def test_rate_budget_alerts_once_and_ignores_jitter_at_the_threshold():
    alerts = []
    budget = RateBudget(limit_per_window=10, window_s=10.0, on_threshold=alerts.append)

    # Push usage up to and past 70% -- one alert.
    for _ in range(8):
        budget.record_and_check()
    assert len(alerts) == 1

    # More calls right around/above the threshold must NOT re-alert --
    # this is exactly the death-spiral scenario (usage hovering near 70%).
    for _ in range(20):
        budget.record_and_check()
    assert len(alerts) == 1


def test_rate_budget_rearms_only_after_dropping_to_clear_threshold():
    alerts = []
    budget = RateBudget(limit_per_window=10, window_s=10.0, alert_threshold_pct=0.70, clear_threshold_pct=0.40, on_threshold=alerts.append)

    for _ in range(8):
        budget.record_and_check()
    assert len(alerts) == 1

    # Simulate the window aging out down to just above the clear threshold --
    # not low enough to re-arm yet.
    budget._timestamps.clear()
    import time as _time

    now = _time.monotonic()
    for _ in range(5):  # 50% usage: above clear_threshold_pct, not re-armed
        budget._timestamps.append(now)
    budget.record_and_check()
    assert len(alerts) == 1  # still just the one

    # Now genuinely drop below the clear threshold, then cross back above --
    # this is a real second incident and must alert again.
    budget._timestamps.clear()
    for _ in range(2):  # 20%, below clear_threshold_pct -- re-arms
        budget._timestamps.append(now)
    budget.record_and_check()
    assert len(alerts) == 1  # re-arming itself doesn't alert

    for _ in range(8):
        budget.record_and_check()
    assert len(alerts) == 2


# ----------------------------------------------------------------------
# permanent method rejection (403 / "method not available") detection
# ----------------------------------------------------------------------


@mock.patch("bot.rpc_gateway.time.sleep")
def test_403_disables_the_method_immediately_no_further_retries(mock_sleep):
    session = _ScriptedSession([("403",)])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)

    with pytest.raises(RpcMethodDisabled):
        gw.call("getProgramAccounts")

    assert session.calls == 1  # not max_retries=5 -- a 403 is never worth retrying
    assert gw.is_method_disabled("getProgramAccounts") is True


@mock.patch("bot.rpc_gateway.time.sleep")
def test_disabled_method_fails_instantly_on_next_call_no_network(mock_sleep):
    session = _ScriptedSession([("403",)])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)

    with pytest.raises(RpcMethodDisabled):
        gw.call("getProgramAccounts")
    assert session.calls == 1

    with pytest.raises(RpcMethodDisabled):
        gw.call("getProgramAccounts")
    assert session.calls == 1  # still 1 -- the second call never touched the network


@mock.patch("bot.rpc_gateway.time.sleep")
def test_jsonrpc_method_not_found_code_disables_the_method(mock_sleep):
    session = _ScriptedSession([("jsonrpc_error", {"code": -32601, "message": "Method not found"})])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)

    with pytest.raises(RpcMethodDisabled):
        gw.call("getSomeEnhancedMethod")
    assert gw.is_method_disabled("getSomeEnhancedMethod") is True


@mock.patch("bot.rpc_gateway.time.sleep")
def test_jsonrpc_plan_gated_message_disables_the_method(mock_sleep):
    session = _ScriptedSession([("jsonrpc_error", {"code": -32000, "message": "This method is not available on your current plan"})])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)

    with pytest.raises(RpcMethodDisabled):
        gw.call("getSomeEnhancedMethod")
    assert gw.is_method_disabled("getSomeEnhancedMethod") is True


@mock.patch("bot.rpc_gateway.time.sleep")
def test_ordinary_jsonrpc_error_does_not_disable_the_method(mock_sleep):
    """An error that doesn't look like "unavailable" -- e.g. a bad
    parameter -- must not be mistaken for a permanent rejection and
    silently disable a method that actually works."""
    session = _ScriptedSession([
        ("jsonrpc_error", {"code": -32602, "message": "Invalid params"}),
        ("jsonrpc_error", {"code": -32602, "message": "Invalid params"}),
    ])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=2)

    with pytest.raises(RpcOutage):
        gw.call("getAccountInfo")
    assert gw.is_method_disabled("getAccountInfo") is False


@mock.patch("bot.rpc_gateway.time.sleep")
def test_disabled_methods_listed_in_call_stats(mock_sleep):
    session = _ScriptedSession([("403",)])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)
    with pytest.raises(RpcMethodDisabled):
        gw.call("getProgramAccounts")
    assert gw.get_call_stats()["disabled_methods"] == ["getProgramAccounts"]


# ----------------------------------------------------------------------
# per-call max_retries override
# ----------------------------------------------------------------------


@mock.patch("bot.rpc_gateway.time.sleep")
def test_max_retries_override_limits_attempts_for_one_call(mock_sleep):
    """Orchestrator's indexing loop passes max_retries=1: it runs its own
    separate backoff across repeated calls and doesn't want this internal
    retry loop also sleeping/retrying on every single call, which would
    mask the outer backoff entirely."""
    session = _ScriptedSession([("error",), ("error",), ("error",)])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)  # instance default is 5

    with pytest.raises(RpcOutage):
        gw.call("getTransaction", max_retries=1)

    assert session.calls == 1  # NOT 5 -- the override wins for this call


def test_max_retries_override_none_falls_back_to_instance_default():
    session = _ScriptedSession([("ok", _ok_body())])
    gw = RpcGateway("http://primary.invalid", session=session, max_retries=5)
    gw.call("getBalance")  # no override passed
    assert session.calls == 1
