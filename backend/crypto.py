"""App-level AES-GCM for stored user credentials.

Master key: 32 bytes, base64, env ``ODDISH_CRED_ENC_KEY`` (oddish-prod Modal
secret). ``key_version`` selects from the key map so a second key can be added
later without a migration. Blob layout: ``nonce(12) || ciphertext+tag``.
"""

from __future__ import annotations

import base64
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_keys_cache: dict[int, bytes] | None = None


class CredentialKeyMissingError(RuntimeError):
    pass


class CredentialDecryptError(RuntimeError):
    pass


def reset_key_cache() -> None:
    global _keys_cache
    _keys_cache = None


def _keys() -> dict[int, bytes]:
    global _keys_cache
    if _keys_cache is None:
        raw = os.environ.get("ODDISH_CRED_ENC_KEY", "").strip()
        if not raw:
            raise CredentialKeyMissingError("ODDISH_CRED_ENC_KEY is not set")
        try:
            key = base64.b64decode(raw, validate=True)
        except Exception as exc:
            raise CredentialKeyMissingError(
                "ODDISH_CRED_ENC_KEY is not valid base64"
            ) from exc
        if len(key) != 32:
            raise CredentialKeyMissingError(
                "ODDISH_CRED_ENC_KEY must decode to exactly 32 bytes"
            )
        _keys_cache = {1: key}
    return _keys_cache


def encrypt_secret(plaintext: str) -> tuple[bytes, int]:
    """Encrypt; returns (blob, key_version) for storage."""
    version = max(_keys())
    nonce = os.urandom(_NONCE_LEN)
    ct = AESGCM(_keys()[version]).encrypt(nonce, plaintext.encode("utf-8"), None)
    return nonce + ct, version


def decrypt_secret(blob: bytes, key_version: int) -> str:
    key = _keys().get(key_version)
    if key is None:
        raise CredentialDecryptError(f"unknown key_version {key_version}")
    if len(blob) < _NONCE_LEN:
        raise CredentialDecryptError("ciphertext blob is truncated")
    try:
        pt = AESGCM(key).decrypt(
            bytes(blob[:_NONCE_LEN]), bytes(blob[_NONCE_LEN:]), None
        )
    except InvalidTag as exc:
        raise CredentialDecryptError("ciphertext failed authentication") from exc
    return pt.decode("utf-8")
