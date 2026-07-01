from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from oddish.host.adapters import (
    DockerHostAdapter,
    EnvSecretSource,
    ModalHostAdapter,
    UvicornHostAdapter,
    run_periodic,
)
from oddish.host.ports import HostAdapter, SecretSource
from oddish.host.registry import get_host_adapter


def test_adapter_names() -> None:
    assert UvicornHostAdapter().name == "uvicorn"
    assert DockerHostAdapter().name == "docker"
    assert ModalHostAdapter().name == "modal"


@pytest.mark.parametrize(
    "adapter", [UvicornHostAdapter(), DockerHostAdapter(), ModalHostAdapter()]
)
def test_adapters_satisfy_protocol(adapter: HostAdapter) -> None:
    assert isinstance(adapter, HostAdapter)
    assert isinstance(adapter.secret_source(), SecretSource)


def test_env_secret_source_reads_environment(monkeypatch) -> None:
    monkeypatch.setenv("ODDISH_TEST_SECRET", "shhh")
    source = EnvSecretSource()
    assert source.get("ODDISH_TEST_SECRET") == "shhh"
    assert source.get("ODDISH_DOES_NOT_EXIST") is None


def test_all_adapters_inject_secrets_as_env_at_runtime() -> None:
    # Modal injects its Secrets as env vars inside the container, so every host
    # reads runtime secrets the same way -- the deploy-time mapping differs.
    for adapter in (UvicornHostAdapter(), DockerHostAdapter(), ModalHostAdapter()):
        assert isinstance(adapter.secret_source(), EnvSecretSource)


def test_run_periodic_invokes_until_stopped() -> None:
    calls = {"n": 0}

    async def _fn() -> None:
        calls["n"] += 1

    def _stop() -> bool:
        return calls["n"] >= 3

    async def _sleep(_seconds: float) -> None:
        return None

    asyncio.run(run_periodic(_fn, period_s=0.0, _stop=_stop, _sleep=_sleep))
    assert calls["n"] == 3


def test_modal_adapter_serve_and_schedule_are_declarative() -> None:
    adapter = ModalHostAdapter()

    async def _noop() -> None:
        return None

    # Modal hosting/scheduling is declarative (decorators in backend/), so the
    # runtime methods point the caller at the manifest rather than running.
    with pytest.raises(NotImplementedError):
        adapter.serve_api(object())
    with pytest.raises(NotImplementedError):
        asyncio.run(adapter.schedule(_noop, period_s=10.0))


def test_uvicorn_schedule_runs_the_periodic_loop() -> None:
    calls = {"n": 0}

    async def _fn() -> None:
        calls["n"] += 1

    async def _sleep(_seconds: float) -> None:
        return None

    asyncio.run(
        UvicornHostAdapter().schedule(
            _fn, period_s=0.0, _stop=lambda: calls["n"] >= 2, _sleep=_sleep
        )
    )
    assert calls["n"] == 2


def test_registry_resolves_host_adapters() -> None:
    assert get_host_adapter("uvicorn").name == "uvicorn"
    assert get_host_adapter("docker").name == "docker"
    assert get_host_adapter("modal").name == "modal"
    assert get_host_adapter("nope") is None


def test_host_adapters_import_without_modal() -> None:
    src = str(Path(__file__).resolve().parents[1] / "src")
    code = (
        f"import sys; sys.path.insert(0, {src!r})\n"
        "import builtins\n"
        "_real = builtins.__import__\n"
        "def _guard(name, *a, **k):\n"
        "    if name == 'modal' or name.startswith('modal.'):\n"
        "        raise AssertionError('modal imported at module load: ' + name)\n"
        "    return _real(name, *a, **k)\n"
        "builtins.__import__ = _guard\n"
        "import oddish.host.adapters, oddish.host.registry, oddish.host.ports\n"
        "print('ok')\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"
