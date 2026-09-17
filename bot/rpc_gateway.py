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
    ) -> None:
        self.primary_url = primary_url
        self.failover_url = failover_url
        self.max_retries = max_retries
        self.budget = RateBudget(limit_per_window=rate_limit_per_10s, on_threshold=on_budget_threshold)
        self.session = session or requests.Session()
        self.logger = logger or logging.getLogger("memebot.rpc_gateway")
        self._consecutive_failures = 0

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
        }

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
        resp.raise_for_status()
        return resp.json()

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

    def call(self, method: str, params: Optional[list] = None) -> Any:
        """JSON-RPC call with retry/backoff, then failover, then RpcOutage.

        A 429 is never retried immediately: it trips a shared cooldown (see
        __init__) that this and every other call waits out before trying
        again, with the wait growing exponentially per consecutive 429 and
        resetting only after a call actually succeeds. This is deliberately
        different from an ordinary transient failure's short fixed backoff
        below -- retrying quickly into an active rate limit is exactly what
        turns one slow endpoint into a budget death spiral.
        """
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        last_exc: Optional[Exception] = None

        for url in [u for u in (self.primary_url, self.failover_url) if u]:
            for attempt in range(self.max_retries):
                self._wait_out_cooldown(method)
                self._record_call(method)
                self.budget.record_and_check()
                try:
                    data = self._post(url, payload)
                    if "error" in data:
                        raise RpcError(f"{method} RPC error: {data['error']}")
                    self._consecutive_failures = 0
                    self._rate_limit_backoff_level = 0
                    self._cooldown_until = 0.0
                    return data.get("result")
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
                    if attempt < self.max_retries - 1:
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
    """

    def __init__(self, ws_url: str, logger: Optional[logging.Logger] = None) -> None:
        self.ws_url = ws_url
        self.logger = logger or logging.getLogger("memebot.rpc_ws")

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
                    async for raw in ws:
                        msg = json.loads(raw)
                        if "params" in msg and "result" in msg["params"]:
                            yield msg["params"]["result"]
            except (websockets.exceptions.WebSocketException, OSError, RpcError) as exc:
                attempt += 1
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
