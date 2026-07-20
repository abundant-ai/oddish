"""Tests for api.services.summary_dump."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from api.services.summary_dump import validate_scope


def test_validate_scope_rejects_no_scope():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=None, task=None, experiment=None)


def test_validate_scope_rejects_two_scopes():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=["tr_a"], task="my-task", experiment=None)


def test_validate_scope_accepts_single_scope():
    validate_scope(trials=["tr_a"], task=None, experiment=None)
    validate_scope(trials=None, task="my-task", experiment=None)
    validate_scope(trials=None, task=None, experiment="exp_1")


def test_validate_scope_rejects_empty_trial_list():
    with pytest.raises(ValueError, match="exactly one"):
        validate_scope(trials=[], task=None, experiment=None)


def _candidate(trial_id: str, *, has_trajectory=True, agent="claude-code", finished_at=object()):
    return SimpleNamespace(
        id=trial_id, has_trajectory=has_trajectory, agent=agent, finished_at=finished_at,
    )


def test_filter_fetchable_keeps_only_trials_with_a_trajectory():
    from api.services.summary_dump import filter_fetchable

    rows = [
        _candidate("tr_a"),
        _candidate("tr_b", has_trajectory=False),
        _candidate("tr_c", has_trajectory=False, agent="grok-build"),
        _candidate("tr_d", has_trajectory=False, agent="grok-build", finished_at=None),
    ]
    assert [t.id for t in filter_fetchable(rows)] == ["tr_a", "tr_c"]


def test_filter_fetchable_applies_limit_after_filtering():
    from api.services.summary_dump import filter_fetchable

    rows = [_candidate("tr_a", has_trajectory=False), _candidate("tr_b"), _candidate("tr_c")]
    assert [t.id for t in filter_fetchable(rows, limit=1)] == ["tr_b"]
