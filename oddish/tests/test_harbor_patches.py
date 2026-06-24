from __future__ import annotations

import base64
import json
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from oddish.registry_auth import (  # noqa: E402
    DOCKER_HUB_AUTH_KEY,
    RegistryCredential,
    current_registry_credentials,
)
from oddish.workers.harbor import patches as harbor_patches  # noqa: E402


class _FakeStrategy:
    def __init__(
        self, *, stdout="", stderr="", return_code=0, raises: Exception | None = None
    ):
        self._stdout = stdout
        self._stderr = stderr
        self._return_code = return_code
        self._raises = raises
        self.calls: list[tuple[str, dict | None]] = []

    async def _vm_exec(self, command, *, env=None, timeout_sec=None):
        self.calls.append((command, env))
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(
            stdout=self._stdout, stderr=self._stderr, return_code=self._return_code
        )


async def _ok(_self):
    return None


def _boom(message):
    async def _raise(_self):
        raise RuntimeError(message)

    return _raise


@pytest.fixture
def creds():
    cred = RegistryCredential("alice", "secrettoken", "docker.io")
    token = current_registry_credentials.set([cred])
    try:
        yield [cred]
    finally:
        current_registry_credentials.reset(token)


# --- dockerd diagnostics + login seam ----------------------------------------


@pytest.mark.asyncio
async def test_wrapper_passes_through_on_success():
    strategy = _FakeStrategy()
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)

    assert await wrapped(strategy) is None
    assert strategy.calls == []  # no diagnostics, and no creds so no login write


@pytest.mark.asyncio
async def test_wrapper_folds_dockerd_log_into_error():
    original = "Docker daemon not ready after 60s. Last output: Cannot connect"
    strategy = _FakeStrategy(stdout="overlay2: driver failed: not supported")
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(
        _boom(original), with_diagnostics=True
    )

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped(strategy)

    message = str(excinfo.value)
    assert original in message  # original cause preserved
    assert "overlay2: driver failed" in message  # dockerd log captured
    assert len(strategy.calls) == 1


@pytest.mark.asyncio
async def test_wrapper_without_diagnostics_reraises_bare():
    original = "Docker daemon not ready after 60s. Last output: Cannot connect"
    strategy = _FakeStrategy(stdout="should not be gathered")
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(
        _boom(original), with_diagnostics=False
    )

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped(strategy)

    assert str(excinfo.value) == original  # no diagnostics appended
    assert strategy.calls == []


@pytest.mark.asyncio
async def test_wrapper_never_masks_original_when_diagnostics_fail():
    original = "Docker daemon not ready after 60s. Last output: Cannot connect"
    strategy = _FakeStrategy(raises=RuntimeError("sandbox gone"))
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(
        _boom(original), with_diagnostics=True
    )

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


# --- registry login / scrub --------------------------------------------------


@pytest.mark.asyncio
async def test_login_writes_authenticated_config(creds):
    strategy = _FakeStrategy()
    await harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)(
        strategy
    )

    assert len(strategy.calls) == 1  # daemon-ready seam wrote the config
    command, env = strategy.calls[0]
    assert harbor_patches._DOCKER_CONFIG_PATH in command
    assert "secrettoken" not in command  # raw token rides the env, never argv
    cfg = json.loads(base64.b64decode(env[harbor_patches._DOCKER_CONFIG_ENV]))
    assert (
        cfg["auths"][DOCKER_HUB_AUTH_KEY]["auth"]
        == base64.b64encode(b"alice:secrettoken").decode()
    )


@pytest.mark.asyncio
async def test_login_is_noop_without_credentials():
    strategy = _FakeStrategy()
    await harbor_patches._perform_registry_login(strategy)
    assert strategy.calls == []  # no creds on the context var → nothing written


@pytest.mark.asyncio
async def test_login_never_raises_into_daemon_wait(creds):
    strategy = _FakeStrategy(raises=RuntimeError("exec failed"))
    # A failing write must not break the (successful) daemon-ready path.
    assert (
        await harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)(
            strategy
        )
        is None
    )


@pytest.mark.asyncio
async def test_login_exception_log_redacts_token_and_env(creds, caplog):
    class _LeakyStrategy(_FakeStrategy):
        async def _vm_exec(self, command, *, env=None, timeout_sec=None):
            self.calls.append((command, env))
            raise RuntimeError(
                f"exec failed: secrettoken {env[harbor_patches._DOCKER_CONFIG_ENV]}"
            )

    strategy = _LeakyStrategy()
    with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
        await harbor_patches._perform_registry_login(strategy)

    leaked_env = strategy.calls[0][1][harbor_patches._DOCKER_CONFIG_ENV]
    assert "secrettoken" not in caplog.text
    assert leaked_env not in caplog.text
    assert "***" in caplog.text


@pytest.mark.asyncio
async def test_scrub_removes_config_then_calls_original(creds):
    strategy = _FakeStrategy()
    stopped = []

    async def _orig_stop(_self, delete):
        # The config must already be scrubbed by the time teardown runs.
        assert strategy.calls[0][0] == harbor_patches._SCRUB_DOCKER_CONFIG_CMD
        stopped.append(delete)

    await harbor_patches._wrap_stop(_orig_stop)(strategy, delete=True)
    assert stopped == [True]


@pytest.mark.asyncio
async def test_scrub_is_noop_without_credentials():
    strategy = _FakeStrategy()
    ran = []

    async def _orig_stop(_self, delete):
        ran.append(delete)

    await harbor_patches._wrap_stop(_orig_stop)(strategy, delete=False)
    assert strategy.calls == []  # nothing to scrub
    assert ran == [False]  # original teardown still runs


# --- install / idempotency ---------------------------------------------------


def test_apply_harbor_patches_is_idempotent(monkeypatch):
    calls = {"n": 0}
    monkeypatch.setattr(harbor_patches, "_PATCHED", False)
    monkeypatch.setattr(
        harbor_patches,
        "_patch_dind_strategies",
        lambda: calls.__setitem__("n", calls["n"] + 1),
    )

    harbor_patches.apply_harbor_patches()
    harbor_patches.apply_harbor_patches()

    assert calls["n"] == 1
