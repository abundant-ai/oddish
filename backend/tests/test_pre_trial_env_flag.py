import os
from unittest import mock

import pytest


def _env_vars():
    """Reimport modal_app so ENV_VARS is rebuilt against the patched environ."""
    import importlib

    import modal_app

    return importlib.reload(modal_app).ENV_VARS


def test_pre_trial_is_enabled_by_default_in_the_deployed_image():
    """Path A writes task_versions.pre_trial, the only {pre_trial_context}
    source; with the flag off the per-org setting cannot opt in."""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("ODDISH_PRE_TRIAL_ENABLED", None)
        assert _env_vars()["ODDISH_PRE_TRIAL_ENABLED"] == "1"


def test_pre_trial_flag_is_overridable_from_the_deploy_environment():
    with mock.patch.dict(os.environ, {"ODDISH_PRE_TRIAL_ENABLED": "0"}):
        assert _env_vars()["ODDISH_PRE_TRIAL_ENABLED"] == "0"


@pytest.mark.parametrize("raw,expected", [("1", True), ("0", False)])
def test_settings_parses_the_baked_value(raw, expected):
    from oddish.config import Settings

    with mock.patch.dict(os.environ, {"ODDISH_PRE_TRIAL_ENABLED": raw}):
        assert Settings().pre_trial_enabled is expected
