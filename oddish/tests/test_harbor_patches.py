from __future__ import annotations

import importlib
import logging
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

pytest = importlib.import_module("pytest")
_registry_auth = importlib.import_module("oddish.registry_auth")
harbor_patches = importlib.import_module("oddish.workers.harbor.patches")

RegistryCredential = _registry_auth.RegistryCredential
current_registry_credentials = _registry_auth.current_registry_credentials


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


@pytest.mark.asyncio
async def test_wrapper_passes_through_on_success():
    strategy = _FakeStrategy()
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)

    assert await wrapped(strategy) is None
    assert strategy.calls == []


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

    assert str(excinfo.value) == original
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


@pytest.mark.asyncio
async def test_login_uses_docker_login_without_putting_token_in_command(creds):
    strategy = _FakeStrategy()
    await harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)(
        strategy
    )

    assert len(strategy.calls) == 1
    command, env = strategy.calls[0]
    assert "docker login" in command
    assert "secrettoken" not in command
    assert "daemon.json" not in command
    assert harbor_patches._DOCKER_CONFIG_BACKUP_PATH in command
    assert env["ODDISH_REGISTRY_LOGIN_REGISTRY_0"] == "docker.io"
    assert env["ODDISH_REGISTRY_LOGIN_USERNAME_0"] == "alice"
    assert env["ODDISH_REGISTRY_LOGIN_TOKEN_0"] == "secrettoken"


@pytest.mark.asyncio
async def test_login_is_noop_without_credentials():
    strategy = _FakeStrategy()
    await harbor_patches._perform_registry_login(strategy)
    assert strategy.calls == []


@pytest.mark.asyncio
async def test_login_failure_raises_into_daemon_wait(creds):
    strategy = _FakeStrategy(raises=RuntimeError("exec failed"))
    with pytest.raises(RuntimeError, match="Registry login failed"):
        await harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)(
            strategy
        )


@pytest.mark.asyncio
async def test_login_failure_still_restores_previous_config(creds):
    strategy = _FakeStrategy(raises=RuntimeError("exec failed"))
    with pytest.raises(RuntimeError, match="Registry login failed"):
        await harbor_patches._perform_registry_login(strategy)
    strategy._raises = None
    strategy.calls.clear()

    await harbor_patches._wrap_stop(lambda _self, delete: _ok(_self))(
        strategy, delete=True
    )

    assert len(strategy.calls) == 1
    assert harbor_patches._RESTORE_DOCKER_CONFIG_CMD == strategy.calls[0][0]


@pytest.mark.asyncio
async def test_login_exception_log_redacts_token_and_env(creds, caplog):
    class _LeakyStrategy(_FakeStrategy):
        async def _vm_exec(self, command, *, env=None, timeout_sec=None):
            self.calls.append((command, env))
            raise RuntimeError(
                "exec failed: secrettoken "
                f"{env['ODDISH_REGISTRY_LOGIN_TOKEN_0']}"
            )

    strategy = _LeakyStrategy()
    with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
        with pytest.raises(RuntimeError):
            await harbor_patches._perform_registry_login(strategy)

    leaked_env = strategy.calls[0][1]["ODDISH_REGISTRY_LOGIN_TOKEN_0"]
    assert "secrettoken" not in caplog.text
    assert leaked_env not in caplog.text
    assert "***" in caplog.text


@pytest.mark.asyncio
async def test_login_nonzero_log_redacts_token(creds, caplog):
    strategy = _FakeStrategy(stdout="bad secrettoken", return_code=1)

    with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
        with pytest.raises(RuntimeError):
            await harbor_patches._perform_registry_login(strategy)

    assert "secrettoken" not in caplog.text
    assert "***" in caplog.text


@pytest.mark.asyncio
async def test_scrub_restores_config_then_calls_original(creds):
    strategy = _FakeStrategy()
    await harbor_patches._perform_registry_login(strategy)
    strategy.calls.clear()
    stopped = []

    async def _orig_stop(_self, delete):
        assert strategy.calls[0][0] == harbor_patches._RESTORE_DOCKER_CONFIG_CMD
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
    assert strategy.calls == []
    assert ran == [False]


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
