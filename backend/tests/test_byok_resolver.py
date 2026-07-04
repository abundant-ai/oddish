import base64
import os
import types

import pytest


@pytest.fixture(autouse=True)
def _enc_key(monkeypatch):
    monkeypatch.setenv("ODDISH_CRED_ENC_KEY", base64.b64encode(os.urandom(32)).decode())
    import crypto

    crypto.reset_key_cache()
    yield
    crypto.reset_key_cache()


@pytest.fixture()
def resolver_mod():
    from worker import byok_resolver as br

    return br


def _row(key="sk-user"):
    import crypto

    blob, ver = crypto.encrypt_secret(key)
    return types.SimpleNamespace(ciphertext=blob, key_version=ver, key_hint=key[-4:])


async def _resolve(mod, **overrides):
    kwargs = dict(
        owner_user_id="u1",
        org_id=None,
        experiment_name=None,
        model="claude-opus-4-8",
        agent="claude-code",
    )
    kwargs.update(overrides)
    return await mod.resolve_byok_for_trial(**kwargs)


@pytest.mark.asyncio
async def test_no_owner_returns_none(resolver_mod, monkeypatch):
    called = []
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: called.append(1) or True)
    assert await _resolve(resolver_mod, owner_user_id=None) is None
    assert called == []  # gate never consulted without an owner


@pytest.mark.asyncio
async def test_non_anthropic_trial_returns_none(resolver_mod, monkeypatch):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)
    assert await _resolve(resolver_mod, agent="codex", model="openai/gpt-5.2") is None


@pytest.mark.asyncio
async def test_gate_off_returns_none(resolver_mod, monkeypatch):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: False)
    assert await _resolve(resolver_mod) is None


@pytest.mark.asyncio
async def test_active_key_injected(resolver_mod, monkeypatch):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        assert user_id == "u1"
        return _row()

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    res = await _resolve(resolver_mod)
    assert res is not None and res.env == {"ANTHROPIC_API_KEY": "sk-user"}


@pytest.mark.asyncio
async def test_no_key_row_returns_none(resolver_mod, monkeypatch):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        return None

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    assert await _resolve(resolver_mod) is None


@pytest.mark.asyncio
async def test_decrypt_failure_fails_open(resolver_mod, monkeypatch):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        row = _row()
        row.ciphertext = b"garbage-not-decryptable"
        return row

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    # A corrupt key must not fail the trial -- it falls back to the platform key.
    assert await _resolve(resolver_mod) is None


@pytest.mark.asyncio
async def test_gate_receives_full_context(resolver_mod, monkeypatch):
    received = {}
    monkeypatch.setattr(
        resolver_mod, "_gate_passes", lambda **k: received.update(k) or False
    )
    await _resolve(resolver_mod, org_id="o9", experiment_name="exp1")
    assert received == {
        "user_id": "u1",
        "org_id": "o9",
        "experiment_name": "exp1",
        "model": "claude-opus-4-8",
        "agent": "claude-code",
    }
