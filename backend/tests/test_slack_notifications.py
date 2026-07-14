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
    UnpricedModel,
    build_alerts,
    load_alerts,
    send_alerts,
)


def _trial(
    trial_id: str,
    cost_usd: float,
    *,
    experiment_id: str = "experiment-1",
    task_id: str = "task/1",
    model: str = "model-1",
    finished_at: datetime,
) -> TrialSpend:
    return TrialSpend(
        id=trial_id,
        name=f"{trial_id} title",
        task_id=task_id,
        experiment_id=experiment_id,
        model=model,
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
    assert experiment_alert.text.splitlines() == [
        ":money_with_wings: *Expensive experiment*",
        "Title: *Exp &lt;One&gt;*",
        "Spend milestone: *$2,000.00* (current spend: *$2,001.00*)",
        "New spend (last 2h): *$2,001.00*",
        "Trials still running: 2",
        "Owner: *Pat &amp; Sam*",
        "Top agent costs:",
        "• `model-1`: *$2,001.00*",
        "<https://www.oddish.app/experiments/experiment%252F1|open experiment>",
    ]


def test_build_alerts_lists_the_top_three_agent_costs() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Experiment", "Ada", 0)],
        [
            _trial("a-1", 150, model="openrouter/opus", finished_at=now),
            _trial("a-2", 100, model="openrouter/opus", finished_at=now),
            _trial("b", 200, model="azure/fable", finished_at=now),
            _trial("c", 100, model="anthropic/sonnet", finished_at=now),
            _trial("d", 50, model="openai/gpt", finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=100,
        experiment_repeat_usd=1000,
        trial_threshold_usd=10_000,
        trial_average_multiplier=2,
    )

    assert alerts[0].text.splitlines()[6:10] == [
        "Top agent costs:",
        "• `openrouter/opus`: *$250.00*",
        "• `azure/fable`: *$200.00*",
        "• `anthropic/sonnet`: *$100.00*",
    ]


def test_build_alerts_silently_claims_milestones_reached_before_the_window() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Exp", "Ada", 0)],
        [
            _trial("old", 4000, finished_at=now - timedelta(hours=3)),
            _trial("recent", 50, finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=1000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=10_000,
        trial_average_multiplier=2,
    )

    milestone_alerts = [a for a in alerts if a.key.startswith("experiment:")]
    assert [a.key for a in milestone_alerts] == [
        "experiment:experiment-1:1000",
        "experiment:experiment-1:2000",
        "experiment:experiment-1:3000",
        "experiment:experiment-1:4000",
    ]
    assert all(a.silent for a in milestone_alerts)
    assert all(a.text for a in milestone_alerts)


def test_build_alerts_fires_only_milestones_new_spend_crosses() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Exp", "Ada", 1)],
        [
            _trial("old", 1800, finished_at=now - timedelta(hours=3)),
            _trial("recent", 1300, finished_at=now),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=1000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=10_000,
        trial_average_multiplier=2,
    )

    milestone_alerts = [a for a in alerts if a.key.startswith("experiment:")]
    silent = [a.key for a in milestone_alerts if a.silent]
    firing = [a for a in milestone_alerts if not a.silent]
    assert silent == ["experiment:experiment-1:1000"]
    assert [a.key for a in firing] == [
        "experiment:experiment-1:2000",
        "experiment:experiment-1:3000",
    ]
    assert "New spend (last 2h): *$1,300.00*" in firing[0].text


def test_build_alerts_new_experiment_fires_every_milestone() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Exp", "Ada", 3)],
        [_trial("burst", 2500, finished_at=now)],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=1000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=10_000,
        trial_average_multiplier=2,
    )

    milestone_alerts = [a for a in alerts if a.key.startswith("experiment:")]
    assert [a.key for a in milestone_alerts] == [
        "experiment:experiment-1:1000",
        "experiment:experiment-1:2000",
    ]
    assert not any(a.silent for a in milestone_alerts)


def test_build_alerts_requires_a_same_task_model_peer() -> None:
    now = datetime.now(timezone.utc)
    experiment = ExperimentCandidate("experiment-1", "Experiment", None, 0)

    alerts = build_alerts(
        [experiment],
        [
            _trial("outlier", 1000, finished_at=now),
            _trial(
                "other-model",
                1,
                model="model-2",
                finished_at=now - timedelta(days=1),
            ),
            _trial(
                "other-task",
                1,
                task_id="task/2",
                finished_at=now - timedelta(days=1),
            ),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )

    assert not any(alert.key.startswith("trial:") for alert in alerts)


def test_build_alerts_uses_same_task_model_peers() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Experiment", None, 0)],
        [
            _trial("outlier", 201, finished_at=now),
            _trial(
                "peer",
                100,
                finished_at=now - timedelta(days=1),
            ),
        ],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
    )

    assert [alert.key for alert in alerts] == ["trial:outlier:100:2"]
    assert alerts[0].text.splitlines() == [
        ":warning: *Expensive trial*",
        "Title: `outlier title`",
        "Experiment: *Experiment*",
        "Cost: *$201.00* — 2.0× the same-task/model average",
        "Model: `model-1`",
        "Author: *Unknown*",
        "<https://www.oddish.app/tasks/task%2F1|open task>",
    ]


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


def test_build_alerts_reports_unpriceable_models_once_each() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Experiment", None, 0)],
        [],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
        unpriced_models=[
            UnpricedModel(model="mystery/model-x", trial_count=3, task_id="task/9"),
        ],
    )

    assert [alert.key for alert in alerts] == ["unpriced-model:mystery/model-x"]
    text = alerts[0].text
    assert "*Unpriceable model:*" in text
    assert "`mystery/model-x`" in text
    assert "3 recent trials recorded" in text
    assert "/tasks/task%2F9|open task>" in text


def test_build_alerts_unpriceable_model_uses_singular_for_one_trial() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        [ExperimentCandidate("experiment-1", "Experiment", None, 0)],
        [],
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
        experiment_threshold_usd=2000,
        experiment_repeat_usd=1000,
        trial_threshold_usd=100,
        trial_average_multiplier=2,
        unpriced_models=[
            UnpricedModel(model="mystery/model-x", trial_count=1, task_id="task/9"),
        ],
    )

    assert "1 recent trial recorded" in alerts[0].text


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
async def test_send_alerts_claims_once_and_retries_failed_posts(
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

    async def mark_sent(*keys: str) -> None:
        sent.update(key for key in keys if key in claimed)

    async def is_pending(key: str) -> bool:
        return key in claimed and key not in sent

    async def is_sent(key: str) -> bool:
        return key in sent

    async def post(_url: str, text: str) -> None:
        posted.append(text)
        if text == "fail":
            raise RuntimeError("failed")

    monkeypatch.setattr(notifications, "_claim_alert", claim)
    monkeypatch.setattr(notifications, "_mark_alert_sent", mark_sent)
    monkeypatch.setattr(notifications, "_alert_is_pending", is_pending)
    monkeypatch.setattr(notifications, "_alert_is_sent", is_sent)
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
    assert "failed" in claimed
    assert "retry:slack:failed" in claimed
    assert "after-failure" in claimed

    await send_alerts(
        "https://hooks.slack.test",
        [SlackAlert("failed", "recovered", silent=True)],
    )

    assert posted == ["ok", "fail", "ok", "recovered"]
    assert {"failed", "retry:slack:failed"}.issubset(sent)


@pytest.mark.asyncio
async def test_send_alerts_claims_silent_baseline_without_posting(
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

    async def mark_sent(*keys: str) -> None:
        sent.update(key for key in keys if key in claimed)

    async def is_pending(key: str) -> bool:
        return key in claimed and key not in sent

    async def is_sent(key: str) -> bool:
        return key in sent

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    monkeypatch.setattr(notifications, "_claim_alert", claim)
    monkeypatch.setattr(notifications, "_mark_alert_sent", mark_sent)
    monkeypatch.setattr(notifications, "_alert_is_pending", is_pending)
    monkeypatch.setattr(notifications, "_alert_is_sent", is_sent)
    monkeypatch.setattr(notifications, "_post", post)

    await send_alerts(
        "https://hooks.slack.test",
        [
            SlackAlert("experiment:1:1000", "", silent=True),
            SlackAlert("experiment:1:2000", "fire"),
        ],
    )

    assert posted == ["fire"]
    assert sent == {"experiment:1:1000", "experiment:1:2000"}
    assert claimed == {"experiment:1:1000", "experiment:1:2000"}


@pytest.mark.asyncio
async def test_send_alerts_closes_retry_after_partial_success_without_reposting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    alert_key = "experiment:1:1000"
    retry_key = f"retry:slack:{alert_key}"
    claimed = {alert_key, retry_key}
    sent = {alert_key}
    posted: list[str] = []

    async def claim(key: str) -> bool:
        if key in claimed:
            return False
        claimed.add(key)
        return True

    async def mark_sent(*keys: str) -> None:
        sent.update(key for key in keys if key in claimed)

    async def is_pending(key: str) -> bool:
        return key in claimed and key not in sent

    async def is_sent(key: str) -> bool:
        return key in sent

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    monkeypatch.setattr(notifications, "_claim_alert", claim)
    monkeypatch.setattr(notifications, "_mark_alert_sent", mark_sent)
    monkeypatch.setattr(notifications, "_alert_is_pending", is_pending)
    monkeypatch.setattr(notifications, "_alert_is_sent", is_sent)
    monkeypatch.setattr(notifications, "_post", post)

    await send_alerts(
        "https://hooks.slack.test",
        [SlackAlert(alert_key, "already delivered", silent=True)],
    )

    assert posted == []
    assert retry_key in sent


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
                    provider="openai",
                    model="gpt-5.3",
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
                # Unpriceable: real tokens, no native cost, and no rate resolves
                # -> settles to $0 and should raise an unpriced-model alert.
                TrialModel(
                    id=f"{task_id}-unpriced",
                    name="unpriced",
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="made-up",
                    model="made-up/no-such-model-9000",
                    queue_key="test",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    is_probe=False,
                    output_tokens=1_000,
                    finished_at=now,
                ),
            ]
        )

    try:
        alerts = await load_alerts(now)
        # The gpt-5.3 outlier has NULL cost + tokens too, but gpt-5.3 IS priced,
        # so it produces a token estimate and never appears as unpriceable --
        # the token estimate does not falsely trigger the alert.
        assert [alert.key for alert in alerts] == [
            f"experiment:{experiment_id}:100",
            f"trial:{task_id}-outlier:100:2",
            "unpriced-model:made-up/no-such-model-9000",
        ]
        assert "Trials still running: 0" in alerts[0].text
        assert "New spend (last 2h):" in alerts[0].text
        assert "Top agent costs:" in alerts[0].text
        assert "Title: `outlier`" in alerts[1].text
        assert "Experiment: *Slack expense test*" in alerts[1].text
        assert "Author: *Unknown*" in alerts[1].text
        assert "same-task/model average" in alerts[1].text
        assert "*Unpriceable model:*" in alerts[2].text
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
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
async def test_load_alerts_reports_unpriced_model_without_candidate_experiment() -> (
    None
):
    # A soft-deleted experiment yields no expensive-experiment candidate, so
    # load_alerts must not early-return before the unpriceable-model scan.
    suffix = uuid4().hex[:12]
    experiment_id = f"slack-exp-{suffix}"
    task_id = f"slack-task-{suffix}"
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        session.add(
            ExperimentModel(id=experiment_id, name="Deleted exp", deleted_at=now)
        )
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                user="test",
                task_path="/tmp/test",
            )
        )
        session.add(
            TrialModel(
                id=f"{task_id}-unpriced",
                name="unpriced",
                task_id=task_id,
                experiment_id=experiment_id,
                agent="claude-code",
                provider="made-up",
                model="made-up/no-such-model-9001",
                queue_key="test",
                status=TrialStatus.SUCCESS,
                origin=TrialOrigin.ODDISH,
                is_probe=False,
                output_tokens=1_000,
                finished_at=now,
            )
        )

    try:
        alerts = await load_alerts(now)
        assert "unpriced-model:made-up/no-such-model-9001" in {
            alert.key for alert in alerts
        }
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id == task_id)
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
