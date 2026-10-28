"""Keyring (HKDF sub-keys) and AES-256-GCM sealing of exchange keys (PLAN ADR-009, D-14)."""

from dataclasses import replace

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from guarded_exchange.domain.credentials import ApiKeyPair, CredentialBinding, CredentialUnreadable
from guarded_exchange.domain.users import Environment
from guarded_exchange.infrastructure.crypto import (
    NONCE_BYTES,
    AesGcmCredentialCipher,
    KeyPurpose,
    Keyring,
    credential_aad,
)

MASTER = bytes(range(32))
KEYS = ApiKeyPair(api_key="CRYPTOKEY0123456", api_secret="crypto-secret-0123456789abcdefgh")  # noqa: S106 - test value
ALICE = CredentialBinding(
    credential_id="c-1", user_id="u-alice", exchange="bybit", environment=Environment.TESTNET
)


def test_every_purpose_gets_its_own_deterministic_hkdf_subkey() -> None:
    keyring = Keyring.single(MASTER)

    subkeys = {purpose: keyring.subkey(purpose) for purpose in KeyPurpose}

    assert {p.value for p in subkeys} == {"credentials", "audit", "request_state", "telegram"}  # D-14
    assert len(set(subkeys.values())) == len(KeyPurpose)
    assert all(len(key) == 32 for key in subkeys.values())
    assert keyring.subkey(KeyPurpose.CREDENTIALS) == Keyring.single(MASTER).subkey(KeyPurpose.CREDENTIALS)
    assert MASTER not in subkeys.values()
    expected = HKDF(
        algorithm=hashes.SHA256(), length=32, salt=None, info=b"guarded-exchange/v1/credentials"
    ).derive(MASTER)
    assert subkeys[KeyPurpose.CREDENTIALS] == expected


def test_keyring_refuses_bad_keys_and_never_prints_them() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        Keyring.single(b"short")
    with pytest.raises(ValueError, match="current version"):
        Keyring({1: MASTER}, current_version=2)

    assert repr(Keyring.single(MASTER)) == "Keyring(versions=[1], current=1)"


@pytest.mark.req("FR-31")
def test_seal_and_open_round_trip_without_the_secret_in_the_ciphertext() -> None:
    cipher = AesGcmCredentialCipher(Keyring.single(MASTER))

    sealed = cipher.seal(KEYS, ALICE)

    assert cipher.open(sealed, ALICE) == KEYS
    assert len(sealed.nonce) == NONCE_BYTES
    assert sealed.key_version == 1
    assert KEYS.api_secret.encode() not in sealed.ciphertext
    assert KEYS.api_key.encode() not in sealed.ciphertext
    assert "crypto-secret" not in repr(sealed)


def test_every_seal_uses_a_fresh_nonce() -> None:
    cipher = AesGcmCredentialCipher(Keyring.single(MASTER))

    nonces = {cipher.seal(KEYS, ALICE).nonce for _ in range(50)}

    assert len(nonces) == 50


@pytest.mark.req("FR-31")
@pytest.mark.parametrize(
    "other",
    [
        replace(ALICE, user_id="u-bob"),  # copied into another user's row
        replace(ALICE, credential_id="c-2"),  # into another credential of the same user
        replace(ALICE, exchange="other"),
        replace(ALICE, environment=Environment.MAINNET),  # D-12: a test key never opens as mainnet
    ],
)
def test_a_ciphertext_does_not_open_under_another_binding(other: CredentialBinding) -> None:
    cipher = AesGcmCredentialCipher(Keyring.single(MASTER))
    sealed = cipher.seal(KEYS, ALICE)

    with pytest.raises(CredentialUnreadable) as err:
        cipher.open(sealed, other)

    assert err.value.__cause__ is None  # nothing about the plaintext in a traceback
    assert err.value.__context__ is None


def test_tampering_or_another_master_key_is_detected() -> None:
    cipher = AesGcmCredentialCipher(Keyring.single(MASTER))
    sealed = cipher.seal(KEYS, ALICE)
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]

    with pytest.raises(CredentialUnreadable):
        cipher.open(replace(sealed, ciphertext=flipped), ALICE)
    with pytest.raises(CredentialUnreadable):
        AesGcmCredentialCipher(Keyring.single(bytes(32))).open(sealed, ALICE)
    with pytest.raises(CredentialUnreadable):
        cipher.open(replace(sealed, key_version=7), ALICE)


def test_aad_parts_are_length_prefixed_so_bindings_cannot_collide() -> None:
    a = CredentialBinding(credential_id="ab", user_id="c", exchange="bybit", environment=Environment.TESTNET)
    b = CredentialBinding(credential_id="a", user_id="bc", exchange="bybit", environment=Environment.TESTNET)

    assert credential_aad(a) != credential_aad(b)
    assert credential_aad(ALICE) == (
        b"\x00\x00\x00\x03c-1\x00\x00\x00\x07u-alice\x00\x00\x00\x05bybit\x00\x00\x00\x07testnet"
    )


def test_a_tampered_plaintext_error_is_not_linked_to_the_raised_exception() -> None:
    cipher = AesGcmCredentialCipher(Keyring.single(MASTER))
    sealed = cipher.seal(KEYS, ALICE)

    with pytest.raises(CredentialUnreadable) as err:
        cipher.open(replace(sealed, nonce=b"\x00" * NONCE_BYTES), ALICE)

    assert (err.value.__cause__, err.value.__context__) == (None, None)
