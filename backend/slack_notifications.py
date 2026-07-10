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
from models import SlackExpenseAlertModel
from oddish.core.admin import _real_spend_filter
from oddish.core.cost_basis import settled_cost_columns, settled_cost_from_row
from oddish.db import (
    ExperimentModel,
    TrialModel,
    TrialStatus,
    close_database_connections,
    get_session,
)

log = logging.getLogger("oddish.slack_notifications")


@dataclass(frozen=True)
class ExperimentCandidate:
    id: str
    name: str
    owner: str | None
    active_trials: int


@dataclass(frozen=True)
class TrialSpend:
    id: str
    task_id: str
    experiment_id: str
    finished_at: datetime
    cost_usd: float


@dataclass(frozen=True)
class SlackAlert:
    key: str
    text: str


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
) -> list[SlackAlert]:
    trials_by_experiment: dict[str, list[TrialSpend]] = {}
    for trial in trials:
        trials_by_experiment.setdefault(trial.experiment_id, []).append(trial)

    alerts: list[SlackAlert] = []
    for experiment in experiments:
        experiment_trials = trials_by_experiment.get(experiment.id, [])
        total_cost = sum(trial.cost_usd for trial in experiment_trials)
        milestones = _experiment_milestones(
            total_cost,
            experiment_threshold_usd,
            experiment_repeat_usd,
        )
        if milestones:
            phase = (
                f"{experiment.active_trials} trial"
                f"{'s' if experiment.active_trials != 1 else ''} still running"
                if experiment.active_trials
                else "finished"
            )
            experiment_url = (
                f"{dashboard_url}/experiments/"
                f"{quote(quote(experiment.id, safe=''), safe='')}"
            )
            for milestone in milestones:
                alerts.append(
                    SlackAlert(
                        key=f"experiment:{experiment.id}:{milestone:g}",
                        text=(
                            f":money_with_wings: *Expensive experiment:* "
                            f"*{_escape(experiment.name)}* reached the "
                            f"*${milestone:,.2f}* spend milestone "
                            f"(now *${total_cost:,.2f}*) — {phase} · "
                            f"owner: *{_escape(experiment.owner or 'Unknown')}* "
                            f"· <{experiment_url}|open experiment>"
                        ),
                    )
                )

        trial_count = len(experiment_trials)
        first_trial_id = (
            min(experiment_trials, key=lambda item: (item.finished_at, item.id)).id
            if experiment_trials
            else None
        )
        for trial in experiment_trials:
            if trial.finished_at < recent_cutoff or trial.cost_usd <= trial_threshold_usd:
                continue
            is_first_trial = trial.id == first_trial_id
            other_average = (
                None
                if trial_count == 1
                else (total_cost - trial.cost_usd) / (trial_count - 1)
            )
            if (
                not is_first_trial
                and other_average is not None
                and trial.cost_usd <= other_average * trial_average_multiplier
            ):
                continue
            comparison = (
                "first trial"
                if is_first_trial
                else f"{trial.cost_usd / other_average:.1f}× the experiment average"
                if other_average
                else "other trials averaged $0"
            )
            task_url = f"{dashboard_url}/tasks/{quote(trial.task_id, safe='')}"
            alerts.append(
                SlackAlert(
                    key=(
                        f"trial:{trial.id}:{trial_threshold_usd:g}:"
                        f"{trial_average_multiplier:g}"
                    ),
                    text=(
                        f":warning: *Expensive trial:* `{_escape(trial.id)}` cost "
                        f"*${trial.cost_usd:,.2f}* — {comparison} · "
                        f"<{task_url}|open task>"
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
                    func.count(TrialModel.id).filter(active_trial).label("active_trials"),
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
            )
            for row in candidate_rows
        ]
        if not experiments:
            return []

        experiment_ids = [experiment.id for experiment in experiments]
        trial_rows = (
            await session.execute(
                select(
                    TrialModel.id,
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
                    TrialModel.task_id,
                    TrialModel.experiment_id,
                    TrialModel.finished_at,
                    TrialModel.model,
                )
                .execution_options(include_deleted=True)
            )
        ).all()

    trials = [
        TrialSpend(
            id=str(row.id),
            task_id=str(row.task_id),
            experiment_id=str(row.experiment_id),
            finished_at=row.finished_at,
            cost_usd=settled_cost_from_row(row),
        )
        for row in trial_rows
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
        experiment_repeat_usd=_env_float(
            "ODDISH_SLACK_EXPERIMENT_REPEAT_USD", 1000
        ),
        trial_threshold_usd=_env_float("ODDISH_SLACK_EXPENSIVE_TRIAL_USD", 25),
        trial_average_multiplier=_env_float(
            "ODDISH_SLACK_TRIAL_AVERAGE_MULTIPLIER", 2
        ),
    )


async def _post(webhook_url: str, text: str) -> None:
    async with httpx.AsyncClient(timeout=30) as client:
        response = await client.post(webhook_url, json={"text": text})
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


@app.function(
    image=image,
    secrets=slack_notification_secrets,
    timeout=120,
    max_containers=1,
    schedule=modal.Period(seconds=300),
)
async def send_slack_expense_notifications() -> None:
    webhook_url = os.environ.get("SLACK_EXPENSE_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return
    try:
        await send_alerts(webhook_url, await load_alerts())
    finally:
        await close_database_connections()
