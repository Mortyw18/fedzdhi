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
from collections import deque
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


@dataclass
class RateBudget:
    """Sliding-window token-bucket-ish budget tracker over a 10s window."""

    limit_per_window: int
    window_s: float = 10.0
    _timestamps: deque = field(default_factory=deque)
    on_threshold: Optional[Callable[[float], None]] = None
    _warned: bool = False

    def record_and_check(self) -> None:
        now = time.monotonic()
        self._timestamps.append(now)
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        usage = len(self._timestamps) / self.limit_per_window
        if usage >= 0.70 and not self._warned:
            self._warned = True
            if self.on_threshold:
                self.on_threshold(usage)
        if usage < 0.70:
            self._warned = False

    def current_usage_pct(self) -> float:
        now = time.monotonic()
        cutoff = now - self.window_s
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        return len(self._timestamps) / self.limit_per_window


class RpcGateway:
    def __init__(
        self,
        primary_url: str,
        failover_url: str = "",
        rate_limit_per_10s: int = 100,
        max_retries: int = 3,
        on_budget_threshold: Optional[Callable[[float], None]] = None,
        session: Optional[requests.Session] = None,
    ) -> None:
        self.primary_url = primary_url
        self.failover_url = failover_url
        self.max_retries = max_retries
        self.budget = RateBudget(limit_per_window=rate_limit_per_10s, on_threshold=on_budget_threshold)
        self.session = session or requests.Session()
        self._consecutive_failures = 0

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures

    def _post(self, url: str, payload: dict) -> dict:
        resp = self.session.post(url, json=payload, timeout=DEFAULT_TIMEOUT_S)
        resp.raise_for_status()
        return resp.json()

    def call(self, method: str, params: Optional[list] = None) -> Any:
        """JSON-RPC call with retry/backoff, then failover, then RpcOutage."""
        payload = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        last_exc: Optional[Exception] = None

        for url in [u for u in (self.primary_url, self.failover_url) if u]:
            for attempt in range(self.max_retries):
                self.budget.record_and_check()
                try:
                    data = self._post(url, payload)
                    if "error" in data:
                        raise RpcError(f"{method} RPC error: {data['error']}")
                    self._consecutive_failures = 0
                    return data.get("result")
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
