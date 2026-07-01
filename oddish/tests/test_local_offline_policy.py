"""Probe/offline trials must get real egress (network_mode=public).

Regression coverage for the bug where ``enable_local_internet`` set the legacy
``allow_internet`` flag, which Harbor ignores whenever a task sets
``network_mode``/``allowed_hosts`` explicitly. Offline tasks therefore stayed on
the model-API-only CIDR allowlist, so the claude-code install (curl
downloads.claude.ai) and the oddish-query CLI were both SYN-dropped (~127s
timeout, exit 28).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from harbor.models.task.config import NetworkMode
from harbor.models.task.config import TaskConfig as HarborTaskConfig

from oddish.worker.local_offline_policy import enable_local_internet


def _write_task(tmp_path: Path, body: str) -> Path:
    (tmp_path / "task.toml").write_text(textwrap.dedent(body))
    return tmp_path


def _resolved_mode(task_dir: Path) -> NetworkMode:
    tc = HarborTaskConfig.model_validate_toml((task_dir / "task.toml").read_text())
    return tc.environment.network_mode


def test_explicit_no_network_becomes_public(tmp_path):
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        network_mode = "no-network"
        """,
    )
    assert enable_local_internet(task_dir) is True
    assert _resolved_mode(task_dir) == NetworkMode.PUBLIC


def test_legacy_allow_internet_false_becomes_public(tmp_path):
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        allow_internet = false
        """,
    )
    assert enable_local_internet(task_dir) is True
    assert _resolved_mode(task_dir) == NetworkMode.PUBLIC


def test_allowlist_mode_becomes_public(tmp_path):
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        network_mode = "allowlist"
        allowed_hosts = ["api.anthropic.com"]
        """,
    )
    assert enable_local_internet(task_dir) is True
    assert _resolved_mode(task_dir) == NetworkMode.PUBLIC


def test_already_public_is_idempotent(tmp_path):
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        network_mode = "public"
        """,
    )
    assert enable_local_internet(task_dir) is True
    assert _resolved_mode(task_dir) == NetworkMode.PUBLIC


def test_missing_task_toml_returns_false(tmp_path):
    assert enable_local_internet(tmp_path) is False
