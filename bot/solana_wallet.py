"""Keypair loading and raw transaction signing.

Deliberately does NOT depend on solana-py/solders (heavy, Rust-backed,
and overkill for what we need: Solana signatures are plain Ed25519 over
the serialized message bytes). We use pynacl for the Ed25519 math and
hand-roll the tiny bit of Solana wire format (compact-u16 / "shortvec"
length prefixes) needed to slot our signature into a Jupiter-built
transaction.

The secret key is read from an env var, held only in memory, and never
logged. `repr(Wallet(...))` deliberately omits the key material.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

import base58
from nacl.signing import SigningKey

ENV_VAR_NAME = "SOLANA_PRIVATE_KEY"


class WalletError(Exception):
    pass


def decode_shortvec(data: bytes, offset: int = 0) -> tuple[int, int]:
    """Decode a Solana compact-u16 ("shortvec") length prefix.

    Returns (value, bytes_consumed).
    """
    value = 0
    shift = 0
    consumed = 0
    while True:
        if offset + consumed >= len(data):
            raise WalletError("truncated shortvec")
        byte = data[offset + consumed]
        consumed += 1
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            break
        shift += 7
    return value, consumed


def encode_shortvec(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            break
    return bytes(out)


@dataclass
class Wallet:
    _signing_key: SigningKey
    pubkey_bytes: bytes

    def __repr__(self) -> str:  # never print key material
        return f"Wallet(pubkey={self.pubkey_base58})"

    @property
    def pubkey_base58(self) -> str:
        return base58.b58encode(self.pubkey_bytes).decode("ascii")

    def sign_message_bytes(self, message: bytes) -> bytes:
        """Return the raw 64-byte Ed25519 signature over `message`."""
        signed = self._signing_key.sign(message)
        return signed.signature

    def sign_versioned_transaction_b64(self, tx_b64: str, signer_index: int = 0) -> str:
        """Sign a base64 Jupiter-built (legacy or v0) transaction.

        Jupiter's /swap response contains an unsigned transaction where our
        wallet is a required signer (almost always index 0, the fee payer).
        We slot our signature into that position and leave any other
        signature slots as returned (Jupiter transactions from a single
        wallet only ever require one signer in this bot's flows).
        """
        import base64

        raw = base64.b64decode(tx_b64)
        num_sigs, prefix_len = decode_shortvec(raw)
        sig_start = prefix_len
        sig_size = 64
        sigs_end = sig_start + num_sigs * sig_size
        if signer_index >= num_sigs:
            raise WalletError(
                f"signer_index {signer_index} out of range for transaction with {num_sigs} required signatures"
            )
        message_bytes = raw[sigs_end:]
        signature = self.sign_message_bytes(message_bytes)

        sigs = bytearray(raw[sig_start:sigs_end])
        sigs[signer_index * sig_size:(signer_index + 1) * sig_size] = signature

        new_raw = encode_shortvec(num_sigs) + bytes(sigs) + message_bytes
        return base64.b64encode(new_raw).decode("ascii")


def build_unsigned_sol_transfer_tx_b64(from_pubkey_b58: str, to_pubkey_b58: str, lamports: int, recent_blockhash_b58: str) -> str:
    """Hand-build a minimal legacy Solana transaction: one SystemProgram::Transfer.

    Used only by --sweep. We don't need a general-purpose transaction
    builder for the trading path (Jupiter builds those), so this is
    intentionally the one instruction we construct ourselves.
    """
    import base64
    import struct

    system_program_id = base58.b58encode(bytes(32)).decode("ascii")
    from_bytes = base58.b58decode(from_pubkey_b58)
    to_bytes = base58.b58decode(to_pubkey_b58)
    program_bytes = base58.b58decode(system_program_id)
    blockhash_bytes = base58.b58decode(recent_blockhash_b58)

    account_keys = [from_bytes, to_bytes, program_bytes]
    header = bytes([1, 0, 1])  # 1 signer, 0 readonly-signed, 1 readonly-unsigned (the program)

    accounts_section = encode_shortvec(len(account_keys)) + b"".join(account_keys)

    instruction_data = struct.pack("<IQ", 2, lamports)  # SystemProgram Transfer = index 2
    instruction = (
        bytes([2])  # programIdIndex: account_keys[2] is the System Program
        + encode_shortvec(2) + bytes([0, 1])  # accounts: [from, to]
        + encode_shortvec(len(instruction_data)) + instruction_data
    )
    instructions_section = encode_shortvec(1) + instruction

    message = header + accounts_section + blockhash_bytes + instructions_section

    unsigned_tx = encode_shortvec(1) + (b"\x00" * 64) + message
    return base64.b64encode(unsigned_tx).decode("ascii")


def _keypair_bytes_from_env_value(raw_value: str) -> bytes:
    raw_value = raw_value.strip()
    if raw_value.startswith("["):
        import json

        arr = json.loads(raw_value)
        data = bytes(arr)
    else:
        data = base58.b58decode(raw_value)
    if len(data) != 64:
        raise WalletError(
            f"expected a 64-byte secret key (32-byte seed + 32-byte pubkey), got {len(data)} bytes"
        )
    return data


def load_wallet_from_env(var_name: str = ENV_VAR_NAME) -> Wallet:
    raw_value = os.environ.get(var_name)
    if not raw_value:
        raise WalletError(
            f"{var_name} is not set. Export it as a base58 or JSON-array 64-byte secret key. "
            "Never commit it or put it in a config file."
        )
    data = _keypair_bytes_from_env_value(raw_value)
    seed, pubkey = data[:32], data[32:]
    signing_key = SigningKey(seed)
    derived_pubkey = bytes(signing_key.verify_key)
    if derived_pubkey != pubkey:
        raise WalletError("secret key's embedded public key does not match its derived public key")
    return Wallet(_signing_key=signing_key, pubkey_bytes=pubkey)
