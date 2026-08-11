from __future__ import annotations

import pytest

import endpoints
import worker.functions as worker_functions


@pytest.mark.asyncio
async def test_control_function_runs_one_ec2_teardown(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    class Backend:
        async def teardown(self, external_id: str) -> bool:
            calls.append(("ec2", external_id))
            return True

    backend = Backend()
    monkeypatch.setattr(worker_functions, "get_backend", lambda _provider: backend)

    result = await worker_functions.teardown_ec2_sandbox.get_raw_f()(
        "ec2://account/region/instance"
    )

    assert result is True
    assert calls == [("ec2", "ec2://account/region/instance")]


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
