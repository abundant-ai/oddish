"""The two Thunder handoff policies and their shared registry."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.config import settings  # noqa: E402
from oddish.core.harbor_artifacts import THUNDER_CAPACITY_UNAVAILABLE_CODE  # noqa: E402
from oddish.db import TrialStatus  # noqa: E402
from oddish.workers.queue import thunder_fallback as policy  # noqa: E402


@pytest.fixture(autouse=True)
def _fallback_settings(monkeypatch):
    monkeypatch.setattr(settings, "thunder_fallback_provider", "modal")
    monkeypatch.setattr(settings, "thunder_capacity_fallback", True)
    monkeypatch.setattr(settings, "thunder_max_failed_attempts", 2)


def test_registry_maps_each_reason_to_its_settlement_shape():
    capacity = policy.thunder_handoff_for_reason(THUNDER_CAPACITY_UNAVAILABLE_CODE)
    budget = policy.thunder_handoff_for_reason(
        policy.THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON
    )
    assert capacity is not None and capacity.settled is False
    assert budget is not None and budget.settled is True
    assert policy.thunder_handoff_for_reason("something_else") is None
    assert policy.thunder_handoff_for_reason(None) is None


def test_registry_gates_follow_their_settings(monkeypatch):
    capacity = policy.THUNDER_HANDOFFS[THUNDER_CAPACITY_UNAVAILABLE_CODE]
    budget = policy.THUNDER_HANDOFFS[policy.THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON]
    assert capacity.enabled() and budget.enabled()
    monkeypatch.setattr(settings, "thunder_capacity_fallback", False)
    monkeypatch.setattr(settings, "thunder_max_failed_attempts", 0)
    assert not capacity.enabled()
    assert not budget.enabled()


@pytest.mark.parametrize(
    "environment,status,attempts,expected",
    [
        ("thunder", TrialStatus.RETRYING, 2, "modal"),
        ("Thunder ", "RETRYING", 3, "modal"),
        # One failure is within budget.
        ("thunder", TrialStatus.RETRYING, 1, None),
        # No retry to move: terminal states and a still-running trial.
        ("thunder", TrialStatus.FAILED, 5, None),
        ("thunder", TrialStatus.SUCCESS, 5, None),
        ("thunder", TrialStatus.RUNNING, 5, None),
        # Not a Thunder trial (already moved, or never there).
        ("modal", TrialStatus.RETRYING, 5, None),
        (None, TrialStatus.RETRYING, 5, None),
        ("thunder", TrialStatus.RETRYING, None, None),
    ],
)
def test_attempt_budget_policy(environment, status, attempts, expected):
    assert (
        policy.thunder_attempt_budget_fallback_provider(
            environment, status=status, attempts=attempts
        )
        == expected
    )


def test_attempt_budget_policy_is_off_at_zero(monkeypatch):
    monkeypatch.setattr(settings, "thunder_max_failed_attempts", 0)
    assert (
        policy.thunder_attempt_budget_fallback_provider(
            "thunder", status=TrialStatus.RETRYING, attempts=50
        )
        is None
    )


def test_capacity_policy_needs_the_exact_provider_code(monkeypatch):
    capacity_miss = SimpleNamespace(
        provider_error_code=THUNDER_CAPACITY_UNAVAILABLE_CODE
    )
    assert (
        policy.thunder_capacity_fallback_provider("thunder", capacity_miss) == "modal"
    )
    assert (
        policy.thunder_capacity_fallback_provider(
            "thunder", SimpleNamespace(provider_error_code="authentication_error")
        )
        is None
    )
    assert policy.thunder_capacity_fallback_provider("modal", capacity_miss) is None
    assert policy.thunder_capacity_fallback_provider("thunder", None) is None
    monkeypatch.setattr(settings, "thunder_capacity_fallback", False)
    assert policy.thunder_capacity_fallback_provider("thunder", capacity_miss) is None


def test_emit_event_logs_ids_but_keeps_metric_attributes_bounded(monkeypatch):
    printed: list[str] = []
    recorded: list[dict] = []
    monkeypatch.setattr(
        policy.console, "print", lambda message: printed.append(message)
    )
    monkeypatch.setattr(
        policy, "record_thunder_handoff", lambda **kwargs: recorded.append(kwargs)
    )

    policy.emit_thunder_handoff_event(
        "rejected",
        job_id="wj-1",
        trial_id=None,
        target="modal",
        handoff=policy.THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON,
        reason="worker_ownership_changed",
    )

    assert printed == [
        "metric=thunder_handoff outcome=rejected job_id=wj-1 trial_id=unknown "
        "target=modal handoff=thunder_attempt_budget_exhausted "
        "reason=worker_ownership_changed"
    ]
    assert recorded == [
        {
            "outcome": "rejected",
            "target_environment": "modal",
            "handoff": policy.THUNDER_ATTEMPT_BUDGET_EXHAUSTED_REASON,
        }
    ]
