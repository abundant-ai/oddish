from __future__ import annotations

import pytest

from oddish.worker.probe_creds import (
    PROBE_KEY_TTL_MINUTES,
    ProbeCredsError,
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
async def test_mint_probe_creds_wraps_mint_failure(monkeypatch):
    """A failure minting the read key must surface as ProbeCredsError, not leak."""
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "oddish.worker.probe_creds.get_session", lambda: _FakeSession()
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(
        "oddish.worker.probe_creds.mint_internal_read_key", _boom
    )

    with pytest.raises(ProbeCredsError) as exc:
        await mint_probe_creds(org_id="org-1", trial_id="t1")
    assert "db down" in str(exc.value)


@pytest.mark.asyncio
async def test_mint_probe_creds_returns_env(monkeypatch):
    monkeypatch.setenv("ODDISH_PUBLIC_API_BASE_URL", "https://api.example")

    class _FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(
        "oddish.worker.probe_creds.get_session", lambda: _FakeSession()
    )

    async def _mint(session, *, org_id, name, ttl_minutes):
        assert org_id == "org-1"
        assert name == "probe:t1"
        assert ttl_minutes == PROBE_KEY_TTL_MINUTES
        return ("key-id-123", "ok_rawsecret")

    monkeypatch.setattr(
        "oddish.worker.probe_creds.mint_internal_read_key", _mint
    )

    key_id, env = await mint_probe_creds(org_id="org-1", trial_id="t1")
    assert key_id == "key-id-123"
    assert env == {
        "ODDISH_API_KEY": "ok_rawsecret",
        "ODDISH_API_BASE_URL": "https://api.example",
    }
