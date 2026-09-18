"""SignalEngine._dispatch: a slow sync callback must not block the event
loop it's dispatched from.

Orchestrator.evaluate_candidate (the real callback in production) makes
several blocking `requests` calls -- TokenSafety's RPC, Jupiter, rugcheck,
and pump.fun-graduation lookups. Calling it directly from a coroutine
blocks the WHOLE event loop for however long that takes, which starves
everything else sharing it, including RpcWebSocket's ping/pong keepalive
and its next logsSubscribe frame read. That's a real, plausible cause of a
WebSocket dropping with close code 1011 under load -- it looks like the
server's fault, but it can just as easily be self-inflicted starvation.
"""
from __future__ import annotations

import asyncio
import time

from bot.models import Candidate, SignalSource
from bot.signal_engine import SignalEngine


def _engine() -> SignalEngine:
    return SignalEngine(
        min_pool_liquidity_usd=0, max_pool_liquidity_usd=1e12, max_pool_age_s=1e12,
        min_volume_liquidity_ratio=0, min_buy_sell_ratio=0,
    )


def _candidate() -> Candidate:
    return Candidate(mint="Mint1", symbol="TST", source=SignalSource.DEXSCREENER)


def test_slow_sync_callback_does_not_block_other_loop_activity():
    engine = _engine()

    def slow_callback(candidate):
        time.sleep(0.2)  # stands in for evaluate_candidate's blocking network calls

    ticks: list[float] = []

    async def ticker():
        while True:
            ticks.append(time.monotonic())
            await asyncio.sleep(0.02)

    async def drive():
        ticker_task = asyncio.create_task(ticker())
        await engine._dispatch(slow_callback, _candidate())
        ticker_task.cancel()

    asyncio.run(drive())

    # If _dispatch ran the callback directly on the loop, the ticker would
    # get zero (or nearly zero) chances to advance during the 0.2s the
    # callback takes -- it should get several, since the callback is
    # actually running in a worker thread.
    assert len(ticks) >= 5


def test_dispatch_propagates_the_sync_callbacks_exception():
    engine = _engine()

    def exploding_callback(candidate):
        raise RuntimeError("boom")

    async def drive():
        await engine._dispatch(exploding_callback, _candidate())

    try:
        asyncio.run(drive())
        raised = False
    except RuntimeError:
        raised = True
    assert raised


def test_coroutine_function_callback_is_awaited_directly_not_via_executor():
    engine = _engine()
    calls: list[str] = []

    async def async_callback(candidate):
        calls.append(candidate.mint)

    async def drive():
        await engine._dispatch(async_callback, _candidate())

    asyncio.run(drive())
    assert calls == ["Mint1"]
