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
    assert stats == {"active_connections": 0, "total_reconnects": 0, "last_drop_at": None}


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
