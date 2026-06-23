from __future__ import annotations

import base64
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PATCHED = False
_DOCKER_CONFIG_PATH = "/root/.docker/config.json"
_DOCKER_CONFIG_ENV = "ODDISH_DOCKERCFG_B64"
_DOCKERD_DIAG_CMD = (
    "echo '=== tail -n 200 /var/log/dockerd.log ==='; "
    "tail -n 200 /var/log/dockerd.log 2>&1 || echo '(dockerd.log unavailable)'; "
    "echo '=== dockerd process ==='; "
    "ps -ef 2>/dev/null | grep -i dockerd | grep -v grep || echo '(no dockerd process)'; "
    "echo '=== memory (MB) ==='; free -m 2>/dev/null || echo '(free unavailable)'; "
    "echo '=== disk ==='; df -h /var/lib/docker 2>/dev/null || df -h 2>/dev/null || true"
)
_WRITE_DOCKER_CONFIG_CMD = (
    f'umask 077 && mkdir -p "$(dirname {_DOCKER_CONFIG_PATH})" && '
    f'printf %s "${_DOCKER_CONFIG_ENV}" | base64 -d > {_DOCKER_CONFIG_PATH} && '
    f"chmod 600 {_DOCKER_CONFIG_PATH}"
)
_SCRUB_DOCKER_CONFIG_CMD = f"rm -f {_DOCKER_CONFIG_PATH}"


def apply_harbor_patches() -> None:
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    _patch_dind_strategies()


async def _collect_dockerd_diagnostics(strategy: Any) -> str:
    try:
        result = await strategy._vm_exec(_DOCKERD_DIAG_CMD, timeout_sec=15)
        out = (getattr(result, "stdout", "") or "") + (
            getattr(result, "stderr", "") or ""
        )
        return "dockerd diagnostics:\n" + (out.strip() or "(no diagnostic output)")
    except Exception as exc:
        return f"dockerd diagnostics unavailable: {exc!r}"


def _redact(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


async def _perform_registry_login(strategy: Any) -> None:
    try:
        from oddish.registry_auth import (
            build_docker_config_json,
            current_registry_credentials,
        )

        creds = current_registry_credentials.get()
        if not creds:
            return

        cfg_b64 = base64.b64encode(build_docker_config_json(creds).encode()).decode()
        result = await strategy._vm_exec(
            _WRITE_DOCKER_CONFIG_CMD,
            env={_DOCKER_CONFIG_ENV: cfg_b64},
            timeout_sec=20,
        )
        registries = ", ".join(sorted({c.auth_key() for c in creds}))
        if getattr(result, "return_code", 1) == 0:
            logger.info("Authenticated DinD daemon for registries: %s", registries)
            return

        detail = (getattr(result, "stdout", "") or "") + (
            getattr(result, "stderr", "") or ""
        )
        logger.warning(
            "Registry login write failed (rc=%s) for %s: %s",
            getattr(result, "return_code", "?"),
            registries,
            _redact(detail, [c.token for c in creds]),
        )
    except Exception as exc:
        logger.warning("Registry login skipped due to error: %r", exc)


async def _scrub_registry_login(strategy: Any) -> None:
    try:
        from oddish.registry_auth import current_registry_credentials

        if current_registry_credentials.get():
            await strategy._vm_exec(_SCRUB_DOCKER_CONFIG_CMD, timeout_sec=10)
    except Exception as exc:
        logger.debug("Registry logout scrub skipped: %r", exc)


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
                raise RuntimeError(
                    f"{exc}\n\n{await _collect_dockerd_diagnostics(self)}"
                ) from exc
            raise
        await _perform_registry_login(self)

    _wait_for_docker_daemon._oddish_wrapped = True
    return _wait_for_docker_daemon


def _wrap_stop(orig: Callable[..., Awaitable[None]]) -> Callable[..., Awaitable[None]]:
    async def _stop(self: Any, *args: Any, **kwargs: Any) -> None:
        await _scrub_registry_login(self)
        return await orig(self, *args, **kwargs)

    _stop._oddish_wrapped = True
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

    logger.info("Installed oddish DinD shims on Harbor %s", label)


def _patch_dind_strategies() -> None:
    try:
        from harbor.environments.daytona import environment as daytona_environment

        _patch_strategy(
            getattr(daytona_environment, "_DaytonaDinD", None),
            with_diagnostics=True,
            label="_DaytonaDinD",
        )
    except Exception:
        logger.debug("Harbor daytona environment unavailable; DinD shims skipped")

    try:
        from harbor.environments import modal

        _patch_strategy(
            getattr(modal, "_ModalDinD", None),
            with_diagnostics=False,
            label="_ModalDinD",
        )
    except Exception:
        logger.debug("Harbor modal environment unavailable; DinD shims skipped")
