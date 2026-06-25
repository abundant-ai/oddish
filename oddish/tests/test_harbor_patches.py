from __future__ import annotations

import base64
import importlib
import importlib.machinery
import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
harbor_patches = importlib.import_module("oddish.workers.harbor.patches")
harbor_entry = importlib.import_module("oddish.workers.harbor._entry")


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
        return SimpleNamespace(stdout=self._stdout, stderr=self._stderr, return_code=0)


async def _ok(_self):
    return None


def _boom(message):
    async def _raise(_self):
        raise RuntimeError(message)

    return _raise


def _assert_shell_parses(command: str) -> None:
    subprocess.run(["sh", "-n", "-c", command], check=True)


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


def test_install_method_patch_replaces_target_once(monkeypatch):
    module_name = "fake_harbor_module"
    module = ModuleType(module_name)

    async def original(_self):
        return None

    def wrap(orig):
        async def wrapped(_self):
            return await orig(_self)

        setattr(wrapped, "_patched", True)
        return wrapped

    class FakeDind:
        target = original

    module.FakeDind = FakeDind
    monkeypatch.setitem(sys.modules, module_name, module)

    harbor_patches._install_method_patch(
        module_name,
        "FakeDind",
        "target",
        wrap,
        marker="_patched",
        label="test",
    )
    first = FakeDind.target
    harbor_patches._install_method_patch(
        module_name,
        "FakeDind",
        "target",
        wrap,
        marker="_patched",
        label="test",
    )

    assert first is not original
    assert FakeDind.target is first
    assert getattr(FakeDind.target, "_patched") is True


def test_install_method_patch_skips_missing_targets(monkeypatch):
    module_name = "fake_harbor_missing"
    module = ModuleType(module_name)
    calls: list[object] = []

    class FakeDind:
        pass

    module.FakeDind = FakeDind
    monkeypatch.setitem(sys.modules, module_name, module)

    def wrap(orig):
        calls.append(orig)
        return orig

    harbor_patches._install_method_patch(
        "not_a_real_harbor_module",
        "FakeDind",
        "target",
        wrap,
        marker="_patched",
        label="test",
    )
    harbor_patches._install_method_patch(
        module_name,
        "FakeDind",
        "target",
        wrap,
        marker="_patched",
        label="test",
    )
    harbor_patches._install_method_patch(
        module_name,
        "MissingDind",
        "target",
        wrap,
        marker="_patched",
        label="test",
    )

    assert calls == []
    assert not hasattr(FakeDind, "target")


class _CaptureStrategy:
    def __init__(self):
        self.calls: list[dict] = []

    async def _vm_exec(self, command, *args, **kwargs):
        self.calls.append({"command": command, "args": args, "kwargs": kwargs})
        return SimpleNamespace(stdout="", stderr="", return_code=0)


@pytest.mark.asyncio
async def test_daytona_vm_exec_adds_registry_mirror_flag_for_dockerd():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    cmd = "dockerd-entrypoint.sh dockerd > /var/log/dockerd.log 2>&1 &"
    await wrapped(strategy, cmd, timeout_sec=10)

    sent = strategy.calls[0]["command"]
    assert "grep -q '\"registry-mirrors\"'" in sent
    assert "grep -q 'https://mirror.gcr.io'" in sent
    assert 'sed \'s/"registry-mirrors"' in sent
    assert "mv /etc/docker/daemon.json.tmp /etc/docker/daemon.json" in sent
    assert (
        "else\n"
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://mirror.gcr.io "
        "> /var/log/dockerd.log 2>&1 &\n"
        "fi"
    ) in sent
    assert "&;" not in sent
    _assert_shell_parses(sent)
    assert "base64 -d" not in sent
    assert strategy.calls[0]["kwargs"] == {"timeout_sec": 10}


@pytest.mark.asyncio
async def test_daytona_vm_exec_does_not_duplicate_registry_mirror_flag():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    cmd = (
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://mirror.gcr.io "
        "> /var/log/dockerd.log 2>&1 &"
    )
    await wrapped(strategy, cmd)

    sent = strategy.calls[0]["command"]
    assert "then\ndockerd-entrypoint.sh dockerd > /var/log/dockerd.log" in sent
    assert (
        "else\n"
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://mirror.gcr.io "
        "> /var/log/dockerd.log"
    ) in sent
    assert sent.count("--registry-mirror=") == 1
    assert "&;" not in sent
    _assert_shell_parses(sent)
    assert strategy.calls[0]["args"] == ()
    assert strategy.calls[0]["kwargs"] == {}


@pytest.mark.asyncio
async def test_daytona_vm_exec_preserves_different_registry_mirror_flag():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    cmd = (
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://example.com "
        "> /var/log/dockerd.log 2>&1 &"
    )
    await wrapped(strategy, cmd)

    sent = strategy.calls[0]["command"]
    assert "--registry-mirror=https://mirror.gcr.io" in sent
    assert "--registry-mirror=https://example.com" in sent
    assert "then\ndockerd-entrypoint.sh dockerd > /var/log/dockerd.log" in sent
    else_branch = 'else\nsed \'s/"registry-mirrors"'
    assert else_branch in sent
    cli_branch = (
        "else\n"
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://mirror.gcr.io "
        "--registry-mirror=https://example.com"
    )
    assert cli_branch in sent
    assert sent.index(cli_branch) < sent.rindex("> /var/log/dockerd.log")
    assert "&;" not in sent
    _assert_shell_parses(sent)


@pytest.mark.asyncio
async def test_daytona_vm_exec_strips_split_registry_mirror_flag():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    cmd = (
        "dockerd-entrypoint.sh dockerd --registry-mirror https://example.com "
        "> /var/log/dockerd.log 2>&1 &"
    )
    await wrapped(strategy, cmd)

    sent = strategy.calls[0]["command"]
    assert "then\ndockerd-entrypoint.sh dockerd > /var/log/dockerd.log" in sent
    assert "&& {\ndockerd-entrypoint.sh dockerd > /var/log/dockerd.log" in sent
    assert "--registry-mirror https://example.com" in sent
    assert "&& {\ndockerd-entrypoint.sh dockerd --registry-mirror" not in sent
    assert "&;" not in sent
    _assert_shell_parses(sent)


@pytest.mark.parametrize(
    "initial, expected, expected_extra",
    [
        (
            {"registry-mirrors": []},
            ["https://mirror.gcr.io"],
            {},
        ),
        (
            '{\n  "registry-mirrors": [\n  ],\n  "iptables": false\n}',
            ["https://mirror.gcr.io"],
            {"iptables": False},
        ),
        (
            {"registry-mirrors": ["https://example.com"], "iptables": False},
            ["https://mirror.gcr.io", "https://example.com"],
            {"iptables": False},
        ),
    ],
)
def test_daytona_merge_mirror_command_writes_valid_json(
    tmp_path, initial, expected, expected_extra
):
    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text(initial if isinstance(initial, str) else json.dumps(initial))

    command = harbor_patches._daytona_merge_mirror_command(str(daemon_json))
    _assert_shell_parses(command)
    subprocess.run(["sh", "-c", command], check=True)

    cfg = json.loads(daemon_json.read_text())
    assert cfg["registry-mirrors"] == expected
    for key, value in expected_extra.items():
        assert cfg[key] == value


def test_daytona_merge_mirror_command_preserves_failures(tmp_path):
    daemon_json = tmp_path / "missing.json"
    stale_tmp = tmp_path / "missing.json.tmp"
    stale_tmp.write_text('{"registry-mirrors": ["https://stale.example"]}')

    command = harbor_patches._daytona_merge_mirror_command(str(daemon_json))
    _assert_shell_parses(command)
    result = subprocess.run(["sh", "-c", command], check=False)

    assert result.returncode != 0
    assert not daemon_json.exists()
    assert not stale_tmp.exists()


def test_daytona_command_with_mirror_stops_when_merge_fails(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()

    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text('{"registry-mirrors": ["https://example.com"]}')
    marker = state_dir / "started"
    entrypoint = bin_dir / "dockerd-entrypoint.sh"
    entrypoint.write_text('touch "$STARTED_FILE"\n')
    entrypoint.chmod(0o755)
    monkeypatch.setattr(harbor_patches, "_DAEMON_JSON_PATH", str(daemon_json))
    monkeypatch.setattr(
        harbor_patches, "_daytona_merge_mirror_command", lambda: "false"
    )

    command = harbor_patches._daytona_command_with_mirror(
        "dockerd-entrypoint.sh dockerd > /dev/null 2>&1 &"
    )
    _assert_shell_parses(command)
    result = subprocess.run(
        ["sh", "-c", command],
        check=False,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
            "STARTED_FILE": str(marker),
        },
    )

    assert result.returncode != 0
    assert not marker.exists()


@pytest.mark.asyncio
async def test_daytona_vm_exec_passes_other_commands_through():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    await wrapped(strategy, "docker info", "extra", timeout_sec=5)

    assert strategy.calls[0]["command"] == "docker info"
    assert strategy.calls[0]["args"] == ("extra",)
    assert strategy.calls[0]["kwargs"] == {"timeout_sec": 5}


class _ModalCaptureStrategy:
    def __init__(
        self, *, cat_stdout, info_stdout="", raises_on=None, bad_result_on=None
    ):
        self._cat_stdout = cat_stdout
        self._info_stdout = info_stdout
        self._raises_on = raises_on
        self._bad_result_on = bad_result_on
        self.calls: list[dict] = []

    async def _vm_exec(self, command, timeout_sec=None):
        self.calls.append({"command": command, "timeout_sec": timeout_sec})
        if self._raises_on is not None and self._raises_on in command:
            raise RuntimeError("vm gone")
        if self._bad_result_on is not None and self._bad_result_on in command:
            return SimpleNamespace(stdout="", stderr="write failed", return_code=1)
        if command.startswith("cat "):
            return SimpleNamespace(stdout=self._cat_stdout, stderr="", return_code=0)
        if command.startswith("docker info"):
            return SimpleNamespace(stdout=self._info_stdout, stderr="", return_code=0)
        return SimpleNamespace(stdout="", stderr="", return_code=0)


def _written_daemon_json(strategy):
    write = next(c for c in strategy.calls if "base64 -d" in c["command"])
    encoded = write["command"].split("printf %s '", 1)[1].split("'", 1)[0]
    return json.loads(base64.b64decode(encoded).decode())


@pytest.mark.asyncio
async def test_modal_inject_merges_and_reloads():
    strategy = _ModalCaptureStrategy(
        cat_stdout=(
            '{"iptables": false, "bridge": "none", '
            '"registry-mirrors": ["https://example.com"]}'
        ),
        info_stdout='["https://mirror.gcr.io/"]',
    )

    await harbor_patches._inject_mirror_and_reload(strategy)

    cfg = _written_daemon_json(strategy)
    assert cfg["iptables"] is False
    assert cfg["bridge"] == "none"
    assert cfg["registry-mirrors"] == [
        "https://mirror.gcr.io",
        "https://example.com",
    ]

    assert any(".tmp && mv" in c["command"] for c in strategy.calls)
    assert any("kill -HUP" in c["command"] for c in strategy.calls)


@pytest.mark.asyncio
async def test_modal_inject_handles_invalid_existing_json():
    strategy = _ModalCaptureStrategy(cat_stdout="not json at all")

    await harbor_patches._inject_mirror_and_reload(strategy)

    assert _written_daemon_json(strategy) == {
        "registry-mirrors": ["https://mirror.gcr.io"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("cat_stdout", ["null", "123", "[]", "true", '"str"'])
async def test_modal_inject_handles_non_object_existing_json(cat_stdout):
    strategy = _ModalCaptureStrategy(cat_stdout=cat_stdout)

    await harbor_patches._inject_mirror_and_reload(strategy)

    assert _written_daemon_json(strategy) == {
        "registry-mirrors": ["https://mirror.gcr.io"]
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("raises_on", ["cat ", "base64 -d", "kill -HUP", "docker info"])
async def test_modal_inject_never_raises(raises_on):
    strategy = _ModalCaptureStrategy(cat_stdout="{}", raises_on=raises_on)
    await harbor_patches._inject_mirror_and_reload(strategy)


@pytest.mark.asyncio
async def test_modal_inject_stops_when_atomic_write_fails():
    strategy = _ModalCaptureStrategy(cat_stdout="{}", bad_result_on="base64 -d")

    await harbor_patches._inject_mirror_and_reload(strategy)

    commands = [c["command"] for c in strategy.calls]
    assert any("base64 -d" in command for command in commands)
    assert not any("kill -HUP" in command for command in commands)
    assert not any(command.startswith("docker info") for command in commands)


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


def test_entry_applies_sibling_harbor_patches(monkeypatch):
    calls: list[str] = []

    class Loader:
        def create_module(self, spec):
            return ModuleType(spec.name)

        def exec_module(self, module):
            module.apply_harbor_patches = lambda: calls.append("patched")

    def _fake_spec(name, path):
        assert name == "_oddish_harbor_patches"
        assert path == Path(harbor_entry._THIS_DIR) / "patches.py"
        return importlib.machinery.ModuleSpec(name, Loader())

    monkeypatch.setattr(
        harbor_entry.importlib.util, "spec_from_file_location", _fake_spec
    )

    harbor_entry._apply_sibling_harbor_patches()

    assert calls == ["patched"]


@pytest.mark.asyncio
async def test_entry_run_applies_patches_before_job_create(monkeypatch, tmp_path):
    order: list[str] = []
    harbor_module = ModuleType("harbor")

    class FakeJob:
        job_dir = str(tmp_path)

        @classmethod
        async def create(cls, _config):
            order.append("create")
            return cls()

        def __getattr__(self, name):
            if name.startswith("on_"):
                return lambda _hook: None
            raise AttributeError(name)

        async def run(self):
            order.append("run")

    harbor_module.Job = FakeJob
    monkeypatch.setitem(sys.modules, "harbor", harbor_module)
    monkeypatch.setattr(
        harbor_entry,
        "_apply_sibling_harbor_patches",
        lambda: order.append("patch"),
    )

    def _fake_build_job_config(_payload):
        order.append("config")
        return object()

    monkeypatch.setattr(harbor_entry, "_build_job_config", _fake_build_job_config)

    outcome = await harbor_entry._run({"jobs_dir": str(tmp_path)})

    assert order == ["patch", "config", "create", "run"]
    assert outcome["error"] is None
