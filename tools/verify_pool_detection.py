#!/usr/bin/env python3
"""Verify the pool-creation matcher against REAL mainnet transactions.

Everything in tests/test_pool_events.py is a synthetic fixture built from
documented log shapes. That proves the logic is self-consistent; it does
NOT prove the shapes match what Solana actually emits today. This script
closes that gap by running the exact production code path
(matches_creation_log_hint -> detect_pool_creation) against live
transactions fetched with your own RPC key.

Usage (from the repo root, with .env holding HELIUS_API_KEY/HELIUS_RPC_URL):

    # Scan recent pump.fun transactions for creates and verify both stages
    python3 tools/verify_pool_detection.py --scan pumpfun

    # Same for Raydium AMM v4 pool initializations
    python3 tools/verify_pool_detection.py --scan raydium

    # Or check one specific transaction you already know is a create
    python3 tools/verify_pool_detection.py --signature <SIGNATURE>

What a healthy result looks like: at least one scanned transaction where
BOTH `hint` and `detected` are true, with a plausible mint. If `hint` is
false on a transaction that IS a create, the log pre-filter's assumption
about that program's logging format is wrong. If `hint` is true but
`detected` is false, the byte-level discriminator or the mint resolution
is wrong. The two failures need completely different fixes, which is
exactly why this prints them separately.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config_from_env  # noqa: E402
from bot.pool_events import (  # noqa: E402
    PUMPFUN_BONDING_CURVE_PROGRAM_ID,
    RAYDIUM_AMM_V4_PROGRAM_ID,
    attributed_log_lines,
    detect_pool_creation,
    matches_creation_log_hint,
)
from bot.rpc_gateway import RpcGateway  # noqa: E402

PROGRAMS = {
    "pumpfun": PUMPFUN_BONDING_CURVE_PROGRAM_ID,
    "raydium": RAYDIUM_AMM_V4_PROGRAM_ID,
}


def _fetch_transaction(rpc: RpcGateway, signature: str) -> dict | None:
    return rpc.call(
        "getTransaction",
        [
            signature,
            {
                "encoding": "jsonParsed",
                "commitment": "confirmed",
                "maxSupportedTransactionVersion": rpc.max_supported_transaction_version,
            },
        ],
    )


def _check(tx: dict, signature: str, program_id: str, verbose: bool) -> tuple[bool, bool, str | None]:
    # logsSubscribe delivers meta.logMessages as its `logs` field, so this
    # is the same input the live pre-filter sees.
    logs = (tx.get("meta") or {}).get("logMessages") or []
    hint = matches_creation_log_hint(logs, program_id)
    event = detect_pool_creation(tx, signature, program_id)
    if verbose:
        print(f"    log lines: {len(logs)}")
        for emitter, line in attributed_log_lines(logs)[:40]:
            marker = "*" if emitter == program_id else " "
            print(f"    {marker} [{emitter[:8] or '--------'}] {line[:120]}")
    return hint, event is not None, event.mint if event else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--scan", choices=sorted(PROGRAMS), help="Scan recent transactions for this program.")
    parser.add_argument("--signature", help="Verify one specific transaction signature.")
    parser.add_argument("--program", choices=sorted(PROGRAMS), default="pumpfun", help="Program for --signature.")
    parser.add_argument("--limit", type=int, default=100, help="How many recent signatures to scan (default 100).")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--verbose", action="store_true", help="Print attributed log lines for each transaction.")
    args = parser.parse_args()

    if not args.scan and not args.signature:
        parser.error("pass --scan pumpfun|raydium or --signature <SIG>")

    cfg = load_config_from_env(args.env_file)
    if not cfg.helius_rpc_url:
        print("No RPC URL configured -- set HELIUS_API_KEY or HELIUS_RPC_URL in .env", file=sys.stderr)
        return 2
    rpc = RpcGateway(
        cfg.helius_rpc_url,
        cfg.failover_rpc_url,
        rate_limit_per_10s=cfg.rpc_rate_limit_per_10s,
        max_supported_transaction_version=cfg.rpc_max_supported_transaction_version,
    )

    if args.signature:
        program_id = PROGRAMS[args.program]
        tx = _fetch_transaction(rpc, args.signature)
        if not tx:
            print(f"Transaction not found: {args.signature}", file=sys.stderr)
            return 1
        hint, detected, mint = _check(tx, args.signature, program_id, verbose=True)
        print(f"\n  hint={hint}  detected={detected}  mint={mint}")
        return 0 if (hint and detected) else 1

    program_id = PROGRAMS[args.scan]
    print(f"Scanning up to {args.limit} recent {args.scan} transactions ({program_id})...\n")
    signatures = rpc.call("getSignaturesForAddress", [program_id, {"limit": args.limit}]) or []
    print(f"Got {len(signatures)} signatures. Fetching each (this costs {len(signatures)} RPC calls)...\n")

    hint_hits = 0
    detected_hits = 0
    checked = 0
    disagreements: list[tuple[str, bool, bool]] = []

    for entry in signatures:
        signature = entry.get("signature")
        if not signature or entry.get("err"):
            continue  # a failed transaction never created anything
        try:
            tx = _fetch_transaction(rpc, signature)
        except Exception as exc:  # noqa: BLE001 -- a diagnostic script should keep going
            print(f"  {signature[:16]}... fetch failed: {exc}")
            continue
        if not tx:
            continue
        checked += 1
        hint, detected, mint = _check(tx, signature, program_id, verbose=args.verbose)
        if hint:
            hint_hits += 1
        if detected:
            detected_hits += 1
        if hint or detected:
            print(f"  {signature[:16]}...  hint={hint}  detected={detected}  mint={mint}")
        if hint != detected:
            disagreements.append((signature, hint, detected))

    print(f"\n--- {checked} transactions checked ---")
    print(f"  pre-filter matched : {hint_hits}")
    print(f"  fully detected     : {detected_hits}")
    if disagreements:
        print(f"\n  {len(disagreements)} disagreement(s) between the two stages:")
        for signature, hint, detected in disagreements[:10]:
            if hint and not detected:
                why = "pre-filter fired but the discriminator/mint resolution did not (false positive, or mint ambiguous)"
            else:
                why = "DETECTED WITHOUT THE PRE-FILTER -- the pre-filter is missing real creations"
            print(f"    {signature}  {why}")
    if detected_hits == 0:
        print("\n  No creations found in this sample. Either none occurred in these")
        print("  transactions (re-run with a larger --limit), or detection is broken.")
        print("  Run with --verbose to inspect the attributed log lines directly.")
    return 0 if detected_hits else 1


if __name__ == "__main__":
    sys.exit(main())
