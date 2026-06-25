"""Runtime shims for the pinned Harbor dependency."""

from __future__ import annotations

import base64
import inspect
import importlib
import json
import logging
import os
import shlex
import tempfile
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PATCHED = False

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
    """Install oddish's Harbor runtime shims exactly once."""
    global _PATCHED
    if _PATCHED:
        return
    _patch_dind_strategies()
    _PATCHED = True


async def _collect_dockerd_diagnostics(strategy: Any) -> str:
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
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
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
    with_diagnostics: bool,
) -> Callable[[Any], Awaitable[None]]:
    async def _wait_for_docker_daemon(self: Any) -> None:
        try:
            await orig(self)
        except RuntimeError as exc:
            if with_diagnostics:
                diag = await _collect_dockerd_diagnostics(self)
                raise RuntimeError(f"{exc}\n\n{diag}") from exc
            raise
        await _perform_registry_login(self)

    setattr(_wait_for_docker_daemon, "_oddish_wrapped", True)
    return _wait_for_docker_daemon


def _wrap_stop(orig: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    async def _stop(self: Any, *args: Any, **kwargs: Any) -> None:
        await _scrub_registry_login(self)
        return await orig(self, *args, **kwargs)

    setattr(_stop, "_oddish_wrapped", True)
    return _stop


def _patch_strategy(dind: type | None, *, with_diagnostics: bool, label: str) -> None:
    if dind is None:
        logger.warning("Harbor %s strategy not found; DinD shims skipped", label)
        return

    wait = getattr(dind, "_wait_for_docker_daemon", None)
    if wait is not None and not getattr(wait, "_oddish_wrapped", False):
        dind._wait_for_docker_daemon = _wrap_wait_for_docker_daemon(
            wait, with_diagnostics=with_diagnostics
        )

    stop = getattr(dind, "stop", None)
    if stop is not None and not getattr(stop, "_oddish_wrapped", False):
        dind.stop = _wrap_stop(stop)

    logger.info("Installed oddish DinD shims (login + cleanup) on Harbor %s", label)


def _patch_dind_strategies() -> None:
    for module_name, class_name, with_diagnostics in (
        ("harbor.environments.daytona.environment", "_DaytonaDinD", True),
        ("harbor.environments.modal", "_ModalDinD", False),
    ):
        try:
            module = importlib.import_module(module_name)
            _patch_strategy(
                getattr(module, class_name, None),
                with_diagnostics=with_diagnostics,
                label=class_name,
            )
        except Exception as exc:
            logger.warning(
                "Harbor %s unavailable or could not be patched; DinD shims skipped: %r",
                module_name,
                exc,
            )
