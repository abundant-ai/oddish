import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
import slack_notifications as notifications
from models import OrganizationModel, UserModel
from oddish.core.cost_basis import CANCELLED_HARBOR_STAGE
from oddish.core.endpoints._common import USER_CANCELLED_MESSAGE
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    VerdictStatus,
    get_session,
)
from slack_notifications import (
    ALWAYS_PING_EMAILS,
    AlertCandidates,
    ExperimentCandidate,
    FailedExperiment,
    FailedTrial,
    QaFailure,
    SlackAlert,
    TrialSpend,
    UnpricedModel,
    build_alerts,
    deliver_pending_alerts,
    load_alerts,
    record_alerts,
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


def _outbox_stubs(monkeypatch: pytest.MonkeyPatch, rows: dict[str, dict]) -> None:
    """In-memory outbox table honoring the first-write-wins insert."""

    async def insert_rows(new_rows: list[dict]) -> None:
        for row in new_rows:
            rows.setdefault(row["alert_key"], dict(row))

    async def pending() -> list[SimpleNamespace]:
        ordered = sorted(rows.values(), key=lambda row: row["claimed_at"])
        return [SimpleNamespace(**row) for row in ordered if row["notified_at"] is None]

    async def mark_sent(*keys: str) -> None:
        now = datetime.now(timezone.utc)
        for key in keys:
            if key in rows:
                rows[key]["notified_at"] = now

    monkeypatch.setattr(notifications, "_insert_alert_rows", insert_rows)
    monkeypatch.setattr(notifications, "_pending_alert_rows", pending)
    monkeypatch.setattr(notifications, "_mark_alert_sent", mark_sent)


def _sent_keys(rows: dict[str, dict]) -> set[str]:
    return {key for key, row in rows.items() if row["notified_at"] is not None}


def _pending_keys(rows: dict[str, dict]) -> set[str]:
    return {key for key, row in rows.items() if row["notified_at"] is None}


async def _record_and_deliver(
    alerts: list[SlackAlert],
    *,
    webhook_url: str = "",
    bot_token: str = "",
) -> None:
    await record_alerts(alerts, channel=bool(webhook_url), dms=bool(bot_token))
    await deliver_pending_alerts(webhook_url, bot_token)


def test_alert_thresholds_are_hardcoded() -> None:
    assert notifications.EXPERIMENT_MILESTONE_USD == 1000.0
    assert notifications.EXPERIMENT_REPEAT_USD == 1000.0
    assert notifications.TRIAL_PING_USD == 200.0
    assert notifications.TRIAL_ESCALATION_USD == 1000.0
    assert notifications.EXPERIMENT_FAILED_RATIO == 0.5
    assert ALWAYS_PING_EMAILS == (
        "charles@abundant.ai",
        "ke@abundant.ai",
        "meji@abundant.ai",
        "jesse@abundant.ai",
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


@pytest.mark.parametrize(
    ("verdict_status", "error", "expected"),
    [
        (VerdictStatus.SUCCESS, None, "verdict judged this task not good"),
        (VerdictStatus.SUCCESS, "ignored", "verdict judged this task not good"),
        (None, None, "verdict judged this task not good"),
        (
            VerdictStatus.FAILED,
            "grader exploded",
            "verdict job failed — grader exploded",
        ),
        # A missing error must not render as "verdict job failed — None".
        (VerdictStatus.FAILED, None, "verdict job failed"),
        (VerdictStatus.FAILED, "", "verdict job failed"),
    ],
)
def test_verdict_reason(
    verdict_status: VerdictStatus | None,
    error: str | None,
    expected: str,
) -> None:
    assert notifications._verdict_reason(verdict_status, error) == expected


def test_build_alerts_reports_each_expense_milestone() -> None:
    now = datetime.now(timezone.utc)
    # Every trial sits under the $200 trial floor, so the only alerts are the
    # milestones.
    trials = [
        _trial(f"trial-{index}", 100, experiment_id="experiment/1", finished_at=now)
        for index in range(20)
    ]
    trials.append(
        _trial("trial-tail", 1, experiment_id="experiment/1", finished_at=now)
    )
    alerts = build_alerts(
        AlertCandidates(
            experiments=[
                ExperimentCandidate(
                    id="experiment/1",
                    name="Exp <One>",
                    owner="Pat & Sam",
                    active_trials=2,
                    owner_email="owner@example.com",
                )
            ],
            trials=trials,
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
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
    assert experiment_alert.dm_only
    assert experiment_alert.recipient_email == "owner@example.com"
    assert experiment_alert.mention_emails == ()


def test_build_alerts_lists_the_top_three_agent_costs() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Experiment", "Ada", 0)],
            trials=[
                _trial("a-1", 600, model="openrouter/opus", finished_at=now),
                _trial("a-2", 400, model="openrouter/opus", finished_at=now),
                _trial("b", 800, model="azure/fable", finished_at=now),
                _trial("c", 400, model="anthropic/sonnet", finished_at=now),
                _trial("d", 200, model="openai/gpt", finished_at=now),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert alerts[0].text.splitlines()[6:10] == [
        "Top agent costs:",
        "• `openrouter/opus`: *$1,000.00*",
        "• `azure/fable`: *$800.00*",
        "• `anthropic/sonnet`: *$400.00*",
    ]


def test_build_alerts_silently_claims_milestones_reached_before_the_window() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[
                ExperimentCandidate(
                    "experiment-1", "Exp", "Ada", 0, owner_email="a@e.com"
                )
            ],
            trials=[
                _trial("old", 2000, finished_at=now - timedelta(hours=3)),
                _trial("recent", 50, finished_at=now),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    milestone_alerts = [a for a in alerts if a.key.startswith("experiment:")]
    assert [a.key for a in milestone_alerts] == [
        "experiment:experiment-1:1000",
        "experiment:experiment-1:2000",
    ]
    assert all(a.silent for a in milestone_alerts)
    assert all(a.text for a in milestone_alerts)
    assert all(a.recipient_email == "a@e.com" for a in milestone_alerts)
    assert all(a.dm_only for a in milestone_alerts)


def test_build_alerts_fires_only_milestones_new_spend_crosses() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Exp", "Ada", 1)],
            trials=[
                _trial("old", 1500, finished_at=now - timedelta(hours=3)),
                _trial("recent", 1600, finished_at=now),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    milestone_alerts = [a for a in alerts if a.key.startswith("experiment:")]
    silent = [a.key for a in milestone_alerts if a.silent]
    firing = [a for a in milestone_alerts if not a.silent]
    assert silent == ["experiment:experiment-1:1000"]
    assert [a.key for a in firing] == [
        "experiment:experiment-1:2000",
        "experiment:experiment-1:3000",
    ]
    assert "New spend (last 2h): *$1,600.00*" in firing[0].text


def test_build_alerts_new_experiment_fires_every_milestone() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Exp", "Ada", 3)],
            trials=[_trial("burst", 2400, finished_at=now)],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    milestone_alerts = [a for a in alerts if a.key.startswith("experiment:")]
    assert [a.key for a in milestone_alerts] == [
        "experiment:experiment-1:1000",
        "experiment:experiment-1:2000",
    ]
    assert not any(a.silent for a in milestone_alerts)


def test_build_alerts_pings_any_trial_over_the_floor_without_peers() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[
                ExperimentCandidate(
                    "experiment-1",
                    "Experiment",
                    None,
                    0,
                    owner_email="owner@example.com",
                )
            ],
            trials=[_trial("lonely", 201, finished_at=now)],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [alert.key for alert in alerts] == ["trial:lonely:200"]
    alert = alerts[0]
    assert alert.text.splitlines() == [
        ":warning: *Expensive trial*",
        "Title: `lonely title`",
        "Experiment: *Experiment*",
        "Cost: *$201.00*",
        "Model: `model-1`",
        "Author: *Unknown*",
        "<https://www.oddish.app/tasks/task%2F1|open task>",
    ]
    assert alert.dm_only
    assert alert.recipient_email == "owner@example.com"
    assert alert.mention_emails == ()


@pytest.mark.parametrize(
    ("cost_usd", "expected"),
    [(200, False), (200.01, True)],
)
def test_build_alerts_trial_floor_is_exclusive(cost_usd: float, expected: bool) -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Experiment", None, 0)],
            trials=[_trial("candidate", cost_usd, finished_at=now)],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert any(alert.key.startswith("trial:") for alert in alerts) is expected


def test_build_alerts_escalates_a_very_expensive_trial_to_the_channel() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[
                ExperimentCandidate(
                    "experiment-1",
                    "Experiment",
                    "Ada",
                    0,
                    owner_email="Owner@Example.com",
                )
            ],
            trials=[_trial("whale", 1500, finished_at=now)],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    dm, escalation = [alert for alert in alerts if alert.key.startswith("trial")]
    assert dm.key == "trial:whale:200"
    assert escalation.key == "trial-escalation:whale:1000"
    assert dm.text == escalation.text
    assert dm.text.splitlines()[0] == ":rotating_light: *Very expensive trial*"
    assert dm.dm_only
    assert dm.recipient_email == "Owner@Example.com"
    assert dm.mention_emails == ()
    assert not escalation.dm_only
    assert escalation.mention_emails == ("owner@example.com", *ALWAYS_PING_EMAILS)


def test_build_alerts_reports_unpriceable_models_once_each() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Experiment", None, 0)],
            unpriced_models=[
                UnpricedModel(model="mystery/model-x", trial_count=3, task_id="task/9"),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
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
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Experiment", None, 0)],
            unpriced_models=[
                UnpricedModel(model="mystery/model-x", trial_count=1, task_id="task/9"),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert "1 recent trial recorded" in alerts[0].text


def test_build_alerts_ignores_old_trials() -> None:
    now = datetime.now(timezone.utc)
    # Over the $100 trial floor but stale, and under the $500 milestone.
    alerts = build_alerts(
        AlertCandidates(
            experiments=[ExperimentCandidate("experiment-1", "Experiment", None, 0)],
            trials=[_trial("old", 150, finished_at=now - timedelta(hours=3))],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert alerts == []


def test_build_alerts_reports_failed_experiments_as_dm_only() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            failed_experiments=[
                FailedExperiment(
                    id="experiment/1",
                    name="Exp <One>",
                    owner="Pat & Sam",
                    failed_trials=3,
                    total_trials=4,
                    owner_email="owner@example.com",
                )
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [alert.key for alert in alerts] == ["experiment-failed:experiment/1"]
    alert = alerts[0]
    assert alert.dm_only
    assert alert.recipient_email == "owner@example.com"
    assert alert.mention_emails == ()
    assert alert.text.splitlines() == [
        ":x: *Experiment failed*",
        "Title: *Exp &lt;One&gt;*",
        "Failed trials: *3/4*",
        "Owner: *Pat &amp; Sam*",
        "<https://www.oddish.app/experiments/experiment%252F1|open experiment>",
    ]


# The 0.5 ratio is a module constant, so the boundary is probed by varying the
# data around it.
@pytest.mark.parametrize(
    ("failed_trials", "total_trials", "expected"),
    [
        (0, 4, False),
        (1, 4, False),
        (2, 4, True),
        (3, 4, True),
        (4, 4, True),
        (1, 3, False),
        (2, 3, True),
        (1, 2, True),
        (0, 0, False),
    ],
)
def test_build_alerts_failed_experiment_respects_ratio(
    failed_trials: int,
    total_trials: int,
    expected: bool,
) -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            failed_experiments=[
                FailedExperiment(
                    id="experiment-1",
                    name="Experiment",
                    owner=None,
                    failed_trials=failed_trials,
                    total_trials=total_trials,
                )
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert bool(alerts) is expected


def test_build_alerts_reports_failed_trials_as_dm_only() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            failed_trials=[
                FailedTrial(
                    name="Trial <One>",
                    task_id="task/1",
                    task_version_id="task/1@v2",
                    experiment_name="Exp & Co",
                    owner="Ada",
                    owner_email="owner@example.com",
                )
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [alert.key for alert in alerts] == ["trial-failed:task/1@v2"]
    alert = alerts[0]
    assert alert.dm_only
    assert alert.recipient_email == "owner@example.com"
    assert alert.text.splitlines() == [
        ":boom: *Trial failed*",
        "Title: `Trial &lt;One&gt;`",
        "Experiment: *Exp &amp; Co*",
        "Owner: *Ada*",
        "<https://www.oddish.app/tasks/task%2F1?version=task%2F1%40v2|open task>",
    ]


def test_build_alerts_collapses_failed_trials_per_task_version() -> None:
    now = datetime.now(timezone.utc)

    def failed(name: str, task_version_id: str | None) -> FailedTrial:
        return FailedTrial(
            name=name,
            task_id="task/1",
            task_version_id=task_version_id,
            experiment_name="Exp",
            owner="Ada",
            owner_email="owner@example.com",
        )

    alerts = build_alerts(
        AlertCandidates(
            failed_trials=[
                failed("trial-1", "task/1@v2"),
                failed("trial-2", "task/1@v2"),
                failed("trial-3", "task/1@v3"),
                failed("legacy", None),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [alert.key for alert in alerts] == [
        "trial-failed:task/1@v2",
        "trial-failed:task/1@v3",
        "trial-failed:task/1",
    ]


def test_build_alerts_keeps_one_failed_trial_dm_per_owner_of_a_task_version() -> None:
    now = datetime.now(timezone.utc)

    def failed(name: str, owner_email: str) -> FailedTrial:
        return FailedTrial(
            name=name,
            task_id="task/1",
            task_version_id="task/1@v2",
            experiment_name="Exp",
            owner="Ada",
            owner_email=owner_email,
        )

    # One task version, two owners running it: dedup is on (key, recipient), so
    # collapsing on the key alone would silently drop the second owner's DM.
    alerts = build_alerts(
        AlertCandidates(
            failed_trials=[
                failed("trial-1", "ada@example.com"),
                failed("trial-2", "grace@example.com"),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [(a.key, a.recipient_email) for a in alerts] == [
        ("trial-failed:task/1@v2", "ada@example.com"),
        ("trial-failed:task/1@v2", "grace@example.com"),
    ]

    same_owner = build_alerts(
        AlertCandidates(
            failed_trials=[
                failed("trial-1", "ada@example.com"),
                failed("trial-2", "ada@example.com"),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [(a.key, a.recipient_email) for a in same_owner] == [
        ("trial-failed:task/1@v2", "ada@example.com")
    ]


def test_build_alerts_reports_qa_failures_as_dm_only() -> None:
    now = datetime.now(timezone.utc)
    alerts = build_alerts(
        AlertCandidates(
            qa_failures=[
                QaFailure(
                    task_id="task/1",
                    task_name="Task <One>",
                    task_version_id="task/1@v2",
                    reason="verdict judged this task not good",
                    owner_email="author@example.com",
                ),
                QaFailure(
                    task_id="task/1",
                    task_name="Task <One>",
                    task_version_id="task/1@v2",
                    reason="verdict job failed — boom",
                    owner_email="author@example.com",
                ),
            ],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )

    assert [alert.key for alert in alerts] == ["qa-failed:task/1@v2"]
    alert = alerts[0]
    assert alert.dm_only
    assert alert.recipient_email == "author@example.com"
    assert alert.text.splitlines() == [
        ":mag: *QA failed*",
        "Task: *Task &lt;One&gt;*",
        "Reason: verdict judged this task not good",
        "<https://www.oddish.app/tasks/task%2F1?version=task%2F1%40v2|open task>",
    ]


@pytest.mark.asyncio
async def test_deliver_posts_recorded_alerts_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_post", post)

    for _ in range(2):
        await _record_and_deliver(
            [SlackAlert("experiment:1:500", "body")],
            webhook_url="https://hooks.slack.test",
        )

    assert posted == ["body"]
    assert _sent_keys(rows) == {"experiment:1:500"}


@pytest.mark.asyncio
async def test_deliver_retries_a_failed_post_with_its_stored_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[str] = []
    healthy = False

    async def post(_url: str, text: str) -> None:
        posted.append(text)
        if text == "flaky" and not healthy:
            raise RuntimeError("failed")

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_post", post)

    await _record_and_deliver(
        [SlackAlert("flaky-key", "flaky"), SlackAlert("ok-key", "ok")],
        webhook_url="https://hooks.slack.test",
    )
    assert posted == ["flaky", "ok"]
    assert _sent_keys(rows) == {"ok-key"}
    assert _pending_keys(rows) == {"flaky-key"}

    # Next run the milestone's spend has aged out of the recent window, so
    # build_alerts recomputes it as silent with different text. The pending
    # row keeps its original payload and keeps retrying regardless.
    healthy = True
    await _record_and_deliver(
        [SlackAlert("flaky-key", "recomputed", silent=True)],
        webhook_url="https://hooks.slack.test",
    )
    assert posted == ["flaky", "ok", "flaky"]
    assert _pending_keys(rows) == set()


@pytest.mark.asyncio
async def test_deliver_prefixes_mentions_and_looks_up_only_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []
    lookups: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def lookup(_token: str, email: str) -> str | None:
        lookups.append(email)
        return {"owner@example.com": "U123", "ke@abundant.ai": "U456"}.get(email)

    _outbox_stubs(monkeypatch, {})
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    alerts = [
        SlackAlert(
            "trial:whale:100",
            "body",
            mention_emails=("owner@example.com", "ke@abundant.ai", "nobody@e.com"),
        )
    ]
    await _record_and_deliver(
        alerts, webhook_url="https://hooks.slack.test", bot_token="xoxb-token"
    )

    assert posted == ["<@U123> <@U456>\nbody"]
    assert lookups == ["owner@example.com", "ke@abundant.ai", "nobody@e.com"]

    # Once sent, a re-record leaves nothing pending: no repost, no lookups.
    lookups.clear()
    await _record_and_deliver(
        alerts, webhook_url="https://hooks.slack.test", bot_token="xoxb-token"
    )
    assert posted == ["<@U123> <@U456>\nbody"]
    assert lookups == []


@pytest.mark.asyncio
async def test_deliver_looks_an_email_up_once_per_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []
    lookups: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def lookup(_token: str, email: str) -> str | None:
        lookups.append(email)
        return "U123"

    _outbox_stubs(monkeypatch, {})
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    await _record_and_deliver(
        [
            SlackAlert("trial:1:100", "one", mention_emails=("owner@example.com",)),
            SlackAlert("trial:2:100", "two", mention_emails=("owner@example.com",)),
        ],
        webhook_url="https://hooks.slack.test",
        bot_token="xoxb-token",
    )

    assert posted == ["<@U123>\none", "<@U123>\ntwo"]
    assert lookups == ["owner@example.com"]


@pytest.mark.asyncio
async def test_deliver_caches_a_terminal_lookup_miss_for_the_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []
    lookups: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def lookup(_token: str, email: str) -> str | None:
        lookups.append(email)
        return None

    _outbox_stubs(monkeypatch, {})
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    await _record_and_deliver(
        [
            SlackAlert("trial:1:100", "one", mention_emails=("nobody@example.com",)),
            SlackAlert("trial:2:100", "two", mention_emails=("nobody@example.com",)),
        ],
        webhook_url="https://hooks.slack.test",
        bot_token="xoxb-token",
    )

    # "No Slack account" is settled: ask once, then post both unprefixed.
    assert posted == ["one", "two"]
    assert lookups == ["nobody@example.com"]


@pytest.mark.asyncio
async def test_deliver_does_not_cache_a_transient_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []
    lookups: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def lookup(_token: str, email: str) -> str | None:
        lookups.append(email)
        if len(lookups) == 1:
            raise RuntimeError("slack user lookup failed: ratelimited")
        return "U123"

    _outbox_stubs(monkeypatch, {})
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    await _record_and_deliver(
        [
            SlackAlert("trial:1:100", "one", mention_emails=("owner@example.com",)),
            SlackAlert("trial:2:100", "two", mention_emails=("owner@example.com",)),
        ],
        webhook_url="https://hooks.slack.test",
        bot_token="xoxb-token",
    )

    # A throttled lookup is not an answer, so it must not poison the cache.
    assert posted == ["one", "<@U123>\ntwo"]
    assert lookups == ["owner@example.com", "owner@example.com"]


@pytest.mark.asyncio
async def test_deliver_without_a_bot_token_posts_unprefixed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    _outbox_stubs(monkeypatch, {})
    monkeypatch.setattr(notifications, "_post", post)

    await _record_and_deliver(
        [SlackAlert("trial:1:100", "body", mention_emails=("owner@example.com",))],
        webhook_url="https://hooks.slack.test",
    )

    assert posted == ["body"]


@pytest.mark.asyncio
async def test_deliver_survives_mention_lookup_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def lookup(_token: str, email: str) -> str | None:
        if email == "boom@example.com":
            raise RuntimeError("slack down")
        return "U123"

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    await _record_and_deliver(
        [
            SlackAlert(
                "trial:1:100", "all broken", mention_emails=("boom@example.com",)
            ),
            SlackAlert(
                "trial:2:100",
                "partly resolved",
                mention_emails=("owner@example.com", "boom@example.com"),
            ),
            # A failure mid-list must not strand the mentions behind it: this is
            # the $1k escalation shape, where losing the tail would silently
            # drop most of the always-ping list.
            SlackAlert(
                "trial:3:100",
                "resolved after the failure",
                mention_emails=("boom@example.com", "owner@example.com"),
            ),
        ],
        webhook_url="https://hooks.slack.test",
        bot_token="xoxb-token",
    )

    assert posted == [
        "all broken",
        "<@U123>\npartly resolved",
        "<@U123>\nresolved after the failure",
    ]
    assert _pending_keys(rows) == set()


@pytest.mark.asyncio
async def test_silent_alerts_settle_both_channels_without_sending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[str] = []
    looked_up: list[str] = []

    async def post(*_args) -> None:
        posted.append("sent")

    async def lookup(_token: str, email: str) -> str | None:
        looked_up.append(email)
        return "U123"

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_post_dm", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    await _record_and_deliver(
        [
            SlackAlert(
                "experiment:1:1000",
                "historical",
                mention_emails=("owner@example.com",),
                silent=True,
            ),
            SlackAlert(
                "experiment-failed:1",
                "historical",
                recipient_email="owner@example.com",
                dm_only=True,
                silent=True,
            ),
        ],
        webhook_url="https://hooks.slack.test",
        bot_token="xoxb-token",
    )

    assert posted == []
    assert looked_up == []
    assert _sent_keys(rows) == {
        "experiment:1:1000",
        "dm:experiment-failed:1:owner@example.com",
    }


@pytest.mark.asyncio
async def test_an_escalated_trial_dms_its_owner_and_posts_to_the_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = datetime.now(timezone.utc)
    posted: list[str] = []
    dmed: list[tuple[str, str]] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def lookup(_token: str, email: str) -> str | None:
        return f"U-{email.split('@')[0]}"

    async def post_dm(_token: str, slack_user_id: str, text: str) -> None:
        dmed.append((slack_user_id, text))

    _outbox_stubs(monkeypatch, {})
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)
    monkeypatch.setattr(notifications, "_post_dm", post_dm)

    alerts = build_alerts(
        AlertCandidates(
            experiments=[
                ExperimentCandidate(
                    "experiment-1", "Exp", "Ada", 0, owner_email="owner@example.com"
                )
            ],
            trials=[_trial("whale", 1500, finished_at=now)],
        ),
        recent_cutoff=now - timedelta(hours=2),
        dashboard_url="https://www.oddish.app",
    )
    await _record_and_deliver(
        alerts, webhook_url="https://hooks.slack.test", bot_token="xoxb-token"
    )

    # The owner hears about the trial once, privately, and the escalation is the
    # only thing that reaches the channel -- the milestone no longer does.
    assert len(posted) == 1
    assert posted[0].splitlines()[:2] == [
        " ".join(
            f"<@U-{email.split('@')[0]}>"
            for email in ("owner@example.com", *ALWAYS_PING_EMAILS)
        ),
        ":rotating_light: *Very expensive trial*",
    ]
    assert [text.splitlines()[0] for _, text in dmed] == [
        ":money_with_wings: *Expensive experiment*",
        ":rotating_light: *Very expensive trial*",
    ]
    assert {user_id for user_id, _ in dmed} == {"U-owner"}


@pytest.mark.asyncio
async def test_deliver_settles_pre_outbox_rows_without_posting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Rows claimed before the outbox carried a payload; there is nothing to
    # deliver, so they settle instead of surfacing as pending forever.
    rows: dict[str, dict] = {
        "experiment:1:1000": {
            "alert_key": "experiment:1:1000",
            "claimed_at": datetime.now(timezone.utc),
            "notified_at": None,
            "payload": None,
            "recipient_email": None,
            "mention_emails": None,
        }
    }
    posted: list[str] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_post", post)

    await deliver_pending_alerts("https://hooks.slack.test", "")

    assert posted == []
    assert _sent_keys(rows) == {"experiment:1:1000"}


@pytest.mark.asyncio
async def test_record_routes_alerts_by_configured_channel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[str] = []
    dms: list[tuple[str, str]] = []

    async def post(_url: str, text: str) -> None:
        posted.append(text)

    async def post_dm(_token: str, slack_user_id: str, text: str) -> None:
        dms.append((slack_user_id, text))

    async def lookup(_token: str, _email: str) -> str | None:
        return "U123"

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_post", post)
    monkeypatch.setattr(notifications, "_post_dm", post_dm)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)

    alerts = [
        SlackAlert("trial-escalation:whale:1000", "channel"),
        SlackAlert(
            "experiment-failed:1",
            "dm only",
            recipient_email="owner@example.com",
            dm_only=True,
        ),
        SlackAlert("qa-failed:task/3", "no recipient", dm_only=True),
    ]

    # Webhook-only: DM alerts are not recorded, so nothing piles up
    # undeliverable.
    await _record_and_deliver(alerts, webhook_url="https://hooks.slack.test")
    assert posted == ["channel"]
    assert dms == []
    assert set(rows) == {"trial-escalation:whale:1000"}

    # Bot-only: channel alerts are not recorded.
    rows.clear()
    posted.clear()
    await _record_and_deliver(alerts, bot_token="xoxb-token")
    assert posted == []
    assert dms == [("U123", "dm only")]
    assert set(rows) == {"dm:experiment-failed:1:owner@example.com"}


@pytest.mark.asyncio
async def test_deliver_dms_per_recipient_and_retries_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    lookups: list[str] = []
    posted: list[tuple[str, str]] = []

    async def lookup(_token: str, email: str) -> str | None:
        lookups.append(email)
        if email == "error@example.com":
            raise RuntimeError("lookup failed")
        return None if email == "unknown@example.com" else "U123"

    async def post_dm(_token: str, slack_user_id: str, text: str) -> None:
        posted.append((slack_user_id, text))
        if text == "fail":
            raise RuntimeError("failed")

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)
    monkeypatch.setattr(notifications, "_post_dm", post_dm)

    delivered = SlackAlert(
        "experiment-failed:1",
        "dm text",
        recipient_email=" Owner@Example.com ",
        dm_only=True,
    )
    await _record_and_deliver([delivered], bot_token="xoxb-token")
    await _record_and_deliver([delivered], bot_token="xoxb-token")
    await _record_and_deliver(
        [
            SlackAlert(
                "trial-failed:task/1",
                "fail",
                recipient_email="owner@example.com",
                dm_only=True,
            ),
            SlackAlert(
                "qa-failed:task/2",
                "qa dm",
                recipient_email="owner@example.com",
                dm_only=True,
            ),
            SlackAlert(
                "experiment-failed:2",
                "unmatched",
                recipient_email="unknown@example.com",
                dm_only=True,
            ),
            SlackAlert(
                "experiment-failed:3",
                "lookup blows up",
                recipient_email="error@example.com",
                dm_only=True,
            ),
        ],
        bot_token="xoxb-token",
    )

    assert posted == [("U123", "dm text"), ("U123", "fail"), ("U123", "qa dm")]
    assert lookups == [
        "owner@example.com",
        "owner@example.com",
        "unknown@example.com",
        "error@example.com",
    ]
    # The recipient is normalized into the key, the settled "no Slack account"
    # answer completes its row, and both explicit failures stay pending for
    # the next run.
    assert _sent_keys(rows) == {
        "dm:experiment-failed:1:owner@example.com",
        "dm:qa-failed:task/2:owner@example.com",
        "dm:experiment-failed:2:unknown@example.com",
    }
    assert _pending_keys(rows) == {
        "dm:trial-failed:task/1:owner@example.com",
        "dm:experiment-failed:3:error@example.com",
    }


@pytest.mark.asyncio
async def test_deliver_dedups_a_task_version_per_recipient(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[tuple[str, str]] = []

    async def lookup(_token: str, email: str) -> str | None:
        return f"U-{email.split('@')[0]}"

    async def post_dm(_token: str, slack_user_id: str, text: str) -> None:
        posted.append((slack_user_id, text))

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)
    monkeypatch.setattr(notifications, "_post_dm", post_dm)

    now = datetime.now(timezone.utc)

    def failed(name: str, owner_email: str) -> FailedTrial:
        return FailedTrial(
            name=name,
            task_id="task/1",
            task_version_id="task/1@v2",
            experiment_name="Exp",
            owner="Ada",
            owner_email=owner_email,
        )

    # Two broken trials on one task version, seen across two separate runs.
    for _ in range(2):
        await _record_and_deliver(
            build_alerts(
                AlertCandidates(
                    failed_trials=[
                        failed("trial-1", "ada@example.com"),
                        failed("trial-2", "ada@example.com"),
                    ],
                ),
                recent_cutoff=now - timedelta(hours=2),
                dashboard_url="https://www.oddish.app",
            ),
            bot_token="xoxb-token",
        )

    assert len(posted) == 1
    assert _sent_keys(rows) == {"dm:trial-failed:task/1@v2:ada@example.com"}


def _mock_slack_http(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    real_client = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(notifications.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
async def test_lookup_slack_user_parses_slack_responses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/users.lookupByEmail"
        assert request.headers["Authorization"] == "Bearer xoxb-token"
        if request.url.params["email"] == "owner@example.com":
            return httpx.Response(200, json={"ok": True, "user": {"id": "U123"}})
        if request.url.params["email"] == "throttled@example.com":
            return httpx.Response(200, json={"ok": False, "error": "ratelimited"})
        return httpx.Response(200, json={"ok": False, "error": "users_not_found"})

    _mock_slack_http(monkeypatch, handler)

    assert (
        await notifications._lookup_slack_user("xoxb-token", "owner@example.com")
        == "U123"
    )
    assert (
        await notifications._lookup_slack_user("xoxb-token", "missing@example.com")
        is None
    )
    # A transient error must raise rather than read as "no Slack account":
    # callers settle a None by marking the alert delivered forever.
    with pytest.raises(RuntimeError, match="ratelimited"):
        await notifications._lookup_slack_user("xoxb-token", "throttled@example.com")


@pytest.mark.asyncio
async def test_deliver_retries_a_throttled_dm_lookup_instead_of_settling_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows: dict[str, dict] = {}
    posted: list[str] = []

    async def lookup(_token: str, _email: str) -> str | None:
        raise RuntimeError("slack user lookup failed: ratelimited")

    async def post_dm(_token: str, _user: str, text: str) -> None:
        posted.append(text)

    _outbox_stubs(monkeypatch, rows)
    monkeypatch.setattr(notifications, "_lookup_slack_user", lookup)
    monkeypatch.setattr(notifications, "_post_dm", post_dm)

    alert = SlackAlert(
        "trial-failed:t-v1",
        "broken",
        recipient_email="owner@example.com",
        dm_only=True,
    )
    await _record_and_deliver([alert], bot_token="xoxb-token")

    assert posted == []
    # Never marked delivered: the row stays pending for the next run.
    assert _pending_keys(rows) == {"dm:trial-failed:t-v1:owner@example.com"}


@pytest.mark.asyncio
async def test_post_dm_raises_on_slack_logical_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    opened: list[dict] = []
    posted: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer xoxb-token"
        payload = json.loads(request.content)
        if request.url.path == "/api/conversations.open":
            opened.append(payload)
            if payload["users"] == "U-open-failed":
                return httpx.Response(200, json={"ok": False, "error": "cannot_dm_bot"})
            return httpx.Response(
                200,
                json={"ok": True, "channel": {"id": f"D-{payload['users']}"}},
            )
        assert request.url.path == "/api/chat.postMessage"
        posted.append(payload)
        if payload["channel"] == "D-U-disabled":
            return httpx.Response(
                200, json={"ok": False, "error": "messaging_disabled"}
            )
        return httpx.Response(200, json={"ok": True})

    _mock_slack_http(monkeypatch, handler)

    await notifications._post_dm("xoxb-token", "U123", "hello")
    with pytest.raises(RuntimeError, match="messaging_disabled"):
        await notifications._post_dm("xoxb-token", "U-disabled", "hello")
    with pytest.raises(RuntimeError, match="cannot_dm_bot"):
        await notifications._post_dm("xoxb-token", "U-open-failed", "hello")
    assert opened == [
        {"users": "U123"},
        {"users": "U-disabled"},
        {"users": "U-open-failed"},
    ]
    assert posted == [
        {"channel": "D-U123", "text": "hello"},
        {"channel": "D-U-disabled", "text": "hello"},
    ]


@pytest.mark.asyncio
async def test_load_alerts_uses_settled_trial_costs() -> None:
    suffix = uuid4().hex[:12]
    experiment_id = f"slack-exp-{suffix}"
    task_id = f"slack-task-{suffix}"
    org_id = f"slack-org-{suffix}"
    user_id = f"slack-user-{suffix}"
    now = datetime.now(timezone.utc)

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
                    cost_usd=500,
                    finished_at=now - timedelta(days=1),
                ),
                # 40M output tokens of a priced model settles to ~$560 via the
                # token estimate: over the trial floor, and enough on top of the
                # baseline to pass the $1,000 experiment milestone.
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
                    output_tokens=40_000_000,
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
            f"experiment:{experiment_id}:1000",
            f"trial:{task_id}-outlier:200",
            "unpriced-model:made-up/no-such-model-9000",
        ]
        assert "Trials still running: 0" in alerts[0].text
        assert "New spend (last 2h):" in alerts[0].text
        assert alerts[0].recipient_email == "expense-owner@example.com"
        assert alerts[0].dm_only
        assert "Top agent costs:" in alerts[0].text
        assert alerts[1].text.splitlines()[0] == ":warning: *Expensive trial*"
        assert "Title: `outlier`" in alerts[1].text
        assert "Experiment: *Slack expense test*" in alerts[1].text
        assert "Author: *Expense Owner*" in alerts[1].text
        assert alerts[1].recipient_email == "expense-owner@example.com"
        assert alerts[1].dm_only
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
async def test_load_alerts_reports_finished_failed_experiments() -> None:
    suffix = uuid4().hex[:12]
    finished_id = f"slack-exp-failed-{suffix}"
    running_id = f"slack-exp-running-{suffix}"
    stale_id = f"slack-exp-stale-{suffix}"
    task_id = f"slack-task-{suffix}"
    org_id = f"slack-org-{suffix}"
    user_id = f"slack-user-{suffix}"
    now = datetime.now(timezone.utc)

    def trial(
        trial_id: str,
        experiment_id: str,
        status: TrialStatus,
        *,
        origin: TrialOrigin = TrialOrigin.ODDISH,
        finished_at: datetime | None = None,
        deleted_at: datetime | None = None,
        harbor_stage: str | None = None,
        stale_reaped_at: datetime | None = None,
        superseded_by: str | None = None,
    ) -> TrialModel:
        finished_at = finished_at or (now if status != TrialStatus.QUEUED else None)
        return TrialModel(
            id=trial_id,
            name=trial_id,
            task_id=task_id,
            experiment_id=experiment_id,
            agent="claude-code",
            provider="openai",
            model="gpt-5.3",
            queue_key="test",
            status=status,
            origin=origin,
            is_probe=False,
            cost_usd=1,
            finished_at=finished_at,
            deleted_at=deleted_at,
            harbor_stage=harbor_stage,
            stale_reaped_at=stale_reaped_at,
            superseded_by_trial_id=superseded_by,
        )

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name="Failed org", slug=org_id))
        session.add(
            UserModel(
                id=user_id,
                org_id=org_id,
                email="failed-owner@example.com",
                name="Failed Owner",
            )
        )
        session.add(
            ExperimentModel(
                id=finished_id,
                name="Failed experiment",
                org_id=org_id,
                owner="Failed Owner",
                owner_user_id=user_id,
            )
        )
        session.add(
            ExperimentModel(
                id=running_id,
                name="Running experiment",
                org_id=org_id,
                owner="Failed Owner",
                owner_user_id=user_id,
            )
        )
        session.add(
            ExperimentModel(
                id=stale_id,
                name="Stale experiment",
                org_id=org_id,
                owner="Failed Owner",
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
                trial(f"{task_id}-failed-1", finished_id, TrialStatus.FAILED),
                trial(f"{task_id}-failed-2", finished_id, TrialStatus.FAILED),
                trial(f"{task_id}-success", finished_id, TrialStatus.SUCCESS),
                trial(
                    f"{task_id}-deleted-failed",
                    finished_id,
                    TrialStatus.FAILED,
                    deleted_at=now,
                ),
                trial(
                    f"{task_id}-superseded-failed",
                    finished_id,
                    TrialStatus.FAILED,
                    superseded_by=f"{task_id}-success",
                ),
                trial(
                    f"{task_id}-cancelled-failed",
                    finished_id,
                    TrialStatus.FAILED,
                    harbor_stage=CANCELLED_HARBOR_STAGE,
                ),
                trial(
                    f"{task_id}-reaped-failed",
                    finished_id,
                    TrialStatus.FAILED,
                    harbor_stage=CANCELLED_HARBOR_STAGE,
                    stale_reaped_at=now,
                ),
                trial(f"{task_id}-running-failed", running_id, TrialStatus.FAILED),
                trial(f"{task_id}-running-active", running_id, TrialStatus.QUEUED),
                trial(
                    f"{task_id}-stale-failed",
                    stale_id,
                    TrialStatus.FAILED,
                    finished_at=now - timedelta(hours=3),
                ),
                trial(
                    f"{task_id}-stale-imported",
                    stale_id,
                    TrialStatus.SUCCESS,
                    origin=TrialOrigin.IMPORTED,
                ),
            ]
        )

    try:
        alerts = await load_alerts(now)
        assert [alert.key for alert in alerts] == [
            f"experiment-failed:{finished_id}",
            f"trial-failed:{task_id}",
        ]
        alert = alerts[0]
        assert alert.dm_only
        assert alert.recipient_email == "failed-owner@example.com"
        assert "Failed trials: *3/4*" in alert.text
        assert "Title: *Failed experiment*" in alert.text
        assert alerts[1].dm_only
        assert alerts[1].recipient_email == "failed-owner@example.com"
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
                    ExperimentModel.id.in_([finished_id, running_id, stale_id])
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
async def test_load_alerts_reports_crashed_trials_and_qa_failures() -> None:
    suffix = uuid4().hex[:12]
    experiment_id = f"slack-exp-{suffix}"
    crash_task_id = f"slack-crash-task-{suffix}"
    qa_task_id = f"slack-qa-task-{suffix}"
    cancelled_task_id = f"slack-cancelled-task-{suffix}"
    good_task_id = f"slack-good-task-{suffix}"
    org_id = f"slack-org-{suffix}"
    user_id = f"slack-user-{suffix}"
    now = datetime.now(timezone.utc)
    task_ids = [crash_task_id, qa_task_id, cancelled_task_id, good_task_id]

    def task(task_id: str, **kwargs) -> TaskModel:
        return TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="test",
            task_path="/tmp/test",
            created_by_user_id=user_id,
            **kwargs,
        )

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name="QA org", slug=org_id))
        session.add(
            UserModel(
                id=user_id,
                org_id=org_id,
                email="qa-author@example.com",
                name="QA Author",
            )
        )
        session.add(
            ExperimentModel(
                id=experiment_id,
                name="Crash experiment",
                org_id=org_id,
                owner="QA Author",
                owner_user_id=user_id,
            )
        )
        session.add(task(crash_task_id))
        session.add(
            task(
                qa_task_id,
                verdict={"is_good": False, "reasoning": "bad task"},
                verdict_status=VerdictStatus.SUCCESS,
                verdict_finished_at=now,
            )
        )
        session.add(
            task(
                cancelled_task_id,
                verdict_status=VerdictStatus.FAILED,
                verdict_error=USER_CANCELLED_MESSAGE,
                verdict_finished_at=now,
            )
        )
        session.add(
            task(
                good_task_id,
                verdict={"is_good": True},
                verdict_status=VerdictStatus.SUCCESS,
                verdict_finished_at=now,
            )
        )
        # A crashed agent still gets verified, so the row lands SUCCESS with a
        # harbor_exception marker rather than FAILED.
        session.add(
            TrialModel(
                id=f"{crash_task_id}-crashed",
                name="crashed",
                task_id=crash_task_id,
                experiment_id=experiment_id,
                agent="mini-swe-agent",
                provider="openai",
                model="gpt-5.3",
                queue_key="test",
                status=TrialStatus.SUCCESS,
                origin=TrialOrigin.ODDISH,
                is_probe=False,
                cost_usd=1,
                finished_at=now,
                result={"harbor_exception": "RuntimeError: boom"},
            )
        )

    try:
        keys = {alert.key for alert in await load_alerts(now)}
        assert f"trial-failed:{crash_task_id}" in keys
        assert f"qa-failed:{qa_task_id}" in keys
        assert f"qa-failed:{cancelled_task_id}" not in keys
        assert f"qa-failed:{good_task_id}" not in keys
        assert not any(key.startswith("experiment-failed:") for key in keys)

        by_key = {alert.key: alert for alert in await load_alerts(now)}
        crashed = by_key[f"trial-failed:{crash_task_id}"]
        assert crashed.dm_only
        assert crashed.recipient_email == "qa-author@example.com"
        assert "Title: `crashed`" in crashed.text
        qa_failed = by_key[f"qa-failed:{qa_task_id}"]
        assert qa_failed.dm_only
        assert qa_failed.recipient_email == "qa-author@example.com"
        assert "Reason: verdict judged this task not good" in qa_failed.text
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id.in_(task_ids))
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
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
async def test_load_alerts_matches_harbor_exception_only_when_present() -> None:
    # ``result["harbor_exception"].astext.isnot(None)`` renders as ``->>``, which
    # yields SQL NULL for a missing key, a JSON null, and a NULL column alike --
    # only a real exception payload marks the trial broken.
    suffix = uuid4().hex[:12]
    experiment_id = f"slack-exp-{suffix}"
    org_id = f"slack-org-{suffix}"
    broken_id = f"slack-broken-task-{suffix}"
    now = datetime.now(timezone.utc)
    results: dict[str, dict | None] = {
        f"slack-null-task-{suffix}": None,
        f"slack-empty-task-{suffix}": {},
        f"slack-jsonnull-task-{suffix}": {"harbor_exception": None},
        broken_id: {"harbor_exception": {"type": "RuntimeError", "message": "boom"}},
    }
    task_ids = list(results)

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name="JSONB org", slug=org_id))
        session.add(
            ExperimentModel(id=experiment_id, name="JSONB experiment", org_id=org_id)
        )
        for task_id, result in results.items():
            session.add(
                TaskModel(
                    id=task_id,
                    name=task_id,
                    org_id=org_id,
                    user="test",
                    task_path="/tmp/test",
                )
            )
            session.add(
                TrialModel(
                    id=f"{task_id}-trial",
                    name=task_id,
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="mini-swe-agent",
                    provider="openai",
                    model="gpt-5.3",
                    queue_key="test",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    is_probe=False,
                    cost_usd=1,
                    finished_at=now,
                    result=result,
                )
            )

    try:
        keys = {alert.key for alert in await load_alerts(now)}
        assert f"trial-failed:{broken_id}" in keys
        for task_id in task_ids:
            if task_id != broken_id:
                assert f"trial-failed:{task_id}" not in keys
    finally:
        async with get_session() as session:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.task_id.in_(task_ids))
            )
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
            )
            await session.execute(
                ExperimentModel.__table__.delete().where(
                    ExperimentModel.id == experiment_id
                )
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


@pytest.mark.asyncio
async def test_load_alerts_qa_cutoff_and_null_matrix() -> None:
    suffix = uuid4().hex[:12]
    org_id = f"slack-org-{suffix}"
    user_id = f"slack-user-{suffix}"
    stale_id = f"slack-qa-stale-{suffix}"
    good_id = f"slack-qa-good-{suffix}"
    null_verdict_id = f"slack-qa-nullverdict-{suffix}"
    missing_is_good_id = f"slack-qa-missing-{suffix}"
    clean_failure_id = f"slack-qa-cleanfail-{suffix}"
    now = datetime.now(timezone.utc)
    task_ids = [
        stale_id,
        good_id,
        null_verdict_id,
        missing_is_good_id,
        clean_failure_id,
    ]

    def task(task_id: str, **kwargs) -> TaskModel:
        return TaskModel(
            id=task_id,
            name=task_id,
            org_id=org_id,
            user="test",
            task_path="/tmp/test",
            created_by_user_id=user_id,
            **kwargs,
        )

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name="QA matrix org", slug=org_id))
        session.add(
            UserModel(
                id=user_id,
                org_id=org_id,
                email="qa-matrix@example.com",
                name="QA Matrix",
            )
        )
        session.add_all(
            [
                # A bad verdict that landed before the watch window opened.
                task(
                    stale_id,
                    verdict={"is_good": False},
                    verdict_status=VerdictStatus.SUCCESS,
                    verdict_finished_at=now - timedelta(hours=3),
                ),
                task(
                    good_id,
                    verdict={"is_good": True},
                    verdict_status=VerdictStatus.SUCCESS,
                    verdict_finished_at=now,
                ),
                # Verdict column NULL: no is_good to read, so no judgement.
                task(
                    null_verdict_id,
                    verdict_status=VerdictStatus.SUCCESS,
                    verdict_finished_at=now,
                ),
                task(
                    missing_is_good_id,
                    verdict={"reasoning": "inconclusive"},
                    verdict_status=VerdictStatus.SUCCESS,
                    verdict_finished_at=now,
                ),
                # A crashed verdict job with no recorded error.
                task(
                    clean_failure_id,
                    verdict_status=VerdictStatus.FAILED,
                    verdict_finished_at=now,
                ),
            ]
        )

    try:
        by_key = {alert.key: alert for alert in await load_alerts(now)}
        for task_id in (stale_id, good_id, null_verdict_id, missing_is_good_id):
            assert f"qa-failed:{task_id}" not in by_key

        alert = by_key[f"qa-failed:{clean_failure_id}"]
        assert alert.dm_only
        assert alert.recipient_email == "qa-matrix@example.com"
        assert "Reason: verdict job failed" in alert.text
        assert "verdict job failed —" not in alert.text
    finally:
        async with get_session() as session:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
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
async def test_load_alerts_reports_failed_verdict_jobs() -> None:
    suffix = uuid4().hex[:12]
    task_id = f"slack-verdict-task-{suffix}"
    org_id = f"slack-org-{suffix}"
    user_id = f"slack-user-{suffix}"
    now = datetime.now(timezone.utc)

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name="Verdict org", slug=org_id))
        session.add(
            UserModel(
                id=user_id,
                org_id=org_id,
                email="verdict-author@example.com",
                name="Verdict Author",
            )
        )
        session.add(
            TaskModel(
                id=task_id,
                name=task_id,
                org_id=org_id,
                user="test",
                task_path="/tmp/test",
                created_by_user_id=user_id,
                verdict_status=VerdictStatus.FAILED,
                verdict_error="grader exploded",
                verdict_finished_at=now,
            )
        )

    try:
        by_key = {alert.key: alert for alert in await load_alerts(now)}
        alert = by_key[f"qa-failed:{task_id}"]
        assert alert.dm_only
        assert alert.recipient_email == "verdict-author@example.com"
        assert "Reason: verdict job failed — grader exploded" in alert.text
    finally:
        async with get_session() as session:
            await session.execute(
                TaskModel.__table__.delete().where(TaskModel.id == task_id)
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
async def test_database_outbox_is_durable() -> None:
    loud_key = f"test-alert-{uuid4().hex}"
    silent_key = f"test-alert-silent-{uuid4().hex}"
    now = datetime.now(timezone.utc)

    def row(key: str, *, payload: str, silent: bool) -> dict:
        return {
            "alert_key": key,
            "claimed_at": now,
            "notified_at": now if silent else None,
            "payload": payload,
            "recipient_email": None,
            "mention_emails": ["owner@example.com"],
        }

    try:
        await notifications._insert_alert_rows(
            [
                row(loud_key, payload="first", silent=False),
                row(silent_key, payload="historical", silent=True),
            ]
        )
        # First write wins: re-recording must not rewrite payload or state.
        await notifications._insert_alert_rows(
            [row(loud_key, payload="second", silent=False)]
        )

        pending = {
            pending_row.alert_key: pending_row
            for pending_row in await notifications._pending_alert_rows()
        }
        assert silent_key not in pending
        assert pending[loud_key].payload == "first"
        assert pending[loud_key].mention_emails == ["owner@example.com"]

        await notifications._mark_alert_sent(loud_key)
        assert loud_key not in {
            pending_row.alert_key
            for pending_row in await notifications._pending_alert_rows()
        }
    finally:
        async with get_session() as session:
            await session.execute(
                notifications.SlackExpenseAlertModel.__table__.delete().where(
                    notifications.SlackExpenseAlertModel.alert_key.in_(
                        [loud_key, silent_key]
                    )
                )
            )
