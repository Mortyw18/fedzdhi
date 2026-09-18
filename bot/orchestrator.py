"""Orchestrator: wires every module into one asyncio process.

This is the only module that constructs every other module and passes
concrete instances between them, so it's also the only place the
dependency graph has to make sense. Everything below `evaluate_candidate`
is the one and only path a token can take to becoming a position -- there
is no second, shorter path for insider copies. See the module docstring
in token_safety.py for why.
"""
from __future__ import annotations

import asyncio
import logging
import signal
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from nacl.signing import SigningKey

from bot.accounting import Accounting
from bot.alerter import Alerter
from bot.config import Config
from bot.execution_engine import ExecutionEngine, ExecutionFailed, LiveExecutionEngine, PaperExecutionEngine
from bot.insider_radar import InsiderRadar
from bot.jupiter_client import JupiterClient
from bot.kill_switch import KillSwitch
from bot.logging_setup import setup_logging
from bot.models import (
    Candidate,
    CopySignal,
    Mode,
    Position,
    SafetyVerdict,
    SignalSource,
    WalletBuyRecord,
    WalletSellRecord,
)
from bot.pool_events import PoolCreationEvent, detect_pool_creation
from bot.risk_manager import RiskManager
from bot.rpc_gateway import RpcGateway, RpcMethodDisabled, RpcOutage, RpcWebSocket
from bot.signal_engine import SignalEngine
from bot.solana_wallet import Wallet, load_wallet_from_env
from bot.token_safety import TokenSafety

# Programs whose activity we index for InsiderRadar. Detection here is a
# lightweight, generic heuristic (token-balance deltas on any tx that
# mentions one of these programs), not a full per-program instruction
# decoder -- that trade-off is deliberate and documented in HONESTY.md.
# It means we can miss or mis-attribute exotic routes; it does NOT affect
# TokenSafety or RiskManager, which never trust this data blindly (see
# TokenSafety.check_bundled_launch's "no data yet" fallback).
INDEXED_PROGRAM_IDS = {
    "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8": "raydium_amm_v4",
    "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P": "pumpfun_bonding_curve",
}


@dataclass
class _PendingPool:
    """A pool/token detected via event-driven discovery, waiting out the
    second-wave window (config.second_wave_min_age_s..max_age_s) before
    it's either evaluated or expires untouched. See
    Orchestrator._second_wave_loop."""

    mint: str
    program_id: str
    created_at: float  # block_time if the tx carried one, else our own detection-time clock
    high_price_usd: float = 0.0
    last_price_usd: float = 0.0
    last_liquidity_usd: float = 0.0
    samples: int = 0


class Orchestrator:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.logger = setup_logging(config.log_dir)
        # Deliberately NOT constructed here: asyncio.Event() must be created
        # while its target loop is running (Orchestrator() is called from
        # cli.main() *before* asyncio.run() starts one). Creating it here
        # binds it to whatever implicit loop exists at that moment -- which,
        # on Python < 3.10, may not be the loop asyncio.run() later creates,
        # and awaiting it then raises "Task got a Future attached to a
        # different loop". It's set for real at the top of run().
        self.stop_event: Optional[asyncio.Event] = None

        self.accounting = Accounting(config.db_path, logger=self.logger)
        self.kill_switch = KillSwitch(
            config.daily_loss_cap_sol,
            max_consecutive_failures=config.max_consecutive_execution_failures,
            max_consecutive_rpc_outages=config.max_consecutive_rpc_outages,
            state_path=config.kill_switch_state_path,
            logger=self.logger,
        )
        self.risk_manager = RiskManager(config, self.kill_switch, logger=self.logger)
        self.alerter = Alerter(config.telegram_bot_token, config.telegram_chat_id, logger=self.logger)

        # The kill switch persisting across restarts is deliberate (see its
        # module docstring) -- but it must never be silent about it. A
        # crash-loop or an operator who forgot it was halted both need this
        # to be impossible to miss, not a line buried in DEBUG output.
        if self.kill_switch.is_halted():
            reason = self.kill_switch.halt_reason()
            self.logger.error(
                "startup_kill_switch_already_halted",
                extra={"fields": {"reason": reason, "state_path": config.kill_switch_state_path}},
            )
            self.alerter.notify(
                f"STARTUP: kill switch is ALREADY HALTED from a previous run ({reason}). "
                "No buys will happen until you run `python run.py --reset-kill-switch`."
            )

        self.rpc = RpcGateway(
            config.helius_rpc_url,
            config.failover_rpc_url,
            rate_limit_per_10s=config.rpc_rate_limit_per_10s,
            on_budget_threshold=lambda pct: self.alerter.notify(f"RPC budget at {pct:.0%} of rate limit"),
        )
        self.jupiter = JupiterClient(logger=self.logger)
        self.token_safety = TokenSafety(
            self.rpc,
            self.jupiter,
            max_top10_holder_pct=config.max_top10_holder_pct,
            max_acceptable_price_impact_pct=config.max_acceptable_price_impact_pct,
            bundled_launch_min_distinct_tokens=config.bundled_launch_min_distinct_tokens,
            enable_pumpfun_lookups=config.enable_pumpfun_graduation_lookup,
            logger=self.logger,
        )
        self.signal_engine = SignalEngine(
            min_pool_liquidity_usd=config.min_pool_liquidity_usd,
            max_pool_liquidity_usd=config.max_pool_liquidity_usd,
            max_pool_age_s=config.max_pool_age_s,
            min_volume_liquidity_ratio=config.min_volume_liquidity_ratio,
            min_buy_sell_ratio=config.min_buy_sell_ratio,
            dexscreener_poll_interval_s=config.dexscreener_poll_interval_s,
            dexscreener_search_queries=config.dexscreener_search_queries,
            pumpfun_max_consecutive_failures=config.pumpfun_max_consecutive_failures,
            enable_pumpfun_source=config.enable_pumpfun_source,
            logger=self.logger,
        )
        self.insider_radar = InsiderRadar(
            first_buyers_n=config.insider_first_buyers_n,
            conviction_min_trades=config.conviction_min_trades,
            conviction_min_distinct_tokens=config.conviction_min_distinct_tokens,
            conviction_min_hold_s=config.conviction_min_hold_s,
            conviction_min_wallet_age_days=config.conviction_min_wallet_age_days,
            auto_unfollow_trailing_n=config.auto_unfollow_trailing_n,
            copy_max_price_move_pct=config.copy_max_price_move_pct,
            logger=self.logger,
        )

        self.wallet: Optional[Wallet] = None
        if config.mode == Mode.LIVE:
            self.wallet = load_wallet_from_env()
            self.execution: ExecutionEngine = LiveExecutionEngine(
                self.rpc,
                self.jupiter,
                self.wallet,
                slippage_multiplier=config.slippage_impact_multiplier,
                slippage_flat_addon_pct=config.slippage_flat_addon_pct,
                slippage_hard_cap_pct=config.slippage_hard_cap_pct,
                slippage_emergency_cap_pct=config.slippage_emergency_cap_pct,
                max_retries=config.execution_max_retries,
                logger=self.logger,
            )
            self.wallet_pubkey = self.wallet.pubkey_base58
        else:
            # Paper mode still needs a syntactically valid pubkey to build
            # (never sign or send) Jupiter transactions for safety-check
            # simulation. Generated fresh each run, never persisted or funded.
            ephemeral = SigningKey.generate()
            import base58 as _b58

            self.wallet_pubkey = _b58.b58encode(bytes(ephemeral.verify_key)).decode("ascii")
            self.execution = PaperExecutionEngine(self.jupiter, haircut_pct=config.paper_fill_haircut_pct, logger=self.logger)

        from bot.exit_monitor import ExitMonitor

        self.exit_monitor = ExitMonitor(
            self.jupiter,
            self.risk_manager,
            self.execution,
            self.accounting,
            self.alerter,
            token_decimals_lookup=self._get_token_decimals,
            poll_interval_s=config.exit_monitor_poll_interval_s,
            logger=self.logger,
        )

        self.open_positions: dict[str, Position] = {}
        self._token_decimals_cache: dict[str, int] = {}
        self._ws: Optional[RpcWebSocket] = None
        self._indexing_skipped_count = 0
        # Kill-switch-independent degrade for indexing's own RPC failures --
        # see _index_program_loop and KillSwitch's module docstring for why
        # this must never call kill_switch.set_rpc_outage. ALL of this is
        # shared/global across every concurrent _index_program_loop task
        # (one per entry in INDEXED_PROGRAM_IDS) -- see config.py's
        # indexing_rpc_failure_backoff_base_s docstring for why per-loop
        # state doesn't actually throttle anything under concurrent load.
        self._indexing_rpc_consecutive_failures = 0
        self._indexing_backoff_until = 0.0  # monotonic; _indexing_skip_reason() checks this
        self._indexing_last_call_at = 0.0
        self._indexing_call_timestamps: deque[float] = deque()  # sliding 60s window, for indexing_max_calls_per_minute
        # mint -> (cached_at, liquidity_usd at that time, verdict). See
        # _get_cached_verdict / evaluate_candidate.
        self._verdict_cache: dict[str, tuple[float, float, SafetyVerdict]] = {}

        # Event-driven discovery + second-wave entry state. See
        # _check_pool_creation / _second_wave_loop / config.py's
        # "event-driven discovery + second-wave entry" section.
        self._pending_second_wave: dict[str, _PendingPool] = {}
        self._pool_events_seen = 0          # every notification actually checked (post RPC-budget throttling)
        self._pool_events_matched = 0       # of those, matched a creation/launch instruction with a resolved mint
        self._second_wave_dispatched_count = 0
        self._second_wave_expired_count = 0
        self._second_wave_rejected_liquidity_count = 0
        self._second_wave_rejected_retention_count = 0

        if config.helius_ws_url:
            self._ws = RpcWebSocket(config.helius_ws_url, logger=self.logger)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _get_token_decimals(self, mint: str) -> int:
        if mint in self._token_decimals_cache:
            return self._token_decimals_cache[mint]
        try:
            supply = self.rpc.get_token_supply(mint)
            decimals = int(supply["decimals"]) if supply else 6
        except RpcOutage:
            decimals = 6
        self._token_decimals_cache[mint] = decimals
        return decimals

    def _open_positions_list(self) -> list[Position]:
        return list(self.open_positions.values())

    @staticmethod
    def _verdict_indicates_rpc_outage(verdict) -> bool:
        """True when at least one of TokenSafety's RPC-backed checks
        (mint_freeze_authority, lp_burned_or_graduated, holder_concentration,
        transfer_fee_tax) failed with TokenSafety's own "rpc error: " detail
        prefix -- i.e. an RPC call inside this evaluate() actually failed,
        not just a candidate that failed on its own merits (no LP mint
        known, too concentrated, price impact too high, ...). Deliberately
        an OR, not "every failing check": check_lp_or_graduation fails with
        a non-RPC reason ("no LP mint known") for almost every DexScreener
        candidate regardless of RPC health, since parse_dexscreener_pair
        never populates lp_mint -- requiring ALL failures to be RPC-flavored
        would make this signal never fire in practice. This is the single
        signal that feeds KillSwitch.set_rpc_outage; see evaluate_candidate."""
        return any(not c.passed and c.detail.startswith("rpc error:") for c in verdict.checks)

    # ------------------------------------------------------------------
    # the one path from candidate to position
    # ------------------------------------------------------------------

    def _get_cached_verdict(self, candidate: Candidate) -> Optional[SafetyVerdict]:
        """A cache hit means "skip the whole safety pipeline for this
        rediscovery," not "reuse a stale verdict to act on" -- callers
        return immediately on a hit rather than doing anything with the
        verdict itself, since a cache hit exists purely to stop
        re-evaluating the SAME mint every poll cycle. See
        verdict_cache_ttl_s's docstring in config.py for why this exists:
        DexScreener rediscovers the same actively-trending mints every
        single cycle by design, so without this, evaluate_candidate's full
        RPC/Jupiter/rugcheck/pump.fun pipeline reran on unchanged mints
        forever.

        Expired entries are evicted here rather than left to accumulate --
        this cache has no separate cleanup pass.
        """
        cached = self._verdict_cache.get(candidate.mint)
        if cached is None:
            return None
        cached_at, cached_liquidity, verdict = cached

        if time.time() - cached_at >= self.config.verdict_cache_ttl_s:
            del self._verdict_cache[candidate.mint]
            return None

        # A big liquidity swing is treated as a state-change event that
        # invalidates the cache early, even inside the TTL -- cheap (data
        # SignalEngine already gave us) and a reasonable proxy for "enough
        # changed here that the old verdict might not hold," e.g. a rug
        # pull draining the pool or a real pump attracting size.
        if cached_liquidity > 0:
            change = abs(candidate.liquidity_usd - cached_liquidity) / cached_liquidity
            if change >= self.config.verdict_cache_liquidity_change_pct:
                del self._verdict_cache[candidate.mint]
                return None

        return verdict

    def evaluate_candidate(self, candidate: Candidate) -> None:
        self.accounting.record_candidate(candidate)

        if self._get_cached_verdict(candidate) is not None:
            self.logger.debug("candidate skipped: cached verdict still fresh for %s", candidate.mint)
            return

        # Observe-only (M2) skips the risk gate entirely: we want a safety
        # verdict logged for every candidate, not a subset filtered by
        # concurrency/day caps that only matter once we're actually trading.
        if not self.config.observe_only:
            allowed, reason = self.risk_manager.can_open_position(candidate.mint, self._open_positions_list())
            if not allowed:
                self.logger.info("candidate skipped (risk gate): %s -- %s", candidate.mint, reason)
                return

        candidate.token_decimals = self._get_token_decimals(candidate.mint)
        first_buyers = self.insider_radar.get_first_buyers(candidate.mint)

        verdict = self.token_safety.evaluate(
            candidate,
            position_size_lamports=int(self.config.position_size_sol * 1_000_000_000),
            wallet_pubkey=self.wallet_pubkey,
            first_buyers=first_buyers,
            distinct_token_lookup=self.insider_radar.distinct_token_count,
        )
        self.accounting.record_safety_verdict(verdict)

        # TokenSafety is the ONLY RPC-outage signal that feeds the kill
        # switch: it's the price-critical path a buy is actually gated on,
        # unlike InsiderRadar's best-effort background indexing (see
        # _index_program_loop and KillSwitch's module docstring for the
        # incident that made this split necessary). See
        # _verdict_indicates_rpc_outage for exactly what counts.
        rpc_outage_signal = self._verdict_indicates_rpc_outage(verdict)
        newly_tripped = self.kill_switch.set_rpc_outage(rpc_outage_signal)
        if newly_tripped:
            self.alerter.notify_kill_switch(self.kill_switch.halt_reason())

        if not rpc_outage_signal:
            # An RPC-error verdict means "unknown," not a real pass/fail --
            # caching it would suppress re-evaluating this mint for the
            # full TTL exactly when a fresh attempt is most wanted (as soon
            # as it's rediscovered, since the RPC may already have
            # recovered by then). It also must never be allowed to mask a
            # SUSTAINED outage: repeatedly rediscovering the same mint
            # during a real outage should keep counting toward
            # set_rpc_outage's threshold above, not get silently absorbed
            # by the cache after the first attempt.
            self._verdict_cache[candidate.mint] = (time.time(), candidate.liquidity_usd, verdict)

        if self.config.observe_only:
            # The entire point of M2: log the verdict, place no trade, live or paper.
            self.logger.info(
                "observe_only_verdict",
                extra={"fields": {"mint": candidate.mint, "passed": verdict.passed, "source": candidate.source.value}},
            )
            return

        if not verdict.passed:
            return

        self._open_position(candidate)

    def _open_position(self, candidate: Candidate) -> None:
        try:
            fill = self.execution.buy(candidate.mint, self.config.position_size_sol, candidate.token_decimals)
        except ExecutionFailed as exc:
            newly_tripped = self.risk_manager.register_execution_failure()
            self.logger.error("buy failed for %s: %s", candidate.mint, exc)
            self.alerter.notify(f"BUY FAILED {candidate.symbol}: {exc}")
            if newly_tripped:
                self.alerter.notify_kill_switch(self.kill_switch.halt_reason())
            return

        self.risk_manager.register_execution_success()
        self.risk_manager.register_buy()

        position = Position(
            mint=candidate.mint,
            symbol=candidate.symbol,
            size_sol=self.config.position_size_sol,
            entry_price_usd=fill.fill_price_usd,
            tokens_held=fill.tokens,
            source=candidate.source,
            leader_wallet=candidate.leader_wallet,
        )
        fill.position_id = position.id
        self.accounting.record_fill(fill)
        self.accounting.record_position_opened(position)
        self.open_positions[position.id] = position
        self.alerter.notify_entry(position)

    def handle_copy_signal(self, signal: CopySignal) -> None:
        candidate = self.signal_engine.fetch_candidate_by_mint(signal.mint)
        if candidate is None:
            self.logger.info("copy signal skipped: no market data for %s", signal.mint)
            return
        valid, reason = self.insider_radar.is_copy_still_valid(signal, candidate.price_usd)
        if not valid:
            self.logger.info("copy signal skipped for %s: %s", signal.mint, reason)
            return
        candidate.source = SignalSource.INSIDER_COPY
        candidate.leader_wallet = signal.leader_wallet
        candidate.leader_entry_price_usd = signal.leader_entry_price_usd
        # Same TokenSafety gate as every other candidate -- no shortcuts for leaders.
        self.evaluate_candidate(candidate)

    # ------------------------------------------------------------------
    # on-chain indexing for InsiderRadar (best-effort, see module docstring)
    # ------------------------------------------------------------------

    def _parse_leader_activity(self, tx: dict, signature: str) -> None:
        meta = tx.get("meta") or {}
        slot = tx.get("slot", 0)
        pre_tb = {(b["owner"], b["mint"]): float(b["uiTokenAmount"]["uiAmount"] or 0.0) for b in meta.get("preTokenBalances") or []}
        post_tb = {(b["owner"], b["mint"]): float(b["uiTokenAmount"]["uiAmount"] or 0.0) for b in meta.get("postTokenBalances") or []}

        account_keys = (tx.get("transaction") or {}).get("message", {}).get("accountKeys", [])
        pre_sol = meta.get("preBalances") or []
        post_sol = meta.get("postBalances") or []

        for (owner, mint), post_amount in post_tb.items():
            pre_amount = pre_tb.get((owner, mint), 0.0)
            token_delta = post_amount - pre_amount
            if abs(token_delta) < 1e-9:
                continue
            try:
                idx = [k.get("pubkey") if isinstance(k, dict) else k for k in account_keys].index(owner)
                sol_delta = (post_sol[idx] - pre_sol[idx]) / 1_000_000_000 if idx < len(post_sol) else 0.0
            except ValueError:
                sol_delta = 0.0

            price_usd = 0.0  # unknown from balance deltas alone without an oracle; scoring tolerates 0
            if token_delta > 0 and sol_delta < 0:
                buy_record = WalletBuyRecord(
                    wallet=owner, mint=mint, slot=slot, amount_sol=abs(sol_delta), price_usd=price_usd, tokens=abs(token_delta)
                )
                self.insider_radar.record_buy(buy_record)
                signal = self.insider_radar.evaluate_copy_signal(buy_record)
                if signal is not None:
                    self.handle_copy_signal(signal)
            elif token_delta < 0 and sol_delta > 0:
                self.insider_radar.record_sell(
                    WalletSellRecord(
                        wallet=owner, mint=mint, slot=slot, amount_sol=abs(sol_delta), price_usd=price_usd, tokens=abs(token_delta)
                    )
                )

    # ------------------------------------------------------------------
    # event-driven discovery + second-wave entry
    # ------------------------------------------------------------------

    def _check_pool_creation(self, tx: dict, signature: str, program_id: str) -> None:
        """Runs on the SAME transaction _parse_leader_activity just used --
        InsiderRadar's indexing subscription already fetches every
        notification's transaction via getTransaction, so detecting a
        pool creation/token launch here costs zero additional RPC calls.
        This is what makes event-driven discovery "free" on top of
        indexing that was already running, rather than a second parallel
        subscription duplicating the same traffic.

        See pool_events.py's module docstring for exactly what confidence
        level the detection itself is built on.
        """
        self._pool_events_seen += 1
        event = detect_pool_creation(tx, signature, program_id)
        if event is None:
            return
        self._pool_events_matched += 1

        if event.mint in self._pending_second_wave or event.mint in self._verdict_cache:
            return  # already tracking it, or already ran it through TokenSafety this cycle

        if len(self._pending_second_wave) >= self.config.second_wave_max_pending:
            oldest_mint = min(self._pending_second_wave, key=lambda m: self._pending_second_wave[m].created_at)
            del self._pending_second_wave[oldest_mint]
            self.logger.warning(
                "second_wave_pending_evicted",
                extra={"fields": {"mint": oldest_mint, "reason": "second_wave_max_pending exceeded"}},
            )

        created_at = event.block_time or time.time()
        self._pending_second_wave[event.mint] = _PendingPool(mint=event.mint, program_id=program_id, created_at=created_at)
        self.logger.info(
            "pool_creation_detected",
            extra={"fields": {"mint": event.mint, "program_id": program_id, "signature": signature}},
        )

    async def _second_wave_loop(self) -> None:
        """Periodically samples every pending pool's live price (building
        a high-water mark) and, once its age enters the second-wave
        window, checks liquidity + price-retention ("first dump
        absorbed") before dispatching it through the EXACT SAME
        evaluate_candidate path every other discovery source uses -- no
        shortcut around TokenSafety for a second-wave candidate.
        """
        loop = asyncio.get_running_loop()
        while not self.stop_event.is_set():
            await asyncio.sleep(self.config.second_wave_sample_interval_s)
            now = time.time()
            expired: list[str] = []
            dispatched: list[str] = []

            for mint, pending in list(self._pending_second_wave.items()):
                age_s = now - pending.created_at
                if age_s > self.config.second_wave_max_age_s:
                    expired.append(mint)
                    continue

                candidate = await loop.run_in_executor(None, self.signal_engine.fetch_candidate_by_mint, mint)
                if candidate is None or candidate.price_usd <= 0:
                    continue  # not indexed by DexScreener yet (or zero liquidity) -- try again next sample

                pending.last_price_usd = candidate.price_usd
                pending.last_liquidity_usd = candidate.liquidity_usd
                pending.high_price_usd = max(pending.high_price_usd, candidate.price_usd)
                pending.samples += 1

                if age_s < self.config.second_wave_min_age_s:
                    continue  # too young -- keep sampling to build the high-water mark, don't evaluate yet

                if candidate.liquidity_usd < self.config.min_pool_liquidity_usd:
                    self._second_wave_rejected_liquidity_count += 1
                    self.logger.debug(
                        "second_wave_reject: %s liquidity $%.0f below floor $%.0f",
                        mint, candidate.liquidity_usd, self.config.min_pool_liquidity_usd,
                    )
                    continue

                retention = (pending.last_price_usd / pending.high_price_usd) if pending.high_price_usd > 0 else 0.0
                if retention < self.config.second_wave_min_price_retention_pct:
                    self._second_wave_rejected_retention_count += 1
                    self.logger.debug(
                        "second_wave_reject: %s price retention %.0f%% below floor %.0f%% (high $%.8f, now $%.8f)",
                        mint, retention * 100, self.config.second_wave_min_price_retention_pct * 100,
                        pending.high_price_usd, pending.last_price_usd,
                    )
                    continue

                dispatched.append(mint)
                self._second_wave_dispatched_count += 1
                self.logger.info(
                    "second_wave_dispatch",
                    extra={
                        "fields": {
                            "mint": mint, "age_s": round(age_s, 1), "price_retention_pct": round(retention, 4),
                            "high_price_usd": pending.high_price_usd, "liquidity_usd": candidate.liquidity_usd,
                            "samples": pending.samples,
                        }
                    },
                )
                candidate.source = SignalSource.DEXSCREENER
                await loop.run_in_executor(None, self._on_candidate, candidate)

            for mint in dispatched:
                del self._pending_second_wave[mint]
            for mint in expired:
                self._second_wave_expired_count += 1
                self.logger.info("second_wave_expired", extra={"fields": {"mint": mint}})
                del self._pending_second_wave[mint]

    def _prune_indexing_call_window(self, now: Optional[float] = None) -> None:
        now = time.monotonic() if now is None else now
        cutoff = now - 60.0
        while self._indexing_call_timestamps and self._indexing_call_timestamps[0] < cutoff:
            self._indexing_call_timestamps.popleft()

    def _indexing_skip_reason(self) -> Optional[str]:
        """Indexing is the lowest-priority RPC consumer in this bot: it's
        background learning, not a trade that's waiting on a price. A
        subscription to an AMM program's logs can be an enormous stream
        (most swap activity network-wide mentions it, not just our own
        candidates), so it must never be what pushes RPC usage into the
        danger zone TokenSafety and ExitMonitor actually depend on.

        This is the ONE gate both concurrent indexing loops (one per entry
        in INDEXED_PROGRAM_IDS) check before every notification, which is
        exactly what makes the backoff/rate-cap/calls-per-minute state
        below actually global instead of each loop independently deciding
        for itself -- see indexing_rpc_failure_backoff_base_s's docstring
        in config.py for the incident that made per-loop state useless.

        Returns a reason string if the current notification should be
        dropped without ever making an RPC call, or None if it's fine to
        proceed. Kept as a small, pure, synchronous method so the backoff
        policy is directly testable without spinning up the async loop.
        """
        if self.kill_switch.is_halted():
            return f"kill switch halted ({self.kill_switch.halt_reason()})"
        if self.rpc.is_method_disabled("getTransaction"):
            return "getTransaction permanently disabled this run (see rpc_method_disabled log)"
        remaining = self._indexing_backoff_until - time.monotonic()
        if remaining > 0:
            return f"backing off ({remaining:.1f}s remaining)"
        self._prune_indexing_call_window()
        if len(self._indexing_call_timestamps) >= self.config.indexing_max_calls_per_minute:
            return f"indexing calls/min cap reached ({self.config.indexing_max_calls_per_minute}/min)"
        usage = self.rpc.budget.current_usage_pct()
        if usage >= self.config.indexing_max_rpc_budget_pct:
            return f"RPC budget usage {usage:.0%} >= indexing ceiling {self.config.indexing_max_rpc_budget_pct:.0%}"
        return None

    async def _index_program_loop(self, program_id: str) -> None:
        if self._ws is None:
            return
        async for notification in self._ws.logs_subscribe(program_id):
            if self.stop_event.is_set():
                return
            skip_reason = self._indexing_skip_reason()
            if skip_reason is not None:
                self._indexing_skipped_count += 1
                self.logger.debug("indexing notification dropped: %s", skip_reason)
                continue
            signature = (notification.get("value") or {}).get("signature")
            if not signature:
                continue

            # Hard local rate cap on indexing's OWN getTransaction calls,
            # shared across every concurrent loop -- independent of the RPC
            # budget ceiling above, of how fast logsSubscribe notifications
            # actually arrive, and of the exponential backoff below. A busy
            # AMM program can fire several notifications a second; without
            # this, that turns directly into several RPC calls a second
            # from indexing as a whole, even before any failure has
            # happened to trigger the backoff path.
            now = time.monotonic()
            elapsed = now - self._indexing_last_call_at
            if elapsed < self.config.indexing_min_call_interval_s:
                await asyncio.sleep(self.config.indexing_min_call_interval_s - elapsed)
            self._indexing_last_call_at = time.monotonic()
            self._indexing_call_timestamps.append(self._indexing_last_call_at)

            try:
                tx = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self.rpc.call(
                        "getTransaction",
                        [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
                        # RpcGateway's own internal retry loop (default 3
                        # attempts, with sleeps between) would otherwise run
                        # on EVERY single call regardless of what indexing's
                        # own backoff below is doing -- at up to ~1.5s of
                        # wall time per failing call, that retry-inside-a-
                        # retry was what actually produced the "still every
                        # 1-3s, looks like fixed-interval retry" pattern,
                        # not a lack of backoff. One attempt here means
                        # indexing's own exponential backoff is the sole
                        # authority over how long a failure costs.
                        max_retries=1,
                        # getTransaction must NEVER be permanently disabled:
                        # it's the single most fundamental Solana read
                        # method, not the kind of plan-gated enhanced
                        # endpoint RpcMethodDisabled exists for, and both
                        # InsiderRadar indexing and event-driven discovery
                        # (pool_events) ride on this exact call. A single
                        # spurious 403 (WAF blip, proxy hiccup, anything
                        # unrelated to "this method is unavailable")
                        # permanently disabling it went completely silent
                        # for a full run in production before this flag
                        # existed -- indexing's own exponential backoff
                        # above already handles a genuine sustained outage
                        # on this call site without needing the disable
                        # mechanism too.
                        allow_method_disable=False,
                    ),
                )
                self._indexing_rpc_consecutive_failures = 0
                if tx:
                    self._parse_leader_activity(tx, signature)
                    if self.config.enable_event_driven_discovery:
                        # Same already-fetched transaction, no extra RPC
                        # cost -- see _check_pool_creation's docstring for
                        # why this rides on indexing's existing
                        # subscription instead of a separate one.
                        self._check_pool_creation(tx, signature, program_id)
            except RpcMethodDisabled as exc:
                # Permanently rejected (403, or a JSON-RPC error that reads
                # like "not available on this plan") -- RpcGateway already
                # logged rpc_method_disabled ONCE, the instant it detected
                # this. Nothing left to do here: _indexing_skip_reason()
                # above will drop every future notification before ever
                # reaching this call again, so there's no ongoing failure
                # to back off from or log repeatedly.
                self.logger.debug("indexing notification dropped: %s", exc)
            except RpcOutage as exc:
                # Indexing is best-effort background learning, not a trade
                # waiting on a price -- its RPC failures must degrade
                # gracefully and NEVER touch the kill switch (that's fed
                # exclusively by TokenSafety's price-critical checks, see
                # evaluate_candidate). The method + error are logged every
                # time so a real outage is still fully diagnosable from the
                # logs alone.
                self._indexing_rpc_consecutive_failures += 1
                self.logger.warning(
                    "indexing_rpc_failure",
                    extra={
                        "fields": {
                            "method": "getTransaction",
                            "program_id": program_id,
                            "consecutive": self._indexing_rpc_consecutive_failures,
                            "error": str(exc),
                        }
                    },
                )
                # Exponential from the very first failure, not after a
                # grace threshold -- and written to the SHARED
                # _indexing_backoff_until timestamp rather than an
                # await-sleep in just this task, so it's respected by every
                # concurrent indexing loop via _indexing_skip_reason() above,
                # not just the one that happened to hit the failure.
                backoff = min(
                    self.config.indexing_rpc_failure_backoff_base_s
                    * (2 ** (self._indexing_rpc_consecutive_failures - 1)),
                    self.config.indexing_rpc_failure_backoff_max_s,
                )
                self._indexing_backoff_until = time.monotonic() + backoff
                self.logger.warning(
                    "indexing_backing_off",
                    extra={
                        "fields": {
                            "program_id": program_id,
                            "backoff_s": backoff,
                            "consecutive_failures": self._indexing_rpc_consecutive_failures,
                        }
                    },
                )

    # ------------------------------------------------------------------
    # daily/weekly reporting
    # ------------------------------------------------------------------

    async def _daily_report_loop(self) -> None:
        while not self.stop_event.is_set():
            await asyncio.sleep(24 * 3600)
            report = self.accounting.daily_report()
            self.logger.info("daily_report", extra={"fields": report})
            self.alerter.notify_rejection_digest(report)

    async def _rpc_budget_snapshot_loop(self) -> None:
        """Persists RpcGateway's live call stats AND InsiderRadar's live
        indexing stats periodically, so both show up in `--daily-report` /
        `--radar-stats` even though those one-shot commands never start a
        real gateway or radar -- they only ever read this history back out
        of SQLite. Same loop, same interval: neither is expensive enough
        to warrant its own timer."""
        while not self.stop_event.is_set():
            await asyncio.sleep(self.config.rpc_budget_snapshot_interval_s)
            stats = self.rpc.get_call_stats()
            self.accounting.record_rpc_snapshot(stats)
            self.logger.debug(
                "rpc_budget_snapshot",
                extra={"fields": {**stats, "indexing_skipped": self._indexing_skipped_count}},
            )
            radar_stats = self.insider_radar.get_stats()
            self.accounting.record_radar_snapshot(radar_stats)
            self.logger.debug("radar_snapshot", extra={"fields": radar_stats})

    async def _heartbeat_loop(self) -> None:
        """One INFO-level line every heartbeat_interval_s (default 60s),
        unconditionally -- unlike the DEBUG-level snapshot logs above, this
        is meant to be visible in a normal console/log-tail without
        cranking verbosity, specifically so an operator (or a `tail -f` at
        3am) can tell "quiet and healthy" from "silently stalled" without
        waiting for the next daily report."""
        while not self.stop_event.is_set():
            await asyncio.sleep(self.config.heartbeat_interval_s)
            rpc_stats = self.rpc.get_call_stats()
            ws_stats = self._ws.get_ws_stats() if self._ws is not None else {"active_connections": 0, "total_reconnects": 0, "last_drop_at": None}
            self.logger.info(
                "heartbeat",
                extra={
                    "fields": {
                        "dexscreener_polls_done": self.signal_engine.dexscreener_polls_done,
                        "pumpfun_polls_done": self.signal_engine.pumpfun_polls_done,
                        "pumpfun_disabled": self.signal_engine.pumpfun_disabled,
                        "rpc_calls_per_minute": rpc_stats["calls_per_minute"],
                        "rpc_budget_usage_pct": rpc_stats["budget_usage_pct"],
                        "rpc_total_calls": rpc_stats["total_calls"],
                        "ws_active_connections": ws_stats["active_connections"],
                        "ws_total_reconnects": ws_stats["total_reconnects"],
                        "kill_switch_halted": self.kill_switch.is_halted(),
                        "open_positions": len(self.open_positions),
                        "indexing_skipped": self._indexing_skipped_count,
                        "indexing_backing_off": self._indexing_backoff_until > time.monotonic(),
                        "rpc_disabled_methods": rpc_stats["disabled_methods"],
                        "ws_pool_events_seen": self._pool_events_seen,
                        "ws_pool_events_matched": self._pool_events_matched,
                        "second_wave_pending": len(self._pending_second_wave),
                        "second_wave_dispatched_total": self._second_wave_dispatched_count,
                        "second_wave_expired_total": self._second_wave_expired_count,
                        "second_wave_rejected_liquidity_total": self._second_wave_rejected_liquidity_count,
                        "second_wave_rejected_retention_total": self._second_wave_rejected_retention_count,
                    }
                },
            )

    def status_text(self) -> str:
        if self.config.observe_only:
            return "mode=observe-only (no trades placed, live or paper) -- see --daily-report for verdict stats"
        open_count = len(self.open_positions)
        return (
            f"mode={self.config.mode.value} open_positions={open_count}/{self.config.max_concurrent_positions} "
            f"buys_today={self.kill_switch.buys_today}/{self.config.max_buys_per_day} "
            f"daily_pnl={self.kill_switch.daily_pnl_sol:.4f} SOL "
            f"halted={self.kill_switch.is_halted()}"
        )

    async def _request_stop(self) -> None:
        self.stop_event.set()

    async def _run_logged(self, coro, name: str) -> None:
        """Wraps every background task in run()'s list so an unhandled
        exception is logged loudly (background_task_crashed) instead of
        silently vanishing.

        Without this, a task that raised mid-run just died -- nothing
        awaits or checks it again until shutdown's
        `asyncio.gather(*tasks, return_exceptions=True)`, which collects
        the exception without ever logging or re-raising it. A crashed
        loop with zero log output was indistinguishable from "just quiet"
        purely from the logs, which is exactly the failure mode a silent
        pool_events/heartbeat outage looked like in production. Every
        task in run() goes through this now, not just the WS-dependent
        ones -- the same gap could as easily have hit DexScreener polling
        or the daily report loop.
        """
        try:
            await coro
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception("background_task_crashed", extra={"fields": {"task": name}})

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        # Created here, not in __init__: this is the first point at which we
        # are guaranteed to be running inside the loop that owns it for the
        # rest of the process's life (see the comment on self.stop_event's
        # declaration in __init__).
        self.stop_event = asyncio.Event()

        # Ctrl+C's default behavior is to raise KeyboardInterrupt wherever
        # the event loop happens to be -- typically inside the
        # `await self.stop_event.wait()` below -- which skips straight past
        # the cancel-and-gather cleanup at the bottom of this function
        # entirely. asyncio.run() then tears down the loop with tasks still
        # pending, which is what prints "Task was destroyed but it is
        # pending" spam. Catching SIGINT/SIGTERM here instead lets us set
        # stop_event cleanly, so the normal shutdown path always runs.
        # Unix-only (SIGTERM/signal handlers aren't supported on Windows);
        # cli.py's `except KeyboardInterrupt` is the fallback there.
        loop = asyncio.get_running_loop()

        def _handle_shutdown_signal(sig: signal.Signals) -> None:
            self.logger.info("shutdown_signal_received", extra={"fields": {"signal": sig.name}})
            self.stop_event.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, _handle_shutdown_signal, sig)
            except NotImplementedError:
                pass  # Windows -- KeyboardInterrupt fallback in cli.py still applies

        mode_label = "observe-only" if self.config.observe_only else self.config.mode.value
        self.logger.info("orchestrator_start", extra={"fields": {"mode": mode_label}})
        self.alerter.notify(f"memebot starting in {mode_label} mode")

        def _task(coro, name: str):
            return asyncio.create_task(self._run_logged(coro, name))

        tasks = [
            _task(self.signal_engine.run_dexscreener_loop(self._on_candidate, self.stop_event), "dexscreener_loop"),
            _task(self.signal_engine.run_pumpfun_loop(self._on_candidate, self.stop_event), "pumpfun_loop"),
            _task(self._daily_report_loop(), "daily_report_loop"),
            _task(self._rpc_budget_snapshot_loop(), "rpc_budget_snapshot_loop"),
            _task(self._heartbeat_loop(), "heartbeat_loop"),
            _task(self.alerter.run_command_loop(self.status_text, self._request_stop, self.stop_event), "alerter_command_loop"),
        ]
        if not self.config.observe_only:
            # No position can ever exist in observe-only mode, so there is
            # nothing for ExitMonitor to poll -- skip it rather than spend
            # RPC/Jupiter budget checking an always-empty list.
            tasks.append(_task(self.exit_monitor.run_forever(self._open_positions_list, self.stop_event), "exit_monitor_loop"))

        if self._ws is not None:
            # Loud and explicit, not inferred from silence: this is the
            # subscription InsiderRadar indexing AND event-driven discovery
            # (pool_events) both ride on -- if it's not visibly announced
            # here, "is it even running" was previously answerable only by
            # waiting for downstream logs that might never come.
            self.logger.info(
                "pool_events_subscribing",
                extra={
                    "fields": {
                        "programs": list(INDEXED_PROGRAM_IDS.values()),
                        "event_driven_discovery": self.config.enable_event_driven_discovery,
                    }
                },
            )
            for program_id, label in INDEXED_PROGRAM_IDS.items():
                tasks.append(_task(self._index_program_loop(program_id), f"index_program_loop:{label}"))
            if self.config.enable_event_driven_discovery:
                tasks.append(_task(self._second_wave_loop(), "second_wave_loop"))
        else:
            self.logger.warning(
                "pool_events_inactive_no_ws",
                extra={
                    "fields": {
                        "reason": "HELIUS_WS_URL not configured -- InsiderRadar indexing and event-driven "
                        "discovery are both inactive; DexScreener polling is the only discovery source running",
                    }
                },
            )

        await self.stop_event.wait()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.accounting.close()
        self.logger.info("orchestrator_stopped")

    def _on_candidate(self, candidate: Candidate) -> None:
        try:
            self.evaluate_candidate(candidate)
        except Exception:  # noqa: BLE001 - one bad candidate must not kill the signal loop
            self.logger.exception("error evaluating candidate %s", candidate.mint)
