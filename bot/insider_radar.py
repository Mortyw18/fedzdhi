"""InsiderRadar: the only real moat this bot has.

A laptop bot has no launch-speed edge and no information edge at entry
time. What it CAN build is a self-indexed, continuously-graded record of
which wallets are actually good at this -- and copy only the ones whose
edge survives our latency.

Two wallet classes come out of the same data:
  - SNIPER: same-block / bundled-launch entries. Structurally uncopyable
    from a laptop (by the time we see the tx, the block already landed).
    Informational only. NEVER copied, no matter how profitable.
  - CONVICTION: sized positions held for real time, across many distinct
    tokens, by a wallet with a track record. The ONLY class we copy, and
    only the entry -- never the exit. Insiders dump into their own
    copy-trade flow; our exit engine is ours alone.

HARD RULE enforced here: `evaluate_copy_signal` only ever fires off a
wallet's BUY. There is no method in this class that reacts to a sell by
issuing a trade -- sells only feed the wallet's scoring.
"""
from __future__ import annotations

import logging
import statistics
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from bot.models import (
    CopySignal,
    LeaderStats,
    WalletBuyRecord,
    WalletClass,
    WalletSellRecord,
    now_ts,
)


@dataclass
class _OpenLot:
    record: WalletBuyRecord
    remaining_tokens: float


class InsiderRadar:
    def __init__(
        self,
        first_buyers_n: int = 50,
        bundle_cluster_min_wallets: int = 3,
        conviction_min_trades: int = 20,
        conviction_min_distinct_tokens: int = 15,
        conviction_min_hold_s: float = 15 * 60.0,
        conviction_min_wallet_age_days: float = 14.0,
        auto_unfollow_trailing_n: int = 20,
        copy_max_price_move_pct: float = 0.10,
        logger: Optional[logging.Logger] = None,
    ) -> None:
        self.first_buyers_n = first_buyers_n
        self.bundle_cluster_min_wallets = bundle_cluster_min_wallets
        self.conviction_min_trades = conviction_min_trades
        self.conviction_min_distinct_tokens = conviction_min_distinct_tokens
        self.conviction_min_hold_s = conviction_min_hold_s
        self.conviction_min_wallet_age_days = conviction_min_wallet_age_days
        self.auto_unfollow_trailing_n = auto_unfollow_trailing_n
        self.copy_max_price_move_pct = copy_max_price_move_pct
        self.logger = logger or logging.getLogger("memebot.insider_radar")

        self._first_buyers: dict[str, list[WalletBuyRecord]] = {}
        self._pool_creation_slot: dict[str, int] = {}
        self._wallet_stats: dict[str, LeaderStats] = {}
        self._wallet_distinct_tokens: dict[str, set[str]] = {}
        self._open_lots: dict[tuple[str, str], deque[_OpenLot]] = {}
        self._hold_times_s: dict[str, list[float]] = {}
        self._trailing_pnl: dict[str, deque[float]] = {}
        self._blocklist: set[str] = set()

    # ------------------------------------------------------------------
    # a. indexing
    # ------------------------------------------------------------------

    def record_pool_creation(self, mint: str, slot: int) -> None:
        self._pool_creation_slot[mint] = slot

    def record_buy(self, record: WalletBuyRecord, wallet_first_seen_ts: Optional[float] = None) -> None:
        bucket = self._first_buyers.setdefault(record.mint, [])
        if len(bucket) < self.first_buyers_n:
            bucket.append(record)

        stats = self._wallet_stats.setdefault(
            record.wallet, LeaderStats(wallet=record.wallet, first_seen=wallet_first_seen_ts or record.timestamp)
        )
        if wallet_first_seen_ts is not None and wallet_first_seen_ts < stats.first_seen:
            stats.first_seen = wallet_first_seen_ts

        self._wallet_distinct_tokens.setdefault(record.wallet, set()).add(record.mint)
        stats.distinct_tokens = len(self._wallet_distinct_tokens[record.wallet])

        self._open_lots.setdefault((record.wallet, record.mint), deque()).append(
            _OpenLot(record=record, remaining_tokens=record.tokens)
        )

        self._maybe_flag_bundling(record.mint)

    def _maybe_flag_bundling(self, mint: str) -> None:
        buyers = self._first_buyers.get(mint, [])
        if not buyers:
            return
        creation_slot = self._pool_creation_slot.get(mint, min(b.slot for b in buyers))
        same_slot_wallets = {b.wallet for b in buyers if b.slot == creation_slot}
        if len(same_slot_wallets) >= self.bundle_cluster_min_wallets:
            for wallet in same_slot_wallets:
                stats = self._wallet_stats.get(wallet)
                if stats is not None:
                    stats.bundled_launch_count += 1

    def get_first_buyers(self, mint: str) -> list[WalletBuyRecord]:
        return list(self._first_buyers.get(mint, []))

    # ------------------------------------------------------------------
    # b. wallet scoring
    # ------------------------------------------------------------------

    def record_sell(self, record: WalletSellRecord) -> None:
        lots = self._open_lots.get((record.wallet, record.mint))
        stats = self._wallet_stats.setdefault(record.wallet, LeaderStats(wallet=record.wallet, first_seen=record.timestamp))

        if not lots:
            # A sell with no tracked buy (we started indexing mid-position).
            # Can't compute PnL or hold time honestly, so we don't guess.
            self.logger.debug("sell with no open lot: wallet=%s mint=%s", record.wallet, record.mint)
            return

        remaining_sell_tokens = record.tokens
        cost_consumed_sol = 0.0
        tokens_consumed = 0.0
        weighted_hold_numerator = 0.0
        while remaining_sell_tokens > 1e-12 and lots:
            lot = lots[0]
            take_tokens = min(lot.remaining_tokens, remaining_sell_tokens)
            if lot.record.tokens > 0:
                cost_consumed_sol += (take_tokens / lot.record.tokens) * lot.record.amount_sol
            weighted_hold_numerator += take_tokens * (record.timestamp - lot.record.timestamp)
            tokens_consumed += take_tokens
            lot.remaining_tokens -= take_tokens
            remaining_sell_tokens -= take_tokens
            if lot.remaining_tokens <= 1e-9:
                lots.popleft()

        if tokens_consumed <= 0:
            return

        # Proceeds are scaled to the fraction of this sell we could actually
        # match against a tracked buy -- an unmatched remainder (we started
        # indexing mid-position) doesn't get counted as profit or loss.
        proceeds_sol = record.amount_sol * (tokens_consumed / record.tokens) if record.tokens > 0 else 0.0
        pnl_sol = proceeds_sol - cost_consumed_sol
        hold_s = weighted_hold_numerator / tokens_consumed

        stats.trades_closed += 1
        if pnl_sol > 0:
            stats.wins += 1
        stats.realized_pnl_sol += pnl_sol

        hold_list = self._hold_times_s.setdefault(record.wallet, [])
        hold_list.append(hold_s)
        stats.median_hold_s = statistics.median(hold_list)

        trailing = self._trailing_pnl.setdefault(record.wallet, deque(maxlen=self.auto_unfollow_trailing_n))
        trailing.append(pnl_sol)
        stats.last_20_pnl_sol = sum(trailing)

        if (
            len(trailing) >= self.auto_unfollow_trailing_n
            and stats.last_20_pnl_sol < 0
            and self.classify(record.wallet) == WalletClass.CONVICTION
        ):
            stats.unfollowed = True
            self._blocklist.add(record.wallet)
            self.logger.info(
                "auto_unfollow", extra={"fields": {"wallet": record.wallet, "trailing_pnl_sol": stats.last_20_pnl_sol}}
            )

    def distinct_token_count(self, wallet: str) -> int:
        return len(self._wallet_distinct_tokens.get(wallet, set()))

    def stats_for(self, wallet: str) -> Optional[LeaderStats]:
        return self._wallet_stats.get(wallet)

    # ------------------------------------------------------------------
    # c. classification / leaderboards
    # ------------------------------------------------------------------

    def classify(self, wallet: str) -> WalletClass:
        stats = self._wallet_stats.get(wallet)
        if stats is None:
            return WalletClass.UNRANKED
        if wallet in self._blocklist or stats.unfollowed:
            return WalletClass.UNRANKED
        if stats.bundled_launch_count > 0:
            # Ever observed sniping a bundled launch -> permanently uncopyable,
            # regardless of how good its other stats look. See module docstring.
            return WalletClass.SNIPER
        if self._qualifies_for_conviction(stats):
            return WalletClass.CONVICTION
        return WalletClass.UNRANKED

    def _qualifies_for_conviction(self, stats: LeaderStats) -> bool:
        return (
            stats.realized_pnl_sol > 0
            and stats.trades_closed >= self.conviction_min_trades
            and stats.distinct_tokens >= self.conviction_min_distinct_tokens
            and stats.median_hold_s > self.conviction_min_hold_s
            and stats.wallet_age_days > self.conviction_min_wallet_age_days
        )

    def sniper_leaderboard(self) -> list[LeaderStats]:
        return sorted(
            (s for w, s in self._wallet_stats.items() if self.classify(w) == WalletClass.SNIPER),
            key=lambda s: s.bundled_launch_count,
            reverse=True,
        )

    def conviction_leaderboard(self) -> list[LeaderStats]:
        return sorted(
            (s for w, s in self._wallet_stats.items() if self.classify(w) == WalletClass.CONVICTION),
            key=lambda s: s.realized_pnl_sol,
            reverse=True,
        )

    def should_copy(self, wallet: str) -> bool:
        return self.classify(wallet) == WalletClass.CONVICTION

    # ------------------------------------------------------------------
    # d. copy execution (entries only -- see module docstring)
    # ------------------------------------------------------------------

    def evaluate_copy_signal(self, record: WalletBuyRecord) -> Optional[CopySignal]:
        """Called when we observe a BUY from a watched wallet. Never called for sells."""
        if not self.should_copy(record.wallet):
            return None
        return CopySignal(mint=record.mint, leader_wallet=record.wallet, leader_entry_price_usd=record.price_usd)

    @staticmethod
    def price_move_pct(leader_entry_price_usd: float, current_price_usd: float) -> float:
        if leader_entry_price_usd <= 0:
            return float("inf")
        return abs(current_price_usd - leader_entry_price_usd) / leader_entry_price_usd

    def is_copy_still_valid(self, signal: CopySignal, current_price_usd: float) -> tuple[bool, str]:
        move = self.price_move_pct(signal.leader_entry_price_usd, current_price_usd)
        if move > self.copy_max_price_move_pct:
            return False, f"price moved {move:.1%} since leader entry (max {self.copy_max_price_move_pct:.0%})"
        return True, f"price moved {move:.1%} since leader entry"

    def watch_list(self) -> set[str]:
        """Wallets worth subscribing to for real-time buy detection."""
        return set(self._wallet_stats.keys()) - self._blocklist
