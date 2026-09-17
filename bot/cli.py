"""CLI entrypoint: memebot.

Handles the one-shot commands (--daily-report, --reset-kill-switch,
--sweep) directly, then hands off to the Orchestrator's asyncio loop for
the actual trading run. Every gate from the parameter block in the spec
(paper-by-default, position sizing, live confirmation) is enforced here
before a single network call to a paid or rate-limited service is made.
"""
from __future__ import annotations

import sys

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


def _cmd_reset_kill_switch(cfg) -> int:
    ks = KillSwitch(cfg.daily_loss_cap_sol, state_path="data/kill_switch_state.json")
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
    if args.reset_kill_switch:
        return _cmd_reset_kill_switch(cfg)
    if args.sweep:
        return _cmd_sweep(cfg, args)

    try:
        cfg.validate()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

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
