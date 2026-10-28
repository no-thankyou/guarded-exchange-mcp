"""Keyring with HKDF sub-keys and AES-256-GCM encryption of exchange keys (PLAN ADR-009, D-5, D-14).

- One master key (32 random bytes, ``master_key.py``) per installation; every purpose gets its own
  sub-key via HKDF-SHA256 (``KeyPurpose``), so the journal MAC key, the ``requestState`` key and
  the credential key are independent: leaking or misusing one says nothing about the others.
- Exchange keys are encrypted with AES-256-GCM, a random 96-bit nonce per encryption and
  AAD = ``credential_id || user_id || exchange || environment`` (length-prefixed, so no two different bindings
  encode alike). A ciphertext copied into another user's row fails authentication.
- The keyring is versioned (``{version: master key}``) for a future ``rotate-keys``; today it
  holds one version.
"""

import json
import os
from collections.abc import Mapping
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from guarded_exchange.domain.credentials import (
    ApiKeyPair,
    CredentialBinding,
    CredentialUnreadable,
    SealedSecret,
)
from guarded_exchange.domain.errors import InvalidParams

MASTER_KEY_BYTES: Final = 32
SUBKEY_BYTES: Final = 32  # AES-256
NONCE_BYTES: Final = 12  # 96-bit GCM nonce (NIST SP 800-38D recommended size)
_HKDF_INFO_PREFIX: Final = b"guarded-exchange/v1/"


class KeyPurpose(StrEnum):
    """HKDF ``info`` labels (D-14). Never reuse a label for a different use."""

    CREDENTIALS = "credentials"
    AUDIT = "audit"
    REQUEST_STATE = "request_state"
    TELEGRAM = "telegram"


class Keyring:
    """Master keys by version and the sub-keys derived from them. Never printed."""

    def __init__(self, master_keys: Mapping[int, bytes], *, current_version: int) -> None:
        if current_version not in master_keys:
            raise ValueError(f"no master key for current version {current_version}")
        for version, key in master_keys.items():
            if len(key) != MASTER_KEY_BYTES:
                raise ValueError(f"master key version {version} must be {MASTER_KEY_BYTES} bytes")
        self._master_keys = MappingProxyType(dict(master_keys))
        self._current_version = current_version

    @classmethod
    def single(cls, master_key: bytes, version: int = 1) -> "Keyring":
        return cls({version: master_key}, current_version=version)

    @property
    def current_version(self) -> int:
        return self._current_version

    def subkey(self, purpose: KeyPurpose, version: int | None = None) -> bytes:
        """HKDF-SHA256(master key of ``version``, info = prefix + purpose), 32 bytes."""
        master = self._master_keys.get(self._current_version if version is None else version)
        if master is None:
            raise KeyError(f"no master key version {version}")
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=SUBKEY_BYTES,
            salt=None,
            info=_HKDF_INFO_PREFIX + purpose.encode(),
        )
        return hkdf.derive(master)

    def __repr__(self) -> str:
        return f"Keyring(versions={sorted(self._master_keys)}, current={self._current_version})"


def credential_aad(binding: CredentialBinding) -> bytes:
    """``credential_id || user_id || exchange || environment``, each prefixed with its 4-byte length."""
    parts = (binding.credential_id, binding.user_id, binding.exchange, binding.environment.value)
    return b"".join(len(raw).to_bytes(4, "big") + raw for raw in (part.encode() for part in parts))


class AesGcmCredentialCipher:
    """``CredentialCipher`` with the ``credentials`` sub-key of the keyring."""

    def __init__(self, keyring: Keyring) -> None:
        self._keyring = keyring

    def seal(self, keys: ApiKeyPair, binding: CredentialBinding) -> SealedSecret:
        version = self._keyring.current_version
        nonce = os.urandom(NONCE_BYTES)
        plaintext = json.dumps({"api_key": keys.api_key, "api_secret": keys.api_secret}).encode()
        ciphertext = self._aead(version).encrypt(nonce, plaintext, credential_aad(binding))
        return SealedSecret(ciphertext=ciphertext, nonce=nonce, key_version=version)

    def open(self, sealed: SealedSecret, binding: CredentialBinding) -> ApiKeyPair:
        keys = self._try_open(sealed, binding)
        if keys is None:
            # Raised outside any ``except``: neither ``__cause__`` nor ``__context__`` links to the
            # decryption or parsing error, so nothing about the plaintext can reach a traceback.
            raise CredentialUnreadable()
        return keys

    def _try_open(self, sealed: SealedSecret, binding: CredentialBinding) -> ApiKeyPair | None:
        try:
            aead = self._aead(sealed.key_version)
            plaintext = aead.decrypt(sealed.nonce, sealed.ciphertext, credential_aad(binding))
            data = json.loads(plaintext)
            return ApiKeyPair(api_key=data["api_key"], api_secret=data["api_secret"])
        except (InvalidTag, KeyError, ValueError, TypeError, InvalidParams):
            return None

    def _aead(self, version: int) -> AESGCM:
        return AESGCM(self._keyring.subkey(KeyPurpose.CREDENTIALS, version))
