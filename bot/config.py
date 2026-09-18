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
    # Failed txs cost fees -- a streak this long in a row means something is
    # structurally wrong, not bad luck.
    max_consecutive_execution_failures: int = 3
    # A single blip (cold-start DNS/TLS, one transient timeout) must never
    # trip the kill switch on its own -- only a sustained outage should.
    # See KillSwitch's module docstring for the incident that set this.
    max_consecutive_rpc_outages: int = 3

    # --- slippage doctrine ---
    slippage_impact_multiplier: float = 2.0
    slippage_flat_addon_pct: float = 0.01
    slippage_hard_cap_pct: float = 0.10
    slippage_emergency_cap_pct: float = 0.15
    max_acceptable_price_impact_pct: float = 0.05

    # --- signal engine ---
    # DexScreener itself isn't the RPC budget concern, but every candidate it
    # surfaces triggers a full TokenSafety pass against Helius -- a slower
    # poll interval is a real lever on RPC load, not just DexScreener's own
    # rate limit. 60s is the top of the spec's 30-60s range.
    dexscreener_poll_interval_s: float = 60.0
    # /search's relevance ranking essentially never surfaces a pool young
    # enough to pass max_pool_age_s -- token-profiles/token-boosts (see
    # signal_engine.py) are the primary discovery path now; these queries
    # are only a supplementary trend signal layered on top of that.
    dexscreener_search_queries: list[str] = field(default_factory=lambda: ["SOL", "pump", "bonk", "meme"])
    min_pool_liquidity_usd: float = 15_000.0
    max_pool_liquidity_usd: float = 400_000.0
    max_pool_age_s: float = 72 * 3600.0
    min_volume_liquidity_ratio: float = 0.20
    min_buy_sell_ratio: float = 1.5
    # pump.fun's API is unofficial and known to have extended outages (e.g.
    # Cloudflare 530s). After this many consecutive poll failures, SignalEngine
    # stops polling it for the rest of the run rather than retrying forever.
    pumpfun_max_consecutive_failures: int = 5
    # Confirmed 530-blocked (Cloudflare, origin unreachable) in production
    # use of this bot -- browser-like headers didn't fix it (see
    # signal_engine.py's fetch_pumpfun_new_coins), so defaulting to
    # enabled just means every run wastes pumpfun_max_consecutive_failures
    # (5) retries against a known-dead endpoint before giving up for the
    # run. Default now OFF; set ENABLE_PUMPFUN=true in .env to re-enable
    # if pump.fun's API recovers. DexScreener signals and InsiderRadar are
    # unaffected either way. Also gates TokenSafety's pump.fun graduation
    # lookup (same API) -- see check_lp_or_graduation in token_safety.py.
    enable_pumpfun_source: bool = False

    # --- verdict cache ---
    # The same mint gets rediscovered every DexScreener/pump.fun poll cycle
    # (DexScreener always returns still-active pools; the whole point of a
    # trend-following search is that it keeps finding what's already
    # trending) -- without a cache, evaluate_candidate() re-runs the full,
    # RPC/Jupiter/rugcheck/pump.fun-lookup-costing safety pipeline on the
    # SAME mint every single cycle, forever. 20 minutes covers several
    # DexScreener poll intervals without leaving genuinely stale data cached
    # for too long. A big swing in the candidate's own reported liquidity
    # (verdict_cache_liquidity_change_pct) is treated as a state-change event
    # and bypasses the cache early even inside the TTL -- cheap to check
    # (data we already have from discovery) and cheaper than a real
    # state-change subscription per mint.
    verdict_cache_ttl_s: float = 1200.0
    verdict_cache_liquidity_change_pct: float = 0.20

    # --- event-driven discovery + second-wave entry ---
    # Primary discovery path: WebSocket logsSubscribe on Raydium AMM v4 +
    # pump.fun's bonding curve program (the same subscription
    # InsiderRadar's indexing already runs -- see
    # Orchestrator._check_pool_creation), reacting to a pool-creation/
    # token-launch instruction within roughly the RPC round-trip latency
    # of it confirming, not a poll interval. DexScreener polling remains
    # running unchanged as a resilience backup (see README.md), not
    # disabled -- if the WS drops or a creation is missed (see
    # pool_events.py's confidence notes), DexScreener still eventually
    # surfaces the same pool once it lists it.
    enable_event_driven_discovery: bool = True
    # Never buy at creation -- the ENTIRE point of the second-wave
    # strategy. A newly detected pool is tracked (not evaluated) until its
    # age is inside [second_wave_min_age_s, second_wave_max_age_s], during
    # which its price is sampled periodically to build a high-water mark.
    # Falls out of tracking (never evaluated) if it ages past the window
    # without qualifying.
    second_wave_min_age_s: float = 180.0    # 3 min
    second_wave_max_age_s: float = 600.0    # 10 min
    # "First dump absorbed": price must have retained at least this
    # fraction of its own early high by the time it's checked -- a proxy
    # for "the initial sniper/bot dump already happened and a floor was
    # found," not "still crashing." 1.0 would require the price to be AT
    # its all-time high when checked (unrealistic); too low defeats the
    # point of waiting at all. Tune against second_wave_reject log volume.
    second_wave_min_price_retention_pct: float = 0.40
    # How often a pending pool's price is re-sampled during the wait
    # window, to build the high-water mark used above.
    second_wave_sample_interval_s: float = 20.0
    # Safety valve on the pending-pool dict's size: pump.fun alone can
    # launch far more tokens than this bot could ever second-wave-evaluate
    # in the same window, especially while RPC-budget-priority throttles
    # (shared with InsiderRadar's indexing) are dropping most notifications
    # anyway. Oldest pending entries are evicted first if this is exceeded,
    # logged loudly -- this is a memory/scale bound, not a real signal.
    second_wave_max_pending: int = 500

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
    # InsiderRadar's WebSocket indexing is the lowest-priority RPC consumer --
    # TokenSafety and ExitMonitor must never be starved by background
    # indexing. Once the gateway's own rolling budget usage crosses this
    # ceiling, the indexer stops issuing getTransaction calls for new
    # notifications until usage drops back down (see Orchestrator).
    indexing_max_rpc_budget_pct: float = 0.50
    # Indexing's own RPC failures (getTransaction on a logsSubscribe
    # notification) must degrade gracefully and never touch the kill switch
    # -- that's reserved for TokenSafety's price-critical checks (see
    # KillSwitch's module docstring). Every throttle below is GLOBAL/SHARED
    # across every concurrent indexing loop (Orchestrator runs one per
    # indexed program ID, see INDEXED_PROGRAM_IDS) -- per-loop state was
    # tried first and doesn't work: one program's loop backing off did
    # nothing to stop the OTHER program's loop from continuing to fail on
    # its own independent schedule at the same time, which from the logs
    # looked exactly like "no backoff at all, fixed-interval retries."
    #
    # Backoff grows exponentially on every consecutive getTransaction
    # failure (base * 2^(consecutive-1), capped), starting on the very
    # first failure -- not after a grace threshold. A flat or
    # threshold-gated cooldown wasn't enough under real load: a busy AMM
    # program firing notifications several times a second needs the pause
    # to start immediately and keep growing for as long as the outage
    # actually persists.
    indexing_rpc_failure_backoff_base_s: float = 2.0
    indexing_rpc_failure_backoff_max_s: float = 60.0
    # A hard, local floor on the spacing between the indexer's OWN
    # getTransaction attempts -- independent of how fast logsSubscribe
    # notifications actually arrive, how the RPC budget ceiling above is
    # doing, or the exponential backoff. "Several notifications a second"
    # from a busy program can never turn into "several RPC calls a second"
    # from this loop, full stop, even on the very first burst before any of
    # the other throttles have had a chance to kick in.
    indexing_min_call_interval_s: float = 0.5
    # A second, independent ceiling: no more than this many getTransaction
    # ATTEMPTS (successful or not) from indexing, combined across every
    # program's loop, in any trailing 60s window. Where the interval floor
    # above bounds the SPACING between calls, this bounds the total VOLUME
    # -- a genuinely sustained outage backing off exponentially can still
    # rack up a lot of near-instant attempts early on before the backoff
    # has grown large; this caps that regardless of what the backoff level
    # currently is.
    indexing_max_calls_per_minute: int = 60
    # How often the running bot snapshots RpcGateway's call stats into
    # Accounting, so `--daily-report` can show the RPC budget after the
    # fact even though that one-shot command never starts a live gateway.
    rpc_budget_snapshot_interval_s: float = 60.0
    # INFO-level proof-of-life log: poll counts, RPC usage, WS status.
    # Exists so an unattended overnight run's silence is either "confirmed
    # quiet and healthy" or "clearly stalled," never ambiguous -- an
    # earlier run went 10 hours with zero of anything and looked, from the
    # logs alone, indistinguishable from a healthy quiet night.
    heartbeat_interval_s: float = 600.0

    # --- telegram ---
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- paper mode ---
    paper_fill_haircut_pct: float = 0.02

    # --- misc ---
    db_path: str = "data/bot.db"
    log_dir: str = "logs"
    # Deliberately a fixed path, not derived from a run ID: --reset-kill-switch
    # is a separate process invocation and must find the same file the
    # running bot is using.
    kill_switch_state_path: str = "data/kill_switch_state.json"

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
    # Previously only settable by constructing Config() directly in Python
    # (e.g. tests) -- there was no actual way to turn pump.fun off from
    # .env, despite enable_pumpfun_source existing as a Config field since
    # the circuit-breaker was added. Any of "false"/"0"/"no" (any case)
    # disables it; anything else (including unset) leaves the default True.
    enable_pumpfun_env = os.environ.get("ENABLE_PUMPFUN")
    if enable_pumpfun_env is not None:
        cfg.enable_pumpfun_source = enable_pumpfun_env.strip().lower() not in ("false", "0", "no")
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
    p.add_argument(
        "--radar-stats", action="store_true",
        help="Print the latest InsiderRadar snapshot (wallets indexed, tokens tracked, events seen) and exit.",
    )
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
