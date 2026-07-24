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


@pytest.mark.asyncio
async def test_experiment_owner_fallback_when_task_owner_has_no_key(
    resolver_mod, monkeypatch
):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        return _row("sk-exp-owner") if user_id == "exp-owner" else None

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    res = await _resolve(resolver_mod, experiment_owner_user_id="exp-owner")
    assert res is not None and res.env == {"ANTHROPIC_API_KEY": "sk-exp-owner"}


@pytest.mark.asyncio
async def test_task_owner_key_wins_over_experiment_owner(resolver_mod, monkeypatch):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        return _row(f"sk-{user_id}")

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    res = await _resolve(resolver_mod, experiment_owner_user_id="exp-owner")
    assert res is not None and res.env == {"ANTHROPIC_API_KEY": "sk-u1"}


@pytest.mark.asyncio
async def test_gate_evaluated_per_candidate(resolver_mod, monkeypatch):
    # Gate off for the task owner but on for the experiment owner: the
    # experiment owner's key is used, and the gate saw both candidates.
    gated = []
    monkeypatch.setattr(
        resolver_mod,
        "_gate_passes",
        lambda **k: gated.append(k["user_id"]) or k["user_id"] == "exp-owner",
    )

    async def fake_fetch(user_id):
        return _row(f"sk-{user_id}")

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    res = await _resolve(resolver_mod, experiment_owner_user_id="exp-owner")
    assert res is not None and res.env == {"ANTHROPIC_API_KEY": "sk-exp-owner"}
    assert gated == ["u1", "exp-owner"]


@pytest.mark.asyncio
async def test_decrypt_failure_falls_through_to_experiment_owner(
    resolver_mod, monkeypatch
):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        if user_id == "u1":
            row = _row()
            row.ciphertext = b"garbage-not-decryptable"
            return row
        return _row("sk-exp-owner")

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    res = await _resolve(resolver_mod, experiment_owner_user_id="exp-owner")
    assert res is not None and res.env == {"ANTHROPIC_API_KEY": "sk-exp-owner"}


@pytest.mark.asyncio
async def test_same_owner_and_experiment_owner_checked_once(resolver_mod, monkeypatch):
    gated = []
    monkeypatch.setattr(
        resolver_mod, "_gate_passes", lambda **k: gated.append(k["user_id"]) or False
    )
    assert await _resolve(resolver_mod, experiment_owner_user_id="u1") is None
    assert gated == ["u1"]  # deduped: one gate check, not two


@pytest.mark.asyncio
async def test_ownerless_trial_with_experiment_owner_uses_their_key(
    resolver_mod, monkeypatch
):
    monkeypatch.setattr(resolver_mod, "_gate_passes", lambda **k: True)

    async def fake_fetch(user_id):
        assert user_id == "exp-owner"
        return _row("sk-exp-owner")

    monkeypatch.setattr(resolver_mod, "_fetch_key_row", fake_fetch)
    res = await _resolve(
        resolver_mod, owner_user_id=None, experiment_owner_user_id="exp-owner"
    )
    assert res is not None and res.env == {"ANTHROPIC_API_KEY": "sk-exp-owner"}
