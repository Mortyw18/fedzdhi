"""RpcWebSocket.get_ws_stats(): the heartbeat log's WS status line needs
active-connection and reconnect-count tracking that didn't exist before --
these are the tests for that bookkeeping in isolation from the rest of
the subscribe()/reconnect machinery.
"""
from __future__ import annotations

import asyncio
import json
from unittest import mock

import pytest
import websockets

from bot.rpc_gateway import RpcWebSocket


class _FakeConnection:
    def __init__(self, messages: list[dict], fail_after: Exception | None = None):
        self._messages = list(messages)
        self._fail_after = fail_after

    async def send(self, data):
        pass

    async def recv(self):
        return json.dumps({"jsonrpc": "2.0", "id": 1, "result": True})

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._messages:
            return json.dumps(self._messages.pop(0))
        if self._fail_after is not None:
            exc, self._fail_after = self._fail_after, None
            raise exc
        raise StopAsyncIteration


class _FakeConnectCM:
    def __init__(self, conn: _FakeConnection):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *args):
        return False


def test_ws_stats_start_at_zero():
    ws = RpcWebSocket("wss://example.invalid")
    stats = ws.get_ws_stats()
    assert stats == {
        "active_connections": 0,
        "total_reconnects": 0,
        "last_drop_at": None,
        "dropped_notifications": 0,
        "dropped_priority_notifications": 0,
        "notifications_received": 0,
        "prefilter_passes": 0,
    }


def test_active_connections_increments_while_connected_and_decrements_after():
    conn = _FakeConnection(messages=[{"params": {"result": {"value": {"signature": "sig1"}}}}])
    ws = RpcWebSocket("wss://example.invalid")

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)):
            agen = ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1)
            got = await agen.__anext__()
            assert got == {"value": {"signature": "sig1"}}
            assert ws.active_connections == 1
            await agen.aclose()
            assert ws.active_connections == 0

    asyncio.run(drive())


def test_reconnect_resumes_yielding_notifications_after_drop():
    """Confirms subscribe() doesn't silently die or get stuck on a drop --
    after reconnecting, the SAME generator (the one InsiderRadar's
    `async for` loop is iterating) keeps yielding live notifications. What
    is genuinely, permanently lost is whatever the chain emitted during the
    gap itself -- logsSubscribe has no replay/cursor to recover that -- but
    the subscription as a whole must not go silent forever after one blip."""
    drop_exc = websockets.exceptions.ConnectionClosedError(None, None)
    conn1 = _FakeConnection(messages=[], fail_after=drop_exc)  # dies right after ack, before any notification
    conn2 = _FakeConnection(messages=[{"params": {"result": {"value": {"signature": "after-reconnect"}}}}])
    ws = RpcWebSocket("wss://example.invalid")

    async def drive():
        with mock.patch(
            "bot.rpc_gateway.websockets.connect",
            side_effect=[_FakeConnectCM(conn1), _FakeConnectCM(conn2)],
        ), mock.patch("bot.rpc_gateway.asyncio.sleep", new=mock.AsyncMock()):
            agen = ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=5)
            got = await agen.__anext__()
            assert got == {"value": {"signature": "after-reconnect"}}
            await agen.aclose()

    asyncio.run(drive())

    stats = ws.get_ws_stats()
    assert stats["total_reconnects"] == 1  # exactly the one drop, before the post-reconnect notification arrived


def test_reconnect_counted_and_stats_updated_on_drop():
    drop_exc = websockets.exceptions.ConnectionClosedError(None, None)
    conn = _FakeConnection(messages=[], fail_after=drop_exc)
    ws = RpcWebSocket("wss://example.invalid")

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)), \
             mock.patch("bot.rpc_gateway.asyncio.sleep", new=mock.AsyncMock()):
            # max_reconnects=1: after the one failure, attempt reaches the
            # cap and subscribe() ends -- deterministic, no real retry loop.
            results = [item async for item in ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1)]

        assert results == []

    asyncio.run(drive())

    stats = ws.get_ws_stats()
    assert stats["total_reconnects"] == 1
    assert stats["last_drop_at"] is not None
    assert stats["active_connections"] == 0


# ----------------------------------------------------------------------
# The socket is drained by a dedicated task, never by the consumer.
#
# subscribe() used to yield straight out of `async for raw in ws`, so the
# socket was only read as fast as the consumer processed notifications.
# Subscribed to two of the busiest programs on Solana, with a consumer
# that awaits a getTransaction per interesting event, that is a permanent
# slow-consumer condition: the library's inbound queue fills, it stops
# reading the TCP socket, and the server hangs up. In production that
# looked like ws_active_connections=0 with ws_total_reconnects climbing.
# ----------------------------------------------------------------------


class _SlowConsumerConnection(_FakeConnection):
    """Delivers every message immediately, without waiting for anyone to
    consume them -- stands in for a firehose subscription."""


def test_socket_is_drained_even_while_the_consumer_is_busy(tmp_path):
    """The whole point: all messages leave the socket promptly, even
    though the consumer never asks for the later ones."""
    messages = [{"params": {"result": {"value": {"signature": f"sig{i}"}}}} for i in range(50)]
    conn = _SlowConsumerConnection(messages=messages)
    ws = RpcWebSocket("wss://example.invalid", max_buffered_notifications=64)

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)):
            agen = ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1)
            first = await agen.__anext__()
            assert first == {"value": {"signature": "sig0"}}
            # Let the drain task run while the consumer does nothing.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert conn._messages == []  # socket fully drained despite an idle consumer
            await agen.aclose()

    asyncio.run(drive())


def test_overflow_drops_oldest_and_counts_it_instead_of_stalling_the_socket():
    """When the consumer can't keep up, falling behind must cost bounded,
    COUNTED coverage -- not the connection itself."""
    messages = [{"params": {"result": {"value": {"signature": f"sig{i}"}}}} for i in range(20)]
    conn = _FakeConnection(messages=messages)
    ws = RpcWebSocket("wss://example.invalid", max_buffered_notifications=4)

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)):
            agen = ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1)
            first = await agen.__anext__()
            assert first is not None
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            assert conn._messages == []  # still fully drained
            await agen.aclose()

    asyncio.run(drive())

    assert ws.get_ws_stats()["dropped_notifications"] > 0  # overflow is visible, not silent


def test_clean_server_close_is_treated_as_a_drop_and_reconnects():
    """A server that closes the stream cleanly (no exception) must still
    trigger the reconnect path rather than ending the generator."""
    conn1 = _FakeConnection(messages=[])  # ends immediately, no exception
    conn2 = _FakeConnection(messages=[{"params": {"result": {"value": {"signature": "after-clean-close"}}}}])
    ws = RpcWebSocket("wss://example.invalid")

    async def drive():
        with mock.patch(
            "bot.rpc_gateway.websockets.connect",
            side_effect=[_FakeConnectCM(conn1), _FakeConnectCM(conn2)],
        ), mock.patch("bot.rpc_gateway.asyncio.sleep", new=mock.AsyncMock()):
            agen = ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=5)
            got = await agen.__anext__()
            assert got == {"value": {"signature": "after-clean-close"}}
            await agen.aclose()

    asyncio.run(drive())

    assert ws.get_ws_stats()["total_reconnects"] == 1


def test_prefiltered_candidates_are_never_evicted_by_ordinary_traffic():
    """The whole reason the prefilter runs on the socket side: a single
    candidate must survive a flood of ordinary traffic many times the
    buffer size. Before this, overflow discarded the oldest of
    EVERYTHING, so a real creation was thrown away at the same rate as
    the swaps burying it (4883 indiscriminate drops in one day)."""
    candidate = {"params": {"result": {"value": {"signature": "CANDIDATE", "logs": ["create"]}}}}
    noise = [
        {"params": {"result": {"value": {"signature": f"noise{i}", "logs": ["swap"]}}}}
        for i in range(200)
    ]
    conn = _FakeConnection(messages=[candidate] + noise)
    ws = RpcWebSocket("wss://example.invalid", max_buffered_notifications=4)

    def prefilter(notification):
        return "create" in ((notification.get("value") or {}).get("logs") or [])

    seen: list[str] = []

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)):
            agen = ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1, prefilter=prefilter)
            # Let the drain task ingest everything before consuming any of it.
            first = await agen.__anext__()
            seen.append(first["value"]["signature"])
            await agen.aclose()

    asyncio.run(drive())

    assert seen == ["CANDIDATE"]  # served first, and never evicted
    stats = ws.get_ws_stats()
    assert stats["prefilter_passes"] == 1
    assert stats["notifications_received"] == 201
    assert stats["dropped_notifications"] > 0          # the noise absorbed all the loss
    assert stats["dropped_priority_notifications"] == 0  # the candidate never did


def test_buffered_notifications_are_drained_before_end_of_stream():
    """End-of-stream must not overtake what is already buffered -- doing
    so silently discards every pending notification at the instant the
    socket drops."""
    messages = [{"params": {"result": {"value": {"signature": f"sig{i}"}}}} for i in range(3)]
    conn = _FakeConnection(messages=messages)
    ws = RpcWebSocket("wss://example.invalid")

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)), \
             mock.patch("bot.rpc_gateway.asyncio.sleep", new=mock.AsyncMock()):
            return [item async for item in ws.subscribe("logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1)]

    got = asyncio.run(drive())

    assert [item["value"]["signature"] for item in got] == ["sig0", "sig1", "sig2"]


def test_a_raising_prefilter_does_not_kill_the_stream():
    """A bug in the matcher must degrade to "treat it as interesting",
    never to a dead subscription."""
    conn = _FakeConnection(messages=[{"params": {"result": {"value": {"signature": "sig1"}}}}])
    ws = RpcWebSocket("wss://example.invalid")

    def exploding_prefilter(_notification):
        raise ValueError("matcher bug")

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)), \
             mock.patch("bot.rpc_gateway.asyncio.sleep", new=mock.AsyncMock()):
            agen = ws.subscribe(
                "logsSubscribe", [{"mentions": ["X"]}], max_reconnects=1, prefilter=exploding_prefilter
            )
            got = await agen.__anext__()
            await agen.aclose()
            return got

    assert asyncio.run(drive()) == {"value": {"signature": "sig1"}}


def test_disconnect_logs_the_reason_and_reconnect_attempt():
    """Explicitly requested diagnostics: every disconnect must say WHY,
    and every reconnect attempt must be visible."""
    import logging

    drop_exc = websockets.exceptions.ConnectionClosedError(None, None)
    conn = _FakeConnection(messages=[], fail_after=drop_exc)
    logger = logging.getLogger("test_ws_disconnect_logging")
    logger.setLevel(logging.INFO)
    records: list[logging.LogRecord] = []
    handler = logging.Handler()
    handler.emit = records.append
    logger.addHandler(handler)

    ws = RpcWebSocket("wss://example.invalid", logger=logger)

    async def drive():
        with mock.patch("bot.rpc_gateway.websockets.connect", return_value=_FakeConnectCM(conn)), \
             mock.patch("bot.rpc_gateway.asyncio.sleep", new=mock.AsyncMock()):
            [item async for item in ws.subscribe("logsSubscribe", [{"mentions": ["ProgramX"]}], max_reconnects=1)]

    asyncio.run(drive())

    connected = [r for r in records if r.getMessage() == "ws_connected"]
    disconnected = [r for r in records if r.getMessage() == "ws_disconnected"]
    assert len(connected) == 1
    assert connected[0].fields["subscription"] == "ProgramX"
    assert len(disconnected) == 1
    fields = disconnected[0].fields
    assert fields["subscription"] == "ProgramX"
    assert "ConnectionClosedError" in fields["reason"]
    assert fields["reconnect_attempt"] == 1
    assert "close_code" in fields
    assert "notifications_received" in fields
