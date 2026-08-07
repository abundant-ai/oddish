from __future__ import annotations

import pytest

import endpoints
import worker.functions as worker_functions


@pytest.mark.asyncio
async def test_control_function_runs_one_ec2_teardown(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []
    lease_calls: list[tuple[str, bool | None]] = []
    import oddish.runtime.registry as runtime_registry
    from oddish.core.helpers import (
        register_provider_teardown_delegate,
        unregister_provider_teardown_delegate,
    )

    class Backend:
        async def teardown(self, external_id: str) -> bool:
            calls.append(("ec2", external_id))
            return True

        def acquire_worker_credentials(self, *, include_ssh: bool) -> None:
            lease_calls.append(("acquire", include_ssh))

        def release_worker_credentials(self) -> None:
            lease_calls.append(("release", None))

    backend = Backend()
    monkeypatch.setattr(runtime_registry, "get_backend", lambda _provider: backend)
    monkeypatch.setitem(runtime_registry.REGISTERED_BACKENDS, "ec2", backend)

    unregister_provider_teardown_delegate("ec2")
    try:
        result = await worker_functions.teardown_ec2_sandbox.get_raw_f()(
            "ec2://account/region/instance"
        )
    finally:
        register_provider_teardown_delegate("ec2", endpoints._teardown_ec2_sandbox)

    assert result is True
    assert calls == [("ec2", "ec2://account/region/instance")]
    assert lease_calls == [("acquire", False), ("release", None)]


@pytest.mark.asyncio
async def test_api_delegate_invokes_remote_control_function_once(monkeypatch) -> None:
    calls: list[tuple[str, tuple, dict]] = []

    async def remote_aio(external_id: str) -> bool:
        calls.append(("remote", (external_id,), {}))
        return True

    class FakeFunction:
        remote = type("Remote", (), {"aio": staticmethod(remote_aio)})()

    def from_name(*args, **kwargs):
        calls.append(("from_name", args, kwargs))
        return FakeFunction()

    monkeypatch.setattr(endpoints.modal.Function, "from_name", from_name)

    result = await endpoints._teardown_ec2_sandbox("ec2://owned")

    assert result is True
    assert calls == [
        (
            "from_name",
            ("oddish", "teardown_ec2_sandbox"),
            {"environment_name": None},
        ),
        ("remote", ("ec2://owned",), {}),
    ]
