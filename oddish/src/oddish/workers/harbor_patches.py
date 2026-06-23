"""Best-effort runtime shims over the vendored Harbor library.

Harbor is a git-pinned dependency (``rishidesai/harbor``) we cannot edit in
place, so behavioral shims that must ship with oddish live here and are applied
once, idempotently, at worker startup via :func:`apply_harbor_patches`. Keep
these minimal and reversible -- prefer upstreaming to the harbor fork when a shim
proves durable.
"""

from __future__ import annotations

import base64
import importlib
import json
import logging
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

_PATCHED = False

# Anonymous pull-through cache of Docker Hub. Pointing dockerd here keeps
# unqualified ``docker.io`` pulls off Docker Hub's anonymous rate limit (429
# toomanyrequests). Credential-free; only affects docker.io by Docker's design.
_MIRROR_URL = "https://mirror.gcr.io"
_MIRROR_HOST = urlsplit(_MIRROR_URL).netloc
_DAEMON_JSON_PATH = "/etc/docker/daemon.json"

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
    _patch_dind_docker_daemon_diagnostics()
    _patch_daytona_registry_mirror()
    _patch_modal_registry_mirror()
    _PATCHED = True


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


def _install_method_patch(
    module_path: str,
    class_name: str,
    method_name: str,
    wrap: Callable[[Any], Any],
    *,
    marker: str,
    label: str,
) -> None:
    """Wrap ``<module_path>.<class_name>.<method_name>`` once, idempotently.

    Harbor extras may be absent and upstream internals may drift, so every step
    degrades to a log line rather than raising into worker startup.
    """
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


# --- Registry mirror: keep docker.io pulls off Docker Hub's anon rate limit ---

_DAYTONA_DOCKERD_MARKER = "dockerd-entrypoint.sh dockerd"


def _wrap_daytona_vm_exec(
    orig: Callable[..., Awaitable[Any]],
) -> Callable[..., Awaitable[Any]]:
    """Wrap Daytona ``_vm_exec`` to write daemon.json before dockerd launches.

    ``registry-mirrors`` must be on disk before the daemon reads it once at
    startup. Daytona starts dockerd via ``dockerd-entrypoint.sh dockerd`` flowing
    through ``_vm_exec``, so we prepend a daemon.json write to that one command
    (joined so it runs first) and pass every other command through untouched.
    """

    async def _vm_exec(self: Any, command: str, *args: Any, **kwargs: Any) -> Any:
        if _DAYTONA_DOCKERD_MARKER in command:
            cfg = json.dumps({"registry-mirrors": [_MIRROR_URL]})
            # Write atomically: a truncated/interrupted write must never leave a
            # corrupt daemon.json (dockerd would refuse to start); ``;`` then lets
            # dockerd launch mirror-less rather than fail.
            prefix = (
                f"mkdir -p /etc/docker && printf %s '{cfg}' > {_DAEMON_JSON_PATH}.tmp "
                f"&& mv {_DAEMON_JSON_PATH}.tmp {_DAEMON_JSON_PATH} ; "
            )
            command = prefix + command
        return await orig(self, command, *args, **kwargs)

    _vm_exec._oddish_mirror_wrapped = True  # type: ignore[attr-defined]
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
    """Add the registry mirror to Modal's daemon.json and SIGHUP dockerd. Never raises.

    Modal's dockerd is started by Modal itself (not via ``_vm_exec``) and the
    image bakes ``{"iptables": false, "bridge": "none"}`` into daemon.json, so we
    merge ``registry-mirrors`` into the existing config (preserving those keys)
    and reload -- ``registry-mirrors`` is SIGHUP-reloadable.
    """
    try:
        res = await strategy._vm_exec(
            f"cat {_DAEMON_JSON_PATH} 2>/dev/null || echo '{{}}'", timeout_sec=10
        )
        try:
            cfg = json.loads(getattr(res, "stdout", "") or "{}")
        except json.JSONDecodeError:
            cfg = {}
        if not isinstance(cfg, dict):
            cfg = {}
        cfg["registry-mirrors"] = [_MIRROR_URL]
        merged = json.dumps(cfg)
        b64 = base64.b64encode(merged.encode()).decode()

        await strategy._vm_exec(
            "umask 077 && mkdir -p /etc/docker && "
            f'printf %s "$ODDISH_DAEMON_JSON_B64" | base64 -d > {_DAEMON_JSON_PATH}',
            env={"ODDISH_DAEMON_JSON_B64": b64},
            timeout_sec=10,
        )
        await strategy._vm_exec(
            'kill -HUP "$(cat /var/run/docker.pid 2>/dev/null '
            "|| pidof dockerd 2>/dev/null)\" 2>/dev/null || true",
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
    """Wrap Modal ``_wait_for_docker_daemon`` to inject the mirror once dockerd is up."""

    async def _wait_for_docker_daemon(self: Any) -> None:
        await orig(self)
        await _inject_mirror_and_reload(self)

    _wait_for_docker_daemon._oddish_mirror_wrapped = True  # type: ignore[attr-defined]
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
