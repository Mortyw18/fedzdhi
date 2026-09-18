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
    SignalSource,
    WalletBuyRecord,
    WalletSellRecord,
)
from bot.risk_manager import RiskManager
from bot.rpc_gateway import RpcGateway, RpcOutage, RpcWebSocket
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
        # Local, kill-switch-independent degrade for indexing's own RPC
        # failures -- see _index_program_loop and KillSwitch's module
        # docstring for why this must never call kill_switch.set_rpc_outage.
        self._indexing_rpc_consecutive_failures = 0
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

    def evaluate_candidate(self, candidate: Candidate) -> None:
        self.accounting.record_candidate(candidate)

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
        newly_tripped = self.kill_switch.set_rpc_outage(self._verdict_indicates_rpc_outage(verdict))
        if newly_tripped:
            self.alerter.notify_kill_switch(self.kill_switch.halt_reason())

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

    def _indexing_skip_reason(self) -> Optional[str]:
        """Indexing is the lowest-priority RPC consumer in this bot: it's
        background learning, not a trade that's waiting on a price. A
        subscription to an AMM program's logs can be an enormous stream
        (most swap activity network-wide mentions it, not just our own
        candidates), so it must never be what pushes RPC usage into the
        danger zone TokenSafety and ExitMonitor actually depend on.

        Returns a reason string if the current notification should be
        dropped without ever making an RPC call, or None if it's fine to
        proceed. Kept as a small, pure, synchronous method so the backoff
        policy is directly testable without spinning up the async loop.
        """
        if self.kill_switch.is_halted():
            return f"kill switch halted ({self.kill_switch.halt_reason()})"
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
            try:
                tx = await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: self.rpc.call(
                        "getTransaction",
                        [signature, {"encoding": "jsonParsed", "commitment": "confirmed", "maxSupportedTransactionVersion": 0}],
                    ),
                )
                self._indexing_rpc_consecutive_failures = 0
                if tx:
                    self._parse_leader_activity(tx, signature)
            except RpcOutage as exc:
                # Indexing is best-effort background learning, not a trade
                # waiting on a price -- its RPC failures must degrade
                # gracefully and NEVER touch the kill switch (that's fed
                # exclusively by TokenSafety's price-critical checks, see
                # evaluate_candidate). A prior overnight run tripped the
                # kill switch from exactly this loop within ~18s of startup
                # -- a busy AMM program can fire many logsSubscribe
                # notifications a second, each one a getTransaction call, so
                # 3 consecutive failures happened here in seconds even
                # though a plain curl to the same endpoint worked fine. The
                # method + error are logged every time so a real outage is
                # still fully diagnosable from the logs alone.
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
                if self._indexing_rpc_consecutive_failures >= self.config.indexing_max_consecutive_rpc_failures:
                    self.logger.warning(
                        "indexing_backing_off",
                        extra={
                            "fields": {
                                "program_id": program_id,
                                "cooldown_s": self.config.indexing_rpc_failure_cooldown_s,
                                "consecutive_failures": self._indexing_rpc_consecutive_failures,
                            }
                        },
                    )
                    await asyncio.sleep(self.config.indexing_rpc_failure_cooldown_s)
                    self._indexing_rpc_consecutive_failures = 0  # self-heals: try again after the cooldown

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
        """One INFO-level line every heartbeat_interval_s (default 10min),
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

        tasks = [
            asyncio.create_task(self.signal_engine.run_dexscreener_loop(self._on_candidate, self.stop_event)),
            asyncio.create_task(self.signal_engine.run_pumpfun_loop(self._on_candidate, self.stop_event)),
            asyncio.create_task(self._daily_report_loop()),
            asyncio.create_task(self._rpc_budget_snapshot_loop()),
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self.alerter.run_command_loop(self.status_text, self._request_stop, self.stop_event)),
        ]
        if not self.config.observe_only:
            # No position can ever exist in observe-only mode, so there is
            # nothing for ExitMonitor to poll -- skip it rather than spend
            # RPC/Jupiter budget checking an always-empty list.
            tasks.append(asyncio.create_task(self.exit_monitor.run_forever(self._open_positions_list, self.stop_event)))
        if self._ws is not None:
            for program_id in INDEXED_PROGRAM_IDS:
                tasks.append(asyncio.create_task(self._index_program_loop(program_id)))

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
