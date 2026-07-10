from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import slack_notifications as notifications
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)
from slack_notifications import (
    ExperimentCandidate,
    SlackAlert,
    TrialSpend,
    build_alerts,
    load_alerts,
    send_alerts,
)


def _trial(
    trial_id: str,
    cost_usd: float,
    *,
    experiment_id: str = "experiment-1",
    finished_at: datetime,
) -> TrialSpend:
    return TrialSpend(
        id=trial_id,
        task_id="task/1",
        experiment_id=experiment_id,
        finished_at=finished_at,
        cost_usd=cost_usd,
    )


@pytest.mark.parametrize(
    ("total_cost", "repeat_interval", "expected"),
    [
        (999, 1000, []),
        (1000, 1000, [1000]),
        (3000, 1000, [1000, 2000, 3000]),
        (3000, 0, [1000]),
    ],
)
def test_experiment_milestones(
    total_cost: float,
    repeat_interval: float,
    expected: list[float],
) -> None:
    assert (
        notifications._experiment_milestones(total_cost, 1000, repeat_interval)
        == expected
    )


def test_build_alerts_reports_each_expense_milestone() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [
            ExperimentCandidate(
                id="experiment/1",
                name="Exp <One>",
                owner="Pat & Sam",
                active_trials=2,
            )
        ],
        [
            _trial("trial-1", 1100, experiment_id="experiment/1", finished_at=now),
            _trial("trial-2", 901, experiment_id="experiment/1", finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=1000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=10_000,
        trial_average_multiplier=2,
    )

    assert [alert.key for alert in alerts] == [
        "experiment:experiment/1:1000",
        "experiment:experiment/1:2000",
    ]
    experiment_alert = alerts[1]
    assert "*Exp &lt;One&gt;*" in experiment_alert.text
    assert "*$2,000.00* spend milestone" in experiment_alert.text
    assert "(now *$2,001.00*)" in experiment_alert.text
    assert "2 trials still running" in experiment_alert.text
    assert "owner: *Pat &amp; Sam*" in experiment_alert.text
    assert "/experiments/experiment%252F1|open experiment>" in experiment_alert.text


def test_build_alerts_reports_first_trial_only_above_floor() -> None:
    now = datetime.now(timezone.utc)
    experiment = ExperimentCandidate("experiment-1", "Experiment", None, 0)

    below = build_alerts(
        [experiment],
        [_trial("trial-100", 100, finished_at=now)],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )
    above = build_alerts(
        [experiment],
        [_trial("trial-101", 101, finished_at=now)],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )

    assert not any(alert.key.startswith("trial:") for alert in below)
    assert [alert.key for alert in above] == ["trial:trial-101:100:2"]
    assert "first trial" in above[0].text


def test_build_alerts_keeps_first_trial_rule_when_multiple_finish_between_polls() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Experiment", None, 0)],
        [
            _trial("first", 101, finished_at=now - timedelta(minutes=1)),
            _trial("second", 1000, finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )

    assert "trial:first:100:2" in [alert.key for alert in alerts]


def test_build_alerts_requires_more_than_double_other_trial_average() -> None:
    now = datetime.now(timezone.utc)
    experiment = ExperimentCandidate("experiment-1", "Experiment", None, 0)
    exactly_double = build_alerts(
        [experiment],
        [
            _trial("baseline", 100, finished_at=now - timedelta(days=1)),
            _trial("exactly-double", 200, finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )
    over_double = build_alerts(
        [experiment],
        [
            _trial("baseline", 100, finished_at=now - timedelta(days=1)),
            _trial("over-double", 201, finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )

    assert not any(alert.key.startswith("trial:") for alert in exactly_double)
    assert [alert.key for alert in over_double] == ["trial:over-double:100:2"]


def test_build_alerts_ignores_old_trials() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Experiment", None, 0)],
        [_trial("old", 500, finished_at=now - timedelta(hours=3))],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )

    assert alerts == []


@pytest.mark.asyncio
async def test_send_alerts_claims_once_and_releases_failed_posts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed: set[str] = set()
    sent: set[str] = set()
    posted: list[str] = []

    async def claim(key: str) -> bool:
        if key in claimed:
            return False
        claimed.add(key)
        return True

    async def mark_sent(key: str) -> None:
        sent.add(key)

    async def release(key: str) -> None:
        claimed.remove(key)

    async def post(_url: str, text: str) -> None:
        posted.append(text)
        if text == "fail":
            raise RuntimeError("failed")

    monkeypatch.setattr(notifications, "_claim_alert", claim)
    monkeypatch.setattr(notifications, "_mark_alert_sent", mark_sent)
    monkeypatch.setattr(notifications, "_release_alert", release)
    monkeypatch.setattr(notifications, "_post", post)

    await send_alerts("https://hooks.slack.test", [SlackAlert("sent", "ok")])
    await send_alerts("https://hooks.slack.test", [SlackAlert("sent", "ok")])

    await send_alerts(
        "https://hooks.slack.test",
        [
            SlackAlert("failed", "fail"),
            SlackAlert("after-failure", "ok"),
        ],
    )

    assert posted == ["ok", "fail", "ok"]
    assert sent == {"sent", "after-failure"}
    assert "failed" not in claimed
    assert "after-failure" in claimed


@pytest.mark.asyncio
async def test_load_alerts_uses_settled_trial_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex[:12]
    experiment_id = f"slack-exp-{suffix}"
    task_id = f"slack-task-{suffix}"
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("ODDISH_SLACK_EXPENSIVE_EXPERIMENT_USD", "100")
    monkeypatch.setenv("ODDISH_SLACK_EXPERIMENT_REPEAT_USD", "1000")
    monkeypatch.setenv("ODDISH_SLACK_EXPENSIVE_TRIAL_USD", "100")
    monkeypatch.setenv("ODDISH_SLACK_TRIAL_AVERAGE_MULTIPLIER", "2")

    async with get_session() as session:
        session.add(ExperimentModel(id=experiment_id, name="Slack expense test"))
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="test",
                task_path="/tmp/test",
            )
        )
        session.add_all(
            [
                TrialModel(
                    id=f"{task_id}-baseline",
                    name="baseline",
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key="test",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    is_probe=False,
                    cost_usd=50,
                    finished_at=now - timedelta(days=1),
                ),
                TrialModel(
                    id=f"{task_id}-outlier",
                    name="outlier",
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="openai",
                    model="gpt-5.3",
                    queue_key="test",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    is_probe=False,
                    output_tokens=10_000_000,
                    finished_at=now,
                    deleted_at=now,
                ),
                TrialModel(
                    id=f"{task_id}-deleted-running",
                    name="deleted running",
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key="test",
                    status=TrialStatus.RUNNING,
                    origin=TrialOrigin.ODDISH,
                    is_probe=False,
                    cost_usd=3000,
                    deleted_at=now,
                ),
            ]
        )

    try:
        alerts = await load_alerts(now)
        assert [alert.key for alert in alerts] == [
            f"experiment:{experiment_id}:100",
            f"trial:{task_id}-outlier:100:2",
        ]
        assert "finished" in alerts[0].text
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(
                    TrialModel.experiment_id == experiment_id
                )
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id == experiment_id
                )
            )


@pytest.mark.asyncio
async def test_database_alert_claim_is_durable() -> None:
    alert_key = f"test-alert-{uuid4().hex}"

    try:
        assert await notifications._claim_alert(alert_key)
        assert not await notifications._claim_alert(alert_key)
        await notifications._release_alert(alert_key)
        assert await notifications._claim_alert(alert_key)
        await notifications._mark_alert_sent(alert_key)
        await notifications._release_alert(alert_key)
        assert not await notifications._claim_alert(alert_key)
    finally:
        async with get_session() as session:
            await session.execute(
                notifications.SlackExpenseAlertModel.__table__.delete().where(
                    notifications.SlackExpenseAlertModel.alert_key == alert_key
                )
            )
