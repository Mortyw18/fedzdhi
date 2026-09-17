from __future__ import annotations

import base64

import base58
import pytest
from nacl.signing import SigningKey, VerifyKey

from bot.solana_wallet import (
    Wallet,
    WalletError,
    build_unsigned_sol_transfer_tx_b64,
    decode_shortvec,
    encode_shortvec,
    load_wallet_from_env,
)


@pytest.mark.parametrize("value", [0, 1, 127, 128, 129, 16383, 16384, 2_097_151])
def test_shortvec_roundtrip(value):
    encoded = encode_shortvec(value)
    decoded, consumed = decode_shortvec(encoded)
    assert decoded == value
    assert consumed == len(encoded)


def test_load_wallet_from_env_base58(monkeypatch):
    sk = SigningKey.generate()
    secret_bytes = bytes(sk) + bytes(sk.verify_key)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", base58.b58encode(secret_bytes).decode("ascii"))

    wallet = load_wallet_from_env()
    assert wallet.pubkey_base58 == base58.b58encode(bytes(sk.verify_key)).decode("ascii")


def test_load_wallet_from_env_json_array(monkeypatch):
    sk = SigningKey.generate()
    secret_bytes = bytes(sk) + bytes(sk.verify_key)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", str(list(secret_bytes)))

    wallet = load_wallet_from_env()
    assert wallet.pubkey_base58 == base58.b58encode(bytes(sk.verify_key)).decode("ascii")


def test_load_wallet_missing_env_raises(monkeypatch):
    monkeypatch.delenv("SOLANA_PRIVATE_KEY", raising=False)
    with pytest.raises(WalletError):
        load_wallet_from_env()


def test_load_wallet_mismatched_pubkey_rejected(monkeypatch):
    sk = SigningKey.generate()
    other = SigningKey.generate()
    bad_secret = bytes(sk) + bytes(other.verify_key)
    monkeypatch.setenv("SOLANA_PRIVATE_KEY", base58.b58encode(bad_secret).decode("ascii"))
    with pytest.raises(WalletError):
        load_wallet_from_env()


def test_wallet_repr_never_leaks_key_material():
    sk = SigningKey.generate()
    wallet = Wallet(_signing_key=sk, pubkey_bytes=bytes(sk.verify_key))
    text = repr(wallet)
    assert wallet.pubkey_base58 in text
    assert base58.b58encode(bytes(sk)).decode("ascii") not in text


def test_sign_versioned_transaction_produces_verifiable_signature():
    sk = SigningKey.generate()
    wallet = Wallet(_signing_key=sk, pubkey_bytes=bytes(sk.verify_key))

    message = b"pretend this is a compiled Solana message" * 3
    unsigned = encode_shortvec(1) + (b"\x00" * 64) + message
    unsigned_b64 = base64.b64encode(unsigned).decode("ascii")

    signed_b64 = wallet.sign_versioned_transaction_b64(unsigned_b64)
    signed_raw = base64.b64decode(signed_b64)

    num_sigs, prefix_len = decode_shortvec(signed_raw)
    assert num_sigs == 1
    signature = signed_raw[prefix_len:prefix_len + 64]
    recovered_message = signed_raw[prefix_len + 64:]
    assert recovered_message == message

    VerifyKey(bytes(sk.verify_key)).verify(message, signature)  # raises if invalid


def test_sign_versioned_transaction_out_of_range_signer_index():
    sk = SigningKey.generate()
    wallet = Wallet(_signing_key=sk, pubkey_bytes=bytes(sk.verify_key))
    unsigned = encode_shortvec(1) + (b"\x00" * 64) + b"msg"
    unsigned_b64 = base64.b64encode(unsigned).decode("ascii")
    with pytest.raises(WalletError):
        wallet.sign_versioned_transaction_b64(unsigned_b64, signer_index=5)


def test_build_and_sign_sol_transfer():
    sk = SigningKey.generate()
    wallet = Wallet(_signing_key=sk, pubkey_bytes=bytes(sk.verify_key))
    to_pubkey = base58.b58encode(bytes([7] * 32)).decode("ascii")
    blockhash = base58.b58encode(bytes([9] * 32)).decode("ascii")

    unsigned_b64 = build_unsigned_sol_transfer_tx_b64(wallet.pubkey_base58, to_pubkey, 1_000_000, blockhash)
    signed_b64 = wallet.sign_versioned_transaction_b64(unsigned_b64)
    signed_raw = base64.b64decode(signed_b64)

    num_sigs, prefix_len = decode_shortvec(signed_raw)
    signature = signed_raw[prefix_len:prefix_len + 64]
    message = signed_raw[prefix_len + 64:]
    VerifyKey(bytes(sk.verify_key)).verify(message, signature)

    # header + 3 account keys (32 bytes each) + blockhash (32) should all be present
    assert len(message) > 3 + 3 * 32 + 32
