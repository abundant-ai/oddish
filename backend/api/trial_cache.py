"""Browser cache policy for reads of one finished trial.

``/trials/{trial_id}`` and ``/trials/{trial_id}/trajectory`` are fetched
every time a trial drawer opens. A trial whose execution *and* analysis
have both reached a terminal state never changes again by itself: the only
writers after that point are an explicit "re-run analysis" (which the
dashboard follows with a cache-bypassing refetch, see
``frontend/src/lib/trial-fetch.ts``) and a retry, which creates a new row
under a new id. Such trials may be held by the browser for a day. Every
other trial, and every non-2xx response, stays ``no-store`` so status,
cost, and analysis polling keep seeing the live row.

Keyed on row state rather than the route template, which is why this lives
next to the handlers instead of in ``api.cache_headers`` (that middleware
only knows the matched route and defers to any header a handler set).
"""

from __future__ import annotations

from datetime import datetime
from hashlib import sha1

from oddish.db.models import AnalysisStatus, TrialStatus

FINISHED_TRIAL_CACHE_CONTROL = "private, max-age=86400"
LIVE_TRIAL_CACHE_CONTROL = "no-store"

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
    """Execution is final and the per-trial analysis is too.

    A finished trial with no analysis yet (``analysis_status`` ``None``) is
    deliberately not final: a later task-level QA run can still write one
    onto it while no drawer is open to notice.
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


def cache_headers(*, final: bool, etag: str) -> dict[str, str]:
    """Headers for a 2xx trial read. ``no-store`` still carries the ETag so a
    client can tell whether a later final response is the same payload."""
    return {
        "Cache-Control": FINISHED_TRIAL_CACHE_CONTROL
        if final
        else LIVE_TRIAL_CACHE_CONTROL,
        "ETag": etag,
    }
