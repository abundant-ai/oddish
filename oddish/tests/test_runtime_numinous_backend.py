from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.runtime.backends.numinous import NuminousBackend


def test_capabilities_shape() -> None:
    caps = NuminousBackend().capabilities()
    assert caps.gpu is None  # GPU lane ships behind a separate flag
    assert caps.private_registry_pull is True
    assert caps.network_egress == "configurable"
    assert caps.streaming_logs is True
    assert caps.memory_snapshot_fork is True


def test_harbor_env_kwargs_passthrough_preserves_caller_kwargs() -> None:
    base = {"labels": {"a": "b"}, "cpus": 4}
    out = NuminousBackend().harbor_env_kwargs(base)
    assert out == base
    assert out is not base  # never mutate the caller's dict


@pytest.mark.asyncio
async def test_teardown_missing_id_is_false() -> None:
    assert await NuminousBackend().teardown("") is False


@pytest.mark.asyncio
async def test_teardown_404_counts_as_gone(monkeypatch) -> None:
    """TTL-expired sandboxes are already destroyed provider-side; a 404 on
    teardown is the goal state, not an error (mirrors the Daytona NotFound
    convention in backends/daytona.py)."""
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = transport
        return orig_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    assert await NuminousBackend().teardown("sbx_gone") is True


@pytest.mark.asyncio
async def test_teardown_verifies_proof(monkeypatch) -> None:
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/v1/sandboxes/sbx_1"
        return httpx.Response(
            200, json={"id": "sbx_1", "state": "terminated",
                       "teardown_proof": {"verified_absent": True}}
        )

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = transport
        return orig_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    assert await NuminousBackend().teardown("sbx_1") is True


@pytest.mark.asyncio
async def test_teardown_200_without_proof_is_success(monkeypatch) -> None:
    """A 2xx terminate is success even if verified_absent is false/absent —
    the shared contract only asks whether teardown was issued/gone, and
    orphan cleanup must not treat a real termination as a failure."""
    import httpx

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"id": "sbx_2", "state": "terminated",
                       "teardown_proof": {"verified_absent": False}}
        )

    transport = httpx.MockTransport(handler)
    orig_client = httpx.AsyncClient

    def client_factory(**kwargs):
        kwargs["transport"] = transport
        return orig_client(**kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client_factory)
    assert await NuminousBackend().teardown("sbx_2") is True


def test_capture_diagnostics_is_noop(tmp_path) -> None:
    with NuminousBackend().capture_diagnostics(tmp_path) as log:
        assert log is None


def test_registry_excludes_numinous_by_default() -> None:
    from oddish.runtime.registry import REGISTERED_BACKENDS

    assert "numinous" not in REGISTERED_BACKENDS
