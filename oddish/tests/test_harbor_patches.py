from __future__ import annotations

import base64
import importlib
import importlib.machinery
import json
import logging
import os
import shlex
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from harbor.trial.hooks import TrialEvent

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

_registry_auth = importlib.import_module("oddish.registry_auth")
harbor_patches = importlib.import_module("oddish.workers.harbor.patches")
harbor_entry = importlib.import_module("oddish.workers.harbor._entry")

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
        self.uploads: list[tuple[str, str]] = []

    async def _vm_exec(self, command, *, env=None, timeout_sec=None):
        self.calls.append((command, env))
        if self._raises is not None:
            raise self._raises
        return SimpleNamespace(
            stdout=self._stdout, stderr=self._stderr, return_code=self._return_code
        )

    def _stage_file_to_host(self, local_path, target_path):
        with open(local_path, encoding="utf-8") as handle:
            self.uploads.append((handle.read(), target_path))


async def _ok(_self):
    return None


def _boom(message):
    async def _raise(_self):
        raise RuntimeError(message)

    return _raise


def _assert_shell_parses(command: str) -> None:
    subprocess.run(["sh", "-n", "-c", command], check=True)


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
    assert original in message
    assert "overlay2: driver failed" in message
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
async def test_login_stages_docker_config_without_token_in_command(creds):
    strategy = _FakeStrategy()
    await harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)(
        strategy
    )

    assert len(strategy.calls) == 1
    command, env = strategy.calls[0]
    assert command == harbor_patches._REGISTRY_LOGIN_CMD
    assert env is None
    assert "secrettoken" not in command

    assert len(strategy.uploads) == 1
    text, target = strategy.uploads[0]
    assert target == harbor_patches._DOCKER_CONFIG_STAGED_PATH
    cfg = json.loads(text)
    encoded = base64.b64encode(b"alice:secrettoken").decode()
    assert cfg["auths"]["https://index.docker.io/v1/"]["auth"] == encoded


@pytest.mark.asyncio
async def test_login_is_noop_without_credentials():
    strategy = _FakeStrategy()
    await harbor_patches._perform_registry_login(strategy)
    assert strategy.calls == []
    assert strategy.uploads == []


@pytest.mark.asyncio
async def test_login_failure_raises_into_daemon_wait(creds):
    strategy = _FakeStrategy(raises=RuntimeError("exec failed"))
    with pytest.raises(RuntimeError, match="Registry login failed"):
        await harbor_patches._wrap_wait_for_docker_daemon(_ok, with_diagnostics=True)(
            strategy
        )


@pytest.mark.asyncio
async def test_login_failure_has_no_secret_cause(creds):
    strategy = _FakeStrategy(raises=RuntimeError("exec failed: secrettoken"))

    with pytest.raises(RuntimeError, match="Registry login failed") as excinfo:
        await harbor_patches._perform_registry_login(strategy)

    assert excinfo.value.__cause__ is None
    assert excinfo.value.__context__ is None
    assert "secrettoken" not in str(excinfo.value)


@pytest.mark.asyncio
async def test_login_skips_second_login(creds):
    strategy = _FakeStrategy()

    await harbor_patches._perform_registry_login(strategy)
    await harbor_patches._perform_registry_login(strategy)

    assert len(strategy.calls) == 1


@pytest.mark.asyncio
async def test_login_exception_log_redacts_token(creds, caplog):
    class _LeakyStrategy(_FakeStrategy):
        async def _vm_exec(self, command, *, env=None, timeout_sec=None):
            self.calls.append((command, env))
            raise RuntimeError("exec failed: secrettoken")

    strategy = _LeakyStrategy()
    with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
        with pytest.raises(RuntimeError):
            await harbor_patches._perform_registry_login(strategy)

    assert "secrettoken" not in caplog.text
    assert "***" in caplog.text


@pytest.mark.asyncio
async def test_login_exception_log_redacts_shell_quoted_token(caplog):
    token_value = "abc'def"
    token = current_registry_credentials.set(
        [RegistryCredential("alice", token_value, "docker.io")]
    )

    class _LeakyStrategy(_FakeStrategy):
        async def _vm_exec(self, command, *, env=None, timeout_sec=None):
            self.calls.append((command, env))
            raise RuntimeError(f"exec failed: {shlex.quote(token_value)}")

    try:
        with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
            with pytest.raises(RuntimeError):
                await harbor_patches._perform_registry_login(_LeakyStrategy())
    finally:
        current_registry_credentials.reset(token)

    assert token_value not in caplog.text
    assert shlex.quote(token_value) not in caplog.text
    assert "***" in caplog.text


@pytest.mark.asyncio
async def test_login_exception_log_redacts_json_escaped_token(caplog):
    token_value = 'abc"def'
    token = current_registry_credentials.set(
        [RegistryCredential("alice", token_value, "docker.io")]
    )

    class _LeakyStrategy(_FakeStrategy):
        async def _vm_exec(self, command, *, env=None, timeout_sec=None):
            self.calls.append((command, env))
            raise RuntimeError(f"exec failed: {json.dumps(token_value)[1:-1]}")

    try:
        with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
            with pytest.raises(RuntimeError):
                await harbor_patches._perform_registry_login(_LeakyStrategy())
    finally:
        current_registry_credentials.reset(token)

    assert token_value not in caplog.text
    assert json.dumps(token_value)[1:-1] not in caplog.text
    assert "***" in caplog.text


def test_redact_replaces_overlapping_secrets_longest_first():
    redacted = harbor_patches._redact(
        "token abc abcdef bob:abc bob:abcdef",
        ["abc", "abcdef", "bob:abc", "bob:abcdef"],
    )

    assert "abc" not in redacted
    assert "abcdef" not in redacted
    assert "bob:" not in redacted
    assert redacted == "token *** *** *** ***"


def test_login_config_redacts_docker_config_auth(creds):
    _config, secrets, _registries = harbor_patches._registry_login_config(creds)
    encoded = base64.b64encode(b"alice:secrettoken").decode()

    assert encoded in secrets
    assert encoded not in harbor_patches._redact(encoded, secrets)


@pytest.mark.asyncio
async def test_login_nonzero_log_redacts_token(creds, caplog):
    strategy = _FakeStrategy(stdout="bad secrettoken", return_code=1)

    with caplog.at_level(logging.WARNING, logger=harbor_patches.__name__):
        with pytest.raises(RuntimeError):
            await harbor_patches._perform_registry_login(strategy)

    assert "secrettoken" not in caplog.text
    assert "***" in caplog.text


def test_apply_harbor_patches_is_idempotent(monkeypatch):
    calls = {"daytona": 0, "modal": 0}
    monkeypatch.setattr(harbor_patches, "_PATCHED", False)
    monkeypatch.setattr(
        harbor_patches,
        "_patch_daytona_dind",
        lambda: calls.__setitem__("daytona", calls["daytona"] + 1),
    )
    monkeypatch.setattr(
        harbor_patches,
        "_patch_modal_dind",
        lambda: calls.__setitem__("modal", calls["modal"] + 1),
    )

    harbor_patches.apply_harbor_patches()
    harbor_patches.apply_harbor_patches()

    assert calls == {"daytona": 1, "modal": 1}


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
async def test_daytona_vm_exec_lets_daemon_config_own_mirrors():
    strategy = _CaptureStrategy()
    wrapped = harbor_patches._wrap_daytona_vm_exec(_CaptureStrategy._vm_exec)

    cmd = (
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://example.com "
        "--registry-mirror 'https://example.org/cache?x=1&y=2' "
        "> /var/log/dockerd.log 2>&1 &"
    )
    await wrapped(strategy, cmd)

    sent = strategy.calls[0]["command"]
    assert "then\ndockerd-entrypoint.sh dockerd > /var/log/dockerd.log" in sent
    assert (
        "else\n"
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://mirror.gcr.io "
        "--registry-mirror=https://example.com "
        "--registry-mirror 'https://example.org/cache?x=1&y=2' "
        "> /var/log/dockerd.log"
    ) in sent
    assert "&;" not in sent
    _assert_shell_parses(sent)


def test_daytona_command_with_mirror_runs_config_branch(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    bin_dir.mkdir()
    state_dir.mkdir()

    daemon_json = tmp_path / "daemon.json"
    daemon_json.write_text(json.dumps({"registry-mirrors": ["https://example.com"]}))
    marker = state_dir / "started"
    entrypoint = bin_dir / "dockerd-entrypoint.sh"
    entrypoint.write_text('touch "$STARTED_FILE"\n')
    entrypoint.chmod(0o755)
    monkeypatch.setattr(harbor_patches, "_DAEMON_JSON_PATH", str(daemon_json))

    command = harbor_patches._daytona_command_with_mirror(
        "dockerd-entrypoint.sh dockerd --registry-mirror=https://example.com "
        "> /dev/null 2>&1 &"
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

    assert result.returncode == 0
    for _ in range(20):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists()


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

    wrapped = harbor_patches._wrap_wait_for_docker_daemon(
        _orig, with_diagnostics=False, with_mirror=True
    )
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


@pytest.mark.parametrize(
    "event_name, expected",
    [
        ("START", TrialEvent.START.value),
        ("AGENT_START", TrialEvent.AGENT_START.value),
        ("agent_start", TrialEvent.AGENT_START.value),
        ("ENVIRONMENT_START", TrialEvent.ENVIRONMENT_START.value),
        ("environment_start", TrialEvent.ENVIRONMENT_START.value),
        ("VERIFICATION_START", TrialEvent.VERIFICATION_START.value),
        ("verification_start", TrialEvent.VERIFICATION_START.value),
        ("AGENT_END", TrialEvent.AGENT_END.value),
        ("agent_end", TrialEvent.AGENT_END.value),
        ("END", TrialEvent.END.value),
        ("CANCEL", TrialEvent.CANCEL.value),
    ],
)
@pytest.mark.asyncio
async def test_entry_hook_emits_canonical_events(monkeypatch, event_name, expected):
    payloads: list[dict] = []
    event = SimpleNamespace(
        event=SimpleNamespace(value=event_name),
        trial_id="t-1",
        environment=None,
        result=None,
    )
    monkeypatch.setattr(harbor_entry, "_emit_event_line", payloads.append)

    hook = harbor_entry._make_hook(None, None)
    await hook(event)

    assert payloads[0]["event"] == expected


@pytest.mark.asyncio
async def test_entry_hook_uploads_probe_task_on_agent_start(monkeypatch, tmp_path):
    uploads: list[dict] = []

    class Environment:
        async def upload_dir(self, *, source_dir, target_dir):
            uploads.append({"source_dir": source_dir, "target_dir": target_dir})

    event = SimpleNamespace(
        event=SimpleNamespace(value="agent-start"),
        trial_id="t-1",
        environment=Environment(),
        result=None,
    )
    monkeypatch.setattr(harbor_entry, "_emit_event_line", lambda _payload: None)

    hook = harbor_entry._make_hook(str(tmp_path / "task"), "/probe")
    await hook(event)

    assert uploads == [{"source_dir": tmp_path / "task", "target_dir": "/probe"}]


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
