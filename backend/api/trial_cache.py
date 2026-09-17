"""Browser cache policy for reads of one trial.

``/trials/{trial_id}`` and ``/trials/{trial_id}/trajectory`` are fetched
every time a trial drawer opens. Two different guarantees apply:

* A finished trial's **trajectory** never changes (a retry is a new row), so
  once the row records one and storage returns it, the browser may keep it
  for a day without asking.

* A finished trial's **detail** is mostly stable but not immutable: a
  task-level QA run rewrites the per-trial analysis in place, on trials that
  already carried a terminal one, while no drawer is open to notice. So the
  browser may keep the body but must revalidate on every use
  (``private, no-cache`` plus an ETag). The revalidation is answered from
  one slim row read, before the five-query response build, so a reopen
  costs a round trip and a ``304`` rather than the full payload.

Anything still running, and every non-2xx, stays ``no-store``.

Keyed on row state rather than the route template, which is why this lives
next to the handlers instead of in ``api.cache_headers`` (that middleware
only knows the matched route and defers to any header a handler set).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from hashlib import sha1

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db.models import AnalysisStatus, TaskModel, TrialModel, TrialStatus

IMMUTABLE_CACHE_CONTROL = "private, max-age=86400"
REVALIDATE_CACHE_CONTROL = "private, no-cache"
LIVE_CACHE_CONTROL = "no-store"

_TERMINAL_TRIAL = frozenset(
    {TrialStatus.SUCCESS, TrialStatus.FAILED, TrialStatus.SKIPPED}
)
_TERMINAL_ANALYSIS = frozenset({AnalysisStatus.SUCCESS, AnalysisStatus.FAILED})


def _enum_value(value: object) -> str | None:
    if value is None:
        return None
    return getattr(value, "value", value)  # type: ignore[return-value]


def _iso(value: datetime | None) -> str:
    return value.isoformat() if value is not None else ""


def trial_execution_is_final(*, status: object, finished_at: datetime | None) -> bool:
    """Execution reached a terminal state and recorded when."""
    return finished_at is not None and _enum_value(status) in {
        s.value for s in _TERMINAL_TRIAL
    }


def trial_detail_is_final(
    *,
    status: object,
    finished_at: datetime | None,
    analysis_status: object,
) -> bool:
    """Execution is final and the per-trial analysis has settled.

    "Final" here means the body is worth keeping and revalidating, not that
    it can never change: task-level QA may still rewrite the analysis, which
    the ETag (built on ``analysis_finished_at``) exposes on the next use.
    A finished trial with no analysis yet stays live so polling sees the
    first analysis land.
    """
    return trial_execution_is_final(
        status=status, finished_at=finished_at
    ) and _enum_value(analysis_status) in {s.value for s in _TERMINAL_ANALYSIS}


def trial_etag(
    *,
    trial_id: str,
    attempts: int,
    finished_at: datetime | None,
    analysis_finished_at: datetime | None = None,
) -> str:
    """Weak validator over the fields that change when the payload does."""
    raw = "|".join(
        (trial_id, str(attempts), _iso(finished_at), _iso(analysis_finished_at))
    )
    return f'W/"{sha1(raw.encode()).hexdigest()[:20]}"'


def matches_if_none_match(header: str | None, etag: str) -> bool:
    if not header:
        return False
    candidates = {part.strip() for part in header.split(",")}
    return etag in candidates or "*" in candidates


def cache_headers(*, policy: str, etag: str) -> dict[str, str]:
    """Headers for a 2xx or 304 trial read. ``no-store`` still carries the
    ETag so a client can tell whether a later response is the same payload."""
    return {"Cache-Control": policy, "ETag": etag}


@dataclass(frozen=True)
class TrialCacheIdentity:
    """The few columns the detail ETag and policy are built from."""

    trial_id: str
    attempts: int
    status: object
    finished_at: datetime | None
    analysis_status: object
    analysis_finished_at: datetime | None

    @property
    def final(self) -> bool:
        return trial_detail_is_final(
            status=self.status,
            finished_at=self.finished_at,
            analysis_status=self.analysis_status,
        )

    @property
    def etag(self) -> str:
        return trial_etag(
            trial_id=self.trial_id,
            attempts=self.attempts,
            finished_at=self.finished_at,
            analysis_finished_at=self.analysis_finished_at,
        )

    @property
    def policy(self) -> str:
        return REVALIDATE_CACHE_CONTROL if self.final else LIVE_CACHE_CONTROL


async def load_trial_cache_identity(
    session: AsyncSession, *, trial_id: str, org_id: str | None
) -> TrialCacheIdentity | None:
    """One slim, org-scoped read of the columns behind the detail ETag.

    Returns ``None`` when the trial does not exist for this org; the caller
    then falls through to the full read, which raises the proper 404.
    """
    row = (
        await session.execute(
            select(
                TrialModel.id,
                TrialModel.attempts,
                TrialModel.status,
                TrialModel.finished_at,
                TrialModel.analysis_status,
                TrialModel.analysis_finished_at,
                TaskModel.org_id,
            )
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(TrialModel.id == trial_id)
        )
    ).first()
    if row is None:
        return None
    (
        found_id,
        attempts,
        status,
        finished_at,
        analysis_status,
        analysis_finished_at,
        task_org_id,
    ) = row
    if org_id is not None and task_org_id != org_id:
        return None
    return TrialCacheIdentity(
        trial_id=str(found_id),
        attempts=int(attempts),
        status=status,
        finished_at=finished_at,
        analysis_status=analysis_status,
        analysis_finished_at=analysis_finished_at,
    )
