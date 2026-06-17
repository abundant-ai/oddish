"""Local-only network policy for offline probe tasks.

Offline tasks (``allow_internet=false``) run under Harbor's Docker env with
``network_mode: none`` -- the container has no network at all. That blocks not
just the agent install but the model API (Bedrock) the agent must call, so the
agent cannot run locally. Production (Modal) keeps host networking plus a
per-domain allowlist (``modal.py``), reaching Bedrock while staying otherwise
isolated; local Docker has no allowlist primitive.

For LOCAL probe runs we therefore relax the offline constraint: patch the staged
task's ``allow_internet`` to true so the run container has egress and the agent
can reach Bedrock. This trades away offline isolation locally (the agent gains
general internet); production keeps the real isolation. Applied only from
``local_runner`` -- the Modal/cloud path is untouched.
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
    """Patch the staged task's ``task.toml`` to ``allow_internet=true``.

    Lets the local (Docker) run container reach Bedrock. ``task_dir`` MUST be a
    writable temp copy. Best-effort; returns True if the file now allows
    internet. Uses Harbor's own task-config serialization so the rest of the
    TOML round-trips unchanged.
    """
    config_path = task_dir / "task.toml"
    if not config_path.exists():
        logger.warning("local-policy: no task.toml at %s", config_path)
        return False
    try:
        from harbor.models.task.config import TaskConfig as HarborTaskConfig

        tc = HarborTaskConfig.model_validate_toml(config_path.read_text())
        if tc.environment.allow_internet:
            return True
        tc.environment.allow_internet = True
        config_path.write_text(tc.model_dump_toml())
    except Exception:
        logger.exception("local-policy: enabling internet for %s failed", config_path)
        return False
    return True
