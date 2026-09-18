"""RpcGateway: the only module that talks to Solana RPC directly.

Everything else (TokenSafety, ExecutionEngine, InsiderRadar) goes through
this so retry/backoff, the rate-limit budget, and failover live in one
place. Helius free tier is generous but finite; we track our own usage
so we get a warning at 70% instead of a stream of 429s.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Optional

import requests
import websockets

DEFAULT_TIMEOUT_S = 10.0


class RpcError(Exception):
    pass


class RpcOutage(RpcError):
    """Raised when both primary and failover RPC are unreachable.

    KillSwitch treats this as a halt-buys condition: if we can't see
    prices, we can't safely manage exits either.
    """


class RpcRateLimited(RpcError):
    """A 429 from the RPC provider. Never retried immediately -- see the
    shared cooldown in RpcGateway.call(). Sustained 429s that exhaust
    retries surface as RpcOutage, same as any other unreachable endpoint.
    """


class RpcMethodDisabled(RpcOutage):
    """A method looks PERMANENTLY rejected, not transiently failing: a 403
    from the provider, or a JSON-RPC error whose code/message says the
    method isn't available (e.g. "not supported on this plan"), confirmed
    RpcGateway.method_disable_threshold separate times (not retries of one
    request -- separate call() invocations) before this is ever raised.
    A single 403 is deliberately NOT enough: it can just as easily be a
    transient WAF/proxy block or an unrelated hiccup as a genuine plan
    restriction, and disabling a method for the rest of the run on one bad
    response was a real bug in an earlier version of this (getTransaction,
    an utterly ordinary method no free tier actually restricts, got
    permanently killed by a single non-representative 403). Once actually
    confirmed, retrying is pointless until the provider config changes --
    RpcGateway remembers it for the rest of the process and every
    subsequent call to that method fails instantly, with zero network I/O.
    Subclasses RpcOutage so existing `except RpcOutage:` call sites keep
    working unchanged; callers that need to react differently (see
    Orchestrator's indexing loop, which stops trying the method at all
    rather than continuing to log a failure every time) can catch this
    specifically. See also `call()`'s `allow_method_disable` parameter for
    opting a call site out of this mechanism entirely.
    """


class _MethodUnavailable(RpcError):
    """Internal signal raised by _post -- caught inside call(), where
    `method` is in scope to update _disabled_methods and turn this into
    the caller-visible RpcMethodDisabled. Never escapes _post/call."""


# Solana's JSON-RPC error code for "this transaction's version is higher
# than what maxSupportedTransactionVersion in the request allowed" -- e.g.
# {"code": -32015, "message": "Transaction version (1) is not supported by
# the requesting client. Please try the request again with the following
# configuration parameter: \"maxSupportedTransactionVersion\": 1"}.
# Checked and handled BEFORE _looks_like_method_unavailable below: that
# message contains "not supported," which the plan-gating heuristic would
# otherwise match, misreading "this one request needs a version bump" as
# "this method looks permanently unavailable on this plan" -- exactly the
# misdiagnosis that cost a production run its entire event-driven discovery
# funnel (getTransaction 3-strike-disabled while the real cause was every
# versioned transaction on-chain having moved past the hardcoded cap).
_TRANSACTION_VERSION_NOT_SUPPORTED_CODE = -32015


class _TransactionVersionTooLow(RpcError):
    """Internal signal raised by _post when maxSupportedTransactionVersion
    in the request was lower than the transaction's actual version --
    never a permanent rejection, always fixable by resending with a higher
    value. Caught inside call(), which bumps
    RpcGateway.max_supported_transaction_version and retries the same
    request in place. Never escapes _post/call."""

    def __init__(self, required_version: int, message: str) -> None:
        super().__init__(message)
        self.required_version = required_version


def _parse_required_transaction_version(message: str) -> Optional[int]:
    """Pulls the version number Solana's -32015 error says the client
    needs to request, straight out of the (freeform, provider-authored)
    error message -- e.g. '...following configuration parameter:
    "maxSupportedTransactionVersion": 1' or '...Transaction version (1) is
    not supported...'. Returns None if the message doesn't match either
    known shape, so the caller can fall back to a conservative +1 bump
    rather than silently doing nothing.
    """
    match = re.search(r"maxSupportedTransactionVersion[\"']?\s*:\s*(\d+)", message)
    if match:
        return int(match.group(1))
    match = re.search(r"[Vv]ersion\s*\((\d+)\)", message)
    if match:
        return int(match.group(1))
    return None


def _patch_max_supported_transaction_version(params: Optional[list], new_version: int) -> bool:
    """Mutates any {"maxSupportedTransactionVersion": ...} entry inside a
    JSON-RPC params list IN PLACE, so the exact payload dict already built
    for this call is what actually gets resent -- the call site that built
    `params` never needs to know a bump happened. Returns whether anything
    was actually found and patched: a method whose call site never set
    this key can't be helped by bumping it, and the caller uses this to
    decide whether retrying is even worth attempting again.
    """
    if not params:
        return False
    patched = False
    for element in params:
        if isinstance(element, dict) and "maxSupportedTransactionVersion" in element:
            element["maxSupportedTransactionVersion"] = new_version
            patched = True
    return patched


def _looks_like_method_unavailable(error: Any) -> bool:
    """Heuristic over a JSON-RPC error object: does this look like "this
    method isn't available to you," not an ordinary transient failure?
    JSON-RPC code -32601 is the standard "Method not found." The message
    substrings cover what free-tier providers commonly say instead of a
    clean error code when a method is plan-gated. A single match is
    deliberately NOT enough to disable a method on its own -- see
    RpcGateway.method_disable_threshold -- so a false positive here costs
    a few logged-but-otherwise-ordinary transient failures, not a
    permanently killed method; a false negative just means the old (safe,
    if wasteful) retry-forever behavior for that one message shape. This
    is intentionally over-inclusive rather than exact, since the sustained
    threshold is what actually guards against acting on a one-off match.
    Code -32015 (see _TRANSACTION_VERSION_NOT_SUPPORTED_CODE) is handled
    entirely separately, upstream of this check, and must never reach here.
    """
    if not isinstance(error, dict):
        return False
    if error.get("code") == -32601:
        return True
    message = str(error.get("message", "")).lower()
    return any(
        phrase in message
        for phrase in ("not available", "not allowed", "not supported", "not enabled", "restricted", "requires a paid plan", "upgrade")
    )


@dataclass
class RateBudget:
    """Sliding-window budget tracker over a 10s window, with a latched alert.

    Alerts ONCE when usage crosses `alert_threshold_pct` and then stays
    silent -- even if usage jitters back and forth across that exact line,
    which is exactly what happens during a rate-limit episode -- until
    usage drops all the way to `clear_threshold_pct`. Only then is the
    alert re-armed for a future crossing. Without this hysteresis gap, a
    death spiral where usage hovers right at 70% fires the same alert
    every few seconds forever.
    """

    limit_per_window: int
    window_s: float = 10.0
    alert_threshold_pct: float = 0.70
    clear_threshold_pct: float = 0.40
    _timestamps: deque = field(default_factory=deque)
    on_threshold: Optional[Callable[[float], None]] = None
    _alerted: bool = False

    def _prune(self) -> float:
        now = time.monotonic()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps) / self.limit_per_window

    def record_and_check(self) -> None:
        self._timestamps.append(time.monotonic())
        usage = self._prune()
        if usage >= self.alert_threshold_pct and not self._alerted:
            self._alerted = True
            if self.on_threshold:
                self.on_threshold(usage)
        elif usage < self.clear_threshold_pct and self._alerted:
            self._alerted = False  # re-armed; the next crossing above threshold will alert again

    def current_usage_pct(self) -> float:
        return self._prune()


class RpcGateway:
    def __init__(
        self,
        primary_url: str,
        failover_url: str = "",
        rate_limit_per_10s: int = 100,
        max_retries: int = 3,
        on_budget_threshold: Optional[Callable[[float], None]] = None,
        session: Optional[requests.Session] = None,
        rate_limit_base_backoff_s: float = 2.0,
        rate_limit_max_backoff_s: float = 60.0,
        logger: Optional[logging.Logger] = None,
        max_supported_transaction_version: int = 1,
    ) -> None:
        self.primary_url = primary_url
        self.failover_url = failover_url
        self.max_retries = max_retries
        self.budget = RateBudget(limit_per_window=rate_limit_per_10s, on_threshold=on_budget_threshold)
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.rpc_gateway")
        self._consecutive_failures = 0

        # The highest Solana transaction version call sites that fetch
        # transactions (indexing's getTransaction, ExecutionEngine's own
        # swap confirmation) should ask for. Mutable and shared: call sites
        # read this live (not a hardcoded literal) so an auto-bump below
        # takes effect for every future call, not just the one that
        # triggered it. Defaults to 1 -- version 0 was the original
        # versioned-transaction format; version 1 (and whatever comes
        # after) is handled the same way by _post_with_auto_version_bump
        # the moment the chain moves again, without needing a code change.
        self.max_supported_transaction_version = max_supported_transaction_version
        self._max_version_autobumps = 5

        # Shared, gateway-wide backoff: a 429 anywhere sets a cooldown that
        # EVERY subsequent call (from any code path, any thread) checks and
        # waits out before attempting another request. This is what makes
        # the backoff actually stop a spiral -- independent per-call retry
        # loops that don't share this state would each retry on their own
        # schedule and collectively keep hammering the rate limit.
        self.rate_limit_base_backoff_s = rate_limit_base_backoff_s
        self.rate_limit_max_backoff_s = rate_limit_max_backoff_s
        self._cooldown_until = 0.0
        self._rate_limit_backoff_level = 0

        # Methods detected as permanently rejected (403, or a JSON-RPC
        # error that looks like "not available on this plan") -- see
        # RpcMethodDisabled. Checked at the top of call() so a disabled
        # method fails instantly, with zero network I/O, instead of paying
        # a full retry cycle every single time it's called for the rest of
        # the run.
        #
        # A single 403 is NOT enough to disable a method -- it used to be,
        # and that was a real bug: a 403 can mean a genuinely plan-gated
        # method, but it can just as easily mean a transient WAF/proxy
        # block, an IP-level hiccup, or anything else with no relation to
        # "this method is permanently unavailable." One bad response used
        # to permanently kill a method (getTransaction, in one observed
        # case -- an utterly ordinary method no free tier actually
        # restricts) for the rest of the run on the first blip, the exact
        # same false-positive shape the RPC-outage kill switch and the
        # indexing backoff both had to fix earlier by requiring sustained
        # failures before acting. method_disable_threshold consecutive
        # _MethodUnavailable signals for the SAME method (across separate
        # call() invocations, e.g. different getTransaction signatures --
        # not retries of the same request) are required before the method
        # is actually disabled; short of that, it's treated as an ordinary
        # transient failure (RpcOutage as usual) and the count keeps
        # accumulating across calls.
        #
        # Deliberately NOT persisted to disk anywhere (unlike KillSwitch's
        # state -- see kill_switch.py): this is a fresh, empty set every
        # time a RpcGateway is constructed, and Orchestrator constructs a
        # brand new one on every process start. A disabled-for-this-run
        # method never survives a restart; there is no state file to clear.
        self._disabled_methods: set[str] = set()
        self._method_unavailable_counts: Counter[str] = Counter()
        self.method_disable_threshold = 3

        # Per-method call counters, for the RPC budget audit in the daily
        # report. Every attempted HTTP request counts, including retries --
        # a retried request still consumes one unit of the provider's budget.
        self.call_counts: Counter[str] = Counter()
        self._call_timestamps: deque = deque()

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _record_call(self, method: str) -> None:
        now = time.monotonic()
        self.call_counts[method] += 1
        self._call_timestamps.append(now)
        cutoff = now - 60.0
        while self._call_timestamps and self._call_timestamps[0] < cutoff:
            self._call_timestamps.popleft()

    def calls_per_minute(self) -> int:
        now = time.monotonic()
        cutoff = now - 60.0
        while self._call_timestamps and self._call_timestamps[0] < cutoff:
            self._call_timestamps.popleft()
        return len(self._call_timestamps)

    def get_call_stats(self) -> dict:
        """Snapshot for Accounting's daily report -- this is the audit trail
        for 'is the free tier budget actually a design constraint.'"""
        return {
            "calls_per_minute": self.calls_per_minute(),
            "budget_usage_pct": self.budget.current_usage_pct(),
            "rate_limit_per_10s": self.budget.limit_per_window,
            "top_methods": dict(self.call_counts.most_common(8)),
            "total_calls": sum(self.call_counts.values()),
            "disabled_methods": sorted(self._disabled_methods),
        }

    def is_method_disabled(self, method: str) -> bool:
        return method in self._disabled_methods

    def _wait_out_cooldown(self, method: str) -> None:
        remaining = self._cooldown_until - time.monotonic()
        if remaining > 0:
            self.logger.warning("RPC cooldown active (%.1fs remaining), pausing before %s", remaining, method)
            time.sleep(remaining)

    def _post(self, url: str, payload: dict) -> dict:
        resp = self.session.post(url, json=payload, timeout=DEFAULT_TIMEOUT_S)
        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            raise RpcRateLimited(f"429 rate limited by {url} (Retry-After={retry_after})")
        if resp.status_code == 403:
            raise _MethodUnavailable(f"403 Forbidden from {url}")
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            error = data["error"]
            if isinstance(error, dict) and error.get("code") == _TRANSACTION_VERSION_NOT_SUPPORTED_CODE:
                message = str(error.get("message", ""))
                required = _parse_required_transaction_version(message)
                if required is None:
                    required = self.max_supported_transaction_version + 1
                raise _TransactionVersionTooLow(required, message)
            if _looks_like_method_unavailable(error):
                raise _MethodUnavailable(f"{payload['method']} RPC error: {error}")
        return data

    def _post_with_auto_version_bump(self, url: str, payload: dict, method: str) -> dict:
        """Wraps _post so a -32015 "transaction version not supported"
        response self-heals within this one call() attempt: bump
        max_supported_transaction_version to whatever the error says the
        client needs, patch it into this exact payload's params, and
        re-issue the SAME request -- instead of surfacing it as an
        ordinary failure that indexing's backoff or the disable-threshold
        machinery (neither of which knows anything about transaction
        versions) would otherwise have to absorb blindly, run after run,
        forever, every time the chain's default version format changes.
        Bounded so a provider that keeps demanding a higher version every
        single response can't spin this forever.
        """
        for _ in range(self._max_version_autobumps + 1):
            try:
                return self._post(url, payload)
            except _TransactionVersionTooLow as exc:
                old = self.max_supported_transaction_version
                new = max(old + 1, exc.required_version)
                self.max_supported_transaction_version = new
                patched = _patch_max_supported_transaction_version(payload.get("params"), new)
                self.logger.warning(
                    "rpc_transaction_version_bumped",
                    extra={
                        "fields": {
                            "method": method, "old_version": old, "new_version": new,
                            "params_patched": patched, "detail": str(exc),
                        }
                    },
                )
                self._record_call(method)
                self.budget.record_and_check()
                if not patched:
                    # Nothing in this request's params names the key --
                    # bumping the instance-wide default can't fix THIS
                    # call, so don't loop pointlessly; let it surface as an
                    # ordinary error instead.
                    raise RpcError(f"{method}: transaction version {exc.required_version} not supported, "
                                    f"and no maxSupportedTransactionVersion param present to patch: {exc}") from exc
        raise RpcOutage(
            f"{method}: transaction version requirement kept increasing past "
            f"{self._max_version_autobumps} auto-bumps -- giving up on this call"
        )

    def _trip_rate_limit_cooldown(self, method: str, exc: Exception) -> None:
        self._rate_limit_backoff_level += 1
        backoff = min(
            self.rate_limit_base_backoff_s * (2 ** (self._rate_limit_backoff_level - 1)),
            self.rate_limit_max_backoff_s,
        )
        self._cooldown_until = time.monotonic() + backoff
        self.logger.warning(
            "rate_limited",
            extra={"fields": {"method": method, "backoff_s": backoff, "level": self._rate_limit_backoff_level, "detail": str(exc)}},
        )

    def call(
        self,
        method: str,
        params: Optional[list] = None,
        max_retries: Optional[int] = None,
        allow_method_disable: bool = True,
    ) -> Any:
        """JSON-RPC call with retry/backoff, then failover, then RpcOutage.

        A 429 is never retried immediately: it trips a shared cooldown (see
        __init__) that this and every other call waits out before trying
        again, with the wait growing exponentially per consecutive 429 and
        resetting only after a call actually succeeds. This is deliberately
        different from an ordinary transient failure's short fixed backoff
        below -- retrying quickly into an active rate limit is exactly what
        turns one slow endpoint into a budget death spiral.

        `max_retries` overrides the instance default for this one call --
        Orchestrator's indexing loop passes 1: it runs its own, separate
        exponential backoff across repeated calls to this same method (see
        _index_program_loop), and letting THIS retry loop also sleep and
        retry internally on every single call was masking that outer
        backoff, making a genuinely backing-off caller look like it was
        retrying on a fixed ~1-2s schedule from the logs alone.

        A method that looks PERMANENTLY rejected (403, or a JSON-RPC error
        that reads like "not available on this plan") needs
        method_disable_threshold sustained confirmations (see __init__)
        before it's actually disabled -- see RpcMethodDisabled. Until then
        it's treated as an ordinary transient failure.

        `allow_method_disable=False` opts a call site out of the
        disable mechanism entirely, always surfacing RpcOutage instead of
        ever raising RpcMethodDisabled -- Orchestrator's indexing loop
        passes this for getTransaction: it's such a fundamental,
        universally-available method that treating any rejection of it as
        "permanently unavailable on this plan" was mis-scoped to begin
        with, and indexing already has its own dedicated, sustained-failure
        backoff (see _index_program_loop) that handles a real outage on
        this specific call site without needing this mechanism too.
        """
        if allow_method_disable and method in self._disabled_methods:
            raise RpcMethodDisabled(f"{method} was disabled earlier this run (looked permanently rejected) -- not retrying")

        effective_max_retries = self.max_retries if max_retries is None else max_retries
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        last_exc: Optional[Exception] = None

        for url in [u for u in (self.primary_url, self.failover_url) if u]:
            for attempt in range(effective_max_retries):
                self._wait_out_cooldown(method)
                self._record_call(method)
                self.budget.record_and_check()
                try:
                    data = self._post_with_auto_version_bump(url, payload, method)
                    if "error" in data:
                        raise RpcError(f"{method} RPC error: {data['error']}")
                    self._consecutive_failures = 0
                    self._rate_limit_backoff_level = 0
                    self._cooldown_until = 0.0
                    self._method_unavailable_counts[method] = 0
                    return data.get("result")
                except _MethodUnavailable as exc:
                    last_exc = exc
                    self._consecutive_failures += 1
                    if not allow_method_disable:
                        self.logger.warning(
                            "rpc_method_rejection_ignored",
                            extra={"fields": {"method": method, "params": params, "detail": str(exc)}},
                        )
                        if attempt < effective_max_retries - 1:
                            time.sleep(min(2 ** attempt * 0.5, 4.0))
                        continue
                    self._method_unavailable_counts[method] += 1
                    count = self._method_unavailable_counts[method]
                    if count >= self.method_disable_threshold:
                        self._disabled_methods.add(method)
                        self.logger.error(
                            "rpc_method_disabled",
                            extra={"fields": {"method": method, "params": params, "detail": str(exc), "confirmations": count}},
                        )
                        raise RpcMethodDisabled(
                            f"{method} permanently disabled this run after {count} sustained rejections: {exc}"
                        ) from exc
                    # `params` included here on purpose: the earlier version
                    # of this warning only logged the error message, which
                    # meant confirming what the OUTGOING request actually
                    # contained (e.g. whether maxSupportedTransactionVersion
                    # was really being sent) required reproducing the call
                    # by hand against the provider directly. It's now right
                    # here in the log line instead.
                    self.logger.warning(
                        "rpc_method_possibly_unavailable",
                        extra={
                            "fields": {
                                "method": method, "params": params, "detail": str(exc), "confirmations": count,
                                "threshold": self.method_disable_threshold,
                            }
                        },
                    )
                    if attempt < effective_max_retries - 1:
                        time.sleep(min(2 ** attempt * 0.5, 4.0))
                except RpcRateLimited as exc:
                    last_exc = exc
                    self._consecutive_failures += 1
                    self._trip_rate_limit_cooldown(method, exc)
                    # No sleep here beyond the cooldown itself -- the NEXT
                    # attempt (this loop, or the next call() entirely) will
                    # block in _wait_out_cooldown until the backoff elapses.
                except (requests.RequestException, RpcError, json.JSONDecodeError) as exc:
                    last_exc = exc
                    self._consecutive_failures += 1
                    if attempt < effective_max_retries - 1:
                        time.sleep(min(2 ** attempt * 0.5, 4.0))
            # exhausted retries on this URL, try failover if any

        raise RpcOutage(f"All RPC endpoints failed for {method}: {last_exc}")

    # --- convenience wrappers over commonly used methods ---

    def get_latest_blockhash(self) -> dict:
        return self.call("getLatestBlockhash", [{"commitment": "confirmed"}])

    def get_balance(self, pubkey: str) -> int:
        result = self.call("getBalance", [pubkey, {"commitment": "confirmed"}])
        return result["value"]

    def get_account_info(self, pubkey: str, encoding: str = "jsonParsed") -> Optional[dict]:
        result = self.call("getAccountInfo", [pubkey, {"encoding": encoding, "commitment": "confirmed"}])
        return result["value"] if result else None

    def get_multiple_accounts(self, pubkeys: list[str], encoding: str = "jsonParsed") -> list[Optional[dict]]:
        """Batched getAccountInfo -- one RPC call for up to 100 pubkeys.

        This is the difference between TokenSafety's holder-concentration
        check costing ~40 individual RPC calls (one owner lookup, one
        pool-authority lookup, per top holder) and costing 2. Always prefer
        this over a loop of get_account_info when checking more than one
        address -- see TokenSafety.check_holder_concentration.
        """
        if not pubkeys:
            return []
        out: list[Optional[dict]] = []
        for i in range(0, len(pubkeys), 100):
            chunk = pubkeys[i:i + 100]
            result = self.call("getMultipleAccounts", [chunk, {"encoding": encoding, "commitment": "confirmed"}])
            out.extend(result["value"] if result else [None] * len(chunk))
        return out

    def get_token_supply(self, mint: str) -> Optional[dict]:
        result = self.call("getTokenSupply", [mint, {"commitment": "confirmed"}])
        return result["value"] if result else None

    def get_token_largest_accounts(self, mint: str) -> list[dict]:
        result = self.call("getTokenLargestAccounts", [mint, {"commitment": "confirmed"}])
        return result["value"] if result else []

    def get_recent_prioritization_fees(self, accounts: Optional[list[str]] = None) -> list[dict]:
        return self.call("getRecentPrioritizationFees", [accounts or []])

    def simulate_transaction(self, tx_b64: str, sig_verify: bool = False) -> dict:
        return self.call(
            "simulateTransaction",
            [tx_b64, {"encoding": "base64", "sigVerify": sig_verify, "commitment": "confirmed"}],
        )

    def send_transaction(self, tx_b64: str, skip_preflight: bool = False) -> str:
        return self.call(
            "sendTransaction",
            [tx_b64, {"encoding": "base64", "skipPreflight": skip_preflight, "maxRetries": 0}],
        )

    def get_signature_statuses(self, signatures: list[str]) -> list[Optional[dict]]:
        result = self.call("getSignatureStatuses", [signatures, {"searchTransactionHistory": True}])
        return result["value"]

    def confirm_signature(self, signature: str, timeout_s: float = 60.0, poll_interval_s: float = 1.5) -> bool:
        """Poll until the tx is confirmed/finalized or timeout_s elapses."""
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            statuses = self.get_signature_statuses([signature])
            status = statuses[0] if statuses else None
            if status is not None:
                if status.get("err") is not None:
                    return False
                if status.get("confirmationStatus") in ("confirmed", "finalized"):
                    return True
            time.sleep(poll_interval_s)
        return False


class RpcWebSocket:
    """Async wrapper over Helius WebSocket logsSubscribe/accountSubscribe.

    Reconnects with backoff on drop. Each subscribed stream is consumed as
    an async generator of decoded notification payloads -- callers (mostly
    InsiderRadar and SignalEngine's pump.fun feed) just iterate it.

    On a drop, subscribe() re-establishes the connection and re-sends the
    exact same subscribe request -- it does NOT silently give up or fail to
    resubscribe (see test_rpc_websocket.py's
    test_reconnect_resumes_yielding_notifications_after_drop). What it
    cannot do is recover: logsSubscribe/accountSubscribe are live streams
    with no replay/cursor mechanism on Solana's side, so any notification
    the chain emitted during the gap between the drop and the new
    connection's ack is gone for good, not just delayed. That gap is a real,
    inherent coverage hole in InsiderRadar's indexing (not a bug fixable
    here) -- total_reconnects/last_drop_at in get_ws_stats() and the
    heartbeat log are what make it visible rather than silent.
    """

    def __init__(self, ws_url: str, logger: Optional[logging.Logger] = None) -> None:
        self.ws_url = ws_url
        self.logger = logger or logging.getLogger("memebot.rpc_ws")
        # Aggregate across every concurrent subscribe() call this instance
        # is running (Orchestrator subscribes multiple program IDs off one
        # RpcWebSocket) -- not per-subscription precision, but exactly the
        # granularity the heartbeat log needs: "is anything connected right
        # now, and how flaky has it been."
        self.active_connections = 0
        self.total_reconnects = 0
        self.last_drop_at: Optional[float] = None

    def get_ws_stats(self) -> dict:
        return {
            "active_connections": self.active_connections,
            "total_reconnects": self.total_reconnects,
            "last_drop_at": self.last_drop_at,
        }

    async def subscribe(
        self, method: str, params: list, max_reconnects: int = 1_000_000
    ) -> AsyncIterator[dict]:
        """Yield notification `params.result` payloads forever, reconnecting on drop."""
        attempt = 0
        while attempt < max_reconnects:
            try:
                async with websockets.connect(self.ws_url, ping_interval=20, ping_timeout=20) as ws:
                    sub_request = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
                    await ws.send(json.dumps(sub_request))
                    ack = json.loads(await ws.recv())
                    if "error" in ack:
                        raise RpcError(f"subscribe {method} failed: {ack['error']}")
                    attempt = 0  # reset backoff after a clean connect
                    self.active_connections += 1
                    try:
                        async for raw in ws:
                            msg = json.loads(raw)
                            if "params" in msg and "result" in msg["params"]:
                                yield msg["params"]["result"]
                    finally:
                        self.active_connections -= 1
            except (websockets.exceptions.WebSocketException, OSError, RpcError) as exc:
                attempt += 1
                self.total_reconnects += 1
                self.last_drop_at = time.time()
                delay = min(2 ** attempt, 30)
                self.logger.warning("ws subscribe %s dropped (%s), reconnecting in %ss", method, exc, delay)
                await asyncio.sleep(delay)

    def logs_subscribe(self, mentions_address: str) -> AsyncIterator[dict]:
        return self.subscribe(
            "logsSubscribe",
            [{"mentions": [mentions_address]}, {"commitment": "confirmed"}],
        )

    def account_subscribe(self, pubkey: str) -> AsyncIterator[dict]:
        return self.subscribe(
            "accountSubscribe",
            [pubkey, {"encoding": "jsonParsed", "commitment": "confirmed"}],
        )
