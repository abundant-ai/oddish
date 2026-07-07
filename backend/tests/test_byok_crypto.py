import base64
import os

import pytest


@pytest.fixture()
def enc_key(monkeypatch):
    key = base64.b64encode(os.urandom(32)).decode()
    monkeypatch.setenv("ODDISH_CRED_ENC_KEY", key)
    import crypto

    crypto.reset_key_cache()
    yield key
    crypto.reset_key_cache()


def test_round_trip(enc_key):
    import crypto

    blob, version = crypto.encrypt_secret("sk-ant-secret")
    assert version == 1 and blob != b"sk-ant-secret"
    assert crypto.decrypt_secret(blob, version) == "sk-ant-secret"


def test_tamper_raises(enc_key):
    import crypto

    blob, version = crypto.encrypt_secret("sk-ant-secret")
    tampered = blob[:-1] + bytes([blob[-1] ^ 0xFF])
    with pytest.raises(crypto.CredentialDecryptError):
        crypto.decrypt_secret(tampered, version)


def test_unknown_key_version_raises(enc_key):
    import crypto

    blob, _ = crypto.encrypt_secret("x")
    with pytest.raises(crypto.CredentialDecryptError):
        crypto.decrypt_secret(blob, 99)


def test_truncated_blob_raises(enc_key):
    import crypto

    with pytest.raises(crypto.CredentialDecryptError):
        crypto.decrypt_secret(b"short", 1)


def test_missing_master_key(monkeypatch):
    monkeypatch.delenv("ODDISH_CRED_ENC_KEY", raising=False)
    import crypto

    crypto.reset_key_cache()
    with pytest.raises(crypto.CredentialKeyMissingError):
        crypto.encrypt_secret("x")
