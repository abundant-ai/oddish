from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from oddish.core.task_browse_cost import resolve_browse_trial_cost
from oddish.core.task_browse_status import matrix_status, status_value
from oddish.core.task_browse_summary import browse_trial_model_key

_MUTABLE_STATUSES = frozenset(("running", "retrying"))


@dataclass(frozen=True, slots=True)
class TrialBrowseContribution:
    trial_id: str
    task_id: str
    task_version_id: str
    org_id: str | None
    name: str
    agent: str
    model: str | None
    model_key: str
    status: str
    status_bucket: str
    reward: float | None
    total_tokens: int
    tokens_known: bool
    runtime_seconds: float | None
    total_steps: int | None
    error_message: str | None
    resolved_cost_usd: float | None
    cost_estimated: bool
    billed: bool
    is_mutable: bool
    activity_at: datetime
    finished_at: datetime | None
    trial_created_at: datetime

    def values(self) -> dict[str, Any]:
        return asdict(self)


def build_trial_browse_contribution(
    row: Mapping[str, Any],
) -> TrialBrowseContribution | None:
    """Return one eligible trial's canonical browser contribution."""
    version_id = row.get("task_version_id")
    idempotency_key = row.get("idempotency_key")
    if (
        not version_id
        or row.get("deleted_at") is not None
        or row.get("superseded_by_trial_id") is not None
        or row.get("is_probe") is True
        or (isinstance(idempotency_key, str) and idempotency_key.startswith("combine:"))
    ):
        return None
    status = status_value(row["status"])
    cost, estimated = resolve_browse_trial_cost(row)
    token_values = (
        row.get("input_tokens"),
        row.get("output_tokens"),
        row.get("cache_tokens"),
    )
    started_at = row.get("started_at")
    finished_at = row.get("finished_at")
    runtime_seconds = (
        (finished_at - started_at).total_seconds()
        if finished_at is not None and started_at is not None
        else None
    )
    activity_at = max(
        value
        for value in (finished_at, started_at, row["created_at"])
        if value is not None
    )
    return TrialBrowseContribution(
        trial_id=str(row["id"]),
        task_id=str(row["task_id"]),
        task_version_id=str(version_id),
        org_id=str(row["org_id"]) if row.get("org_id") is not None else None,
        name=str(row["name"]),
        agent=str(row["agent"]),
        model=row.get("model"),
        model_key=browse_trial_model_key(row.get("model")),
        status=status,
        status_bucket=matrix_status(row),
        reward=float(row["reward"]) if row.get("reward") is not None else None,
        total_tokens=sum(int(value or 0) for value in token_values),
        tokens_known=any(value is not None for value in token_values),
        runtime_seconds=runtime_seconds,
        total_steps=(
            int(row["total_steps"]) if row.get("total_steps") is not None else None
        ),
        error_message=row.get("error_message"),
        resolved_cost_usd=cost,
        cost_estimated=estimated,
        billed=row.get("billed_user_id") is not None,
        is_mutable=status in _MUTABLE_STATUSES,
        activity_at=activity_at,
        trial_created_at=row["created_at"],
        finished_at=finished_at,
    )
