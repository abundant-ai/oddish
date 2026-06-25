"""Patch Harbor at runtime."""

from __future__ import annotations

import base64
import importlib
import json
import logging
import re
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PATCHED = False

_MIRROR_URL = "https://mirror.gcr.io"
_MIRROR_HOST = "mirror.gcr.io"
_DAEMON_JSON_PATH = "/etc/docker/daemon.json"

_DOCKERD_DIAG_CMD = (
    "echo '=== tail -n 200 /var/log/dockerd.log ==='; "
    "tail -n 200 /var/log/dockerd.log 2>&1 || echo '(dockerd.log unavailable)'; "
    "echo '=== dockerd process ==='; "
    "ps -ef 2>/dev/null | grep -i dockerd | grep -v grep || echo '(no dockerd process)'; "
    "echo '=== memory (MB) ==='; free -m 2>/dev/null || echo '(free unavailable)'; "
    "echo '=== disk ==='; df -h /var/lib/docker 2>/dev/null || df -h 2>/dev/null || true"
)


def apply_harbor_patches() -> None:
    """Install Harbor patches once."""
    global _PATCHED
    if _PATCHED:
        return
    _patch_dind_docker_daemon_diagnostics()
    _patch_daytona_registry_mirror()
    _patch_modal_registry_mirror()
    _PATCHED = True


async def _collect_dockerd_diagnostics(strategy: Any) -> str:
    """Read dockerd logs."""
    try:
        result = await strategy._vm_exec(_DOCKERD_DIAG_CMD, timeout_sec=15)
        out = (getattr(result, "stdout", "") or "") + (
            getattr(result, "stderr", "") or ""
        )
        out = out.strip() or "(no diagnostic output)"
        return (
            "dockerd diagnostics (captured by oddish before sandbox teardown):\n" + out
        )
    except Exception as exc:
        return f"dockerd diagnostics unavailable: {exc!r}"


def _wrap_wait_for_docker_daemon(
    orig: Callable[[Any], Awaitable[None]],
) -> Callable[[Any], Awaitable[None]]:
    async def _wait_for_docker_daemon(self: Any) -> None:
        try:
            return await orig(self)
        except RuntimeError as exc:
            diag = await _collect_dockerd_diagnostics(self)
            raise RuntimeError(f"{exc}\n\n{diag}") from exc

    setattr(_wait_for_docker_daemon, "_oddish_wrapped", True)
    return _wait_for_docker_daemon


def _install_method_patch(
    module_path: str,
    class_name: str,
    method_name: str,
    wrap: Callable[[Any], Any],
    *,
    marker: str,
    label: str,
) -> None:
    try:
        module = importlib.import_module(module_path)
    except Exception:
        logger.debug("Harbor %s unavailable; skipping %s patch", module_path, label)
        return

    cls = getattr(module, class_name, None)
    orig = getattr(cls, method_name, None)
    if orig is None:
        logger.warning(
            "Harbor %s.%s not found; %s patch skipped", class_name, method_name, label
        )
        return
    if getattr(orig, marker, False):
        return

    setattr(cls, method_name, wrap(orig))
    logger.info("Installed oddish %s patch on Harbor %s", label, class_name)


def _patch_dind_docker_daemon_diagnostics() -> None:
    _install_method_patch(
        "harbor.environments.daytona.environment",
        "_DaytonaDinD",
        "_wait_for_docker_daemon",
        _wrap_wait_for_docker_daemon,
        marker="_oddish_wrapped",
        label="dockerd-diagnostics",
    )


_DAYTONA_DOCKERD_MARKER = "dockerd-entrypoint.sh dockerd"


def _mirror_daemon_json(raw: str) -> str:
    try:
        cfg = json.loads(raw or "{}")
    except json.JSONDecodeError:
        cfg = {}
    if not isinstance(cfg, dict):
        cfg = {}
    mirrors = cfg.get("registry-mirrors")
    if not isinstance(mirrors, list):
        mirrors = []
    cfg["registry-mirrors"] = [_MIRROR_URL, *[m for m in mirrors if m != _MIRROR_URL]]
    return json.dumps(cfg)


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _raise_for_bad_result(result: Any) -> None:
    code = getattr(result, "return_code", 0)
    if code not in (None, 0):
        stderr = getattr(result, "stderr", "") or ""
        stdout = getattr(result, "stdout", "") or ""
        raise RuntimeError((stderr or stdout or f"command failed with {code}").strip())


def _daytona_merge_mirror_command(path: str = _DAEMON_JSON_PATH) -> str:
    flat_path = f"{path}.flat"
    tmp_path = f"{path}.tmp"
    empty = (
        'sed \'s/"registry-mirrors"[[:space:]]*:[[:space:]]*\\[[[:space:]]*\\]/'
        '"registry-mirrors": ["https:\\/\\/mirror.gcr.io"]/'
        "' "
        f"{flat_path} > {tmp_path}"
    )
    prefix = (
        'sed \'s/"registry-mirrors"[[:space:]]*:[[:space:]]*\\[/'
        '"registry-mirrors": ["https:\\/\\/mirror.gcr.io", /'
        "' "
        f"{flat_path} > {tmp_path}"
    )
    return (
        f"(tr -d '\\n' < {path} > {flat_path} && "
        f'if grep -q \'"registry-mirrors"[[:space:]]*:[[:space:]]*'
        f"\\[[[:space:]]*\\]' {flat_path} 2>/dev/null; then\n"
        f"{empty}\nelse\n{prefix}\nfi &&\n"
        f"mv {tmp_path} {path})\n"
        f"status=$?\nrm -f {flat_path} {tmp_path}\n"
        "test $status -eq 0"
    )


def _strip_registry_mirror_flags(command: str) -> str:
    return re.sub(r"\s+--registry-mirror=\S+", "", command)


def _daytona_command_with_mirror(command: str) -> str:
    if _DAYTONA_DOCKERD_MARKER not in command:
        return command
    before, after = command.split(_DAYTONA_DOCKERD_MARKER, 1)
    existing = f"{_DAYTONA_DOCKERD_MARKER}{after}"
    config_command = _strip_registry_mirror_flags(existing)
    if f"--registry-mirror={_MIRROR_URL}" in existing:
        mirrored = existing
    else:
        mirrored = f"{_DAYTONA_DOCKERD_MARKER} --registry-mirror={_MIRROR_URL}{after}"
    merge = _daytona_merge_mirror_command()
    return (
        f"{before}if grep -q '\"registry-mirrors\"' {_DAEMON_JSON_PATH} "
        f"2>/dev/null; then\n"
        f"if grep -q '{_MIRROR_URL}' {_DAEMON_JSON_PATH} 2>/dev/null; then\n"
        f"{config_command}\nelse\n{merge} &&\n{config_command}\nfi\n"
        f"else\n{mirrored}\nfi"
    )


def _wrap_daytona_vm_exec(
    orig: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    async def _vm_exec(self: Any, command: str, *args: Any, **kwargs: Any) -> Any:
        command = _daytona_command_with_mirror(command)
        return await orig(self, command, *args, **kwargs)

    setattr(_vm_exec, "_oddish_mirror_wrapped", True)
    return _vm_exec


def _patch_daytona_registry_mirror() -> None:
    _install_method_patch(
        "harbor.environments.daytona.environment",
        "_DaytonaDinD",
        "_vm_exec",
        _wrap_daytona_vm_exec,
        marker="_oddish_mirror_wrapped",
        label="registry-mirror",
    )


async def _inject_mirror_and_reload(strategy: Any) -> None:
    try:
        res = await strategy._vm_exec(
            f"cat {_DAEMON_JSON_PATH} 2>/dev/null || echo '{{}}'", timeout_sec=10
        )
        daemon_json_b64 = _b64(_mirror_daemon_json(getattr(res, "stdout", "")))

        result = await strategy._vm_exec(
            "umask 077 && mkdir -p /etc/docker && "
            f"printf %s '{daemon_json_b64}' | base64 -d > {_DAEMON_JSON_PATH}.tmp "
            f"&& mv {_DAEMON_JSON_PATH}.tmp {_DAEMON_JSON_PATH}",
            timeout_sec=10,
        )
        _raise_for_bad_result(result)
        await strategy._vm_exec(
            'kill -HUP "$(cat /var/run/docker.pid 2>/dev/null '
            '|| pidof dockerd 2>/dev/null)" 2>/dev/null || true',
            timeout_sec=10,
        )

        check = await strategy._vm_exec(
            "docker info --format '{{json .RegistryConfig.Mirrors}}' 2>/dev/null",
            timeout_sec=10,
        )
        out = (getattr(check, "stdout", "") or "").strip()
        if _MIRROR_HOST not in out:
            logger.warning(
                "Modal DinD registry mirror not confirmed by docker info: %r", out
            )
        else:
            logger.info("Pointed Modal DinD dockerd at registry mirror %s", _MIRROR_URL)
    except Exception as exc:
        logger.warning("Failed to inject registry mirror into Modal DinD: %r", exc)


def _wrap_modal_wait_for_docker_daemon(
    orig: Callable[[Any], Awaitable[None]],
) -> Callable[[Any], Awaitable[None]]:
    async def _wait_for_docker_daemon(self: Any) -> None:
        await orig(self)
        await _inject_mirror_and_reload(self)

    setattr(_wait_for_docker_daemon, "_oddish_mirror_wrapped", True)
    return _wait_for_docker_daemon


def _patch_modal_registry_mirror() -> None:
    _install_method_patch(
        "harbor.environments.modal",
        "_ModalDinD",
        "_wait_for_docker_daemon",
        _wrap_modal_wait_for_docker_daemon,
        marker="_oddish_mirror_wrapped",
        label="registry-mirror",
    )
