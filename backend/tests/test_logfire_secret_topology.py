"""Logfire credentials stay isolated from the broad production secret."""

import modal_app
import modal_runtime
import worker.functions as worker_functions


def test_logfire_uses_a_dedicated_runtime_secret() -> None:
    assert modal_runtime.RUNTIME_SECRET_NAME == "oddish-prod"
    assert modal_runtime.LOGFIRE_SECRET_NAME == "oddish-logfire"
    assert modal_runtime.logfire_secret is not modal_runtime.runtime_secret
    assert modal_app.runtime_secrets[:2] == [
        modal_runtime.runtime_secret,
        modal_runtime.logfire_secret,
    ]
    assert modal_runtime.LOGFIRE_SECRET_NAME in modal_app._broad_runtime_secret_names


def test_logfire_secret_reaches_api_dispatcher_and_workers() -> None:
    assert modal_runtime.logfire_secret in modal_app.runtime_secrets
    assert modal_runtime.logfire_secret in worker_functions.trial_worker_secrets
    assert modal_runtime.logfire_secret in worker_functions.ec2_trial_worker_secrets
    assert modal_runtime.logfire_secret in worker_functions.thunder_trial_worker_secrets
    assert modal_runtime.logfire_secret in worker_functions.reconciler_secrets
