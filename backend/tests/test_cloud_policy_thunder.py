"""Hosted Thunder availability and the GPU default follow its opt-in
runtime registration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_ODDISH_SRC = _BACKEND_ROOT.parent / "oddish" / "src"
_THUNDER_ENV_NAMES = {
    "ODDISH_THUNDER_CAPACITY_FALLBACK",
    "ODDISH_THUNDER_ENABLED",
    "ODDISH_THUNDER_FALLBACK_PROVIDER",
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
        "print(cloud_policy.get_default_cloud_environment(gpu_submission).value);"
        "task_gpu_submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'requires_gpu':True,'gpu_types':['H100']});"
        "print(cloud_policy.get_default_cloud_environment(task_gpu_submission).value);"
        "registry_submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'requires_gpu':True,'gpu_types':['H100'],"
        "'registry_auth':[{'registry':'ghcr.io','username':'u','token':'t'}]});"
        "print(cloud_policy.get_default_cloud_environment(registry_submission).value);"
        "untyped_submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'requires_gpu':True});"
        "print(cloud_policy.get_default_cloud_environment(untyped_submission).value);"
        "kwarg_submission=TaskSweepSubmission.model_validate({"
        "'task_id':'t','configs':[{'agent':'nop','n_trials':1}],"
        "'requires_gpu':True,'gpu_types':['L4'],"
        "'harbor':{'environment':{'kwargs':{'gpu_type':'H100'}}}});"
        "print(cloud_policy.get_default_cloud_environment(kwarg_submission).value)"
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
    allowed, decision, *defaults = result.stdout.strip().splitlines()
    return set(allowed.split(",")), decision, tuple(defaults)


def test_thunder_is_accepted_only_when_enabled() -> None:
    disabled_allowed, disabled_decision, disabled_defaults = _cloud_policy_values(
        thunder_enabled=False
    )
    enabled_allowed, enabled_decision, enabled_defaults = _cloud_policy_values(
        thunder_enabled=True
    )

    assert "thunder" not in disabled_allowed
    assert "thunder" in enabled_allowed
    assert disabled_decision == "rejected:400"
    assert enabled_decision == "accepted"
    # (override_gpus naming no type, task GPUs naming H100, H100 plus a
    # private registry, task GPUs naming no type, an exact ``gpu_type``
    # environment kwarg naming H100 over a list naming L4): a Thunder-less
    # deployment negotiates Modal instead of rejecting. With Thunder, only the
    # request naming one accelerator Thunder offers lands there; a
    # private-registry pull stays on Modal, a request naming no type goes to
    # Modal because Harbor's Thunder environment would reject it at launch,
    # and the exact kwarg wins over the list as it does at launch.
    assert disabled_defaults == ("modal", "modal", "modal", "modal", "modal")
    assert enabled_defaults == ("modal", "thunder", "modal", "modal", "thunder")
