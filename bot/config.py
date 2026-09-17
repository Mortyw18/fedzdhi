"""Configuration, defaults, and the reckless-config guardrails.

Every default here is chosen around one goal: surviving a 0.2 SOL bankroll
long enough for InsiderRadar to build a real track record. See README.md
section "Strategy Doctrine" and HONESTY.md for the reasoning.
"""
from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

from bot.models import Mode

DEFAULT_BANKROLL_SOL = 0.2
DEFAULT_POSITION_SIZE_SOL = 0.05
HEAVY_POSITION_SIZE_SOL = 0.10
MAX_POSITION_SIZE_SOL = 0.10  # hard ceiling regardless of flags

LIVE_CONFIRMATION_PHRASE = "I UNDERSTAND THIS IS REAL MONEY"
HEAVY_SIZING_CONFIRMATION_PHRASE = "I ACCEPT THE RUIN TABLE"


class ConfigError(ValueError):
    """Raised when a config value is reckless. Always carries a human explanation."""


def _load_dotenv(path: str = ".env") -> None:
    """Minimal .env loader (no external dependency). Never overrides real env vars."""
    p = Path(path)
    if not p.exists():
        return
    for raw_line in p.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


@dataclass
class Config:
    # --- capital & sizing ---
    bankroll_sol: float = DEFAULT_BANKROLL_SOL
    position_size_sol: float = DEFAULT_POSITION_SIZE_SOL
    allow_heavy_sizing: bool = False

    # --- mode ---
    mode: Mode = Mode.PAPER
    # Observe-only (M2): runs SignalEngine + TokenSafety + InsiderRadar indexing
    # against live data and logs every candidate/verdict, but never calls
    # ExecutionEngine.buy -- not even a paper fill. See Orchestrator.evaluate_candidate.
    observe_only: bool = False

    # --- risk manager ---
    max_concurrent_positions: int = 3
    max_buys_per_day: int = 3
    hard_stop_pct: float = -0.35
    ladder_tp1_trigger_pct: float = 1.00      # +100%
    ladder_tp1_sell_fraction: float = 0.50    # sell 50%
    trail_pct: float = 0.25                   # trail 25% on the rest
    time_stop_minutes: float = 360.0          # 6h
    time_stop_min_gain_pct: float = 0.10      # +10%
    daily_loss_cap_sol: float = 0.04
    max_position_fraction_of_bankroll: float = 0.50  # refuse configs above this

    # --- slippage doctrine ---
    slippage_impact_multiplier: float = 2.0
    slippage_flat_addon_pct: float = 0.01
    slippage_hard_cap_pct: float = 0.10
    slippage_emergency_cap_pct: float = 0.15
    max_acceptable_price_impact_pct: float = 0.05

    # --- signal engine ---
    dexscreener_poll_interval_s: float = 45.0
    min_pool_liquidity_usd: float = 15_000.0
    max_pool_liquidity_usd: float = 400_000.0
    max_pool_age_s: float = 72 * 3600.0
    min_volume_liquidity_ratio: float = 0.20
    min_buy_sell_ratio: float = 1.5

    # --- insider radar ---
    insider_first_buyers_n: int = 50
    conviction_min_trades: int = 20
    conviction_min_distinct_tokens: int = 15
    conviction_min_hold_s: float = 15 * 60.0
    conviction_min_wallet_age_days: float = 14.0
    bundled_launch_min_distinct_tokens: int = 15
    copy_max_price_move_pct: float = 0.10
    auto_unfollow_trailing_n: int = 20

    # --- token safety ---
    max_top10_holder_pct: float = 0.30

    # --- execution ---
    execution_max_retries: int = 2
    exit_monitor_poll_interval_s: float = 3.0

    # --- rpc ---
    helius_api_key: str = ""
    helius_rpc_url: str = ""
    helius_ws_url: str = ""
    failover_rpc_url: str = ""
    rpc_rate_limit_per_10s: int = 100  # Helius free tier budget, conservative

    # --- telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- paper mode ---
    paper_fill_haircut_pct: float = 0.02

    # --- misc ---
    db_path: str = "data/bot.db"
    log_dir: str = "logs"

    def validate(self) -> None:
        """Raise ConfigError with a printed explanation for any reckless value.

        This is the one place every startup path (CLI, tests, replay harness)
        must call before the bot is allowed to touch a wallet.
        """
        problems: list[str] = []

        if self.position_size_sol > MAX_POSITION_SIZE_SOL + 1e-9:
            problems.append(
                f"position_size_sol={self.position_size_sol} exceeds the hard ceiling "
                f"of {MAX_POSITION_SIZE_SOL} SOL. This bot never sizes a single position "
                "larger than that, no matter what flags are passed."
            )

        if self.position_size_sol > DEFAULT_POSITION_SIZE_SOL + 1e-9 and not self.allow_heavy_sizing:
            problems.append(
                f"position_size_sol={self.position_size_sol} is above the "
                f"{DEFAULT_POSITION_SIZE_SOL} SOL default but --allow-heavy-sizing was not "
                "set. Heavy sizing requires the flag AND a typed confirmation printed "
                "with the ruin table."
            )

        if self.bankroll_sol > 0 and (
            self.position_size_sol / self.bankroll_sol
        ) > self.max_position_fraction_of_bankroll + 1e-9:
            problems.append(
                f"position_size_sol={self.position_size_sol} is "
                f"{self.position_size_sol / self.bankroll_sol:.0%} of bankroll_sol="
                f"{self.bankroll_sol}, above the {self.max_position_fraction_of_bankroll:.0%} "
                "ceiling this bot enforces categorically. Sizing a position at 50%+ of "
                "bankroll is negative-growth by any sizing math -- see README section 1."
            )

        if self.max_concurrent_positions * self.position_size_sol > self.bankroll_sol + 1e-9:
            problems.append(
                f"max_concurrent_positions={self.max_concurrent_positions} at "
                f"position_size_sol={self.position_size_sol} could deploy "
                f"{self.max_concurrent_positions * self.position_size_sol} SOL, more than "
                f"the {self.bankroll_sol} SOL bankroll. Lower one of the two."
            )

        if self.daily_loss_cap_sol <= 0:
            problems.append("daily_loss_cap_sol must be positive: a kill switch with no cap is not a kill switch.")

        if self.daily_loss_cap_sol > self.bankroll_sol * 0.5 + 1e-9:
            problems.append(
                f"daily_loss_cap_sol={self.daily_loss_cap_sol} allows losing more than half "
                f"the {self.bankroll_sol} SOL bankroll in a single day before the kill switch "
                "trips. That defeats the point of a daily cap."
            )

        if self.hard_stop_pct >= 0:
            problems.append("hard_stop_pct must be negative (it's a loss threshold).")

        if self.slippage_hard_cap_pct > 0.10 + 1e-9:
            problems.append("slippage_hard_cap_pct must not exceed 10% per the slippage doctrine.")

        if self.slippage_emergency_cap_pct > 0.15 + 1e-9:
            problems.append("slippage_emergency_cap_pct must not exceed 15% (emergency exits only).")

        if self.max_buys_per_day < 1:
            problems.append("max_buys_per_day must be at least 1.")

        if self.mode == Mode.LIVE and not (self.helius_api_key or self.helius_rpc_url):
            problems.append("LIVE mode requires HELIUS_API_KEY or HELIUS_RPC_URL to be set.")

        if self.observe_only and self.mode == Mode.LIVE:
            problems.append(
                "observe_only cannot be combined with LIVE mode -- observe-only never trades, live or "
                "paper, so there is nothing for a live wallet to do. Drop --live to run --observe-only."
            )

        if self.observe_only and not (self.helius_api_key or self.helius_rpc_url):
            problems.append(
                "observe_only mode is meant to run against live on-chain data (that's the point of the "
                "M2 gate), which needs HELIUS_API_KEY or HELIUS_RPC_URL. Set one in .env."
            )

        if problems:
            explanation = "\n".join(f"  - {p}" for p in problems)
            raise ConfigError(
                "Refusing to start: this configuration is reckless.\n" + explanation
            )

    def ruin_table(self) -> str:
        """Render the position-sizing ruin table the operator must see and accept."""
        lines = [
            "RUIN TABLE (bankroll={:.3f} SOL)".format(self.bankroll_sol),
            "-" * 72,
            f"{'position size':>14} | {'% of bankroll':>14} | {'loss @ -35% stop':>17} | {'% bankroll/stop':>16}",
        ]
        for size in sorted({DEFAULT_POSITION_SIZE_SOL, HEAVY_POSITION_SIZE_SOL, self.position_size_sol}):
            if self.bankroll_sol <= 0:
                continue
            pct_bankroll = size / self.bankroll_sol
            loss_at_stop = size * abs(self.hard_stop_pct)
            pct_bankroll_lost = loss_at_stop / self.bankroll_sol
            lines.append(
                f"{size:>13.3f} | {pct_bankroll:>13.1%} | {loss_at_stop:>16.4f} | {pct_bankroll_lost:>15.1%}"
            )
        lines.append("-" * 72)
        # consecutive-loss-to-ruin approximation: (1 - x)^n <= 0.2 (an illustrative 80% drawdown)
        for size in sorted({DEFAULT_POSITION_SIZE_SOL, HEAVY_POSITION_SIZE_SOL, self.position_size_sol}):
            if self.bankroll_sol <= 0:
                continue
            pct_bankroll_lost = (size * abs(self.hard_stop_pct)) / self.bankroll_sol
            if pct_bankroll_lost <= 0:
                continue
            import math

            n = math.log(0.2) / math.log(1 - pct_bankroll_lost) if pct_bankroll_lost < 1 else 1
            lines.append(
                f"  at {size:.3f} SOL/position, ~{n:.1f} consecutive stop-outs reach an 80% bankroll drawdown"
            )
        lines.append("-" * 72)
        lines.append(
            "Even a good edge caps out near 25% of bankroll per position; sizing at 50%+ "
            "of bankroll is negative-growth under any sizing math. Default stays 0.05 SOL."
        )
        return "\n".join(lines)


def load_config_from_env(env_path: str = ".env") -> Config:
    _load_dotenv(env_path)
    cfg = Config()
    cfg.bankroll_sol = float(os.environ.get("BANKROLL_SOL", cfg.bankroll_sol))
    cfg.helius_api_key = os.environ.get("HELIUS_API_KEY", "")
    if cfg.helius_api_key and not os.environ.get("HELIUS_RPC_URL"):
        cfg.helius_rpc_url = f"https://mainnet.helius-rpc.com/?api-key={cfg.helius_api_key}"
        cfg.helius_ws_url = f"wss://mainnet.helius-rpc.com/?api-key={cfg.helius_api_key}"
    else:
        cfg.helius_rpc_url = os.environ.get("HELIUS_RPC_URL", "")
        cfg.helius_ws_url = os.environ.get("HELIUS_WS_URL", "")
    cfg.failover_rpc_url = os.environ.get("FAILOVER_RPC_URL", "")
    cfg.telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    cfg.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
    cfg.db_path = os.environ.get("DB_PATH", cfg.db_path)
    return cfg


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="memebot",
        description="Solana memecoin trend-following + insider-copy trading bot (paper-first).",
    )
    p.add_argument("--live", action="store_true", help="Trade with real funds. Requires typed confirmation.")
    p.add_argument(
        "--observe-only",
        action="store_true",
        help="M2 mode: log candidates, safety verdicts, and InsiderRadar indexing against live data. "
        "Never places a trade, live or paper. Cannot be combined with --live.",
    )
    p.add_argument(
        "--allow-heavy-sizing",
        action="store_true",
        help=f"Allow {HEAVY_POSITION_SIZE_SOL} SOL positions instead of the {DEFAULT_POSITION_SIZE_SOL} default. "
        "Requires typed confirmation of the ruin table.",
    )
    p.add_argument("--position-size", type=float, default=None, help="Override position size in SOL.")
    p.add_argument("--bankroll", type=float, default=None, help="Override bankroll in SOL.")
    p.add_argument("--yes", action="store_true", help="Skip interactive typed confirmations (for CI/tests only).")
    p.add_argument("--env-file", default=".env", help="Path to .env file.")
    p.add_argument("--reset-kill-switch", action="store_true", help="Manually reset a tripped kill switch and exit.")
    p.add_argument("--sweep", action="store_true", help="Drain the wallet to a destination address and exit.")
    p.add_argument("--sweep-to", default=None, help="Destination address for --sweep.")
    p.add_argument("--daily-report", action="store_true", help="Print today's report and exit.")
    return p


def apply_cli_overrides(cfg: Config, args: argparse.Namespace) -> Config:
    if args.bankroll is not None:
        cfg.bankroll_sol = args.bankroll
    if args.position_size is not None:
        cfg.position_size_sol = args.position_size
    elif args.allow_heavy_sizing:
        cfg.position_size_sol = HEAVY_POSITION_SIZE_SOL
    cfg.allow_heavy_sizing = args.allow_heavy_sizing
    cfg.mode = Mode.LIVE if args.live else Mode.PAPER
    cfg.observe_only = args.observe_only
    return cfg


def require_typed_confirmation(prompt: str, phrase: str, skip: bool = False) -> bool:
    """Ask the operator to type an exact phrase. Returns True if confirmed.

    `skip=True` is only ever passed from --yes, which is documented as
    CI/tests-only -- never wired to a live-money path without --yes being an
    explicit, visible operator choice on the command line.
    """
    if skip:
        return True
    print(prompt)
    print(f'Type exactly: {phrase}')
    try:
        typed = input("> ").strip()
    except EOFError:
        return False
    return typed == phrase


def confirm_startup(cfg: Config, args: argparse.Namespace) -> None:
    """Run the required typed-confirmation gates for live mode / heavy sizing."""
    if cfg.allow_heavy_sizing:
        print(cfg.ruin_table())
        ok = require_typed_confirmation(
            "\nHeavy sizing (0.10 SOL/position) requested. This doubles the bankroll "
            "fraction lost per stop-out. Confirm you accept the ruin table above.",
            HEAVY_SIZING_CONFIRMATION_PHRASE,
            skip=args.yes,
        )
        if not ok:
            print("Confirmation not received. Exiting without starting.")
            sys.exit(1)

    if cfg.mode == Mode.LIVE:
        print(cfg.ruin_table())
        ok = require_typed_confirmation(
            "\nLIVE mode requested. Real SOL will be spent. Paper mode is the default "
            "for a reason -- see HONESTY.md. Confirm you want to trade live.",
            LIVE_CONFIRMATION_PHRASE,
            skip=args.yes,
        )
        if not ok:
            print("Confirmation not received. Exiting without starting.")
            sys.exit(1)
