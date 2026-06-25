"""Patch Harbor at runtime."""

from __future__ import annotations

import base64
import importlib
import inspect
import json
import logging
import os
import re
import shlex
import tempfile
from functools import partial
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

_DOCKER_CONFIG_PATH = "/root/.docker/config.json"
_DOCKER_CONFIG_BACKUP_PATH = "/tmp/oddish-docker-config.before-registry-auth.json"
_DOCKER_CONFIG_ABSENT_PATH = "/tmp/oddish-docker-config.was-absent"
_DOCKER_CONFIG_STAGED_PATH = "/tmp/oddish-docker-config.registry-auth.json"
_REGISTRY_LOGIN_CMD = """
set -eu
config="/root/.docker/config.json"
backup="/tmp/oddish-docker-config.before-registry-auth.json"
absent="/tmp/oddish-docker-config.was-absent"
staged="/tmp/oddish-docker-config.registry-auth.json"
if [ ! -f "$backup" ] && [ ! -f "$absent" ]; then
  if [ -f "$config" ]; then
    cp "$config" "$backup"
    chmod 600 "$backup"
  else
    touch "$absent"
  fi
fi
mkdir -p "$(dirname "$config")"
cp "$staged" "$config"
chmod 600 "$config"
rm -f "$staged"
""".strip()
_RESTORE_DOCKER_CONFIG_CMD = """
set +e
config="/root/.docker/config.json"
backup="/tmp/oddish-docker-config.before-registry-auth.json"
absent="/tmp/oddish-docker-config.was-absent"
if [ -f "$backup" ]; then
    mkdir -p "$(dirname "$config")"
    cp "$backup" "$config"
    chmod 600 "$config"
elif [ -f "$absent" ]; then
    rm -f "$config"
fi
rm -f "$backup" "$absent"
exit 0
""".strip()
_RESTORE_ATTR = "_oddish_restore_docker_config"


def apply_harbor_patches() -> None:
    """Install Harbor patches once."""
    global _PATCHED
    if _PATCHED:
        return
    _patch_daytona_dind()
    _patch_modal_dind()
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


def _redact(text: str, secrets: list[str]) -> str:
    out = text
    for secret in sorted(
        {secret for secret in secrets if secret}, key=len, reverse=True
    ):
        out = out.replace(secret, "***")
    return out


def _result_text(result: Any) -> str:
    return (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")


def _secret_variants(secret: str) -> list[str]:
    quoted = shlex.quote(secret)
    json_secret = json.dumps(secret)[1:-1]
    json_quoted = json.dumps(quoted)[1:-1]
    return [
        secret,
        quoted,
        repr(secret)[1:-1],
        repr(quoted)[1:-1],
        json_secret,
        json_quoted,
        repr(json_secret)[1:-1],
        repr(json_quoted)[1:-1],
    ]


def _registry_login_config(creds: list[Any]) -> tuple[str, list[str], list[str]]:
    auths: dict[str, dict[str, str]] = {}
    secrets: list[str] = []
    registries: list[str] = []
    for cred in creds:
        registry = cred.auth_key()
        if registry == "https://index.docker.io/v1/":
            auth_key = "https://index.docker.io/v1/"
            registry_label = "docker.io"
        else:
            auth_key = registry
            registry_label = registry
        registries.append(registry_label)
        basic = f"{cred.username}:{cred.token}"
        encoded_basic = base64.b64encode(basic.encode()).decode()
        auths[auth_key] = {"auth": encoded_basic}
        secrets.extend(_secret_variants(cred.token))
        secrets.extend(_secret_variants(basic))
        secrets.extend(_secret_variants(encoded_basic))
    return json.dumps({"auths": auths}), secrets, registries


async def _upload_text(strategy: Any, text: str, target_path: str) -> None:
    tmp_name = ""
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as tmp:
            tmp.write(text)
            tmp_name = tmp.name
        uploaded = strategy.upload_file(tmp_name, target_path)
        if inspect.isawaitable(uploaded):
            await uploaded
    finally:
        if tmp_name:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass


async def _perform_registry_login(strategy: Any) -> None:
    from oddish.registry_auth import current_registry_credentials

    creds = current_registry_credentials.get()
    if not creds:
        return
    if getattr(strategy, _RESTORE_ATTR, False):
        return

    config_json, secrets, registries = _registry_login_config(creds)
    logged_registries = sorted(set(registries))
    setattr(strategy, _RESTORE_ATTR, True)
    labels = ", ".join(logged_registries)
    message: str | None = None
    try:
        await _upload_text(strategy, config_json, _DOCKER_CONFIG_STAGED_PATH)
        result = await strategy._vm_exec(
            _REGISTRY_LOGIN_CMD,
            timeout_sec=30,
        )
    except Exception as exc:
        message = f"Registry login failed for {labels}: {_redact(repr(exc), secrets)}"
        logger.warning(message)
    if message is not None:
        raise RuntimeError(message)
    if getattr(result, "return_code", 1) != 0:
        message = (
            f"Registry login failed (rc={getattr(result, 'return_code', '?')}) "
            f"for {labels}: {_redact(_result_text(result), secrets)}"
        )
        logger.warning(message)
        raise RuntimeError(message)
    logger.info("Authenticated DinD daemon for registries: %s", labels)


async def _scrub_registry_login(strategy: Any) -> None:
    if not getattr(strategy, _RESTORE_ATTR, False):
        return
    try:
        await strategy._vm_exec(_RESTORE_DOCKER_CONFIG_CMD, timeout_sec=15)
    except Exception as exc:
        logger.debug("Registry config restore skipped: %r", exc)
    finally:
        setattr(strategy, _RESTORE_ATTR, False)


def _wrap_wait_for_docker_daemon(
    orig: Callable[[Any], Awaitable[None]],
    *,
    with_diagnostics: bool = False,
    with_mirror: bool = False,
) -> Callable[[Any], Awaitable[None]]:
    async def _wait_for_docker_daemon(self: Any) -> None:
        try:
            await orig(self)
        except RuntimeError as exc:
            if with_diagnostics:
                diag = await _collect_dockerd_diagnostics(self)
                raise RuntimeError(f"{exc}\n\n{diag}") from exc
            raise
        if with_mirror:
            await _inject_mirror_and_reload(self)
        await _perform_registry_login(self)

    setattr(_wait_for_docker_daemon, "_oddish_wrapped", True)
    return _wait_for_docker_daemon


def _wrap_stop(orig: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    async def _stop(self: Any, *args: Any, **kwargs: Any) -> None:
        await _scrub_registry_login(self)
        return await orig(self, *args, **kwargs)

    setattr(_stop, "_oddish_stop_wrapped", True)
    return _stop


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


_DAYTONA_DOCKERD_MARKER = "dockerd-entrypoint.sh dockerd"
_REGISTRY_MIRROR_FLAG_RE = re.compile(
    r"\s+--registry-mirror(?:=(\"[^\"]*\"|'[^']*'|\S+)|\s+(\"[^\"]*\"|'[^']*'|\S+))"
)


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


def _strip_registry_mirror_flags(command: str) -> str:
    return _REGISTRY_MIRROR_FLAG_RE.sub("", command)


def _daytona_command_with_mirror(command: str) -> str:
    if _DAYTONA_DOCKERD_MARKER not in command:
        return command
    before, after = command.split(_DAYTONA_DOCKERD_MARKER, 1)
    existing = f"{_DAYTONA_DOCKERD_MARKER}{after}"
    config_command = _strip_registry_mirror_flags(existing)
    if _MIRROR_URL in existing:
        mirrored = existing
    else:
        mirrored = f"{_DAYTONA_DOCKERD_MARKER} --registry-mirror={_MIRROR_URL}{after}"
    return (
        f"{before}if grep -q '\"registry-mirrors\"' {_DAEMON_JSON_PATH} "
        f"2>/dev/null; then\n"
        f"{config_command}\n"
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


def _patch_daytona_dind() -> None:
    module_path = "harbor.environments.daytona.environment"
    class_name = "_DaytonaDinD"
    _install_method_patch(
        module_path,
        class_name,
        "_wait_for_docker_daemon",
        partial(_wrap_wait_for_docker_daemon, with_diagnostics=True, with_mirror=False),
        marker="_oddish_wrapped",
        label="dockerd-diagnostics+registry-login",
    )
    _install_method_patch(
        module_path,
        class_name,
        "_vm_exec",
        _wrap_daytona_vm_exec,
        marker="_oddish_mirror_wrapped",
        label="registry-mirror",
    )
    _install_method_patch(
        module_path,
        class_name,
        "stop",
        _wrap_stop,
        marker="_oddish_stop_wrapped",
        label="registry-cleanup",
    )


def _patch_modal_dind() -> None:
    module_path = "harbor.environments.modal"
    class_name = "_ModalDinD"
    _install_method_patch(
        module_path,
        class_name,
        "_wait_for_docker_daemon",
        partial(_wrap_wait_for_docker_daemon, with_diagnostics=False, with_mirror=True),
        marker="_oddish_wrapped",
        label="registry-mirror+registry-login",
    )
    _install_method_patch(
        module_path,
        class_name,
        "stop",
        _wrap_stop,
        marker="_oddish_stop_wrapped",
        label="registry-cleanup",
    )
