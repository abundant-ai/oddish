"""Best-effort runtime shims over the vendored Harbor library.

Harbor is a git-pinned dependency (``rishidesai/harbor``) we cannot edit in
place, so behavioral shims that must ship with oddish live here and are applied
once, idempotently, at worker startup via :func:`apply_harbor_patches`. Keep
these minimal and reversible -- prefer upstreaming to the harbor fork when a shim
proves durable.

Two shims live here, both targeting the Docker-in-Docker (compose) strategies
(``_DaytonaDinD`` / ``_ModalDinD``):

* **dockerd diagnostics** -- fold the DinD daemon's own log into the
  "Docker daemon not ready" error so failures are debuggable.
* **registry login** -- when the run supplied a per-run container-registry
  credential (see :mod:`oddish.registry_auth`), write ``/root/.docker/config.json``
  inside the sandbox *after the daemon is ready and before ``compose build``/``up``*
  so image pulls authenticate (fixing Docker Hub ``toomanyrequests``), then remove
  it again on teardown.
"""

from __future__ import annotations

import base64
import logging
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_PATCHED = False

# Diagnostics gathered from the DinD VM when the Docker daemon never becomes
# ready. Alpine/sh-compatible (the sandbox is ``docker:*-dind``, Alpine-based).
_DOCKERD_DIAG_CMD = (
    "echo '=== tail -n 200 /var/log/dockerd.log ==='; "
    "tail -n 200 /var/log/dockerd.log 2>&1 || echo '(dockerd.log unavailable)'; "
    "echo '=== dockerd process ==='; "
    "ps -ef 2>/dev/null | grep -i dockerd | grep -v grep || echo '(no dockerd process)'; "
    "echo '=== memory (MB) ==='; free -m 2>/dev/null || echo '(free unavailable)'; "
    "echo '=== disk ==='; df -h /var/lib/docker 2>/dev/null || df -h 2>/dev/null || true"
)

# Root's docker config inside the DinD VM. The docker CLI (running as root for
# ``compose build``/``up``) reads this and forwards the auth to the daemon on
# every pull.
_DOCKER_CONFIG_PATH = "/root/.docker/config.json"
_DOCKER_CONFIG_ENV = "ODDISH_DOCKERCFG_B64"

# Write the config from a base64 blob passed via the exec env so the raw token
# is never embedded literally in the command. Busybox (Alpine) provides printf
# and base64. ``umask 077`` makes the redirection create config.json as 0600
# from the start (no world-readable window) — chmod is belt-and-suspenders.
_WRITE_DOCKER_CONFIG_CMD = (
    f'umask 077 && mkdir -p "$(dirname {_DOCKER_CONFIG_PATH})" && '
    f'printf %s "${_DOCKER_CONFIG_ENV}" | base64 -d > {_DOCKER_CONFIG_PATH} && '
    f"chmod 600 {_DOCKER_CONFIG_PATH}"
)
_SCRUB_DOCKER_CONFIG_CMD = f"rm -f {_DOCKER_CONFIG_PATH}"


def apply_harbor_patches() -> None:
    """Install oddish's Harbor runtime shims exactly once."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    _patch_dind_strategies()


# =============================================================================
# dockerd diagnostics
# =============================================================================


async def _collect_dockerd_diagnostics(strategy: Any) -> str:
    """Read dockerd's log + resource state from the DinD VM. Never raises.

    Runs inside the failure window: the sandbox VM still exists (teardown happens
    only after the exception propagates), but the agent never started, so this is
    the last chance to learn *why* dockerd died before the VM is destroyed.
    """
    try:
        result = await strategy._vm_exec(_DOCKERD_DIAG_CMD, timeout_sec=15)
        out = (getattr(result, "stdout", "") or "") + (
            getattr(result, "stderr", "") or ""
        )
        out = out.strip() or "(no diagnostic output)"
        return (
            "dockerd diagnostics (captured by oddish before sandbox teardown):\n" + out
        )
    except Exception as exc:  # the VM/sandbox may already be unreachable
        return f"dockerd diagnostics unavailable: {exc!r}"


# =============================================================================
# per-run registry login
# =============================================================================


def _redact(text: str, secrets: list[str]) -> str:
    out = text
    for secret in secrets:
        if secret:
            out = out.replace(secret, "***")
    return out


async def _perform_registry_login(strategy: Any) -> None:
    """Authenticate the DinD daemon to the run's registries, if any were supplied.

    Reads the request-scoped credentials published by the worker. Writes
    ``/root/.docker/config.json`` so subsequent ``compose build``/``up`` pulls are
    authenticated. Never raises -- a login failure should degrade to the old
    (anonymous) behavior, not abort the trial before it starts.
    """
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
        if getattr(result, "return_code", 1) != 0:
            tokens = [c.token for c in creds]
            detail = (getattr(result, "stdout", "") or "") + (
                getattr(result, "stderr", "") or ""
            )
            logger.warning(
                "Registry login write failed (rc=%s) for %s: %s",
                getattr(result, "return_code", "?"),
                registries,
                _redact(detail, tokens),
            )
        else:
            logger.info("Authenticated DinD daemon for registries: %s", registries)
    except Exception as exc:  # never block trial startup on login
        logger.warning("Registry login skipped due to error: %r", exc)


async def _scrub_registry_login(strategy: Any) -> None:
    """Remove the docker config (log the token off) on sandbox teardown."""
    try:
        from oddish.registry_auth import current_registry_credentials

        if not current_registry_credentials.get():
            return
        await strategy._vm_exec(_SCRUB_DOCKER_CONFIG_CMD, timeout_sec=10)
    except Exception as exc:  # pragma: no cover - best-effort cleanup
        logger.debug("Registry logout scrub skipped: %r", exc)


# =============================================================================
# wrappers
# =============================================================================


def _wrap_wait_for_docker_daemon(
    orig: Callable[[Any], Awaitable[None]],
    *,
    with_diagnostics: bool,
) -> Callable[[Any], Awaitable[None]]:
    """Wrap ``_wait_for_docker_daemon`` to (optionally) add dockerd diagnostics on
    failure and to run the per-run registry login on success.

    The daemon-ready return is the exact seam: the daemon is up and ``compose
    build``/``up`` have not started, so writing the docker config here makes every
    subsequent pull authenticated.
    """

    async def _wait_for_docker_daemon(self: Any) -> None:
        try:
            await orig(self)
        except RuntimeError as exc:
            if with_diagnostics:
                diag = await _collect_dockerd_diagnostics(self)
                raise RuntimeError(f"{exc}\n\n{diag}") from exc
            raise
        await _perform_registry_login(self)

    _wait_for_docker_daemon._oddish_wrapped = True  # type: ignore[attr-defined]
    return _wait_for_docker_daemon


def _wrap_stop(
    orig: Callable[..., Awaitable[None]],
) -> Callable[..., Awaitable[None]]:
    """Wrap ``stop`` to scrub the docker config before the original teardown.

    Signature-agnostic (``*args``/``**kwargs``) so it survives any drift in
    Harbor's ``stop(self, delete)`` signature.
    """

    async def _stop(self: Any, *args: Any, **kwargs: Any) -> None:
        await _scrub_registry_login(self)
        return await orig(self, *args, **kwargs)

    _stop._oddish_wrapped = True  # type: ignore[attr-defined]
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
    # Daytona is the default cloud environment; its DinD strategy also gets the
    # dockerd-diagnostics fold-in. Daytona extras may be absent in some contexts.
    try:
        from harbor.environments.daytona import environment as _dt

        _patch_strategy(
            getattr(_dt, "_DaytonaDinD", None),
            with_diagnostics=True,
            label="_DaytonaDinD",
        )
    except Exception:
        logger.debug(
            "Harbor daytona environment unavailable; Daytona DinD shims skipped"
        )

    # Modal DinD (used for GPU / Modal-backed trials).
    try:
        from harbor.environments import modal as _modal

        _patch_strategy(
            getattr(_modal, "_ModalDinD", None),
            with_diagnostics=False,
            label="_ModalDinD",
        )
    except Exception:
        logger.debug("Harbor modal environment unavailable; Modal DinD shims skipped")
