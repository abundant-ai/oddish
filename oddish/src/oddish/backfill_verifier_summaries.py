from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy import or_, select, text

from oddish.core.harbor_artifacts import (
    VERIFIER_CTRF_MAX_BYTES,
    parse_ctrf_summary,
)
from oddish.db import TrialModel, TrialStatus, get_session, get_storage_client
from oddish.db.storage import StorageClient, resolve_trial_s3_prefix

_BACKFILL_COMPONENT = "backfill.verifier_summaries.v1"
_PAGE_SIZE = 100
_TERMINAL_STATUSES = (
    TrialStatus.SUCCESS,
    TrialStatus.FAILED,
    TrialStatus.SKIPPED,
)


@dataclass(frozen=True)
class _Candidate:
    id: str
    trial_s3_key: str | None
    harbor_result_path: str | None


async def _completion_payload() -> dict[str, Any] | None:
    async with get_session() as session:
        payload = (
            await session.execute(
                text(
                    """
                    SELECT payload
                    FROM queue_runtime_status
                    WHERE component = :component
                    """
                ),
                {"component": _BACKFILL_COMPONENT},
            )
        ).scalar_one_or_none()
    return payload if isinstance(payload, dict) else None


async def _candidate_page(after: str) -> list[_Candidate]:
    async with get_session() as session:
        rows = (
            await session.execute(
                select(
                    TrialModel.id,
                    TrialModel.trial_s3_key,
                    TrialModel.harbor_result_path,
                )
                .where(
                    TrialModel.id > after,
                    TrialModel.deleted_at.is_(None),
                    TrialModel.status.in_(_TERMINAL_STATUSES),
                    or_(
                        TrialModel.result.is_(None),
                        ~TrialModel.result.op("?")("_verifier"),
                    ),
                )
                .order_by(TrialModel.id)
                .limit(_PAGE_SIZE)
            )
        ).all()
    return [_Candidate(*row) for row in rows]


async def _read_summary(
    storage: StorageClient, candidate: _Candidate
) -> tuple[
    Literal["found", "missing", "oversized", "unreadable"],
    dict[str, Any] | None,
]:
    prefix = resolve_trial_s3_prefix(
        candidate.id,
        trial_s3_key=candidate.trial_s3_key,
        trial_result_path=candidate.harbor_result_path,
    )
    try:
        objects = await storage.list_objects_all(prefix)
    except Exception:
        return "unreadable", None

    reports = sorted(
        (
            obj
            for obj in objects
            if isinstance(obj.get("key"), str)
            and str(obj["key"]).endswith("/verifier/ctrf.json")
        ),
        key=lambda obj: str(obj["key"]),
    )
    if not reports:
        return "missing", None

    saw_unreadable = False
    for report in reports:
        size = report.get("size")
        if isinstance(size, int) and size > VERIFIER_CTRF_MAX_BYTES:
            continue
        key = str(report["key"])
        try:
            document = await storage.download_bytes(key, VERIFIER_CTRF_MAX_BYTES + 1)
        except Exception:
            saw_unreadable = True
            continue
        if len(document) > VERIFIER_CTRF_MAX_BYTES:
            continue
        summary = parse_ctrf_summary(document, report_path=key[len(prefix) :])
        if summary is not None:
            return "found", summary
        saw_unreadable = True
    return ("unreadable" if saw_unreadable else "oversized"), None


async def _write_summaries(updates: list[tuple[str, dict[str, Any]]]) -> int:
    written = 0
    async with get_session() as session:
        for trial_id, summary in updates:
            result = await session.execute(
                text(
                    """
                    UPDATE trials
                    SET result = CASE
                                   WHEN jsonb_typeof(result) = 'object' THEN result
                                   ELSE '{}'::jsonb
                                 END
                               || jsonb_build_object(
                                    '_verifier', CAST(:summary AS jsonb)
                                  )
                    WHERE id = :trial_id
                      AND deleted_at IS NULL
                      AND NOT (COALESCE(result, '{}'::jsonb) ? '_verifier')
                    """
                ),
                {"trial_id": trial_id, "summary": json.dumps(summary)},
            )
            written += result.rowcount or 0
    return written


async def _record_status(
    status: Literal["complete", "failed"], stats: Counter[str]
) -> None:
    payload = {"status": status, **dict(stats)}
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO queue_runtime_status (component, updated_at, payload)
                VALUES (:component, NOW(), CAST(:payload AS jsonb))
                ON CONFLICT (component) DO UPDATE
                SET updated_at = NOW(), payload = EXCLUDED.payload
                """
            ),
            {
                "component": _BACKFILL_COMPONENT,
                "payload": json.dumps(payload),
            },
        )


async def run_backfill(*, apply: bool) -> dict[str, int]:
    completed = await _completion_payload()
    if completed and completed.get("status") == "complete":
        print(f"Verifier summary backfill already complete: {completed}")
        return {
            key: value
            for key, value in completed.items()
            if key != "status" and isinstance(value, int)
        }

    stats: Counter[str] = Counter(
        scanned=0,
        found=0,
        missing=0,
        oversized=0,
        unreadable=0,
        updated=0,
    )
    storage = get_storage_client()
    after = ""
    try:
        while candidates := await _candidate_page(after):
            after = candidates[-1].id
            results = await asyncio.gather(
                *(_read_summary(storage, candidate) for candidate in candidates)
            )
            updates: list[tuple[str, dict[str, Any]]] = []
            for candidate, (status, summary) in zip(candidates, results, strict=True):
                stats["scanned"] += 1
                stats[status] += 1
                if summary is not None:
                    updates.append((candidate.id, summary))
            if apply and updates:
                stats["updated"] += await _write_summaries(updates)
    finally:
        await storage.close()

    print(f"Verifier summary backfill: {dict(stats)}")
    if stats["unreadable"]:
        if apply:
            await _record_status("failed", stats)
        raise RuntimeError(
            f"Verifier summary backfill left {stats['unreadable']} unreadable "
            "trial report(s); deployment must not continue."
        )
    if apply:
        await _record_status("complete", stats)
    else:
        print("Dry run complete; re-run with --apply to persist summaries.")
    return dict(stats)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Backfill canonical CTRF summaries on historical trial rows."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Persist summaries and the completion marker.",
    )
    args = parser.parse_args()
    asyncio.run(run_backfill(apply=args.apply))


if __name__ == "__main__":
    main()
