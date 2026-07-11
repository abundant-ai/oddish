from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest
import slack_notifications as notifications
from models import OrganizationModel, UserModel
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
    send_owner_emails,
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
                owner_email="owner@example.com",
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
        "Trials still running: 2",
        "Owner: *Pat &amp; Sam*",
        "Top agent costs:",
        "• `model-1`: *$2,001.00*",
        "<https://www.oddish.app/experiments/experiment%252F1|open experiment>",
    ]
    assert experiment_alert.recipient_email == "owner@example.com"
    assert experiment_alert.email_subject == "Cost alert: Exp <One> reached $2,000"
    assert experiment_alert.email_text is not None
    assert experiment_alert.email_text.splitlines() == [
        "Expensive experiment",
        "",
        "Exp <One>",
        "Spend milestone: $2,000.00",
        "Current spend: $2,001.00",
        "Trials still running: 2",
        "Owner: Pat & Sam",
        "",
        "Top agent costs:",
        "- model-1: $2,001.00",
        "",
        "Open experiment: https://www.oddish.app/experiments/experiment%252F1",
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

    assert alerts[0].text.splitlines()[5:9] == [
        "Top agent costs:",
        "• `openrouter/opus`: *$250.00*",
        "• `azure/fable`: *$200.00*",
        "• `anthropic/sonnet`: *$100.00*",
    ]


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
async def test_send_owner_emails_claims_per_recipient_and_releases_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed: set[str] = set()
    sent: set[str] = set()
    posted: list[tuple[str, str, str]] = []

    async def claim(key: str) -> bool:
        if key in claimed:
            return False
        claimed.add(key)
        return True

    async def mark_sent(key: str) -> None:
        sent.add(key)

    async def release(key: str) -> None:
        claimed.remove(key)

    async def post_email(
        _api_key: str,
        _sender: str,
        recipient: str,
        subject: str,
        body: str,
    ) -> None:
        posted.append((recipient, subject, body))
        if subject == "fail":
            raise RuntimeError("failed")

    monkeypatch.setattr(notifications, "_claim_alert", claim)
    monkeypatch.setattr(notifications, "_mark_alert_sent", mark_sent)
    monkeypatch.setattr(notifications, "_release_alert", release)
    monkeypatch.setattr(notifications, "_post_email", post_email)

    delivered = SlackAlert(
        "experiment:1:1000",
        "slack text",
        recipient_email=" Owner@Example.com ",
        email_subject="Cost alert",
        email_text="Details",
    )
    await send_owner_emails("secret", "Oddish <alerts@example.com>", [delivered])
    await send_owner_emails("secret", "Oddish <alerts@example.com>", [delivered])
    await send_owner_emails(
        "secret",
        "Oddish <alerts@example.com>",
        [
            SlackAlert(
                "trial:2:70:2",
                "slack text",
                recipient_email="owner@example.com",
                email_subject="fail",
                email_text="Details",
            ),
            SlackAlert("unpriced:model", "admin-only Slack alert"),
        ],
    )

    assert posted == [
        ("owner@example.com", "Cost alert", "Details"),
        ("owner@example.com", "fail", "Details"),
    ]
    assert sent == {"email:experiment:1:1000:owner@example.com"}
    assert "email:trial:2:70:2:owner@example.com" not in claimed


@pytest.mark.asyncio
async def test_load_alerts_uses_settled_trial_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    suffix = uuid4().hex[:12]
    experiment_id = f"slack-exp-{suffix}"
    task_id = f"slack-task-{suffix}"
    org_id = f"slack-org-{suffix}"
    user_id = f"slack-user-{suffix}"
    now = datetime.now(timezone.utc)
    monkeypatch.setenv("ODDISH_SLACK_EXPENSIVE_EXPERIMENT_USD", "100")
    monkeypatch.setenv("ODDISH_SLACK_EXPERIMENT_REPEAT_USD", "1000")
    monkeypatch.setenv("ODDISH_SLACK_EXPENSIVE_TRIAL_USD", "100")
    monkeypatch.setenv("ODDISH_SLACK_TRIAL_AVERAGE_MULTIPLIER", "2")

    async with get_session() as session:
        session.add(
            OrganizationModel(
                id=org_id,
                name="Slack expense test org",
                slug=org_id,
            )
        )
        session.add(
            UserModel(
                id=user_id,
                org_id=org_id,
                email="expense-owner@example.com",
                name="Expense Owner",
            )
        )
        session.add(
            ExperimentModel(
                id=experiment_id,
                name="Slack expense test",
                org_id=org_id,
                owner="Expense Owner",
                owner_user_id=user_id,
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
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
        assert alerts[0].recipient_email == "expense-owner@example.com"
        assert "Top agent costs:" in alerts[0].text
        assert "Title: `outlier`" in alerts[1].text
        assert "Experiment: *Slack expense test*" in alerts[1].text
        assert "Author: *Expense Owner*" in alerts[1].text
        assert alerts[1].recipient_email == "expense-owner@example.com"
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
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == user_id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
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
