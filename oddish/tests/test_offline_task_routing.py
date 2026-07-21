"""Offline tasks must route to a backend with configurable egress (Modal).

Regression coverage for experiment 086f6140, where 63/72 trials of 8
``allow_internet = false`` tasks died in agent bootstrap. Routing negotiated on
GPU/TPU/private-registry only, so plain-CPU offline tasks took the cheap-first
default (Daytona). Daytona's egress is all-or-nothing: under an offline policy
the agent install loses apt and github.com outright (apt 403 via the sandbox
proxy, ``Could not resolve host: raw.githubusercontent.com``) and the trial dies
before the agent runs. Modal's egress is ``configurable`` -- a CIDR allowlist
keeps the model API reachable while the rest stays blocked -- which is why every
earlier experiment on these same tasks passed on Modal.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from harbor.models.environment_type import EnvironmentType

from oddish.cli.run import _default_cloud_environment_for_task
from oddish.runtime.routing import default_cloud_environment, select_backend
from oddish.worker.local_offline_policy import task_is_offline


def _write_task(tmp_path: Path, body: str) -> Path:
    (tmp_path / "task.toml").write_text(textwrap.dedent(body))
    return tmp_path


# --- capability negotiation -------------------------------------------------


def test_select_backend_configurable_egress_skips_daytona_picks_modal() -> None:
    assert select_backend(requires_configurable_egress=True).name == "modal"


def test_default_cloud_environment_configurable_egress_routes_to_modal() -> None:
    assert (
        default_cloud_environment(requires_configurable_egress=True)
        == EnvironmentType.MODAL
    )


def test_plain_cpu_still_routes_to_daytona() -> None:
    # The cheap-first default must not move for ordinary online tasks.
    assert select_backend().name == "daytona"
    assert default_cloud_environment() == EnvironmentType.DAYTONA


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


# --- end-to-end: the actual 086f6140 shape ----------------------------------


def test_offline_cpu_task_defaults_to_modal(tmp_path) -> None:
    # Exactly the 8 failing tasks: CPU-only, allow_internet = false.
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
        == EnvironmentType.MODAL
    )


def test_offline_task_via_network_mode_defaults_to_modal(tmp_path) -> None:
    task_dir = _write_task(
        tmp_path,
        """
        version = "1.0"
        [environment]
        docker_image = "python:3.11"
        network_mode = "no-network"
        """,
    )
    assert (
        _default_cloud_environment_for_task(task_dir, override_gpus=None)
        == EnvironmentType.MODAL
    )


def test_online_cpu_task_still_defaults_to_daytona(tmp_path) -> None:
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
