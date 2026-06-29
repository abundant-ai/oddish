from __future__ import annotations

import pytest

from oddish.worker.probe_creds import (
    PROBE_KEY_TTL_MINUTES,
    ProbeCredsError,
    delete_probe_key,
    mint_probe_creds,
    resolve_probe_api_base_url,
)


def test_base_url_resolves_from_env(monkeypatch):
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")
    assert resolve_probe_api_base_url() == "https://api.example"


def test_base_url_missing_raises(monkeypatch):
    monkeypatch.delenv("ODDISH_PUBLIC_API_BASE_URL", raising=False)
    monkeypatch.setattr("oddish.worker.probe_creds._modal_fallback", lambda: "")
    with pytest.raises(ProbeCredsError):
        resolve_probe_api_base_url()


def test_base_url_falls_back_to_modal(monkeypatch):
    monkeypatch.delenv("ODDISH_PUBLIC_API_BASE_URL", raising=False)
    monkeypatch.setattr(
        "oddish.worker.probe_creds._modal_fallback", lambda: "https://modal.example"
    )
    assert resolve_probe_api_base_url() == "https://modal.example"


def test_modal_fallback_swallows_errors(monkeypatch):
    def _boom(*_a, **_k):
        raise RuntimeError("no modal app")

    monkeypatch.setattr(
        "oddish.worker.probe_creds.api_base_url_for_modal_app", _boom
    )
    from oddish.worker.probe_creds import _modal_fallback

    assert _modal_fallback() == ""


@pytest.mark.asyncio
async def test_mint_probe_creds_requires_org_id(monkeypatch):
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")
    with pytest.raises(ProbeCredsError):
        await mint_probe_creds(org_id=None, trial_id="t1")


@pytest.mark.asyncio
async def test_mint_probe_creds_without_provider_raises(monkeypatch):
    """With no cloud provider registered (OSS worker), minting must fail clearly."""
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")
    monkeypatch.setattr("oddish.worker.probe_creds._mint_read_key", None)
    with pytest.raises(ProbeCredsError) as exc:
        await mint_probe_creds(org_id="org-1", trial_id="t1")
    assert "provider" in str(exc.value)


@pytest.mark.asyncio
async def test_mint_probe_creds_wraps_mint_failure(monkeypatch):
    """A failure minting the read key must surface as ProbeCredsError, not leak."""
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")

    async def _boom(*, org_id, name, ttl_minutes):
        raise RuntimeError("db down")

    monkeypatch.setattr("oddish.worker.probe_creds._mint_read_key", _boom)

    with pytest.raises(ProbeCredsError) as exc:
        await mint_probe_creds(org_id="org-1", trial_id="t1")
    assert "db down" in str(exc.value)


@pytest.mark.asyncio
async def test_mint_probe_creds_returns_env(monkeypatch):
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")

    async def _mint(*, org_id, name, ttl_minutes):
        assert org_id == "org-1"
        assert name == "probe:t1"
        assert ttl_minutes == PROBE_KEY_TTL_MINUTES
        return ("key-id-123", "ok_rawsecret")

    monkeypatch.setattr("oddish.worker.probe_creds._mint_read_key", _mint)

    key_id, env = await mint_probe_creds(org_id="org-1", trial_id="t1")
    assert key_id == "key-id-123"
    assert env == {
        "ODDISH_API_KEY": "ok_rawsecret",
        "ODDISH_API_BASE_URL": "https://api.example",
    }


@pytest.mark.asyncio
async def test_delete_probe_key_noop_without_provider(monkeypatch):
    monkeypatch.setattr("oddish.worker.probe_creds._delete_key", None)
    # Must not raise when no cloud provider is registered.
    await delete_probe_key("key-id-123")


@pytest.mark.asyncio
async def test_delete_probe_key_calls_provider(monkeypatch):
    seen = {}

    async def _delete(api_key_id):
        seen["id"] = api_key_id

    monkeypatch.setattr("oddish.worker.probe_creds._delete_key", _delete)
    await delete_probe_key("key-id-123")
    assert seen == {"id": "key-id-123"}
