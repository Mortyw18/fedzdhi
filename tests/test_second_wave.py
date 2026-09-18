"""Orchestrator's event-driven discovery + second-wave entry wiring:
_check_pool_creation (detection -> pending queue) and _second_wave_loop
(price sampling -> age-window/liquidity/price-retention filtering ->
dispatch through the SAME evaluate_candidate path every other discovery
source uses -- no shortcut around TokenSafety for a second-wave entry).
"""
from __future__ import annotations

import asyncio

import time

from bot.config import Config
from bot.models import Candidate, Mode, SafetyCheckResult, SafetyVerdict, SignalSource
from bot.orchestrator import Orchestrator, _PendingPool
from bot.pool_events import PUMPFUN_BONDING_CURVE_PROGRAM_ID, RAYDIUM_AMM_V4_PROGRAM_ID
from conftest import FakeRpc

from test_pool_events import _pumpfun_create_tx, _raydium_initialize2_tx


class _CountingTokenSafety:
    def __init__(self, passed: bool = True) -> None:
        self.calls: list[str] = []
        self.passed = passed

    def evaluate(self, candidate, position_size_lamports, wallet_pubkey, first_buyers=None, distinct_token_lookup=None):
        self.calls.append(candidate.mint)
        return SafetyVerdict(mint=candidate.mint, passed=self.passed, checks=[SafetyCheckResult("x", self.passed, "ok")])


class _FakeSignalEngine:
    """fetch_candidate_by_mint returns whatever's next in a per-mint
    script -- lets a test simulate price/liquidity changing across
    successive _second_wave_loop samples."""

    def __init__(self) -> None:
        self.scripts: dict[str, list] = {}
        self.calls: list[str] = []

    def fetch_candidate_by_mint(self, mint: str):
        self.calls.append(mint)
        script = self.scripts.get(mint)
        if not script:
            return None
        return script.pop(0)


def _price_candidate(mint: str, price_usd: float, liquidity_usd: float = 50_000.0) -> Candidate:
    return Candidate(mint=mint, symbol="TST", source=SignalSource.DEXSCREENER, price_usd=price_usd, liquidity_usd=liquidity_usd)


def _orch(tmp_path, **cfg_overrides) -> Orchestrator:
    defaults = dict(
        mode=Mode.PAPER,
        observe_only=True,
        helius_rpc_url="https://example.invalid/rpc",
        helius_ws_url="wss://example.invalid/ws",
        db_path=":memory:",
        log_dir=str(tmp_path / "logs"),
        kill_switch_state_path=str(tmp_path / "kill_switch_state.json"),
        second_wave_min_age_s=180.0,
        second_wave_max_age_s=600.0,
        second_wave_min_price_retention_pct=0.40,
        second_wave_sample_interval_s=0.01,  # fast for tests
        min_pool_liquidity_usd=15_000.0,
    )
    defaults.update(cfg_overrides)
    cfg = Config(**defaults)
    cfg.validate()
    orch = Orchestrator(cfg)
    orch.rpc = FakeRpc()
    orch.token_safety = _CountingTokenSafety()
    return orch


# ----------------------------------------------------------------------
# _check_pool_creation
# ----------------------------------------------------------------------


def test_matched_creation_event_added_to_pending(tmp_path):
    orch = _orch(tmp_path)
    tx = _raydium_initialize2_tx()
    orch._check_pool_creation(tx, "sig1", RAYDIUM_AMM_V4_PROGRAM_ID)

    assert orch._pool_events_seen == 1
    assert orch._pool_events_matched == 1
    assert len(orch._pending_second_wave) == 1
    pending = next(iter(orch._pending_second_wave.values()))
    assert pending.created_at == 1_700_000_000  # blockTime from the fixture


def test_non_matching_transaction_not_added(tmp_path):
    orch = _orch(tmp_path)
    tx = _raydium_initialize2_tx()
    tx["meta"]["innerInstructions"] = []  # breaks mint resolution -- see test_pool_events.py
    orch._check_pool_creation(tx, "sig1", RAYDIUM_AMM_V4_PROGRAM_ID)

    assert orch._pool_events_seen == 1
    assert orch._pool_events_matched == 0
    assert orch._pending_second_wave == {}


def test_duplicate_creation_event_for_the_same_mint_not_re_added(tmp_path):
    orch = _orch(tmp_path)
    tx = _raydium_initialize2_tx()
    orch._check_pool_creation(tx, "sig1", RAYDIUM_AMM_V4_PROGRAM_ID)
    first = next(iter(orch._pending_second_wave.values()))
    first.samples = 5  # mark it so we can tell it wasn't replaced

    orch._check_pool_creation(tx, "sig2", RAYDIUM_AMM_V4_PROGRAM_ID)

    assert len(orch._pending_second_wave) == 1
    assert next(iter(orch._pending_second_wave.values())).samples == 5


def test_mint_already_verdict_cached_is_not_re_tracked(tmp_path):
    orch = _orch(tmp_path)
    tx = _raydium_initialize2_tx()
    mint = "BaseTokenMint111111111111111111111111111111"
    orch._verdict_cache[mint] = (0.0, 0.0, SafetyVerdict(mint=mint, passed=True, checks=[]))

    orch._check_pool_creation(tx, "sig1", RAYDIUM_AMM_V4_PROGRAM_ID)

    assert orch._pending_second_wave == {}


def test_pending_queue_evicts_oldest_when_over_the_cap(tmp_path):
    orch = _orch(tmp_path, second_wave_max_pending=2)

    for i in range(3):
        tx = _pumpfun_create_tx()
        tx["meta"]["postTokenBalances"][0]["mint"] = f"Mint{i}"
        tx["meta"]["innerInstructions"][0]["instructions"][0]["parsed"]["info"]["mint"] = f"Mint{i}"
        tx["blockTime"] = 1000 + i  # oldest first
        orch._check_pool_creation(tx, f"sig{i}", PUMPFUN_BONDING_CURVE_PROGRAM_ID)

    assert len(orch._pending_second_wave) == 2
    assert "Mint0" not in orch._pending_second_wave  # oldest evicted
    assert "Mint1" in orch._pending_second_wave
    assert "Mint2" in orch._pending_second_wave


# ----------------------------------------------------------------------
# _second_wave_loop
# ----------------------------------------------------------------------


def test_too_young_pool_is_sampled_but_not_dispatched_or_rejected(tmp_path):
    orch = _orch(tmp_path)
    now = time.time()
    orch._pending_second_wave["MintA"] = _PendingPool(mint="MintA", program_id=RAYDIUM_AMM_V4_PROGRAM_ID, created_at=now - 30.0)  # 30s old, window starts at 180s
    fake_se = _FakeSignalEngine()
    fake_se.scripts["MintA"] = [_price_candidate("MintA", price_usd=0.001)]
    orch.signal_engine = fake_se

    async def drive():
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._second_wave_loop())
        await asyncio.sleep(0.03)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    assert "MintA" in orch._pending_second_wave
    assert orch._pending_second_wave["MintA"].samples >= 1
    assert orch._second_wave_dispatched_count == 0
    assert orch._second_wave_expired_count == 0


def test_in_window_pool_with_good_liquidity_and_retention_is_dispatched(tmp_path):

    orch = _orch(tmp_path)
    now = time.time()
    pending = _PendingPool(mint="MintB", program_id=RAYDIUM_AMM_V4_PROGRAM_ID, created_at=now - 240.0)  # 4 min old -- inside [3,10] min
    pending.high_price_usd = 0.0010  # simulate an already-built high-water mark from earlier samples
    orch._pending_second_wave["MintB"] = pending

    fake_se = _FakeSignalEngine()
    fake_se.scripts["MintB"] = [_price_candidate("MintB", price_usd=0.0006, liquidity_usd=50_000.0)]  # 60% retention, above 40% floor
    orch.signal_engine = fake_se

    async def drive():
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._second_wave_loop())
        await asyncio.sleep(0.03)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    assert orch.token_safety.calls == ["MintB"]  # reached the exact same evaluate_candidate path
    assert orch._second_wave_dispatched_count == 1
    assert "MintB" not in orch._pending_second_wave


def test_in_window_pool_below_liquidity_floor_rejected_not_dispatched(tmp_path):

    orch = _orch(tmp_path, min_pool_liquidity_usd=15_000.0)
    now = time.time()
    pending = _PendingPool(mint="MintC", program_id=RAYDIUM_AMM_V4_PROGRAM_ID, created_at=now - 240.0)
    pending.high_price_usd = 0.0010
    orch._pending_second_wave["MintC"] = pending

    fake_se = _FakeSignalEngine()
    fake_se.scripts["MintC"] = [_price_candidate("MintC", price_usd=0.0009, liquidity_usd=5_000.0)]  # good retention, bad liquidity
    orch.signal_engine = fake_se

    async def drive():
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._second_wave_loop())
        await asyncio.sleep(0.03)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    assert orch.token_safety.calls == []
    assert orch._second_wave_rejected_liquidity_count == 1
    assert orch._second_wave_dispatched_count == 0
    assert "MintC" in orch._pending_second_wave  # stays pending -- might recover by the next sample


def test_in_window_pool_price_crashed_below_retention_floor_rejected(tmp_path):

    orch = _orch(tmp_path, second_wave_min_price_retention_pct=0.40)
    now = time.time()
    pending = _PendingPool(mint="MintD", program_id=RAYDIUM_AMM_V4_PROGRAM_ID, created_at=now - 240.0)
    pending.high_price_usd = 0.0010
    orch._pending_second_wave["MintD"] = pending

    fake_se = _FakeSignalEngine()
    fake_se.scripts["MintD"] = [_price_candidate("MintD", price_usd=0.0002, liquidity_usd=50_000.0)]  # 20% retention, below 40% floor
    orch.signal_engine = fake_se

    async def drive():
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._second_wave_loop())
        await asyncio.sleep(0.03)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    assert orch.token_safety.calls == []
    assert orch._second_wave_rejected_retention_count == 1
    assert orch._second_wave_dispatched_count == 0


def test_pool_that_ages_past_the_window_expires_never_dispatched(tmp_path):

    orch = _orch(tmp_path, second_wave_max_age_s=600.0)
    now = time.time()
    pending = _PendingPool(mint="MintE", program_id=RAYDIUM_AMM_V4_PROGRAM_ID, created_at=now - 700.0)  # already past 600s max
    orch._pending_second_wave["MintE"] = pending

    fake_se = _FakeSignalEngine()  # never even queried -- expiry is checked before fetching a fresh price
    orch.signal_engine = fake_se

    async def drive():
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._second_wave_loop())
        await asyncio.sleep(0.03)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    assert orch.token_safety.calls == []
    assert orch._second_wave_expired_count == 1
    assert "MintE" not in orch._pending_second_wave
    assert fake_se.calls == []


def test_pool_not_yet_indexed_by_dexscreener_stays_pending_no_crash(tmp_path):

    orch = _orch(tmp_path)
    now = time.time()
    pending = _PendingPool(mint="MintF", program_id=RAYDIUM_AMM_V4_PROGRAM_ID, created_at=now - 240.0)
    orch._pending_second_wave["MintF"] = pending

    fake_se = _FakeSignalEngine()  # scripts empty -> fetch_candidate_by_mint returns None
    orch.signal_engine = fake_se

    async def drive():
        orch.stop_event = asyncio.Event()
        task = asyncio.create_task(orch._second_wave_loop())
        await asyncio.sleep(0.03)
        orch.stop_event.set()
        await asyncio.wait_for(task, timeout=5.0)

    asyncio.run(drive())

    assert "MintF" in orch._pending_second_wave
    assert orch._second_wave_dispatched_count == 0
    assert orch._second_wave_expired_count == 0
