"""Network-policy relaxation for offline / probe trials.

Offline tasks run under ``no-network`` (or an ``allowlist`` covering only the
model API). On Modal that's a CIDR allowlist; on local Docker it's
``network_mode: none``. Either way the agent loses egress to everything except
its model endpoint -- which breaks two things probe trials need:

  * Harbor's claude-code install (``curl downloads.claude.ai/.../bootstrap.sh``),
    which otherwise SYN-times-out (~127s, curl exit 28), failing the whole trial.
  * The oddish-query CLI staged into the sandbox, which must reach the backend.

We relax this by forcing the staged task's ``network_mode`` to ``public``.
Used by BOTH the local Docker runner (``local_runner``) and the cloud (Modal)
probe path (``workers/queue/trial_handler``). This trades offline isolation for
working egress -- acceptable because probes are auditing runs, not graded.

IMPORTANT: we set ``network_mode``, not the legacy ``allow_internet`` flag.
Harbor ignores ``allow_internet`` whenever a task sets ``network_mode`` or
``allowed_hosts`` explicitly (see ``_apply_legacy_allow_internet`` in Harbor's
``models/task/config.py``), so flipping ``allow_internet`` was a silent no-op
for exactly the tasks that needed it.
"""

from __future__ import annotations

import logging
import tomllib
from pathlib import Path

logger = logging.getLogger(__name__)


def task_is_offline(task_dir: Path) -> bool:
    """True if the task's ``task.toml`` sets ``environment.allow_internet = false``."""
    config_path = task_dir / "task.toml"
    if not config_path.exists():
        return False
    try:
        data = tomllib.loads(config_path.read_text())
    except Exception:
        logger.exception("local-policy: could not parse %s", config_path)
        return False
    env = data.get("environment") or {}
    return env.get("allow_internet", True) is False


def enable_local_internet(task_dir: Path) -> bool:
    """Force the staged task's ``task.toml`` to ``network_mode = "public"``.

    Gives the run container (Docker or Modal) full egress so Harbor's agent
    install and the oddish-query CLI both work. ``task_dir`` MUST be a writable
    temp copy. Best-effort; returns True if the file now allows internet. Uses
    Harbor's own task-config serialization so the rest of the TOML round-trips
    unchanged.
    """
    config_path = task_dir / "task.toml"
    if not config_path.exists():
        logger.warning("offline-policy: no task.toml at %s", config_path)
        return False
    try:
        from harbor.models.task.config import NetworkMode
        from harbor.models.task.config import TaskConfig as HarborTaskConfig

        tc = HarborTaskConfig.model_validate_toml(config_path.read_text())
        if tc.environment.network_mode == NetworkMode.PUBLIC:
            return True
        tc.environment.network_mode = NetworkMode.PUBLIC
        # ``allowed_hosts`` is only valid in allowlist mode; Harbor rejects it
        # under ``public`` at parse time, so clear it when relaxing.
        tc.environment.allowed_hosts = None
        config_path.write_text(tc.model_dump_toml())
    except Exception:
        logger.exception("offline-policy: enabling internet for %s failed", config_path)
        return False
    return True
