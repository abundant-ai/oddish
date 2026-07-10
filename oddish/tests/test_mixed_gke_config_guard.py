"""Per-config environment=gke without a GKE submission-level environment would
run trials on GKE while the harbor stamp resolved the lean default pin -- the
one mismatch direction that produces a broken image. Rejected loudly at submit.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException
from harbor.models.environment_type import EnvironmentType

from oddish.core.endpoints.sweep import _reject_mixed_gke_configs
from oddish.schemas import AgentModelPair


def _configs(*envs):
    return [
        AgentModelPair(agent="oracle", environment=e)
        if e
        else AgentModelPair(agent="oracle")
        for e in envs
    ]


def test_config_gke_under_non_gke_submission_rejected():
    with pytest.raises(HTTPException) as exc:
        _reject_mixed_gke_configs(_configs(EnvironmentType.GKE), EnvironmentType.MODAL)
    assert exc.value.status_code == 422
    assert "environment" in str(exc.value.detail)


def test_config_gke_under_none_effective_rejected():
    with pytest.raises(HTTPException):
        _reject_mixed_gke_configs(_configs(EnvironmentType.GKE), None)


def test_config_gke_under_gke_submission_allowed():
    _reject_mixed_gke_configs(_configs(EnvironmentType.GKE), EnvironmentType.GKE)


def test_non_gke_configs_under_gke_submission_allowed():
    # The gke variant image is a superset (full default dependency set plus the
    # gke extras), so this direction runs correctly and stays permitted.
    _reject_mixed_gke_configs(_configs(EnvironmentType.MODAL), EnvironmentType.GKE)


def test_plain_configs_never_rejected():
    _reject_mixed_gke_configs(_configs(None, None), EnvironmentType.MODAL)
    _reject_mixed_gke_configs([], None)
