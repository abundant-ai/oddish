"""Shipment findings retain their evidence even when a review is replaced.

Reading this module's collection never generates analysis or rewrites artifacts.
Historical tiers are facts; every recorded tier requires delivery acknowledgment.
"""

from __future__ import annotations

import hashlib

from sqlalchemy import case, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.analyze.models import ActionTier
from oddish.db import TaskVersionModel, TrialModel
from oddish.filters.trial_predicates import EligibleTrialScope

RECORDED_DEFECT_TIERS = tuple(t.value for t in ActionTier)
FINDING_IDENTITY_FIELDS = (
    "id",
    "title",
    "file",
    "line_start",
    "line_end",
    "tier",
    "severity",
    "links_to",
)


def _defect_id(version_id: str, item: dict, source: str) -> str:
    """A stable id for one must-fix item, for 'ack:<id>' ticks.

    A non-empty string id is the author's own dedup key and is used as-is.
    Anything else hashes the item's identifying fields — source and raw id
    included — so two distinct defects that happen to share a title cannot
    collapse into one acknowledgement.
    """
    raw = item.get("id")
    if isinstance(raw, str) and raw:
        return raw[:56]
    seed = ":".join(
        [
            version_id,
            source,
            "" if raw is None else str(raw),
            str(item.get("file") or ""),
            str(item.get("line_start") or ""),
            str(item.get("title") or ""),
        ]
    )
    return hashlib.sha1(seed.encode()).hexdigest()[:16]


def pre_trial_items(version: TaskVersionModel) -> list[dict]:
    """The pre-trial audit items, dicts only; any malformed shape reads []."""
    items = (version.pre_trial or {}).get("items")
    if not isinstance(items, list):
        return []
    return [i for i in items if isinstance(i, dict)]


async def task_defect_items(
    session: AsyncSession,
    versions: dict[str, TaskVersionModel],
    *,
    include_details: bool = True,
) -> dict[str, list[dict]]:
    """Reported task defects per version, with original evidence and severity.

    Sources are the version's pre-trial audit and the trial analyses on the
    version. Superseded trials stay in: a defect describes the task version,
    not the run, so retrying the run must not clear the finding (a real fix
    edits the task and lands on a new version anyway).
    """
    out: dict[str, list[dict]] = {vid: [] for vid in versions}
    seen: dict[str, set[str]] = {vid: set() for vid in versions}

    def add(
        vid: str,
        item: dict,
        source: str,
        reporting_trial_id: str | None = None,
        review_trial_id: str | None = None,
    ) -> None:
        defect_id = _defect_id(vid, item, source)
        if defect_id in seen[vid]:
            return
        seen[vid].add(defect_id)
        out[vid].append(
            {
                "id": defect_id,
                "title": str(item.get("title") or "untitled defect"),
                "source": source,
                "recorded_tier": (
                    item["tier"]
                    if item.get("tier") is not None
                    else item.get("severity")
                ),
                "finding": item
                if include_details
                else {key: item[key] for key in FINDING_IDENTITY_FIELDS if key in item},
                "reporting_trial_id": reporting_trial_id,
                "review_trial_id": review_trial_id,
            }
        )

    version: TaskVersionModel
    for vid, version in versions.items():
        for report in version.reported_findings or []:
            add(
                vid,
                report["finding"],
                report["source"],
                report.get("reporting_trial_id"),
                report.get("review_trial_id"),
            )
        for item in pre_trial_items(version):
            tier = (
                item["tier"] if item.get("tier") is not None else item.get("severity")
            )
            if tier in RECORDED_DEFECT_TIERS:
                add(
                    vid,
                    item,
                    "pre_trial",
                    review_trial_id=(version.pre_trial or {}).get("block_id"),
                )

    if versions:
        defect_scope = EligibleTrialScope(
            membership=[TrialModel.task_version_id.in_(list(versions))],
            include_superseded=True,
            include_deleted=True,
        )
        # The array-shape guard must live INSIDE the set-returning function:
        # jsonb_array_elements runs in FROM before any WHERE filter, so a row
        # whose action_items is an object or scalar would otherwise raise.
        items = func.jsonb_array_elements(
            case(
                (
                    func.jsonb_typeof(TrialModel.analysis["action_items"]) == "array",
                    TrialModel.analysis["action_items"],
                ),
                else_=text("'[]'::jsonb"),
            )
        ).table_valued("value", with_ordinality="ordinality", joins_implicitly=True)
        rows = (
            await session.execute(
                select(
                    TrialModel.task_version_id,
                    items.c.value
                    if include_details
                    else func.jsonb_build_object(
                        *[
                            value
                            for key in FINDING_IDENTITY_FIELDS
                            for value in (key, items.c.value.op("->")(key))
                        ]
                    ),
                    TrialModel.id,
                    TrialModel.analysis["_graded_by"].astext,
                )
                .where(
                    *defect_scope.clauses(),
                    func.coalesce(
                        items.c.value.op("->>")("tier"),
                        items.c.value.op("->>")("severity"),
                    ).in_(RECORDED_DEFECT_TIERS),
                )
                # A defect describes the version, not the run: deleting or
                # superseding the trial that reported it must not clear it.
                # Only an acknowledgement or a new version does.
                .execution_options(include_deleted=True)
                .order_by(TrialModel.id, items.c.ordinality)
            )
        ).all()
        for vid, item, reporting_trial_id, review_trial_id in rows:
            if isinstance(item, dict):
                add(vid, item, "trial", reporting_trial_id, review_trial_id)

    return out


async def preserve_task_findings(
    session: AsyncSession, version_id: str | None
) -> list[dict]:
    """Retain findings before replacing review state, under the version lock."""
    if version_id is None:
        return []
    version = await session.get(TaskVersionModel, version_id, with_for_update=True)
    if version is None:
        return []
    reports = (await task_defect_items(session, {version.id: version}))[version.id]
    version.reported_findings = reports
    return reports
