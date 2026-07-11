from __future__ import annotations

import html
import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from urllib.parse import quote

import httpx
import modal
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from modal_app import app, image, slack_notification_secrets
from models import SlackExpenseAlertModel, UserModel
from oddish.core.admin import _real_spend_filter
from oddish.core.cost_basis import (
    CANCELLED_HARBOR_STAGE,
    settled_cost_columns,
    settled_cost_from_row,
)
from oddish.db import (
    ExperimentModel,
    TrialModel,
    TrialStatus,
    close_database_connections,
    get_session,
)
from oddish.model_pricing import has_pricing

log = logging.getLogger("oddish.slack_notifications")


@dataclass(frozen=True)
class ExperimentCandidate:
    id: str
    name: str
    owner: str | None
    active_trials: int
    owner_email: str | None = None


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
class SlackAlert:
    key: str
    text: str
    recipient_email: str | None = None
    email_subject: str | None = None
    email_text: str | None = None


def _env_float(name: str, default: float) -> float:
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def _escape(value: str) -> str:
    return html.escape(value, quote=False)


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
    experiments: list[ExperimentCandidate],
    trials: list[TrialSpend],
    *,
    recent_cutoff: datetime,
    dashboard_url: str,
    experiment_threshold_usd: float,
    experiment_repeat_usd: float,
    trial_threshold_usd: float,
    trial_average_multiplier: float,
    unpriced_models: list[UnpricedModel] | None = None,
) -> list[SlackAlert]:
    trials_by_experiment: dict[str, list[TrialSpend]] = {}
    task_model_costs: dict[tuple[str, str], float] = {}
    task_model_counts: dict[tuple[str, str], int] = {}
    for trial in trials:
        trials_by_experiment.setdefault(trial.experiment_id, []).append(trial)
        peer_key = (trial.task_id, trial.model)
        task_model_costs[peer_key] = task_model_costs.get(peer_key, 0) + trial.cost_usd
        task_model_counts[peer_key] = task_model_counts.get(peer_key, 0) + 1

    alerts: list[SlackAlert] = []
    experiments_by_id = {experiment.id: experiment for experiment in experiments}
    for experiment in experiments:
        experiment_trials = trials_by_experiment.get(experiment.id, [])
        total_cost = sum(trial.cost_usd for trial in experiment_trials)
        milestones = _experiment_milestones(
            total_cost,
            experiment_threshold_usd,
            experiment_repeat_usd,
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
                email_subject = (
                    f"Cost alert: {experiment.name} reached ${milestone:,.0f}"
                )
                email_text = (
                    "Expensive experiment\n\n"
                    f"{experiment.name}\n"
                    f"Spend milestone: ${milestone:,.2f}\n"
                    f"Current spend: ${total_cost:,.2f}\n"
                    f"Trials still running: {experiment.active_trials}\n"
                    f"Owner: {experiment.owner or 'Unknown'}\n\n"
                    "Top agent costs:\n"
                    + "\n".join(
                        f"- {model}: ${cost:,.2f}" for model, cost in top_agent_costs
                    )
                    + f"\n\nOpen experiment: {experiment_url}"
                )
                alerts.append(
                    SlackAlert(
                        key=f"experiment:{experiment.id}:{milestone:g}",
                        text=(
                            ":money_with_wings: *Expensive experiment*\n"
                            f"Title: *{_escape(experiment.name)}*\n"
                            f"Spend milestone: *${milestone:,.2f}* "
                            f"(current spend: *${total_cost:,.2f}*)\n"
                            f"Trials still running: {experiment.active_trials}\n"
                            f"Owner: *{_escape(experiment.owner or 'Unknown')}*\n"
                            "Top agent costs:\n"
                            f"{top_agent_cost_lines}\n"
                            f"<{experiment_url}|open experiment>"
                        ),
                        recipient_email=experiment.owner_email,
                        email_subject=email_subject,
                        email_text=email_text,
                    )
                )

        for trial in experiment_trials:
            if (
                trial.finished_at < recent_cutoff
                or trial.cost_usd <= trial_threshold_usd
            ):
                continue
            peer_key = (trial.task_id, trial.model)
            peer_count = task_model_counts[peer_key] - 1
            if peer_count == 0:
                continue
            peer_average = (task_model_costs[peer_key] - trial.cost_usd) / peer_count
            if trial.cost_usd <= peer_average * trial_average_multiplier:
                continue
            comparison = (
                f"{trial.cost_usd / peer_average:.1f}× the same-task/model average"
                if peer_average
                else "other same-task/model trials averaged $0"
            )
            task_url = f"{dashboard_url}/tasks/{quote(trial.task_id, safe='')}"
            experiment = experiments_by_id[trial.experiment_id]
            email_subject = f"Cost alert: expensive trial in {experiment.name}"
            email_text = (
                "Expensive trial\n\n"
                f"{trial.name}\n"
                f"Experiment: {experiment.name}\n"
                f"Cost: ${trial.cost_usd:,.2f} — {comparison}\n"
                f"Model: {trial.model}\n"
                f"Author: {experiment.owner or 'Unknown'}\n\n"
                f"Open task: {task_url}"
            )
            alerts.append(
                SlackAlert(
                    key=(
                        f"trial:{trial.id}:{trial_threshold_usd:g}:"
                        f"{trial_average_multiplier:g}"
                    ),
                    text=(
                        ":warning: *Expensive trial*\n"
                        f"Title: `{_escape(trial.name)}`\n"
                        f"Experiment: *{_escape(experiment.name)}*\n"
                        f"Cost: *${trial.cost_usd:,.2f}* — {comparison}\n"
                        f"Model: `{_escape(trial.model)}`\n"
                        f"Author: *{_escape(experiment.owner or 'Unknown')}*\n"
                        f"<{task_url}|open task>"
                    ),
                    recipient_email=experiment.owner_email,
                    email_subject=email_subject,
                    email_text=email_text,
                )
            )

    for unpriced in unpriced_models or []:
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
                    UserModel.email.label("owner_email"),
                    func.count(TrialModel.id)
                    .filter(active_trial)
                    .label("active_trials"),
                )
                .join(TrialModel, TrialModel.experiment_id == ExperimentModel.id)
                .outerjoin(
                    UserModel,
                    and_(
                        UserModel.id == ExperimentModel.owner_user_id,
                        UserModel.org_id == ExperimentModel.org_id,
                        UserModel.is_active.is_(True),
                        UserModel.deleted_at.is_(None),
                    ),
                )
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
                    UserModel.email,
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
    dashboard_url = os.environ.get(
        "ODDISH_DASHBOARD_URL", "https://www.oddish.app"
    ).rstrip("/")
    return build_alerts(
        experiments,
        trials,
        recent_cutoff=recent_cutoff,
        dashboard_url=dashboard_url,
        experiment_threshold_usd=_env_float(
            "ODDISH_SLACK_EXPENSIVE_EXPERIMENT_USD", 1000
        ),
        experiment_repeat_usd=_env_float("ODDISH_SLACK_EXPERIMENT_REPEAT_USD", 1000),
        trial_threshold_usd=_env_float("ODDISH_SLACK_EXPENSIVE_TRIAL_USD", 70),
        trial_average_multiplier=_env_float("ODDISH_SLACK_TRIAL_AVERAGE_MULTIPLIER", 2),
        unpriced_models=unpriced_models,
    )


async def _post(webhook_url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(webhook_url, json={"text": text})
        response.raise_for_status()


async def _post_email(
    api_key: str,
    sender: str,
    recipient: str,
    subject: str,
    body: str,
) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": sender,
                "to": [recipient],
                "subject": subject,
                "text": body,
            },
        )
        response.raise_for_status()


async def _claim_alert(alert_key: str) -> bool:
    now = datetime.now(timezone.utc)
    statement = (
        insert(SlackExpenseAlertModel)
        .values(alert_key=alert_key, claimed_at=now)
        .on_conflict_do_nothing(index_elements=[SlackExpenseAlertModel.alert_key])
        .returning(SlackExpenseAlertModel.alert_key)
    )
    async with get_session() as session:
        return (await session.scalar(statement)) is not None


async def _mark_alert_sent(alert_key: str) -> None:
    async with get_session() as session:
        await session.execute(
            update(SlackExpenseAlertModel)
            .where(SlackExpenseAlertModel.alert_key == alert_key)
            .values(notified_at=datetime.now(timezone.utc))
        )


async def _release_alert(alert_key: str) -> None:
    async with get_session() as session:
        await session.execute(
            delete(SlackExpenseAlertModel).where(
                SlackExpenseAlertModel.alert_key == alert_key,
                SlackExpenseAlertModel.notified_at.is_(None),
            )
        )


async def send_alerts(webhook_url: str, alerts: list[SlackAlert]) -> None:
    for alert in alerts:
        if not await _claim_alert(alert.key):
            continue
        try:
            await _post(webhook_url, alert.text)
        except Exception:
            await _release_alert(alert.key)
            log.exception("slack expense alert post failed alert_key=%s", alert.key)
            continue
        await _mark_alert_sent(alert.key)


async def send_owner_emails(
    api_key: str,
    sender: str,
    alerts: list[SlackAlert],
) -> None:
    for alert in alerts:
        if not (alert.recipient_email and alert.email_subject and alert.email_text):
            continue
        recipient = alert.recipient_email.strip().lower()
        if not recipient:
            continue
        claim_key = f"email:{alert.key}:{recipient}"
        if not await _claim_alert(claim_key):
            continue
        try:
            await _post_email(
                api_key,
                sender,
                recipient,
                alert.email_subject,
                alert.email_text,
            )
        except Exception:
            await _release_alert(claim_key)
            log.exception(
                "expense alert email failed alert_key=%s recipient=%s",
                alert.key,
                recipient,
            )
            continue
        await _mark_alert_sent(claim_key)


@app.function(
    image=image,
    secrets=slack_notification_secrets,
    timeout=120,
    max_containers=1,
    schedule=modal.Period(seconds=300),
)
async def send_slack_expense_notifications() -> None:
    webhook_url = os.environ.get("SLACK_EXPENSE_WEBHOOK_URL", "").strip()
    email_api_key = os.environ.get("RESEND_API_KEY", "").strip()
    email_sender = os.environ.get("ODDISH_EXPENSE_EMAIL_FROM", "").strip()
    if not webhook_url and not (email_api_key and email_sender):
        return
    try:
        alerts = await load_alerts()
        if webhook_url:
            await send_alerts(webhook_url, alerts)
        if email_api_key and email_sender:
            await send_owner_emails(email_api_key, email_sender, alerts)
    finally:
        await close_database_connections()
