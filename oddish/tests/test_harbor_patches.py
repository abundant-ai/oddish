from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from oddish.workers import harbor_patches  # noqa: E402


class _FakeStrategy:
    def __init__(self, *, stdout="", stderr="", raises: Exception | None = None):
        self._stdout = stdout
        self._stderr = stderr
        self._raises = raises
        self.calls: list[str] = []

    async def _vm_exec(self, command, timeout_sec=None):
        self.calls.append(command)
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(
            stdout=self._stdout, stderr=self._stderr, return_code=0
        )


async def _ok(_self):
    return None


def _boom(message):
    async def _raise(_self):
        raise RuntimeError(message)

    return _raise


@pytest.mark.asyncio
async def test_wrapper_passes_through_on_success():
    strategy = _FakeStrategy()
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(_ok)

    assert await wrapped(strategy) is None
    assert strategy.calls == []  # no diagnostics gathered on success


@pytest.mark.asyncio
async def test_wrapper_folds_dockerd_log_into_error():
    original = "Docker daemon not ready after 60s. Last output: Cannot connect"
    strategy = _FakeStrategy(stdout="overlay2: driver failed: not supported")
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(_boom(original))

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped(strategy)

    message = str(excinfo.value)
    assert original in message  # original cause preserved
    assert "overlay2: driver failed" in message  # dockerd log captured
    assert len(strategy.calls) == 1


@pytest.mark.asyncio
async def test_wrapper_never_masks_original_when_diagnostics_fail():
    original = "Docker daemon not ready after 60s. Last output: Cannot connect"
    strategy = _FakeStrategy(raises=RuntimeError("sandbox gone"))
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(_boom(original))

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped(strategy)

    message = str(excinfo.value)
    assert original in message
    assert "diagnostics unavailable" in message


@pytest.mark.asyncio
async def test_collect_diagnostics_never_raises():
    strategy = _FakeStrategy(raises=ValueError("boom"))
    out = await harbor_patches._collect_dockerd_diagnostics(strategy)
    assert "diagnostics unavailable" in out


def test_apply_harbor_patches_is_idempotent(monkeypatch):
    calls = {"n": 0}

    def _fake_patch():
        calls["n"] += 1

    monkeypatch.setattr(harbor_patches, "_PATCHED", False)
    monkeypatch.setattr(
        harbor_patches, "_patch_dind_docker_daemon_diagnostics", _fake_patch
    )

    harbor_patches.apply_harbor_patches()
    harbor_patches.apply_harbor_patches()

    assert calls["n"] == 1
