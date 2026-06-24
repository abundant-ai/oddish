from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest  # noqa: E402

from oddish.workers.harbor import patches as harbor_patches  # noqa: E402


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
    assert strategy.calls == []


@pytest.mark.asyncio
async def test_wrapper_folds_dockerd_log_into_error():
    original = "Docker daemon not ready after 60s. Last output: Cannot connect"
    strategy = _FakeStrategy(stdout="overlay2: driver failed: not supported")
    wrapped = harbor_patches._wrap_wait_for_docker_daemon(_boom(original))

    with pytest.raises(RuntimeError) as excinfo:
        await wrapped(strategy)

    message = str(excinfo.value)
    assert original in message
    assert "overlay2: driver failed" in message
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


def test_apply_harbor_patches_is_idempotent(monkeypatch):
    calls = {"diag": 0, "daytona_mirror": 0, "modal_mirror": 0}

    monkeypatch.setattr(harbor_patches, "_PATCHED", False)
    monkeypatch.setattr(
        harbor_patches,
        "_patch_dind_docker_daemon_diagnostics",
        lambda: calls.__setitem__("diag", calls["diag"] + 1),
    )
    monkeypatch.setattr(
        harbor_patches,
        "_patch_daytona_registry_mirror",
        lambda: calls.__setitem__("daytona_mirror", calls["daytona_mirror"] + 1),
    )
    monkeypatch.setattr(
        harbor_patches,
        "_patch_modal_registry_mirror",
        lambda: calls.__setitem__("modal_mirror", calls["modal_mirror"] + 1),
    )

    harbor_patches.apply_harbor_patches()
    harbor_patches.apply_harbor_patches()

    assert calls == {"diag": 1, "daytona_mirror": 1, "modal_mirror": 1}

class _CaptureStrategy:
    def __init__(self):
        self.calls: list[dict] = []

    async def _vm_exec(self, command, *args, **kwargs):
        self.calls.append({"command": command, "args": args, "kwargs": kwargs})
        return SimpleNamespace(stdout="", stderr="", return_code=0)


@pytest.mark.asyncio
async def test_daytona_vm_exec_prepends_daemon_json_for_dockerd():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    cmd = "dockerd-entrypoint.sh dockerd > /var/log/dockerd.log 2>&1 &"
    await wrapped(strategy, cmd, timeout_sec=10)

    sent = strategy.calls[0]["command"]
    assert sent.endswith(cmd)
    assert "/etc/docker/daemon.json" in sent
    assert '{"registry-mirrors": ["https://mirror.gcr.io"]}' in sent
    assert sent.index("daemon.json") < sent.index("dockerd-entrypoint.sh")
    assert strategy.calls[0]["kwargs"] == {"timeout_sec": 10}


@pytest.mark.asyncio
async def test_daytona_vm_exec_passes_other_commands_through():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    await wrapped(strategy, "docker info", "extra", timeout_sec=5)

    assert strategy.calls[0]["command"] == "docker info"
    assert strategy.calls[0]["args"] == ("extra",)
    assert strategy.calls[0]["kwargs"] == {"timeout_sec": 5}

class _ModalCaptureStrategy:
    def __init__(self, *, cat_stdout, info_stdout="", raises_on=None):
        self._cat_stdout = cat_stdout
        self._info_stdout = info_stdout
        self._raises_on = raises_on
        self.calls: list[dict] = []

    async def _vm_exec(self, command, env=None, timeout_sec=None):
        self.calls.append({"command": command, "env": env})
        if self._raises_on is not None and self._raises_on in command:
            raise RuntimeError("vm gone")
        if command.startswith("cat "):
            return SimpleNamespace(stdout=self._cat_stdout, stderr="", return_code=0)
        if command.startswith("docker info"):
            return SimpleNamespace(stdout=self._info_stdout, stderr="", return_code=0)
        return SimpleNamespace(stdout="", stderr="", return_code=0)


@pytest.mark.asyncio
async def test_modal_inject_merges_and_reloads():
    strategy = _ModalCaptureStrategy(
        cat_stdout='{"iptables": false, "bridge": "none"}',
        info_stdout='["https://mirror.gcr.io/"]',
    )

    await harbor_patches._inject_mirror_and_reload(strategy)

    write = next(c for c in strategy.calls if c["env"])
    decoded = base64.b64decode(write["env"]["ODDISH_DAEMON_JSON_B64"]).decode()
    cfg = json.loads(decoded)
    assert cfg["iptables"] is False
    assert cfg["bridge"] == "none"
    assert cfg["registry-mirrors"] == ["https://mirror.gcr.io"]

    assert any("kill -HUP" in c["command"] for c in strategy.calls)


@pytest.mark.asyncio
async def test_modal_inject_handles_invalid_existing_json():
    strategy = _ModalCaptureStrategy(cat_stdout="not json at all")

    await harbor_patches._inject_mirror_and_reload(strategy)

    write = next(c for c in strategy.calls if c["env"])
    cfg = json.loads(base64.b64decode(write["env"]["ODDISH_DAEMON_JSON_B64"]).decode())
    assert cfg == {"registry-mirrors": ["https://mirror.gcr.io"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("cat_stdout", ["null", "123", "[]", "true", '"str"'])
async def test_modal_inject_handles_non_object_existing_json(cat_stdout):
    strategy = _ModalCaptureStrategy(cat_stdout=cat_stdout)

    await harbor_patches._inject_mirror_and_reload(strategy)

    write = next(c for c in strategy.calls if c["env"])
    cfg = json.loads(base64.b64decode(write["env"]["ODDISH_DAEMON_JSON_B64"]).decode())
    assert cfg == {"registry-mirrors": ["https://mirror.gcr.io"]}


@pytest.mark.asyncio
async def test_modal_inject_never_raises():
    strategy = _ModalCaptureStrategy(cat_stdout="{}", raises_on="cat ")
    await harbor_patches._inject_mirror_and_reload(strategy)


@pytest.mark.asyncio
async def test_modal_wait_wrapper_runs_orig_before_inject(monkeypatch):
    order: list[str] = []

    async def _orig(_self):
        order.append("orig")

    async def _fake_inject(_self):
        order.append("inject")

    monkeypatch.setattr(harbor_patches, "_inject_mirror_and_reload", _fake_inject)

    wrapped = harbor_patches._wrap_modal_wait_for_docker_daemon(_orig)
    await wrapped(object())

    assert order == ["orig", "inject"]


@pytest.mark.parametrize(
    "make_wrapped, attr",
    [
        (lambda: harbor_patches._wrap_wait_for_docker_daemon(_ok), "_oddish_wrapped"),
        (
            lambda: harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec),
            "_oddish_mirror_wrapped",
        ),
        (
            lambda: harbor_patches._wrap_modal_wait_for_docker_daemon(_ok),
            "_oddish_mirror_wrapped",
        ),
    ],
)
def test_wrappers_set_marker_to_prevent_double_wrap(make_wrapped, attr):
    assert getattr(make_wrapped(), attr, False) is True
