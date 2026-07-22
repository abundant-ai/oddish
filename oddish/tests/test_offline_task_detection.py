"""Offline-task detection, and the cloud default that deliberately ignores it.

``task_is_offline`` must recognize both spellings of "this task has no egress":
the modern ``environment.network_mode`` and the legacy
``environment.allow_internet``. Checking only the legacy flag is the blind spot
that once made ``enable_local_internet`` a silent no-op, because Harbor ignores
``allow_internet`` whenever ``network_mode`` is set explicitly.

The routing half of #831 is deliberately NOT reinstated here. That change routed
offline tasks to Modal on the theory that Daytona's all-or-nothing egress was
what killed experiment 086f6140. Re-running the same 8 tasks 3x3 on Modal
(experiment b99711e9) reproduced the failure exactly -- 60/72 dead in agent
bootstrap, 0 input tokens -- which falsified it. The real variable was the
Harbor pin, not the backend:

    prior passing runs   2ae61e86 / 555fc203 / b802d4ba   pass
    086f6140 (daytona)   d070837                          fail
    b99711e9 (modal)     d070837                          fail

086f6140 was both the first Daytona run and the first run on ``d070837``, which
is what made the backend look causal. Between those pins Harbor deleted
``environments/modal_agent_tools.py`` (which baked claude-code, codex AND
gemini-cli into ``/opt/harbor-agent-tools/bin``, so offline trials never
installed anything) and ``environments/modal_network.py``. The vestigial PATH
export at ``agents/installed/codex.py:30`` is the leftover consumer of a layer
nothing produces anymore.

So offline tasks keep the plain cheap-first default. The tests below pin that,
so a future reroute has to argue its case against the falsified one.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from harbor.models.environment_type import EnvironmentType

from oddish.cli.run import _default_cloud_environment_for_task
from oddish.worker.local_offline_policy import task_is_offline


def _write_task(tmp_path: Path, body: str) -> Path:
    (tmp_path / "task.toml").write_text(textwrap.dedent(body))
    return tmp_path


# --- offline detection ------------------------------------------------------


def test_task_is_offline_detects_no_network_mode(tmp_path) -> None:
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        network_mode = "no-network"
        """,
    )
    assert task_is_offline(task_dir) is True


def test_task_is_offline_detects_allowlist_mode(tmp_path) -> None:
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
    assert task_is_offline(task_dir) is True


def test_task_is_offline_detects_legacy_allow_internet_false(tmp_path) -> None:
    # The spelling used by the 8 tasks in 086f6140.
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        allow_internet = false
        """,
    )
    assert task_is_offline(task_dir) is True


def test_network_mode_public_wins_over_legacy_allow_internet(tmp_path) -> None:
    # Harbor ignores allow_internet when network_mode is explicit, so an
    # explicit "public" must not be reported as offline.
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        network_mode = "public"
        allow_internet = false
        """,
    )
    assert task_is_offline(task_dir) is False


def test_task_is_offline_false_for_public_task(tmp_path) -> None:
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        """,
    )
    assert task_is_offline(task_dir) is False


# --- the cloud default does NOT branch on offline-ness ----------------------


def test_offline_cpu_task_keeps_the_cheap_default(tmp_path) -> None:
    # Being offline is not on its own a reason to move a task off Daytona --
    # see the module docstring for the experiment that falsified that theory.
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        allow_internet = false
        """,
    )
    assert (
        _default_cloud_environment_for_task(task_dir, override_gpus=None)
        == EnvironmentType.DAYTONA
    )


def test_online_cpu_task_keeps_the_cheap_default(tmp_path) -> None:
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        """,
    )
    assert (
        _default_cloud_environment_for_task(task_dir, override_gpus=None)
        == EnvironmentType.DAYTONA
    )


def test_gpu_task_still_routes_to_modal(tmp_path) -> None:
    # The GPU rule is untouched by the revert.
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        gpus = 1
        """,
    )
    assert (
        _default_cloud_environment_for_task(task_dir, override_gpus=None)
        == EnvironmentType.MODAL
    )
