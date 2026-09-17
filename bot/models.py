"""Shared data structures used across every module.

Keeping these in one place means TokenSafety, InsiderRadar, RiskManager,
ExecutionEngine and Accounting all agree on the shape of a candidate, a
verdict, a position and a fill -- there is exactly one definition of each.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


def now_ts() -> float:
    return time.time()


class Mode(str, Enum):
    PAPER = "paper"
    LIVE = "live"


class SignalSource(str, Enum):
    DEXSCREENER = "dexscreener"
    PUMPFUN = "pumpfun"
    INSIDER_COPY = "insider_copy"


class WalletClass(str, Enum):
    UNRANKED = "unranked"
    SNIPER = "sniper"          # informational only -- NEVER copied
    CONVICTION = "conviction"  # the only class the bot copies


class PositionStatus(str, Enum):
    OPEN = "open"
    CLOSED = "closed"


class ExitReason(str, Enum):
    HARD_STOP = "hard_stop"
    LADDER_TP = "ladder_take_profit"
    TRAIL_STOP = "trail_stop"
    TIME_STOP = "time_stop"
    MANUAL = "manual"
    KILL_SWITCH = "kill_switch"
    EMERGENCY = "emergency"


@dataclass
class Candidate:
    """A token surfaced by SignalEngine or InsiderRadar, not yet safety-checked."""

    mint: str
    symbol: str
    source: SignalSource
    pair_address: Optional[str] = None
    dex: Optional[str] = None
    liquidity_usd: float = 0.0
    volume_5m_usd: float = 0.0
    volume_1h_usd: float = 0.0
    buys_5m: int = 0
    sells_5m: int = 0
    price_usd: float = 0.0
    pool_created_at: Optional[float] = None
    discovered_at: float = field(default_factory=now_ts)
    leader_wallet: Optional[str] = None          # set when source == INSIDER_COPY
    leader_entry_price_usd: Optional[float] = None
    lp_mint: Optional[str] = None
    pump_fun_graduated: Optional[bool] = None    # None = not a pump.fun token
    token_decimals: int = 6
    pool_creation_slot: Optional[int] = None

    @property
    def pool_age_s(self) -> float:
        if self.pool_created_at is None:
            return 0.0
        return max(0.0, now_ts() - self.pool_created_at)

    @property
    def buy_sell_ratio(self) -> float:
        if self.sells_5m <= 0:
            return float(self.buys_5m) if self.buys_5m > 0 else 0.0
        return self.buys_5m / self.sells_5m


@dataclass
class SafetyCheckResult:
    name: str
    passed: bool
    detail: str
    data: dict = field(default_factory=dict)


@dataclass
class SafetyVerdict:
    mint: str
    passed: bool
    checks: list[SafetyCheckResult]
    timestamp: float = field(default_factory=now_ts)

    @property
    def rejection_reasons(self) -> list[str]:
        return [c.name + ": " + c.detail for c in self.checks if not c.passed]


@dataclass
class WalletBuyRecord:
    """One buy observed while indexing the first N buyers of a new pool.

    `amount_sol` is SOL spent (cost basis) and `tokens` is the token
    quantity received. Both are needed: InsiderRadar's FIFO matching
    tracks lots by token quantity (price moves between buy and sell, so
    SOL amounts on the two legs aren't directly comparable), while
    `amount_sol` supplies the actual cost basis once a lot is matched.
    """

    wallet: str
    mint: str
    slot: int
    amount_sol: float
    price_usd: float
    tokens: float = 0.0
    timestamp: float = field(default_factory=now_ts)


@dataclass
class WalletSellRecord:
    wallet: str
    mint: str
    slot: int
    amount_sol: float  # SOL received
    price_usd: float
    tokens: float = 0.0  # token quantity sold
    timestamp: float = field(default_factory=now_ts)


@dataclass
class LeaderStats:
    wallet: str
    wallet_class: WalletClass = WalletClass.UNRANKED
    first_seen: float = field(default_factory=now_ts)
    trades_closed: int = 0
    distinct_tokens: int = 0
    wins: int = 0
    realized_pnl_sol: float = 0.0
    median_hold_s: float = 0.0
    last_20_pnl_sol: float = 0.0
    bundled_launch_count: int = 0
    unfollowed: bool = False

    @property
    def win_rate(self) -> float:
        if self.trades_closed == 0:
            return 0.0
        return self.wins / self.trades_closed

    @property
    def wallet_age_days(self) -> float:
        return (now_ts() - self.first_seen) / 86400.0


@dataclass
class LadderState:
    """Tracks how much of a position has already been taken profit on."""

    tp1_filled: bool = False
    trail_active: bool = False
    trail_high_price: float = 0.0


@dataclass
class Position:
    mint: str
    symbol: str
    size_sol: float
    # Despite the name (kept for symmetry with Candidate.price_usd), this is
    # SOL-per-token from the entry Fill, not a USD price. ExitMonitor and
    # RiskManager work exclusively in Jupiter's SOL-denominated executable
    # prices post-entry -- USD only matters pre-trade, for SignalEngine's
    # liquidity/volume filters and InsiderRadar's price-moved-since-entry gate.
    entry_price_usd: float
    tokens_held: float
    opened_at: float = field(default_factory=now_ts)
    source: SignalSource = SignalSource.DEXSCREENER
    leader_wallet: Optional[str] = None
    status: PositionStatus = PositionStatus.OPEN
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ladder: LadderState = field(default_factory=LadderState)
    remaining_fraction: float = 1.0  # fraction of original tokens still held
    closed_at: Optional[float] = None
    realized_pnl_sol: float = 0.0

    @property
    def age_s(self) -> float:
        return now_ts() - self.opened_at


@dataclass
class Fill:
    position_id: str
    mint: str
    side: str  # "buy" or "sell"
    quote_price_usd: float  # SOL-per-token, see Position.entry_price_usd note
    fill_price_usd: float   # SOL-per-token
    size_sol: float
    tokens: float
    fee_sol: float
    slippage_bps: float
    tx_sig: str
    timestamp: float = field(default_factory=now_ts)
    reason: Optional[str] = None  # exit reason for sells


@dataclass
class CopySignal:
    mint: str
    leader_wallet: str
    leader_entry_price_usd: float
    observed_at: float = field(default_factory=now_ts)
