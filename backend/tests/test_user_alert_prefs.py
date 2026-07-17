"""build_alerts honouring per-user alert preferences (toggles + cutoffs)."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import ProgrammingError

import user_alert_prefs
from slack_alert_settings import DEFAULT_ALERT_SETTINGS
from slack_notifications import (
    AlertCandidates,
    ExperimentCandidate,
    FailedExperiment,
    FailedTrial,
    QaFailure,
    TrialSpend,
    build_alerts,
)
from user_alert_prefs import UserAlertPrefs

NOW = datetime(2026, 7, 17, 12, 0, tzinfo=timezone.utc)
CUTOFF = NOW - timedelta(hours=2)
OWNER = "owner@example.com"


def _trial(trial_id, cost, *, experiment_id="e1", model="m"):
    return TrialSpend(
        id=trial_id,
        name=f"{trial_id} title",
        task_id="task/1",
        experiment_id=experiment_id,
        model=model,
        finished_at=NOW,
        cost_usd=cost,
    )


def _experiment(**kw):
    return ExperimentCandidate(
        id=kw.get("id", "e1"),
        name=kw.get("name", "Exp"),
        owner=kw.get("owner", "Ada"),
        active_trials=kw.get("active_trials", 0),
        owner_email=kw.get("owner_email", OWNER),
    )


def _build(candidates, prefs=None):
    return build_alerts(
        candidates,
        settings=DEFAULT_ALERT_SETTINGS,
        recent_cutoff=CUTOFF,
        dashboard_url="https://www.oddish.app",
        user_prefs={OWNER: prefs} if prefs is not None else None,
    )


def _keys(alerts):
    return [a.key for a in alerts]


# --- toggles ---------------------------------------------------------------


def test_cost_milestone_toggle_off_drops_only_the_milestone():
    candidates = AlertCandidates(
        experiments=[_experiment()],
        trials=[_trial("t", 1500)],  # $1500 > $1000 milestone and > $200 floor
    )
    on = _build(candidates)
    off = _build(candidates, UserAlertPrefs(cost_milestone_enabled=False))
    assert "experiment:e1:1000" in _keys(on)
    assert "experiment:e1:1000" not in _keys(off)
    # The expensive-trial DM for the same spend is a different toggle, untouched.
    assert "trial:t" in _keys(off)


def test_expensive_trial_toggle_off_drops_the_dm_but_not_the_escalation():
    candidates = AlertCandidates(
        experiments=[_experiment()],
        trials=[_trial("whale", 1500)],  # > $1000 escalation
    )
    off = _build(candidates, UserAlertPrefs(expensive_trial_enabled=False))
    # No owner DM, but the shared in-channel escalation still fires.
    assert "trial:whale" not in _keys(off)
    assert "trial-escalation:whale" in _keys(off)
    escalation = next(a for a in off if a.key == "trial-escalation:whale")
    assert escalation.recipient_email is None and not escalation.dm_only


def test_each_failure_toggle_off_drops_its_own_dm():
    exp_failed = AlertCandidates(
        failed_experiments=[
            FailedExperiment("e1", "Exp", "Ada", 5, 5, owner_email=OWNER)
        ]
    )
    assert _keys(_build(exp_failed)) == ["experiment-failed:e1"]
    assert _build(exp_failed, UserAlertPrefs(experiment_failed_enabled=False)) == []

    trial_failed = AlertCandidates(
        failed_trials=[
            FailedTrial("t", "task/1", None, "Exp", "Ada", owner_email=OWNER)
        ]
    )
    assert _keys(_build(trial_failed)) == ["trial-failed:task/1"]
    assert _build(trial_failed, UserAlertPrefs(trial_failed_enabled=False)) == []

    qa_failed = AlertCandidates(
        qa_failures=[QaFailure("task/1", "Task", None, "bad", owner_email=OWNER)]
    )
    assert _keys(_build(qa_failed)) == ["qa-failed:task/1"]
    assert _build(qa_failed, UserAlertPrefs(qa_failed_enabled=False)) == []


def test_one_owners_mute_does_not_touch_another_owner():
    candidates = AlertCandidates(
        experiments=[
            _experiment(id="e1", owner_email=OWNER),
            _experiment(id="e2", owner_email="other@example.com"),
        ],
        trials=[
            _trial("a", 1500, experiment_id="e1"),
            _trial("b", 1500, experiment_id="e2"),
        ],
    )
    alerts = _build(candidates, UserAlertPrefs(expensive_trial_enabled=False))
    keys = _keys(alerts)
    assert "trial:a" not in keys  # muted owner
    assert "trial:b" in keys  # untouched owner


# --- cutoffs ---------------------------------------------------------------


def test_personal_trial_floor_below_global_alerts_a_cheaper_trial():
    # $120 is under the $200 global floor, so nobody is alerted by default.
    candidates = AlertCandidates(experiments=[_experiment()], trials=[_trial("t", 120)])
    assert "trial:t" not in _keys(_build(candidates))
    assert "trial:t" in _keys(_build(candidates, UserAlertPrefs(trial_ping_usd=100)))


def test_personal_trial_floor_above_a_trial_suppresses_the_dm_only():
    # $600 clears the $200 global floor but not this owner's $5000 floor; the
    # trial is also over the $1000 escalation, which must still fire.
    candidates = AlertCandidates(experiments=[_experiment()], trials=[_trial("t", 600)])
    alerts = _build(candidates, UserAlertPrefs(trial_ping_usd=5000))
    assert "trial:t" not in _keys(alerts)


def test_personal_trial_floor_above_escalation_still_lets_the_channel_fire():
    candidates = AlertCandidates(
        experiments=[_experiment()], trials=[_trial("t", 1500)]
    )
    alerts = _build(candidates, UserAlertPrefs(trial_ping_usd=5000))
    assert "trial:t" not in _keys(alerts)  # no personal DM
    assert "trial-escalation:t" in _keys(alerts)  # shared oversight survives


def test_personal_milestone_cutoff_overrides_both_first_and_repeat():
    # $600 of spend: nothing at the $1000 default, three steps at a $200 cutoff.
    candidates = AlertCandidates(
        experiments=[_experiment()],
        trials=[_trial("t", 600)],
    )
    assert [k for k in _keys(_build(candidates)) if k.startswith("experiment:")] == []
    lowered = _build(candidates, UserAlertPrefs(experiment_milestone_usd=200))
    assert [k for k in _keys(lowered) if k.startswith("experiment:")] == [
        "experiment:e1:200",
        "experiment:e1:400",
        "experiment:e1:600",
    ]


def test_no_prefs_matches_empty_prefs_matches_baseline():
    candidates = AlertCandidates(
        experiments=[_experiment()],
        trials=[_trial("t", 1500)],
        failed_trials=[
            FailedTrial("f", "task/9", None, "Exp", "Ada", owner_email=OWNER)
        ],
    )
    baseline = build_alerts(
        candidates,
        settings=DEFAULT_ALERT_SETTINGS,
        recent_cutoff=CUTOFF,
        dashboard_url="https://www.oddish.app",
    )
    empty = build_alerts(
        candidates,
        settings=DEFAULT_ALERT_SETTINGS,
        recent_cutoff=CUTOFF,
        dashboard_url="https://www.oddish.app",
        user_prefs={},
    )
    assert _keys(baseline) == _keys(empty)


# --- read_prefs_by_email ---------------------------------------------------


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _Session:
    def __init__(self, rows=None, raise_exc=None):
        self._rows = rows or []
        self._raise = raise_exc

    async def execute(self, _stmt):
        if self._raise is not None:
            raise self._raise
        return _Rows(self._rows)


class _Ctx:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *exc):
        return False


def _row(**kw):
    return SimpleNamespace(
        cost_milestone_enabled=kw.get("cost_milestone_enabled", True),
        expensive_trial_enabled=kw.get("expensive_trial_enabled", True),
        experiment_failed_enabled=kw.get("experiment_failed_enabled", True),
        trial_failed_enabled=kw.get("trial_failed_enabled", True),
        qa_failed_enabled=kw.get("qa_failed_enabled", True),
        experiment_milestone_usd=kw.get("experiment_milestone_usd"),
        trial_ping_usd=kw.get("trial_ping_usd"),
    )


class _UndefinedColumn(Exception):
    sqlstate = "42703"


@pytest.mark.asyncio
async def test_read_prefs_by_email_keys_by_lowercased_email(monkeypatch):
    rows = [("Owner@Example.COM", _row(expensive_trial_enabled=False))]
    monkeypatch.setattr(user_alert_prefs, "get_session", lambda: _Ctx(_Session(rows)))
    result = await user_alert_prefs.read_prefs_by_email()
    assert set(result) == {"owner@example.com"}
    assert result["owner@example.com"].expensive_trial_enabled is False


@pytest.mark.asyncio
async def test_read_prefs_by_email_fails_open_on_missing_schema(monkeypatch):
    exc = ProgrammingError("SELECT", {}, _UndefinedColumn("no column"))
    monkeypatch.setattr(
        user_alert_prefs, "get_session", lambda: _Ctx(_Session(raise_exc=exc))
    )
    # A missing table/column must not sink the alert run -- fail open to "no
    # overrides" so alerting keeps its pre-feature behaviour.
    assert await user_alert_prefs.read_prefs_by_email() == {}
