from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote

import httpx
import modal
from sqlalchemy import and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from auth.provisioning import fetch_slack_user_id_from_clerk
from modal_app import app, image, slack_notification_secrets
from models import SlackExpenseAlertModel, UserModel
from oddish.core.admin import _real_spend_filter
from oddish.core.cost_basis import (
    CANCELLED_HARBOR_STAGE,
    settled_cost_columns,
    settled_cost_from_row,
)
from oddish.core.endpoints._common import USER_CANCELLED_MESSAGE
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialStatus,
    VerdictStatus,
    close_database_connections,
    get_session,
)
from oddish.model_pricing import has_pricing

log = logging.getLogger("oddish.slack_notifications")

# Everyone here is @-mentioned in the channel when a single trial clears
# TRIAL_ESCALATION_USD. Matched to Slack accounts by email, so these must be the
# addresses on their Slack profiles, not necessarily their Clerk logins.
ALWAYS_PING_EMAILS: tuple[str, ...] = (
    "charles@abundant.ai",
    "ke@abundant.ai",
    "meji@abundant.ai",
    "jesse@abundant.ai",
)

EXPERIMENT_MILESTONE_USD = 1000.0
EXPERIMENT_REPEAT_USD = 1000.0
TRIAL_PING_USD = 200.0
TRIAL_ESCALATION_USD = 1000.0
EXPERIMENT_FAILED_RATIO = 0.5

# Slack lookup errors that genuinely mean "this person has no Slack account".
# Only these are safe to treat as a settled answer and cache.
_TERMINAL_LOOKUP_ERRORS = frozenset({"users_not_found", "invalid_email"})

# Healthy Slack API calls finish well under a second; a tight budget keeps a
# hung or throttled call from eating the serial delivery window.
_SLACK_TIMEOUT_SECONDS = 5


@dataclass(frozen=True)
class ExperimentCandidate:
    id: str
    name: str
    owner: str | None
    active_trials: int
    owner_email: str | None = None
    owner_clerk_user_id: str | None = None


@dataclass(frozen=True)
class TrialSpend:
    id: str
    name: str
    task_id: str
    experiment_id: str
    model: str
    finished_at: datetime
    cost_usd: float


@dataclass(frozen=True)
class UnpricedModel:
    model: str
    trial_count: int
    task_id: str


@dataclass(frozen=True)
class FailedExperiment:
    id: str
    name: str
    owner: str | None
    failed_trials: int
    total_trials: int
    owner_email: str | None = None
    owner_clerk_user_id: str | None = None


@dataclass(frozen=True)
class FailedTrial:
    name: str
    task_id: str
    task_version_id: str | None
    experiment_name: str
    owner: str | None
    owner_email: str | None = None
    owner_clerk_user_id: str | None = None


@dataclass(frozen=True)
class QaFailure:
    task_id: str
    task_name: str
    task_version_id: str | None
    reason: str
    owner_email: str | None = None
    owner_clerk_user_id: str | None = None


@dataclass(frozen=True)
class AlertCandidates:
    experiments: list[ExperimentCandidate] = field(default_factory=list)
    trials: list[TrialSpend] = field(default_factory=list)
    unpriced_models: list[UnpricedModel] = field(default_factory=list)
    failed_experiments: list[FailedExperiment] = field(default_factory=list)
    failed_trials: list[FailedTrial] = field(default_factory=list)
    qa_failures: list[QaFailure] = field(default_factory=list)


@dataclass(frozen=True)
class SlackAlert:
    key: str
    text: str
    recipient_email: str | None = None
    recipient_clerk_user_id: str | None = None
    mention_emails: tuple[str, ...] = ()
    dm_only: bool = False
    silent: bool = False


_MILESTONE_EPSILON = 1e-6


def _escape(value: str) -> str:
    return html.escape(value, quote=False)


def _version_bucket(task_id: str, task_version_id: str | None) -> str:
    # Trials predating task versioning have no task_version_id; bucketing them
    # by task keeps one-alert-per-version dedup from degrading into per-trial
    # spam on legacy rows.
    return task_version_id or task_id


def _task_url(dashboard_url: str, task_id: str, task_version_id: str | None) -> str:
    url = f"{dashboard_url}/tasks/{quote(task_id, safe='')}"
    if task_version_id:
        url = f"{url}?version={quote(task_version_id, safe='')}"
    return url


def _mention_targets(*emails: str | None) -> tuple[str, ...]:
    targets: dict[str, None] = {}
    for email in emails:
        normalized = (email or "").strip().lower()
        if normalized:
            targets.setdefault(normalized, None)
    return tuple(targets)


def _verdict_reason(verdict_status: VerdictStatus | None, error: str | None) -> str:
    if verdict_status != VerdictStatus.FAILED:
        return "verdict judged this task not good"
    return f"verdict job failed — {error}" if error else "verdict job failed"


def _experiment_milestones(
    total_cost: float,
    first_threshold: float,
    repeat_interval: float,
) -> list[float]:
    total = Decimal(str(total_cost))
    first = Decimal(str(first_threshold))
    repeat = Decimal(str(repeat_interval))
    if total <= 0 or total < first:
        return []
    if repeat <= 0:
        return [first_threshold]

    milestone_count = int((total - first) // repeat) + 1
    return [float(first + repeat * index) for index in range(milestone_count)]


def build_alerts(
    candidates: AlertCandidates,
    *,
    recent_cutoff: datetime,
    dashboard_url: str,
) -> list[SlackAlert]:
    trials_by_experiment: dict[str, list[TrialSpend]] = {}
    for trial in candidates.trials:
        trials_by_experiment.setdefault(trial.experiment_id, []).append(trial)

    alerts: list[SlackAlert] = []
    seen_failure_dms: set[tuple[str, str]] = set()

    def add_failure_dm(
        key: str,
        recipient: str | None,
        text: str,
        clerk_user_id: str | None = None,
    ) -> None:
        # Dedup on (key, recipient), not key alone: many broken trials on one
        # task version collapse to a single DM per person, but two owners
        # running that same version each still hear about it. The durable DM
        # claim key carries the recipient too, so both agree.
        dedup = (key, (recipient or "").strip().lower())
        if dedup in seen_failure_dms:
            return
        seen_failure_dms.add(dedup)
        alerts.append(
            SlackAlert(
                key=key,
                text=text,
                recipient_email=recipient,
                recipient_clerk_user_id=clerk_user_id,
                dm_only=True,
            )
        )

    for experiment in candidates.experiments:
        experiment_trials = trials_by_experiment.get(experiment.id, [])
        total_cost = sum(trial.cost_usd for trial in experiment_trials)
        recent_spend = sum(
            trial.cost_usd
            for trial in experiment_trials
            if trial.finished_at >= recent_cutoff
        )
        baseline_cost = total_cost - recent_spend
        milestones = _experiment_milestones(
            total_cost,
            EXPERIMENT_MILESTONE_USD,
            EXPERIMENT_REPEAT_USD,
        )
        if milestones:
            agent_costs: dict[str, float] = {}
            for trial in experiment_trials:
                agent_costs[trial.model] = (
                    agent_costs.get(trial.model, 0) + trial.cost_usd
                )
            top_agent_costs = sorted(
                agent_costs.items(), key=lambda item: (-item[1], item[0])
            )[:3]
            top_agent_cost_lines = "\n".join(
                f"• `{_escape(model)}`: *${cost:,.2f}*"
                for model, cost in top_agent_costs
            )
            experiment_url = (
                f"{dashboard_url}/experiments/"
                f"{quote(quote(experiment.id, safe=''), safe='')}"
            )
            for milestone in milestones:
                key = f"experiment:{experiment.id}:{milestone:g}"
                # Milestones already covered before the watch window opened are
                # historical: claim them silently so pre-existing spend never
                # dumps a backlog of alerts into any delivery channel.
                silent = milestone <= baseline_cost + _MILESTONE_EPSILON
                alerts.append(
                    SlackAlert(
                        key=key,
                        text=(
                            ":money_with_wings: *Expensive experiment*\n"
                            f"Title: *{_escape(experiment.name)}*\n"
                            f"Spend milestone: *${milestone:,.2f}* "
                            f"(current spend: *${total_cost:,.2f}*)\n"
                            f"New spend (last 2h): *${recent_spend:,.2f}*\n"
                            f"Trials still running: {experiment.active_trials}\n"
                            f"Owner: *{_escape(experiment.owner or 'Unknown')}*\n"
                            "Top agent costs:\n"
                            f"{top_agent_cost_lines}\n"
                            f"<{experiment_url}|open experiment>"
                        ),
                        recipient_email=experiment.owner_email,
                        recipient_clerk_user_id=experiment.owner_clerk_user_id,
                        dm_only=True,
                        silent=silent,
                    )
                )

        for trial in experiment_trials:
            if trial.finished_at < recent_cutoff or trial.cost_usd <= TRIAL_PING_USD:
                continue
            task_url = f"{dashboard_url}/tasks/{quote(trial.task_id, safe='')}"
            escalated = trial.cost_usd > TRIAL_ESCALATION_USD
            heading = (
                ":rotating_light: *Very expensive trial*"
                if escalated
                else ":warning: *Expensive trial*"
            )
            text = (
                f"{heading}\n"
                f"Title: `{_escape(trial.name)}`\n"
                f"Experiment: *{_escape(experiment.name)}*\n"
                f"Cost: *${trial.cost_usd:,.2f}*\n"
                f"Model: `{_escape(trial.model)}`\n"
                f"Author: *{_escape(experiment.owner or 'Unknown')}*\n"
                f"<{task_url}|open task>"
            )
            alerts.append(
                SlackAlert(
                    key=f"trial:{trial.id}:{TRIAL_PING_USD:g}",
                    text=text,
                    recipient_email=experiment.owner_email,
                    recipient_clerk_user_id=experiment.owner_clerk_user_id,
                    dm_only=True,
                )
            )
            if escalated:
                # The DM above covers the owner; this keeps spend this large
                # visible outside the DM.
                alerts.append(
                    SlackAlert(
                        key=f"trial-escalation:{trial.id}:{TRIAL_ESCALATION_USD:g}",
                        text=text,
                        mention_emails=_mention_targets(
                            experiment.owner_email, *ALWAYS_PING_EMAILS
                        ),
                    )
                )

    for failed in candidates.failed_experiments:
        if (
            not failed.failed_trials
            or not failed.total_trials
            or failed.failed_trials < failed.total_trials * EXPERIMENT_FAILED_RATIO
        ):
            continue
        add_failure_dm(
            f"experiment-failed:{failed.id}",
            failed.owner_email,
            ":x: *Experiment failed*\n"
            f"Title: *{_escape(failed.name)}*\n"
            f"Failed trials: *{failed.failed_trials}/{failed.total_trials}*\n"
            f"Owner: *{_escape(failed.owner or 'Unknown')}*\n"
            f"<{dashboard_url}/experiments/"
            f"{quote(quote(failed.id, safe=''), safe='')}|open experiment>",
            failed.owner_clerk_user_id,
        )

    for failed_trial in candidates.failed_trials:
        bucket = _version_bucket(failed_trial.task_id, failed_trial.task_version_id)
        task_url = _task_url(
            dashboard_url, failed_trial.task_id, failed_trial.task_version_id
        )
        add_failure_dm(
            f"trial-failed:{bucket}",
            failed_trial.owner_email,
            ":boom: *Trial failed*\n"
            f"Title: `{_escape(failed_trial.name)}`\n"
            f"Experiment: *{_escape(failed_trial.experiment_name)}*\n"
            f"Owner: *{_escape(failed_trial.owner or 'Unknown')}*\n"
            f"<{task_url}|open task>",
            failed_trial.owner_clerk_user_id,
        )

    for qa_failure in candidates.qa_failures:
        bucket = _version_bucket(qa_failure.task_id, qa_failure.task_version_id)
        task_url = _task_url(
            dashboard_url, qa_failure.task_id, qa_failure.task_version_id
        )
        add_failure_dm(
            f"qa-failed:{bucket}",
            qa_failure.owner_email,
            ":mag: *QA failed*\n"
            f"Task: *{_escape(qa_failure.task_name)}*\n"
            f"Reason: {_escape(qa_failure.reason)}\n"
            f"<{task_url}|open task>",
            qa_failure.owner_clerk_user_id,
        )

    for unpriced in candidates.unpriced_models:
        plural = "s" if unpriced.trial_count != 1 else ""
        task_url = f"{dashboard_url}/tasks/{quote(unpriced.task_id, safe='')}"
        alerts.append(
            SlackAlert(
                key=f"unpriced-model:{unpriced.model}",
                text=(
                    f":grey_question: *Unpriceable model:* "
                    f"`{_escape(unpriced.model)}` has no price — "
                    f"{unpriced.trial_count} recent trial{plural} recorded "
                    f"token usage but settled to *$0* because no rate could be "
                    f"resolved (spend is going uncounted). Add it to the "
                    f"pricing table · <{task_url}|open task>"
                ),
            )
        )
    return alerts


def _owner_email_by_handle():
    """Resolve the notify target from the experiment's shown ``owner`` handle.

    We notify the person the alert names: ``owner``, the displayed GitHub handle.
    Not ``owner_user_id`` -- a set-once FK that falls back to the API key's creator,
    so on re-runs and off-the-shelf sweeps it points at a different person than the
    one named. Returns the email only when the handle maps to exactly one active
    user in the org; a shared or unlinked handle yields NULL, notifying no one
    rather than the wrong person.
    """
    handle = func.lower(func.ltrim(ExperimentModel.owner, "@"))
    return (
        select(case((func.count() == 1, func.max(UserModel.email)), else_=None))
        .where(
            ExperimentModel.owner.isnot(None),
            UserModel.github_username.isnot(None),
            func.lower(func.ltrim(UserModel.github_username, "@")) == handle,
            UserModel.org_id == ExperimentModel.org_id,
            UserModel.is_active.is_(True),
            UserModel.deleted_at.is_(None),
        )
        .scalar_subquery()
    )


def _owner_clerk_user_id_by_handle():
    """Clerk user id of the single active user ``_owner_email_by_handle`` resolves.

    Kept parallel to that resolver -- same shown-handle match -- so the
    Clerk-linked Slack lookup targets the person the alert names. NULL (delivery
    falls back to the email lookup) when the handle is shared, unlinked, or that
    user has no linked Clerk id.
    """
    handle = func.lower(func.ltrim(ExperimentModel.owner, "@"))
    return (
        select(case((func.count() == 1, func.max(UserModel.clerk_user_id)), else_=None))
        .where(
            ExperimentModel.owner.isnot(None),
            UserModel.github_username.isnot(None),
            func.lower(func.ltrim(UserModel.github_username, "@")) == handle,
            UserModel.org_id == ExperimentModel.org_id,
            UserModel.is_active.is_(True),
            UserModel.deleted_at.is_(None),
        )
        .scalar_subquery()
    )


async def load_alerts(now: datetime | None = None) -> list[SlackAlert]:
    now = now or datetime.now(timezone.utc)
    recent_cutoff = now - timedelta(hours=2)
    active_statuses = [
        TrialStatus.PENDING,
        TrialStatus.QUEUED,
        TrialStatus.RUNNING,
        TrialStatus.RETRYING,
    ]
    active_trial = and_(
        TrialModel.deleted_at.is_(None),
        TrialModel.status.in_(active_statuses),
    )

    async with get_session() as session:
        candidate_rows = (
            await session.execute(
                select(
                    ExperimentModel.id,
                    ExperimentModel.name,
                    ExperimentModel.owner,
                    _owner_email_by_handle().label("owner_email"),
                    _owner_clerk_user_id_by_handle().label("owner_clerk_user_id"),
                    func.count(TrialModel.id)
                    .filter(active_trial)
                    .label("active_trials"),
                )
                .join(TrialModel, TrialModel.experiment_id == ExperimentModel.id)
                .where(
                    ExperimentModel.is_collection.is_(False),
                    ExperimentModel.deleted_at.is_(None),
                    _real_spend_filter(),
                    or_(
                        active_trial,
                        TrialModel.finished_at >= recent_cutoff,
                    ),
                )
                .group_by(
                    ExperimentModel.id,
                    ExperimentModel.name,
                    ExperimentModel.owner,
                    ExperimentModel.org_id,
                )
                .execution_options(include_deleted=True)
            )
        ).all()
        experiments = [
            ExperimentCandidate(
                id=str(row.id),
                name=str(row.name),
                owner=row.owner,
                active_trials=int(row.active_trials or 0),
                owner_email=row.owner_email,
                owner_clerk_user_id=row.owner_clerk_user_id,
            )
            for row in candidate_rows
        ]
        # Expensive-experiment/trial alerts need candidate experiments, but the
        # unpriceable-model scan below is independent of them (an unpriced trial
        # can live under a collection or soft-deleted experiment that is never a
        # candidate), so don't early-return on an empty candidate set -- only
        # skip the experiment-scoped trial query.
        experiment_ids = [experiment.id for experiment in experiments]
        trial_rows = []
        if experiment_ids:
            trial_rows = (
                await session.execute(
                    select(
                        TrialModel.id,
                        TrialModel.name,
                        TrialModel.task_id,
                        TrialModel.experiment_id,
                        TrialModel.finished_at,
                        TrialModel.model,
                        *settled_cost_columns(),
                    )
                    .where(
                        TrialModel.experiment_id.in_(experiment_ids),
                        TrialModel.finished_at.isnot(None),
                        _real_spend_filter(),
                    )
                    .group_by(
                        TrialModel.id,
                        TrialModel.name,
                        TrialModel.task_id,
                        TrialModel.experiment_id,
                        TrialModel.finished_at,
                        TrialModel.model,
                    )
                    .execution_options(include_deleted=True)
                )
            ).all()

        # Trials that ran (recorded tokens) but settled to NULL cost: no native
        # cost and no token estimate. Grouping by model lets us alert once per
        # model rather than once per trial. Whether the model is genuinely
        # unpriceable -- rather than merely tokenless -- is decided below with
        # ``has_pricing``: the token-estimate path shares that same lookup, so a
        # priced model always produces an estimate here and never reaches the
        # alert, and an unpriced one never produces a token estimate to hide it.
        unpriced_rows = (
            await session.execute(
                select(
                    TrialModel.model,
                    func.count(TrialModel.id).label("trial_count"),
                    func.max(TrialModel.task_id).label("task_id"),
                )
                .where(
                    TrialModel.finished_at >= recent_cutoff,
                    TrialModel.cost_usd.is_(None),
                    func.coalesce(TrialModel.harbor_stage, "")
                    != CANCELLED_HARBOR_STAGE,
                    or_(
                        func.coalesce(TrialModel.input_tokens, 0) > 0,
                        func.coalesce(TrialModel.output_tokens, 0) > 0,
                        func.coalesce(TrialModel.cache_write_tokens, 0) > 0,
                    ),
                    _real_spend_filter(),
                )
                .group_by(TrialModel.model)
                .execution_options(include_deleted=True)
            )
        ).all()

        current_trial = and_(
            TrialModel.deleted_at.is_(None),
            TrialModel.superseded_by_trial_id.is_(None),
            or_(
                func.coalesce(TrialModel.harbor_stage, "") != CANCELLED_HARBOR_STAGE,
                # The orphan reaper uses the cancelled runtime stage too, but
                # exhausted reaps are real FAILED outcomes and must count
                # toward failed-experiment notifications.
                TrialModel.stale_reaped_at.isnot(None),
            ),
        )
        recently_finished = (
            select(TrialModel.experiment_id)
            .where(
                current_trial,
                TrialModel.finished_at >= recent_cutoff,
                _real_spend_filter(),
            )
            .distinct()
        )
        failed_rows = (
            await session.execute(
                select(
                    ExperimentModel.id,
                    ExperimentModel.name,
                    ExperimentModel.owner,
                    _owner_email_by_handle().label("owner_email"),
                    _owner_clerk_user_id_by_handle().label("owner_clerk_user_id"),
                    func.count(TrialModel.id).label("total_trials"),
                    func.count(TrialModel.id)
                    .filter(TrialModel.status == TrialStatus.FAILED)
                    .label("failed_trials"),
                )
                .join(TrialModel, TrialModel.experiment_id == ExperimentModel.id)
                .where(
                    ExperimentModel.is_collection.is_(False),
                    ExperimentModel.deleted_at.is_(None),
                    ExperimentModel.id.in_(recently_finished),
                    current_trial,
                    _real_spend_filter(),
                )
                .group_by(
                    ExperimentModel.id,
                    ExperimentModel.name,
                    ExperimentModel.owner,
                    ExperimentModel.org_id,
                )
                .having(
                    func.count(TrialModel.id).filter(
                        TrialModel.status.in_(active_statuses)
                    )
                    == 0
                )
                .execution_options(include_deleted=True)
            )
        ).all()

        # A crashed agent still gets its verifier run, so the row lands as
        # SUCCESS with reward 0 and a harbor_exception marker. Matching on
        # status alone would miss exactly the breakages worth a DM.
        broken_trial = or_(
            TrialModel.status == TrialStatus.FAILED,
            and_(
                TrialModel.status == TrialStatus.SUCCESS,
                TrialModel.result["harbor_exception"].astext.isnot(None),
            ),
        )
        failed_trial_rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.name,
                    TrialModel.task_id,
                    TrialModel.task_version_id,
                    ExperimentModel.name.label("experiment_name"),
                    ExperimentModel.owner,
                    _owner_email_by_handle().label("owner_email"),
                    _owner_clerk_user_id_by_handle().label("owner_clerk_user_id"),
                )
                .join(ExperimentModel, ExperimentModel.id == TrialModel.experiment_id)
                .where(
                    ExperimentModel.is_collection.is_(False),
                    ExperimentModel.deleted_at.is_(None),
                    current_trial,
                    TrialModel.finished_at >= recent_cutoff,
                    broken_trial,
                    _real_spend_filter(),
                )
                .execution_options(include_deleted=True)
            )
        ).all()

        # verdict_status FAILED also covers user-cancelled verdicts, which are
        # not QA verdicts at all -- excluding them here keeps cancellations from
        # DMing owners a bogus "QA failed".
        qa_failed = or_(
            and_(
                TaskModel.verdict_status == VerdictStatus.SUCCESS,
                TaskModel.verdict["is_good"].astext == "false",
            ),
            and_(
                TaskModel.verdict_status == VerdictStatus.FAILED,
                func.coalesce(TaskModel.verdict_error, "") != USER_CANCELLED_MESSAGE,
            ),
        )
        qa_rows = (
            await session.execute(
                select(
                    TaskModel.id,
                    TaskModel.name,
                    TaskModel.current_version_id,
                    TaskModel.verdict_status,
                    TaskModel.verdict_error,
                    UserModel.email.label("owner_email"),
                    UserModel.clerk_user_id.label("owner_clerk_user_id"),
                )
                .outerjoin(
                    UserModel,
                    and_(
                        UserModel.id == TaskModel.created_by_user_id,
                        UserModel.org_id == TaskModel.org_id,
                        UserModel.is_active.is_(True),
                        UserModel.deleted_at.is_(None),
                    ),
                )
                .where(
                    TaskModel.deleted_at.is_(None),
                    TaskModel.verdict_finished_at >= recent_cutoff,
                    qa_failed,
                )
                .execution_options(include_deleted=True)
            )
        ).all()

    trials = [
        TrialSpend(
            id=str(row.id),
            name=str(row.name),
            task_id=str(row.task_id),
            experiment_id=str(row.experiment_id),
            model=str(row.model),
            finished_at=row.finished_at,
            cost_usd=settled_cost_from_row(row),
        )
        for row in trial_rows
    ]
    unpriced_models = [
        UnpricedModel(
            model=str(row.model),
            trial_count=int(row.trial_count or 0),
            task_id=str(row.task_id),
        )
        for row in unpriced_rows
        if row.model and not has_pricing(str(row.model))
    ]
    failed_experiments = [
        FailedExperiment(
            id=str(row.id),
            name=str(row.name),
            owner=row.owner,
            failed_trials=int(row.failed_trials or 0),
            total_trials=int(row.total_trials or 0),
            owner_email=row.owner_email,
            owner_clerk_user_id=row.owner_clerk_user_id,
        )
        for row in failed_rows
    ]
    failed_trial_records = [
        FailedTrial(
            name=str(row.name),
            task_id=str(row.task_id),
            task_version_id=row.task_version_id,
            experiment_name=str(row.experiment_name),
            owner=row.owner,
            owner_email=row.owner_email,
            owner_clerk_user_id=row.owner_clerk_user_id,
        )
        for row in failed_trial_rows
    ]
    qa_failures = [
        QaFailure(
            task_id=str(row.id),
            task_name=str(row.name),
            task_version_id=row.current_version_id,
            reason=_verdict_reason(row.verdict_status, row.verdict_error),
            owner_email=row.owner_email,
            owner_clerk_user_id=row.owner_clerk_user_id,
        )
        for row in qa_rows
    ]
    dashboard_url = os.environ.get(
        "ODDISH_DASHBOARD_URL", "https://www.oddish.app"
    ).rstrip("/")
    return build_alerts(
        AlertCandidates(
            experiments=experiments,
            trials=trials,
            unpriced_models=unpriced_models,
            failed_experiments=failed_experiments,
            failed_trials=failed_trial_records,
            qa_failures=qa_failures,
        ),
        recent_cutoff=recent_cutoff,
        dashboard_url=dashboard_url,
    )


async def _post(webhook_url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=_SLACK_TIMEOUT_SECONDS) as client:
        response = await client.post(webhook_url, json={"text": text})
        response.raise_for_status()


async def _lookup_slack_user(bot_token: str, email: str) -> str | None:
    async with httpx.AsyncClient(timeout=_SLACK_TIMEOUT_SECONDS) as client:
        response = await client.get(
            "https://slack.com/api/users.lookupByEmail",
            params={"email": email},
            headers={"Authorization": f"Bearer {bot_token}"},
        )
        response.raise_for_status()
        payload = response.json()
    if payload.get("ok"):
        return str(payload["user"]["id"])
    error = payload.get("error")
    if error not in _TERMINAL_LOOKUP_ERRORS:
        # Everything else -- ratelimited, service_unavailable, a revoked token --
        # is transient or operator-fixable. Raising keeps it on the retry path;
        # returning None here would let callers record it as a settled "no Slack
        # account" and drop the message for good.
        raise RuntimeError(f"slack user lookup failed: {error}")
    log.warning("slack user lookup failed email=%s error=%s", email, error)
    return None


async def _resolve_dm_slack_id(
    bot_token: str, clerk_user_id: str | None, email: str
) -> str | None:
    # Prefer the Slack account the user linked through Clerk: that identity is
    # authoritative, whereas users.lookupByEmail silently misses anyone whose
    # Oddish email differs from their Slack workspace email. Fall back to the
    # email lookup only when Clerk has no linked Slack account (or is
    # unreachable), so delivery never regresses for users who never linked one.
    if clerk_user_id:
        slack_user_id = await fetch_slack_user_id_from_clerk(clerk_user_id)
        if slack_user_id:
            return slack_user_id
    return await _lookup_slack_user(bot_token, email)


async def _post_dm(bot_token: str, slack_user_id: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=_SLACK_TIMEOUT_SECONDS) as client:
        open_response = await client.post(
            "https://slack.com/api/conversations.open",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"users": slack_user_id},
        )
        open_response.raise_for_status()
        open_payload = open_response.json()
        if not open_payload.get("ok"):
            raise RuntimeError(f"slack dm open failed: {open_payload.get('error')}")
        channel_id = open_payload.get("channel", {}).get("id")
        if not channel_id:
            raise RuntimeError("slack dm open failed: missing channel id")

        response = await client.post(
            "https://slack.com/api/chat.postMessage",
            headers={"Authorization": f"Bearer {bot_token}"},
            json={"channel": channel_id, "text": text},
        )
        response.raise_for_status()
        payload = response.json()
    if not payload.get("ok"):
        raise RuntimeError(f"slack dm failed: {payload.get('error')}")


async def _insert_alert_rows(rows: list[dict]) -> None:
    if not rows:
        return
    # First write wins: an existing pending or sent row keeps its original
    # payload and state, so re-recording an alert -- including re-recording it
    # as silent once its spend ages out of the recent window -- never rewrites
    # or revives history. A pending row keeps retrying until delivered.
    async with get_session() as session:
        await session.execute(
            insert(SlackExpenseAlertModel)
            .values(rows)
            .on_conflict_do_nothing(index_elements=[SlackExpenseAlertModel.alert_key])
        )


async def _pending_alert_rows() -> list:
    statement = (
        select(
            SlackExpenseAlertModel.alert_key,
            SlackExpenseAlertModel.payload,
            SlackExpenseAlertModel.recipient_email,
            SlackExpenseAlertModel.recipient_clerk_user_id,
            SlackExpenseAlertModel.mention_emails,
        )
        .where(SlackExpenseAlertModel.notified_at.is_(None))
        .order_by(SlackExpenseAlertModel.claimed_at)
    )
    async with get_session() as session:
        return (await session.execute(statement)).all()


async def _mark_alert_sent(*alert_keys: str) -> None:
    if not alert_keys:
        return
    async with get_session() as session:
        await session.execute(
            update(SlackExpenseAlertModel)
            .where(SlackExpenseAlertModel.alert_key.in_(alert_keys))
            .values(notified_at=datetime.now(timezone.utc))
        )


async def record_alerts(
    alerts: list[SlackAlert],
    *,
    channel: bool,
    dms: bool,
) -> None:
    """Write each alert into the outbox with its rendered payload.

    Silent alerts are recorded born-sent: they settle the milestone without a
    post. Storing the payload frees delivery from recomputation, so a failed
    post keeps retrying even after its spend ages out of the recent window.
    Only alert kinds whose delivery channel is configured are recorded, so
    nothing piles up undeliverable.
    """
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for alert in alerts:
        clerk_user_id: str | None = None
        if alert.dm_only:
            recipient: str | None = (alert.recipient_email or "").strip().lower()
            if not dms or not recipient:
                continue
            # Keyed per recipient, matching pre-outbox rows so already sent
            # DMs stay deduplicated.
            key = f"dm:{alert.key}:{recipient}"
            mentions: list | None = None
            # Carried into the row so delivery can prefer the recipient's
            # Clerk-linked Slack account over the email lookup.
            clerk_user_id = alert.recipient_clerk_user_id
        elif channel:
            key = alert.key
            recipient = None
            mentions = list(alert.mention_emails) or None
        else:
            continue
        rows.append(
            {
                "alert_key": key,
                "claimed_at": now,
                "notified_at": now if alert.silent else None,
                "payload": alert.text,
                "recipient_email": recipient,
                "recipient_clerk_user_id": clerk_user_id,
                "mention_emails": mentions,
            }
        )
    await _insert_alert_rows(rows)


async def _mention_prefix(
    bot_token: str,
    emails: tuple[str, ...],
    cache: dict[str, str | None],
) -> str:
    ids: list[str] = []
    for email in emails:
        if email not in cache:
            try:
                cache[email] = await _lookup_slack_user(bot_token, email)
            except Exception:
                # A mention is a nicety; never let lookup trouble sink the alert
                # itself. Drop only this id -- the rest of the escalation list
                # still gets pinged -- and leave the miss uncached to retry.
                log.exception("slack mention lookup failed email=%s", email)
                continue
        if slack_id := cache[email]:
            ids.append(slack_id)
    return " ".join(f"<@{slack_id}>" for slack_id in ids)


async def deliver_pending_alerts(webhook_url: str, bot_token: str) -> None:
    """Post every pending outbox row and mark it sent.

    At-least-once: a row is marked sent only after its post succeeds, and a
    failure leaves it pending for the next scheduled run. Slack posts have no
    idempotency key, so a crash between the post and the mark can duplicate a
    ping -- accepted, since a dropped spend alert costs far more than a
    repeated one.
    """
    lookup_cache: dict[str, str | None] = {}
    for row in await _pending_alert_rows():
        if not row.payload:
            # Pre-outbox claim with no payload to deliver; settle it.
            await _mark_alert_sent(row.alert_key)
            continue
        try:
            if row.recipient_email:
                if not bot_token:
                    continue
                if row.recipient_email not in lookup_cache:
                    # Prefer the recipient's Clerk-linked Slack account; fall
                    # back to matching their Oddish email against Slack's
                    # directory when they have none linked.
                    lookup_cache[row.recipient_email] = await _resolve_dm_slack_id(
                        bot_token, row.recipient_clerk_user_id, row.recipient_email
                    )
                if slack_user_id := lookup_cache[row.recipient_email]:
                    await _post_dm(bot_token, slack_user_id, row.payload)
                # A None lookup is the settled "no Slack account" answer --
                # transient errors raise instead -- so fall through and mark
                # the row sent rather than retrying forever.
            else:
                if not webhook_url:
                    continue
                text = row.payload
                if bot_token and row.mention_emails:
                    if prefix := await _mention_prefix(
                        bot_token, tuple(row.mention_emails), lookup_cache
                    ):
                        text = f"{prefix}\n{text}"
                await _post(webhook_url, text)
        except Exception:
            log.exception(
                "slack expense alert delivery failed alert_key=%s", row.alert_key
            )
            continue
        await _mark_alert_sent(row.alert_key)


@app.function(
    image=image,
    secrets=slack_notification_secrets,
    # Ten minutes: delivery is serial and alerts arrive in bursts, so give the
    # run room. The 5s Slack timeouts bound each call, and a run that overlaps
    # the next tick just delays it (max_containers=1) rather than failing.
    timeout=600,
    max_containers=1,
    schedule=modal.Period(seconds=300),
)
async def send_slack_expense_notifications() -> None:
    webhook_url = os.environ.get("SLACK_EXPENSE_WEBHOOK_URL", "").strip()
    bot_token = os.environ.get("SLACK_ALERT_BOT_TOKEN", "").strip()
    if not webhook_url and not bot_token:
        return
    try:
        alerts = await load_alerts()
        await record_alerts(alerts, channel=bool(webhook_url), dms=bool(bot_token))
        await deliver_pending_alerts(webhook_url, bot_token)
    finally:
        await close_database_connections()
