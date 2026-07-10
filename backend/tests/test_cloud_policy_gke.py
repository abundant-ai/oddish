"""cloud_policy derives its allowed environments from the runtime backend
registry, so enabling GKE via settings must surface it in
``ALLOWED_CLOUD_ENVIRONMENTS`` with no edit to cloud_policy itself.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

_BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _allowed_environment_values(*, cluster: str | None) -> set[str]:
    """Import cloud_policy in a fresh interpreter with a given GKE cluster
    setting so its import-time ALLOWED_CLOUD_ENVIRONMENTS reflects that config."""
    code = (
        "import cloud_policy;"
        "print(','.join(sorted(e.value for e in "
        "cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS)))"
    )
    env = {k: v for k, v in os.environ.items() if k != "ODDISH_GKE_CLUSTER_NAME"}
    if cluster is not None:
        env["ODDISH_GKE_CLUSTER_NAME"] = cluster
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    assert result.returncode == 0, result.stderr
    return set(result.stdout.strip().split(","))


def test_gke_in_allowed_cloud_environments_when_configured() -> None:
    assert "gke" in _allowed_environment_values(cluster="oddish-tpu")


def test_gke_absent_from_allowed_cloud_environments_without_config() -> None:
    assert "gke" not in _allowed_environment_values(cluster=None)
