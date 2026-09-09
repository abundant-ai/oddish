"""Cold containers fetch the Clerk JWKS at startup and survive one dropped connection."""

from __future__ import annotations

import httpx
import pytest

import auth.verification as verification


@pytest.fixture(autouse=True)
def _cold_jwks(monkeypatch):
    monkeypatch.setattr(verification, "_jwks_cache", None)
    monkeypatch.setattr(verification, "_jwks_cache_time", 0.0)
    monkeypatch.setattr(verification, "CLERK_DOMAIN", "clerk.example.test")


_REAL_CLIENT = verification.RequestTimedAsyncClient


def _client_factory(responder):
    class _Client(_REAL_CLIENT):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(responder)
            super().__init__(*args, **kwargs)

    return _Client


@pytest.mark.asyncio
async def test_one_transport_failure_is_retried(monkeypatch):
    attempts: list[str] = []

    def responder(request: httpx.Request) -> httpx.Response:
        attempts.append(request.url.path)
        if len(attempts) == 1:
            raise httpx.ConnectError("All connection attempts failed", request=request)
        return httpx.Response(200, json={"keys": [{"kid": "k1"}]})

    monkeypatch.setattr(
        verification, "RequestTimedAsyncClient", _client_factory(responder)
    )
    monkeypatch.setattr(verification.asyncio, "sleep", _no_sleep)

    jwks = await verification.get_clerk_jwks()

    assert jwks == {"keys": [{"kid": "k1"}]}
    assert attempts == ["/.well-known/jwks.json"] * 2


@pytest.mark.asyncio
async def test_two_transport_failures_still_surface(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("All connection attempts failed", request=request)

    monkeypatch.setattr(
        verification, "RequestTimedAsyncClient", _client_factory(responder)
    )
    monkeypatch.setattr(verification.asyncio, "sleep", _no_sleep)

    with pytest.raises(httpx.ConnectError):
        await verification.get_clerk_jwks()


@pytest.mark.asyncio
async def test_warm_up_fills_the_cache_and_never_raises(monkeypatch):
    def responder(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"keys": []})

    monkeypatch.setattr(
        verification, "RequestTimedAsyncClient", _client_factory(responder)
    )
    assert await verification.warm_clerk_jwks() is True
    assert verification._jwks_cache == {"keys": []}

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    monkeypatch.setattr(verification, "_jwks_cache", None)
    monkeypatch.setattr(
        verification, "RequestTimedAsyncClient", _client_factory(broken)
    )
    monkeypatch.setattr(verification.asyncio, "sleep", _no_sleep)
    assert await verification.warm_clerk_jwks() is False

    monkeypatch.setattr(verification, "CLERK_DOMAIN", "")
    assert await verification.warm_clerk_jwks() is False


async def _no_sleep(_seconds: float) -> None:
    return None
