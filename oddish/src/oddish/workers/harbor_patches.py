"""Best-effort runtime shims over the vendored Harbor library.

Harbor is a git-pinned dependency (``rishidesai/harbor``) we cannot edit in
place, so behavioral shims that must ship with oddish live here and are applied
once, idempotently, at worker startup via :func:`apply_harbor_patches`. Keep
these minimal and reversible -- prefer upstreaming to the harbor fork when a shim
proves durable.
"""

from __future__ import annotations

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


def apply_harbor_patches() -> None:
    """Install oddish's Harbor runtime shims exactly once."""
    global _PATCHED
    if _PATCHED:
        return
    _PATCHED = True
    _patch_dind_docker_daemon_diagnostics()


async def _collect_dockerd_diagnostics(strategy: Any) -> str:
    """Read dockerd's log + resource state from the DinD VM. Never raises.

    Runs inside the failure window: the sandbox VM still exists (teardown happens
    only after the exception propagates), but the agent never started, so this is
    the last chance to learn *why* dockerd died before the VM is destroyed.
    """
    try:
        result = await strategy._vm_exec(_DOCKERD_DIAG_CMD, timeout_sec=15)
        out = (getattr(result, "stdout", "") or "") + (getattr(result, "stderr", "") or "")
        out = out.strip() or "(no diagnostic output)"
        return "dockerd diagnostics (captured by oddish before sandbox teardown):\n" + out
    except Exception as exc:  # the VM/sandbox may already be unreachable
        return f"dockerd diagnostics unavailable: {exc!r}"


def _wrap_wait_for_docker_daemon(
    orig: Callable[[Any], Awaitable[None]],
) -> Callable[[Any], Awaitable[None]]:
    """Wrap ``_wait_for_docker_daemon`` to fold dockerd logs into its error.

    Harbor raises ``RuntimeError("Docker daemon not ready after 60s. Last
    output: ...")`` where "Last output" is only the failing ``docker info`` --
    always just "Cannot connect to the Docker daemon", which never says why the
    daemon died. We capture dockerd's own log and re-raise with it appended.
    """

    async def _wait_for_docker_daemon(self: Any) -> None:
        try:
            return await orig(self)
        except RuntimeError as exc:
            diag = await _collect_dockerd_diagnostics(self)
            raise RuntimeError(f"{exc}\n\n{diag}") from exc

    _wait_for_docker_daemon._oddish_wrapped = True  # type: ignore[attr-defined]
    return _wait_for_docker_daemon


def _patch_dind_docker_daemon_diagnostics() -> None:
    try:
        from harbor.environments.daytona import environment as _dt
    except Exception:  # daytona extras may be absent in some contexts
        logger.debug(
            "Harbor daytona environment unavailable; skipping dockerd diagnostics patch"
        )
        return

    dind = getattr(_dt, "_DaytonaDinD", None)
    orig = getattr(dind, "_wait_for_docker_daemon", None)
    if orig is None:
        logger.warning(
            "Harbor _DaytonaDinD._wait_for_docker_daemon not found; "
            "dockerd diagnostics patch skipped"
        )
        return
    if getattr(orig, "_oddish_wrapped", False):
        return

    dind._wait_for_docker_daemon = _wrap_wait_for_docker_daemon(orig)
    logger.info("Installed oddish dockerd-diagnostics patch on Harbor _DaytonaDinD")
