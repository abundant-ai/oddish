"""Small, read-only Slack unfurls for Oddish task and experiment links."""

from __future__ import annotations

import logging
import os
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import noload

from oddish.config import is_nop_oracle_agent
from oddish.core.experiment_membership import trial_in_experiment
from oddish.db import (
    ExperimentModel,
    TaskModel,
    TaskVersionModel,
    TrialModel,
    get_session,
    task_experiments,
)
from oddish.model_pricing import estimate_cost_usd

log = logging.getLogger(__name__)

_MAX_LINKS_PER_EVENT = 10
_MATRIX_TASK_LIMIT = 8
_MATRIX_COLUMN_LIMIT = 6
_MATRIX_GLYPH_LIMIT = 48
_TRIAL_LOAD_LIMIT = 500


@dataclass(frozen=True)
class UnfurlTarget:
    kind: Literal["task", "experiment"]
    identifier: str
    url: str
    public: bool = False


@dataclass(frozen=True)
class SlackUnfurlConfig:
    enabled: bool
    signing_secret: str
    bot_token: str
    org_id: str
    team_id: str
    allowed_channels: frozenset[str]
    dashboard_url: str

    @property
    def ready(self) -> bool:
        return bool(
            self.enabled
            and self.signing_secret
            and self.bot_token
            and self.org_id
            and self.dashboard_url
        )


def load_slack_unfurl_config() -> SlackUnfurlConfig:
    enabled = os.environ.get("ODDISH_SLACK_UNFURL_ENABLED", "").strip().lower()
    channels = os.environ.get("ODDISH_SLACK_UNFURL_ALLOWED_CHANNELS", "")
    return SlackUnfurlConfig(
        enabled=enabled in {"1", "true", "yes", "on"},
        signing_secret=os.environ.get("ODDISH_SLACK_UNFURL_SIGNING_SECRET", "").strip(),
        bot_token=os.environ.get("ODDISH_SLACK_UNFURL_BOT_TOKEN", "").strip(),
        org_id=os.environ.get("ODDISH_SLACK_UNFURL_ORG_ID", "").strip(),
        team_id=os.environ.get("ODDISH_SLACK_UNFURL_TEAM_ID", "").strip(),
        allowed_channels=frozenset(
            value.strip() for value in channels.split(",") if value.strip()
        ),
        dashboard_url=(
            os.environ.get("ODDISH_SLACK_UNFURL_DASHBOARD_URL")
            or os.environ.get("ODDISH_DASHBOARD_URL")
            or "https://www.oddish.app"
        ).strip(),
    )


@dataclass(frozen=True)
class TrialSnapshot:
    task_id: str
    task_name: str
    agent: str
    model: str | None
    status: str
    reward: float | None
    error_message: str | None
    input_tokens: int | None
    cache_tokens: int | None
    cache_write_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None


@dataclass(frozen=True)
class Summary:
    kind: Literal["task", "experiment"]
    name: str
    url: str
    trials: tuple[TrialSnapshot, ...]
    task_count: int
    version: int | None = None
    total_trials: int | None = None


def _decode_segment(value: str) -> str:
    # Experiment route params are double-encoded by the frontend so encoded
    # slashes survive Next's first path decode. Two bounded passes mirror it.
    decoded = value
    for _ in range(2):
        next_value = unquote(decoded)
        if next_value == decoded:
            break
        decoded = next_value
    return decoded


def parse_oddish_link(url: str, dashboard_url: str) -> UnfurlTarget | None:
    """Recognize only exact Oddish dashboard routes on the configured origin."""
    try:
        parsed = urlparse(url)
        dashboard = urlparse(dashboard_url)
    except ValueError:
        return None
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.scheme != dashboard.scheme
        or parsed.netloc.lower() != dashboard.netloc.lower()
        or parsed.username is not None
        or parsed.password is not None
    ):
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or not parts[1]:
        return None
    identifier = _decode_segment(parts[1])
    if not identifier or len(identifier) > 160:
        return None
    if parts[0] == "tasks":
        return UnfurlTarget("task", identifier, url)
    if parts[0] == "experiments":
        return UnfurlTarget("experiment", identifier, url)
    if parts[0] == "share":
        return UnfurlTarget("experiment", identifier, url, public=True)
    return None


def trial_outcome(trial: TrialSnapshot) -> str:
    """Mirror frontend ``getMatrixStatus`` for Slack's compact result row."""
    status = trial.status
    error = trial.error_message or ""
    has_reward = trial.reward is not None
    timed_out = "AgentTimeoutError" in error or "Agent execution timed out" in error
    if status == "skipped":
        return "skipped"
    if error and not (timed_out and has_reward):
        return "error"
    if status == "failed":
        if not (timed_out and has_reward):
            return "error"
    if status in {"success", "failed"} and has_reward:
        if trial.reward == 1:
            return "pass"
        if trial.reward == 0:
            return "fail"
        return "partial"
    if status == "success":
        return "scoreless"
    if status in {"queued", "pending"}:
        return "queued"
    if status == "running":
        return "running"
    return "pending"


def outcome_glyph(trial: TrialSnapshot) -> str:
    outcome = trial_outcome(trial)
    if outcome == "partial" and trial.reward is not None:
        value = f"{trial.reward:.2f}"
        return value[1:] if value.startswith("0.") else value
    return {
        "pass": ":white_check_mark:",
        "fail": ":x:",
        "error": ":warning:",
        "skipped": ":no_entry_sign:",
        "scoreless": ":heavy_minus_sign:",
        "queued": ":clock3:",
        "running": ":arrows_counterclockwise:",
        "pending": ":white_circle:",
    }[outcome]


def _trial_from_row(row: Any) -> TrialSnapshot:
    return TrialSnapshot(
        task_id=str(row.task_id),
        task_name=str(row.task_name),
        agent=str(row.agent),
        model=str(row.model) if row.model else None,
        status=getattr(row.status, "value", str(row.status)),
        reward=float(row.reward) if row.reward is not None else None,
        error_message=row.error_message,
        input_tokens=row.input_tokens,
        cache_tokens=row.cache_tokens,
        cache_write_tokens=row.cache_write_tokens,
        output_tokens=row.output_tokens,
        cost_usd=float(row.cost_usd) if row.cost_usd is not None else None,
    )


def _trial_columns() -> tuple[Any, ...]:
    return (
        TrialModel.task_id,
        TaskModel.name.label("task_name"),
        TrialModel.agent,
        TrialModel.model,
        TrialModel.status,
        TrialModel.reward,
        TrialModel.error_message,
        TrialModel.input_tokens,
        TrialModel.cache_tokens,
        TrialModel.cache_write_tokens,
        TrialModel.output_tokens,
        TrialModel.cost_usd,
    )


async def resolve_summary(target: UnfurlTarget, org_id: str) -> Summary | None:
    async with get_session() as session:
        if target.kind == "task":
            task_row = (
                await session.execute(
                    select(TaskModel, TaskVersionModel.version)
                    .options(
                        noload(TaskModel.experiments),
                        noload(TaskModel.trials),
                    )
                    .outerjoin(
                        TaskVersionModel,
                        TaskVersionModel.id == TaskModel.current_version_id,
                    )
                    .where(
                        TaskModel.id == target.identifier,
                        TaskModel.org_id == org_id,
                        TaskModel.deleted_at.is_(None),
                    )
                )
            ).one_or_none()
            if task_row is None:
                return None
            task, version = task_row
            filters = [
                TrialModel.task_id == task.id,
                TrialModel.org_id == org_id,
                TrialModel.is_probe.is_(False),
                TrialModel.superseded_by_trial_id.is_(None),
                TrialModel.deleted_at.is_(None),
                TaskModel.org_id == org_id,
                TaskModel.deleted_at.is_(None),
            ]
            if task.current_version_id:
                filters.append(TrialModel.task_version_id == task.current_version_id)
            total_trials = int(
                await session.scalar(
                    select(func.count(TrialModel.id))
                    .join(TaskModel, TaskModel.id == TrialModel.task_id)
                    .where(*filters)
                )
                or 0
            )
            rows = await session.execute(
                select(*_trial_columns())
                .join(TaskModel, TaskModel.id == TrialModel.task_id)
                .where(*filters)
                .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
                .limit(_TRIAL_LOAD_LIMIT)
            )
            trials = tuple(reversed([_trial_from_row(row) for row in rows]))
            return Summary(
                "task",
                task.name,
                target.url,
                trials,
                1,
                version,
                total_trials,
            )

        experiment_filters = [
            ExperimentModel.org_id == org_id,
            ExperimentModel.deleted_at.is_(None),
        ]
        if target.public:
            experiment_filters.extend(
                [
                    ExperimentModel.public_token == target.identifier,
                    ExperimentModel.is_public.is_(True),
                ]
            )
        else:
            experiment_filters.append(ExperimentModel.id == target.identifier)
        experiment = (
            await session.execute(select(ExperimentModel).where(*experiment_filters))
        ).scalar_one_or_none()
        if experiment is None:
            return None

        task_count = int(
            await session.scalar(
                select(func.count(func.distinct(TaskModel.id)))
                .join(task_experiments, task_experiments.c.task_id == TaskModel.id)
                .where(
                    task_experiments.c.experiment_id == experiment.id,
                    task_experiments.c.deleted_at.is_(None),
                    TaskModel.org_id == org_id,
                    TaskModel.deleted_at.is_(None),
                )
            )
            or 0
        )
        trial_filters = [
            trial_in_experiment(experiment.id),
            TrialModel.org_id == org_id,
            TrialModel.is_probe.is_(False),
            TrialModel.superseded_by_trial_id.is_(None),
            TrialModel.deleted_at.is_(None),
            TaskModel.org_id == org_id,
            TaskModel.deleted_at.is_(None),
        ]
        total_trials = int(
            await session.scalar(
                select(func.count(TrialModel.id))
                .join(TaskModel, TaskModel.id == TrialModel.task_id)
                .where(*trial_filters)
            )
            or 0
        )
        rows = await session.execute(
            select(*_trial_columns())
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(*trial_filters)
            .order_by(TrialModel.created_at.desc(), TrialModel.id.desc())
            .limit(_TRIAL_LOAD_LIMIT)
        )
        trials = tuple(reversed([_trial_from_row(row) for row in rows]))
        return Summary(
            "experiment",
            experiment.name,
            target.url,
            trials,
            max(task_count, len({trial.task_id for trial in trials})),
            total_trials=total_trials,
        )


def _cost(trials: tuple[TrialSnapshot, ...]) -> tuple[float, bool, int]:
    total = 0.0
    estimated = False
    unpriced = 0
    for trial in trials:
        if trial.cost_usd is not None:
            total += trial.cost_usd
            continue
        value = estimate_cost_usd(
            trial.model,
            trial.input_tokens,
            trial.output_tokens,
            trial.cache_tokens,
            trial.cache_write_tokens,
        )
        if value is None:
            # Fresh queued rows have neither spend nor enough information to
            # price yet; calling them "unpriced" makes an active sweep look
            # broken. Count only terminal rows or rows that already report use.
            has_usage = any(
                (value or 0) > 0
                for value in (
                    trial.input_tokens,
                    trial.output_tokens,
                    trial.cache_write_tokens,
                )
            )
            if (
                trial_outcome(trial)
                in {
                    "pass",
                    "partial",
                    "fail",
                    "error",
                    "skipped",
                    "scoreless",
                }
                or has_usage
            ):
                unpriced += 1
        else:
            total += value
            estimated = True
    return total, estimated, unpriced


def _outcome_counts(trials: tuple[TrialSnapshot, ...]) -> Counter[str]:
    return Counter(trial_outcome(trial) for trial in trials)


def _result_summary(counts: Counter[str]) -> str:
    parts: list[str] = []
    for key, icon, label in (
        ("pass", ":white_check_mark:", "pass"),
        ("partial", ":large_orange_circle:", "partial"),
        ("fail", ":x:", "fail"),
        ("error", ":warning:", "error"),
        ("skipped", ":no_entry_sign:", "skipped"),
        ("scoreless", ":heavy_minus_sign:", "scoreless"),
        ("running", ":arrows_counterclockwise:", "running"),
        ("queued", ":clock3:", "queued"),
        ("pending", ":white_circle:", "pending"),
    ):
        if counts[key]:
            parts.append(f"{icon} {counts[key]} {label}")
    return " · ".join(parts) if parts else "No trials"


def _avg_score(summary: Summary) -> float | None:
    if summary.kind == "task":
        rewards = [t.reward for t in summary.trials if t.reward is not None]
        return sum(rewards) / len(rewards) if rewards else None

    by_task: dict[str, list[float]] = defaultdict(list)
    for trial in summary.trials:
        if trial.reward is not None and not is_nop_oracle_agent(trial.agent):
            by_task[trial.task_id].append(trial.reward)
    task_scores = [sum(values) / len(values) for values in by_task.values() if values]
    return sum(task_scores) / len(task_scores) if task_scores else None


def _agent_keys(trials: tuple[TrialSnapshot, ...]) -> dict[int, str]:
    return {
        index: f"{trial.agent} · {trial.model}" if trial.model else trial.agent
        for index, trial in enumerate(trials)
    }


def _mrkdwn_label(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("*", "∗")
        .replace("`", "'")
    )


def _compact_result_blocks(summary: Summary) -> list[dict[str, Any]]:
    task_names = sorted({t.task_name for t in summary.trials})
    keys_by_index = _agent_keys(summary.trials)
    columns = list(dict.fromkeys(keys_by_index.values()))
    if (
        len(task_names) > _MATRIX_TASK_LIMIT
        or len(columns) > _MATRIX_COLUMN_LIMIT
        or len(summary.trials) > _MATRIX_GLYPH_LIMIT
        or not summary.trials
    ):
        return []

    cells: dict[tuple[str, str], list[str]] = defaultdict(list)
    for index, trial in enumerate(summary.trials):
        cells[(trial.task_name, keys_by_index[index])].append(outcome_glyph(trial))

    if summary.kind == "task":
        return [
            {
                "type": "section",
                "fields": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"*{_mrkdwn_label(column)}*\n"
                            + " ".join(cells[(task_names[0], column)])
                        ),
                    }
                    for column in columns
                ],
            }
        ]

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"*{_mrkdwn_label(task_name)}*\n"
                    + "   ·   ".join(
                        f"`{_mrkdwn_label(column)}` "
                        + (" ".join(cells[(task_name, column)]) or "—")
                        for column in columns
                    )
                ),
            },
        }
        for task_name in task_names
    ]


def render_blocks(summary: Summary) -> list[dict[str, Any]]:
    counts = _outcome_counts(summary.trials)
    terminal = sum(
        counts[key]
        for key in ("pass", "partial", "fail", "error", "skipped", "scoreless")
    )
    cost, estimated, unpriced = _cost(summary.trials)
    score = _avg_score(summary)
    total_trials = summary.total_trials or len(summary.trials)
    sampled = total_trials > len(summary.trials)
    title_suffix = f" · v{summary.version}" if summary.version is not None else ""
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{summary.name}{title_suffix}"[:150],
            },
        }
    ]
    blocks.extend(_compact_result_blocks(summary))

    cost_text = f"{'~' if estimated else ''}${cost:,.2f}"
    if unpriced:
        cost_text += f" · {unpriced} unpriced"
    completion_text = (
        f"{terminal}/{len(summary.trials)} sampled · {total_trials} total"
        if sampled
        else f"{terminal}/{len(summary.trials)} complete"
    )
    metrics = [
        f"{summary.task_count} tasks" if summary.kind == "experiment" else None,
        f"latest {len(summary.trials)}: {completion_text}"
        if sampled
        else completion_text,
        f"{score * 100:.1f}% avg score" if score is not None else "— avg score",
        f"{cost_text}{' so far' if terminal < len(summary.trials) else ''}",
    ]
    result_label = f"Latest {len(summary.trials)} · " if sampled else ""
    context_elements = [
        {
            "type": "mrkdwn",
            "text": f"*{result_label}Results*  {_result_summary(counts)}",
        },
        {
            "type": "mrkdwn",
            "text": " · ".join(value for value in metrics if value is not None),
        },
    ]
    blocks.append({"type": "context", "elements": context_elements})
    blocks.append(
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {
                        "type": "plain_text",
                        "text": f"Open {summary.kind}",
                    },
                    "url": summary.url,
                    "action_id": f"open_{summary.kind}",
                }
            ],
        }
    )
    return blocks


async def process_link_shared_event(payload: dict[str, Any]) -> None:
    config = load_slack_unfurl_config()
    if not config.ready:
        return
    event = payload.get("event") or {}
    links = event.get("links") or []
    targets = [
        target
        for link in links[:_MAX_LINKS_PER_EVENT]
        if isinstance(link, dict)
        if isinstance(link.get("url"), str)
        if (target := parse_oddish_link(link["url"], config.dashboard_url)) is not None
    ]
    if not targets:
        return

    unfurls: dict[str, Any] = {}
    for target in targets:
        try:
            summary = await resolve_summary(target, config.org_id)
        except Exception:
            log.exception("Slack unfurl summary failed kind=%s", target.kind)
            continue
        if summary is not None:
            unfurls[target.url] = {"blocks": render_blocks(summary)}
    if not unfurls:
        return

    body: dict[str, Any] = {"unfurls": unfurls}
    if event.get("unfurl_id") and event.get("source"):
        body.update({"unfurl_id": event["unfurl_id"], "source": event["source"]})
    else:
        body.update({"channel": event.get("channel"), "ts": event.get("message_ts")})
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                "https://slack.com/api/chat.unfurl",
                headers={"Authorization": f"Bearer {config.bot_token}"},
                json=body,
            )
        response.raise_for_status()
        result = response.json()
        if not result.get("ok"):
            log.warning("Slack chat.unfurl rejected request: %s", result.get("error"))
    except Exception:
        log.exception("Slack chat.unfurl request failed")
