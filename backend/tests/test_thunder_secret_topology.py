from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import modal_app
import thunder_readiness
import worker.functions as worker_functions


def test_disabled_deploy_has_no_thunder_secret_dependency() -> None:
    assert modal_app.THUNDER_SECRET_PLAN == []
    assert modal_app.thunder_worker_secrets == []


def test_thunder_public_image_env_excludes_credentials() -> None:
    assert modal_app._THUNDER_PUBLIC_ENV_NAMES == {
        "ODDISH_THUNDER_ENABLED",
        "ODDISH_THUNDER_MAX_CAPACITY",
        "ODDISH_THUNDER_SECRET_NAME",
    }
    assert "TNR_API_URL" not in modal_app.ENV_VARS
    assert "TNR_API_TOKEN" not in modal_app.ENV_VARS


def test_enabled_import_builds_one_worker_only_secret() -> None:
    code = """
import json
import modal_app
import worker.functions as worker_functions
print(json.dumps({
    "plan": modal_app.THUNDER_SECRET_PLAN,
    "worker_count": len(modal_app.thunder_worker_secrets),
    "base_overlap": any(
        secret in modal_app.runtime_secrets
        for secret in modal_app.thunder_worker_secrets
    ),
    "thunder_lane_has_secret": all(
        secret in worker_functions.thunder_trial_worker_secrets
        for secret in modal_app.thunder_worker_secrets
    ),
    "generic_has_secret": any(
        secret in worker_functions.trial_worker_secrets
        for secret in modal_app.thunder_worker_secrets
    ),
    "capacity": modal_app.ENV_VARS["ODDISH_THUNDER_MAX_CAPACITY"],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(modal_app.__file__).parent,
        env={
            **os.environ,
            "ODDISH_THUNDER_ENABLED": "true",
            "ODDISH_THUNDER_SECRET_NAME": "test-thunder",
            "ODDISH_THUNDER_MAX_CAPACITY": "16",
            "ODDISH_SAURON_AWS_SECRET_NAME": "",
        },
        capture_output=True,
        text=True,
        check=True,
    )

    assert json.loads(result.stdout.splitlines()[-1]) == {
        "plan": ["test-thunder"],
        "worker_count": 1,
        "base_overlap": False,
        "thunder_lane_has_secret": True,
        "generic_has_secret": False,
        "capacity": "16",
    }


def test_only_thunder_lane_workers_receive_thunder_secret() -> None:
    assert worker_functions.trial_worker_secrets == [*modal_app.runtime_secrets]
    assert worker_functions.thunder_trial_worker_secrets == [
        *modal_app.runtime_secrets,
        *modal_app.thunder_worker_secrets,
    ]
    assert all(
        secret not in worker_functions.ec2_trial_worker_secrets
        for secret in modal_app.thunder_worker_secrets
    )
    assert all(
        secret not in worker_functions.reconciler_secrets
        for secret in modal_app.thunder_worker_secrets
    )


def test_worker_readiness_validates_dependencies_without_returning_credentials(
    monkeypatch,
) -> None:
    monkeypatch.setenv("TNR_API_URL", "https://thunder.invalid")
    monkeypatch.setenv("TNR_API_TOKEN", "do-not-return")
    monkeypatch.setitem(sys.modules, "asyncssh", ModuleType("asyncssh"))
    versions = {
        "thunder-sandbox": "0.4.0",
        "aiohttp": "3.12.0",
        "asyncssh": "2.21.0",
        "cryptography": "45.0.0",
    }
    monkeypatch.setattr(thunder_readiness, "version", versions.__getitem__)

    result = thunder_readiness.check_thunder_worker.get_raw_f()()

    assert result["thunder_sandbox"] == "0.4.0"
    assert result["api_url_resolved"] is True
    assert result["api_token_resolved"] is True
    assert "https://thunder.invalid" not in result.values()
    assert "do-not-return" not in result.values()
