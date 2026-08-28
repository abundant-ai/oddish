"""Hosted Thunder availability follows its opt-in runtime registration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ODDISH_SRC = _BACKEND_ROOT.parent / "oddish" / "src"
_THUNDER_ENV_NAMES = {
    "ODDISH_THUNDER_ENABLED",
    "ODDISH_THUNDER_MAX_CAPACITY",
    "ODDISH_THUNDER_SECRET_NAME",
    "TNR_API_URL",
    "TNR_API_TOKEN",
}


def _cloud_policy_values(*, thunder_enabled: bool) -> tuple[set[str], str, str]:
    code = (
        "import cloud_policy;"
        "from fastapi import HTTPException;"
        "from oddish.core.sweeps import build_trial_specs_from_sweep;"
        "from oddish.schemas import TaskSweepSubmission;"
        "print(','.join(sorted(e.value for e in "
        "cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS)));"
        "submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'environment':'thunder'});"
        "decision='accepted';"
        "\ntry:\n"
        " build_trial_specs_from_sweep(submission,allowed_environments="
        "cloud_policy.ALLOWED_CLOUD_ENVIRONMENTS)\n"
        "except HTTPException as exc:\n decision=f'rejected:{exc.status_code}'\n"
        "print(decision);"
        "gpu_submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'harbor':{'environment':{'override_gpus':1}}});"
        "print(cloud_policy.get_default_cloud_environment(gpu_submission).value)"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in _THUNDER_ENV_NAMES
    }
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_ODDISH_SRC), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    if thunder_enabled:
        env.update(
            {
                "ODDISH_THUNDER_ENABLED": "true",
                "ODDISH_THUNDER_MAX_CAPACITY": "16",
            }
        )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(_BACKEND_ROOT),
    )
    assert result.returncode == 0, result.stderr
    allowed, decision, gpu_default = result.stdout.strip().splitlines()
    return set(allowed.split(",")), decision, gpu_default


def test_thunder_is_accepted_only_when_enabled() -> None:
    disabled_allowed, disabled_decision, disabled_gpu_default = _cloud_policy_values(
        thunder_enabled=False
    )
    enabled_allowed, enabled_decision, enabled_gpu_default = _cloud_policy_values(
        thunder_enabled=True
    )

    assert "thunder" not in disabled_allowed
    assert "thunder" in enabled_allowed
    assert disabled_decision == "rejected:400"
    assert enabled_decision == "accepted"
    assert disabled_gpu_default == "modal"
    assert enabled_gpu_default == "modal"
