"""CLI entrypoint: memebot.

Handles the one-shot commands (--daily-report, --reset-kill-switch,
--sweep) directly, then hands off to the Orchestrator's asyncio loop for
the actual trading run. Every gate from the parameter block in the spec
(paper-by-default, position sizing, live confirmation) is enforced here
before a single network call to a paid or rate-limited service is made.
"""
from __future__ import annotations

import sys
import time

from bot.accounting import Accounting
from bot.config import ConfigError, apply_cli_overrides, build_arg_parser, confirm_startup, load_config_from_env, require_typed_confirmation
from bot.kill_switch import KillSwitch
from bot.rpc_gateway import RpcGateway
from bot.solana_wallet import build_unsigned_sol_transfer_tx_b64, load_wallet_from_env

SWEEP_CONFIRMATION_PHRASE = "SWEEP WALLET"


def _cmd_daily_report(cfg) -> int:
    accounting = Accounting(cfg.db_path)
    print(accounting.render_daily_report())
    accounting.close()
    return 0


def _cmd_radar_stats(cfg) -> int:
    """InsiderRadar's own state lives entirely in the running process's
    memory -- this only ever shows what the bot last snapshotted into
    SQLite (every rpc_budget_snapshot_interval_s, default 60s), so it can
    be a little stale but is never wrong about the shape of the data."""
    accounting = Accounting(cfg.db_path)
    snapshot = accounting.latest_radar_snapshot()
    accounting.close()
    if snapshot is None:
        print(
            "No InsiderRadar snapshots recorded yet. Either the bot hasn't run long enough\n"
            "to hit its first snapshot interval, or it hasn't been started at all."
        )
        return 0
    age_s = time.time() - snapshot["timestamp"]
    print("=== InsiderRadar Stats ===")
    print(f"As of: {age_s / 60:.1f} minutes ago")
    print(f"Wallets indexed:   {snapshot['wallets_indexed']}")
    print(f"Tokens tracked:    {snapshot['tokens_tracked']}")
    print(f"Buy events seen:   {snapshot['total_buy_events']}")
    print(f"Sell events seen:  {snapshot['total_sell_events']}")
    print(f"Sniper wallets:    {snapshot['sniper_count']} (never copied)")
    print(f"Conviction wallets:{snapshot['conviction_count']:>3} (copyable)")
    print(f"Auto-unfollowed:   {snapshot['unfollowed_count']}")
    print(f"Currently watched: {snapshot['watch_list_size']}")
    if snapshot["wallets_indexed"] == 0:
        print(
            "\nZero wallets indexed. If this bot has been running for a while, check that\n"
            "HELIUS_WS_URL is set (WebSocket indexing needs it) and look for "
            "'indexing notification dropped' or 'ws subscribe ... dropped' lines in the logs."
        )
    return 0


def _cmd_reset_kill_switch(cfg) -> int:
    ks = KillSwitch(cfg.daily_loss_cap_sol, state_path=cfg.kill_switch_state_path)
    was_halted = ks.is_halted()
    reason = ks.halt_reason()
    ks.reset()
    if was_halted:
        print(f"Kill switch reset. Was halted for: {reason}")
    else:
        print("Kill switch was not halted. Nothing to reset.")
    return 0


def _cmd_sweep(cfg, args) -> int:
    if not args.sweep_to:
        print("Refusing: --sweep requires --sweep-to <destination address>.")
        return 1
    wallet = load_wallet_from_env()
    rpc = RpcGateway(cfg.helius_rpc_url, cfg.failover_rpc_url, rate_limit_per_10s=cfg.rpc_rate_limit_per_10s)
    balance_lamports = rpc.get_balance(wallet.pubkey_base58)
    fee_reserve_lamports = 5_000
    amount_lamports = balance_lamports - fee_reserve_lamports
    if amount_lamports <= 0:
        print(f"Nothing to sweep: balance is {balance_lamports} lamports (need > {fee_reserve_lamports}).")
        return 1

    print(f"Sweeping {amount_lamports / 1e9:.6f} SOL from {wallet.pubkey_base58} to {args.sweep_to}")
    ok = require_typed_confirmation(
        "This drains the wallet. Confirm the destination address above is correct.",
        SWEEP_CONFIRMATION_PHRASE,
        skip=args.yes,
    )
    if not ok:
        print("Confirmation not received. Aborting sweep.")
        return 1

    blockhash_info = rpc.get_latest_blockhash()
    blockhash = blockhash_info["value"]["blockhash"]
    unsigned_b64 = build_unsigned_sol_transfer_tx_b64(wallet.pubkey_base58, args.sweep_to, amount_lamports, blockhash)
    signed_b64 = wallet.sign_versioned_transaction_b64(unsigned_b64)
    sig = rpc.send_transaction(signed_b64)
    print(f"Sweep transaction sent: {sig}")
    confirmed = rpc.confirm_signature(sig, timeout_s=60.0)
    print("Confirmed." if confirmed else "Not confirmed within timeout -- check explorer manually.")
    return 0 if confirmed else 1


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)

    cfg = load_config_from_env(args.env_file)
    cfg = apply_cli_overrides(cfg, args)

    if args.daily_report:
        return _cmd_daily_report(cfg)
    if args.radar_stats:
        return _cmd_radar_stats(cfg)
    if args.reset_kill_switch:
        return _cmd_reset_kill_switch(cfg)
    if args.sweep:
        return _cmd_sweep(cfg, args)

    try:
        cfg.validate()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if cfg.observe_only:
        print(
            "OBSERVE-ONLY mode (M2): logging candidates, safety verdicts, and InsiderRadar\n"
            "indexing against live data. ExecutionEngine.buy is never called -- no trade will\n"
            "be placed, live or paper. No wallet is required. Review rejections with\n"
            "`python run.py --daily-report` or the JSON logs in logs/memebot.jsonl."
        )

    confirm_startup(cfg, args)

    from bot.orchestrator import Orchestrator
    import asyncio

    orchestrator = Orchestrator(cfg)
    try:
        asyncio.run(orchestrator.run())
    except KeyboardInterrupt:
        print("\nShutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
