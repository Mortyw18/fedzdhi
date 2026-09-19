"""Pool-creation / token-launch event detection from raw `getTransaction`
results -- the core of event-driven discovery (see Orchestrator's
_check_pool_creation and README.md's "Event-driven discovery" section).

Confidence levels, stated plainly rather than implied:

- Raydium AMM V4's Initialize2 discriminator (a raw u8 tag, not an Anchor
  program) = 1, verified directly against Raydium's own source
  (raydium-io/raydium-amm, program/src/instruction.rs's AmmInstruction::
  unpack: `1 => { ... Initialize2 ... }`, with the legacy `Initialize`
  variant at 0). HIGH confidence.
- pump.fun's "create" discriminator is derived via Anchor's own documented,
  deterministic rule (sha256("global:<name>")[:8], see
  anchor_discriminator) rather than a hardcoded magic byte array copied
  from memory. Cross-checked against pump.fun's own public docs
  (github.com/pump-fun/pump-public-docs, PUMP_PROGRAM_README.md: "create
  (user, name, symbol, uri, creator) allows a user to create a new coin"
  -- confirming "create" is the current, documented instruction name, not
  a deprecated one) and against a third-party byte-level analysis
  (allenhark.com/blog/pumpfun-create-instruction-discriminator) that
  independently states the same discriminator bytes this derives.
  HIGH confidence. A "create_v2" naming has been seen referenced in at
  least one third-party SDK's convenience-function name; whether that
  reflects a genuinely different ON-CHAIN instruction (vs. an SDK
  bundling create with a metadata-upload step under a "v2" function name)
  was not resolved, and isn't accounted for here. If
  events_matched_creation stays at/near zero for the pump.fun program
  specifically while raw events_seen is clearly nonzero, this is the
  first thing to re-check.
- Mint resolution avoids hardcoding either program's account ordering
  entirely (which would be a third, compounding guess) by instead relying
  on the Solana SPL Token Program's initializeMint/initializeMint2
  instructions, which Solana's own jsonParsed RPC encoding reliably
  auto-parses (unlike Raydium/pump.fun's own custom instructions, which
  come back as raw, undecoded bytes). See resolve_new_mint's docstring for
  the reasoning. HIGH confidence in the technique; correctness still
  depends on both programs' instruction sets not having changed since this
  was written.
"""
from __future__ import annotations

import base64
import hashlib
from dataclasses import dataclass
from typing import Optional

import base58

from bot.jupiter_client import SOL_MINT

RAYDIUM_AMM_V4_PROGRAM_ID = "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8"
PUMPFUN_BONDING_CURVE_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"


def anchor_discriminator(instruction_name: str) -> bytes:
    """Anchor's documented, deterministic instruction discriminator:
    sha256("global:<name>")[:8]. Computed here rather than hardcoded so
    the derivation is auditable in one line instead of trusting a magic
    byte array."""
    return hashlib.sha256(f"global:{instruction_name}".encode()).digest()[:8]


# Raydium AMM V4 predates Anchor and uses its own simple instruction-index
# scheme (a raw leading u8), not the sha256 derivation above. Initialize2
# is index 1 -- see this module's docstring for the confidence note.
RAYDIUM_INITIALIZE2_DISCRIMINATOR = bytes([1])
PUMPFUN_CREATE_DISCRIMINATOR = anchor_discriminator("create")

# --- free, pre-getTransaction log-content pre-filter ---
#
# The original design fetched getTransaction for a rate-capped SAMPLE of
# ALL logsSubscribe traffic (indexing_max_calls_per_minute, default
# 60/min) and ran _matches_creation_instruction against whatever came
# back. That's fine for InsiderRadar's purpose (a representative sample of
# swap activity), but pool creations are a small fraction of a busy
# program's total traffic -- confirmed in production at
# ws_pool_events_seen=244, ws_pool_events_matched=0 after tens of
# thousands of notifications, with indexing_skipped climbing into the tens
# of thousands from the calls/min cap alone. At real-world volumes
# (thousands/min network-wide), a ~60/min random sample can statistically
# never be expected to land on a rare event: the two probabilities
# multiply (chance a given notification is even sampled) x (chance a
# random notification is a creation), and the first factor alone is
# already on the order of 1% or less.
#
# The fix: a pool-creation instruction is not silent BEFORE any RPC call
# is made -- logsSubscribe's own notification already carries the
# program's on-chain log lines for free (that's the entire point of
# "logs"Subscribe). Both programs here log something distinctive for
# their creation instruction specifically, verified against real,
# independent sources rather than assumed:
#
# - pump.fun's "create" is an Anchor instruction. Anchor's generated
#   dispatcher unconditionally logs "Program log: Instruction: <Name>"
#   (PascalCase) as the very first thing an instruction handler does --
#   see https://docs.chainstack.com/docs/solana-listening-to-pumpfun-token-mint-using-only-logssubscribe,
#   which documents using exactly "Program log: Instruction: Create" to
#   detect pump.fun launches via logsSubscribe ALONE, with no
#   getTransaction call at all.
# - Raydium AMM v4 predates Anchor and has its own logging convention
#   instead: raydium-amm/program/src/log.rs's encode_ray_log emits
#   "ray_log: <base64>" for every instruction, where the base64-decoded
#   payload's FIRST byte is a LogType discriminant (Init=0, Deposit=1,
#   Withdraw=2, SwapBaseIn=3, SwapBaseOut=4) -- confirmed directly against
#   that file's source. LogType::Init covers pool initialization (the
#   legacy Initialize AND Initialize2), which is exactly what
#   _matches_creation_instruction below goes on to narrow down precisely
#   once the transaction is actually fetched.
#
# A False here means "not worth fetching" -- indistinguishable in effect
# from the old design simply not having sampled this notification. A
# False POSITIVE (log content looked promising but
# _matches_creation_instruction later disagrees) costs exactly one
# ordinary getTransaction call that correctly finds no match -- never a
# correctness problem, since dispatch still requires
# _matches_creation_instruction + resolve_new_mint to both succeed on the
# real, fetched transaction. This function only decides what's worth
# fetching in the first place.
#
# False positives are cheap per-event but NOT free in aggregate: the first
# version of this matched a bare substring across the whole (whole-
# transaction) log list and fired on a large share of ordinary pump.fun
# buys, driving ~5 fetches/sec. That in turn starved the WebSocket
# consumer badly enough to cause repeated server-side disconnects -- see
# attributed_log_lines below for the mechanism and RpcWebSocket.subscribe
# for the independent fix on the socket side.
PUMPFUN_CREATE_LOG_LINE = "Program log: Instruction: Create"
_RAY_LOG_MARKER = "ray_log: "
_RAYDIUM_INIT_LOG_TYPE = 0


def attributed_log_lines(logs: list[str]) -> list[tuple[str, str]]:
    """Pairs every log line with the program that actually EMITTED it, by
    replaying Solana's invoke/success bracketing to track the call stack:

        Program <id> invoke [1]        <- push <id>
        Program log: Instruction: Foo  <- emitted BY <id>
        Program <inner> invoke [2]     <- push <inner>
        Program log: Instruction: Bar  <- emitted BY <inner>, NOT by <id>
        Program <inner> success        <- pop
        Program <id> success           <- pop

    This attribution is not a nicety, it's the whole correctness of the
    pre-filter. A `logsSubscribe` subscription filtered on `mentions:
    [<program>]` delivers the ENTIRE transaction's logs, including every
    OTHER program invoked in it -- so a bare substring search over the raw
    list matches text that a completely unrelated program wrote.

    That is exactly what happened in production: the SPL Associated Token
    Account program logs `Program log: Instruction: Create` (and
    `Instruction: CreateIdempotent`) when it creates a buyer's token
    account -- byte-identical to what pump.fun's own Anchor dispatcher
    logs for its `create` instruction. Nearly every pump.fun BUY creates
    an ATA, so an unattributed match fired on a large share of ordinary
    buy traffic: ~5 fetches/sec, every one of them correctly reported
    `matched: false` by the precise discriminator check downstream. Note
    an exact-string comparison alone does NOT fix this, because the ATA
    program's line is not merely similar to pump.fun's, it is identical --
    only knowing WHO emitted it separates them.

    A line before any `invoke` (or after the stack has unwound) is
    attributed to "", which matches no program and is therefore ignored.
    """
    stack: list[str] = []
    out: list[tuple[str, str]] = []
    for raw_line in logs:
        line = raw_line.strip()
        parts = line.split(" ")
        if len(parts) >= 3 and parts[0] == "Program" and parts[2] == "invoke":
            stack.append(parts[1])
            continue
        if len(parts) >= 3 and parts[0] == "Program" and parts[2] in ("success", "failed"):
            if stack:
                stack.pop()
            continue
        out.append((stack[-1] if stack else "", line))
    return out


def matches_creation_log_hint(logs: list[str], program_id: str) -> bool:
    """True if `logs` (a logsSubscribe notification's own log lines --
    see Orchestrator._index_program_loop) already look like a creation
    for `program_id`, with zero RPC calls made to decide this.

    Only lines `program_id` itself emitted are considered -- see
    attributed_log_lines for why that qualifier is load-bearing.
    """
    if program_id not in (PUMPFUN_BONDING_CURVE_PROGRAM_ID, RAYDIUM_AMM_V4_PROGRAM_ID):
        return False
    for emitter, line in attributed_log_lines(logs):
        if emitter != program_id:
            continue
        if program_id == PUMPFUN_BONDING_CURVE_PROGRAM_ID:
            # Exact, not a substring: "Instruction: CreateIdempotent" must
            # never match "Instruction: Create".
            if line == PUMPFUN_CREATE_LOG_LINE:
                return True
            continue
        marker_at = line.find(_RAY_LOG_MARKER)
        if marker_at == -1:
            continue
        payload = line[marker_at + len(_RAY_LOG_MARKER):].strip()
        try:
            decoded = base64.b64decode(payload, validate=False)
        except ValueError:
            continue
        if decoded and decoded[0] == _RAYDIUM_INIT_LOG_TYPE:
            return True
    return False


_DISCRIMINATORS = {
    RAYDIUM_AMM_V4_PROGRAM_ID: RAYDIUM_INITIALIZE2_DISCRIMINATOR,
    PUMPFUN_BONDING_CURVE_PROGRAM_ID: PUMPFUN_CREATE_DISCRIMINATOR,
}


@dataclass
class PoolCreationEvent:
    mint: str
    program_id: str
    signature: str
    slot: int
    block_time: Optional[float]


def _account_keys(tx: dict) -> list[str]:
    message = (tx.get("transaction") or {}).get("message") or {}
    keys = message.get("accountKeys", [])
    return [k.get("pubkey") if isinstance(k, dict) else k for k in keys]


def _top_level_instructions(tx: dict) -> list[dict]:
    message = (tx.get("transaction") or {}).get("message") or {}
    return message.get("instructions", []) or []


def _inner_instructions(tx: dict) -> list[dict]:
    groups = (tx.get("meta") or {}).get("innerInstructions") or []
    out: list[dict] = []
    for group in groups:
        out.extend(group.get("instructions", []) or [])
    return out


def _matches_creation_instruction(tx: dict, program_id: str) -> bool:
    """True if a TOP-LEVEL instruction in this transaction targets
    `program_id` and its raw instruction data starts with the known
    creation discriminator. Only top-level -- a creation instruction
    wrapped inside an aggregator/router as an inner instruction is a real
    but accepted gap, not a case this checks for.

    Raydium/pump.fun aren't "well-known" programs to Solana's jsonParsed
    encoding, so their instructions come back as raw {programIdIndex,
    accounts, data} with `data` still base58-encoded rather than a
    friendly `parsed` object -- this decodes it manually.
    """
    discriminator = _DISCRIMINATORS.get(program_id)
    if discriminator is None:
        return False
    keys = _account_keys(tx)
    for ix in _top_level_instructions(tx):
        idx = ix.get("programIdIndex")
        if idx is None or idx >= len(keys) or keys[idx] != program_id:
            continue
        data = ix.get("data")
        if not data or not isinstance(data, str):
            continue
        try:
            raw = base58.b58decode(data)
        except ValueError:
            continue
        if raw.startswith(discriminator):
            return True
    return False


def _mints_initialized_in_tx(tx: dict) -> set[str]:
    """Mints whose SPL Token initializeMint/initializeMint2 instruction
    appears anywhere in this transaction (top-level or inner) -- i.e. the
    mint(s) actually CREATED by this transaction, not merely touched by
    it. Both programs' instruction sets are well-known/stable enough that
    jsonParsed reliably gives a `parsed.type` + `parsed.info.mint` for
    these, which is what makes this more trustworthy than guessing either
    Raydium's or pump.fun's own account ordering."""
    out: set[str] = set()
    for ix in _top_level_instructions(tx) + _inner_instructions(tx):
        parsed = ix.get("parsed") if isinstance(ix, dict) else None
        if not isinstance(parsed, dict):
            continue
        if parsed.get("type") in ("initializeMint", "initializeMint2"):
            mint = (parsed.get("info") or {}).get("mint")
            if mint:
                out.add(mint)
    return out


def _newly_appeared_mints(tx: dict) -> set[str]:
    """Mints with a postTokenBalances entry at an account index that had
    NO preTokenBalances entry -- i.e. an account that didn't hold (or
    didn't exist for) this mint before this transaction. Pool creation
    always funds brand-new vault/LP-token accounts this way."""
    meta = tx.get("meta") or {}
    pre = {(b.get("accountIndex"), b.get("mint")) for b in meta.get("preTokenBalances") or []}
    out: set[str] = set()
    for b in meta.get("postTokenBalances") or []:
        mint = b.get("mint")
        if not mint:
            continue
        key = (b.get("accountIndex"), mint)
        if key not in pre:
            out.add(mint)
    return out


def resolve_new_mint(tx: dict, program_id: str) -> Optional[str]:
    """The mint a trader would actually buy, or None if it can't be
    determined unambiguously.

    Every AMM pool creation mints a brand-new LP token; every pump.fun
    launch mints a brand-new bonding-curve token. Those are opposite
    cases for which mint we want:

    - pump.fun "create": the wanted mint IS the one initialized in this
      transaction (pump.fun mints the launch token itself, right here).
    - Raydium "Initialize2": the wanted mint is NOT the one initialized in
      this transaction (that's the LP token) -- it's whichever OTHER
      newly-funded, non-SOL mint appears, i.e. the pre-existing base token
      the pool is being created for.
    """
    initialized = _mints_initialized_in_tx(tx)
    appeared = _newly_appeared_mints(tx) - {SOL_MINT}

    if program_id == PUMPFUN_BONDING_CURVE_PROGRAM_ID:
        candidates = initialized & appeared
        if len(candidates) == 1:
            return next(iter(candidates))
        if len(initialized) == 1:  # balance-appearance didn't line up (e.g. zero initial dev buy) -- still unambiguous
            return next(iter(initialized))
        return None

    if program_id == RAYDIUM_AMM_V4_PROGRAM_ID:
        candidates = appeared - initialized
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    return None


def detect_pool_creation(tx: dict, signature: str, program_id: str) -> Optional[PoolCreationEvent]:
    """None if this transaction isn't a pool-creation/token-launch for the
    given program, or if it is but the mint couldn't be resolved
    unambiguously (see resolve_new_mint) -- a PoolCreationEvent otherwise.
    Never raises: a malformed/unexpected transaction shape degrades to
    None, same as "not a match," rather than crashing the indexing loop
    that calls this on every notification.
    """
    try:
        if not _matches_creation_instruction(tx, program_id):
            return None
        mint = resolve_new_mint(tx, program_id)
    except (KeyError, TypeError, AttributeError):
        return None
    if mint is None:
        return None
    return PoolCreationEvent(
        mint=mint,
        program_id=program_id,
        signature=signature,
        slot=tx.get("slot", 0),
        block_time=tx.get("blockTime"),
    )
