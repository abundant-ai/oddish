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
_LIST_PAGE_SIZE = 1000
_READ_CONCURRENCY = 16
_CANDIDATE_TIMEOUT_SECONDS = 30
_STAT_KEYS = ("scanned", "found", "missing", "oversized", "unreadable", "updated")
_UPSERT_STATUS_SQL = text(
    """
    INSERT INTO queue_runtime_status (component, updated_at, payload)
    VALUES (:component, NOW(), CAST(:payload AS jsonb))
    ON CONFLICT (component) DO UPDATE
    SET updated_at = NOW(), payload = EXCLUDED.payload
    """
)
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
    continuation_token: str | None = None
    saw_oversized = False
    saw_unreadable = False
    try:
        while True:
            listing = await storage.list_objects(
                prefix,
                max_keys=_LIST_PAGE_SIZE,
                continuation_token=continuation_token,
            )
            for report in listing["objects"]:
                key = report.get("key")
                if not isinstance(key, str) or not key.endswith("/verifier/ctrf.json"):
                    continue
                size = report.get("size")
                if isinstance(size, int) and size > VERIFIER_CTRF_MAX_BYTES:
                    saw_oversized = True
                    continue
                try:
                    document = await storage.download_bytes(
                        key, VERIFIER_CTRF_MAX_BYTES + 1
                    )
                except Exception:
                    saw_unreadable = True
                    continue
                if len(document) > VERIFIER_CTRF_MAX_BYTES:
                    saw_oversized = True
                    continue
                summary = parse_ctrf_summary(document, report_path=key[len(prefix) :])
                if summary is not None:
                    return "found", summary
                saw_unreadable = True

            if not listing["is_truncated"]:
                break
            continuation_token = listing.get("next_token")
            if not isinstance(continuation_token, str) or not continuation_token:
                return "unreadable", None
    except Exception:
        return "unreadable", None

    if saw_unreadable:
        return "unreadable", None
    if saw_oversized:
        return "oversized", None
    return "missing", None


async def _read_candidate(
    storage: StorageClient,
    semaphore: asyncio.Semaphore,
    candidate: _Candidate,
) -> tuple[
    Literal["found", "missing", "oversized", "unreadable"],
    dict[str, Any] | None,
]:
    async with semaphore:
        try:
            async with asyncio.timeout(_CANDIDATE_TIMEOUT_SECONDS):
                return await _read_summary(storage, candidate)
        except TimeoutError:
            print(
                f"Verifier summary backfill timed out reading trial {candidate.id}",
                flush=True,
            )
            return "unreadable", None


async def _commit_page(
    updates: list[tuple[str, dict[str, Any]]],
    *,
    after: str,
    stats: Counter[str],
) -> None:
    async with get_session() as session:
        written = 0
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
        stats["updated"] += written
        await session.execute(
            _UPSERT_STATUS_SQL,
            {
                "component": _BACKFILL_COMPONENT,
                "payload": json.dumps(
                    {"status": "running", "after": after, **dict(stats)}
                ),
            },
        )


async def _record_status(
    status: Literal["complete", "failed"],
    stats: Counter[str],
    *,
    after: str,
) -> None:
    payload = {"status": status, "after": after, **dict(stats)}
    async with get_session() as session:
        await session.execute(
            _UPSERT_STATUS_SQL,
            {
                "component": _BACKFILL_COMPONENT,
                "payload": json.dumps(payload),
            },
        )


async def run_backfill(*, apply: bool) -> dict[str, int]:
    checkpoint = await _completion_payload()
    if checkpoint and checkpoint.get("status") == "complete":
        print(f"Verifier summary backfill already complete: {checkpoint}")
        return {
            key: value
            for key, value in checkpoint.items()
            if key != "status" and isinstance(value, int)
        }

    resume = bool(apply and checkpoint and checkpoint.get("status") == "running")
    stats: Counter[str] = Counter(
        {
            key: (
                int(checkpoint.get(key, 0))
                if resume and isinstance(checkpoint.get(key), int)
                else 0
            )
            for key in _STAT_KEYS
        }
    )
    after = (
        str(checkpoint["after"])
        if resume and isinstance(checkpoint.get("after"), str)
        else ""
    )
    if after:
        print(
            f"Resuming verifier summary backfill after {after}: {dict(stats)}",
            flush=True,
        )

    storage = get_storage_client()
    semaphore = asyncio.Semaphore(_READ_CONCURRENCY)
    try:
        while candidates := await _candidate_page(after):
            results = await asyncio.gather(
                *(
                    _read_candidate(storage, semaphore, candidate)
                    for candidate in candidates
                )
            )
            updates: list[tuple[str, dict[str, Any]]] = []
            for candidate, (status, summary) in zip(candidates, results, strict=True):
                stats["scanned"] += 1
                stats[status] += 1
                if summary is not None:
                    updates.append((candidate.id, summary))
            after = candidates[-1].id
            if apply:
                await _commit_page(updates, after=after, stats=stats)
            print(
                f"Verifier summary backfill progress after {after}: {dict(stats)}",
                flush=True,
            )
    finally:
        await storage.close()

    print(f"Verifier summary backfill: {dict(stats)}", flush=True)
    if stats["unreadable"]:
        if apply:
            await _record_status("failed", stats, after=after)
        raise RuntimeError(
            f"Verifier summary backfill left {stats['unreadable']} unreadable "
            "trial report(s); deployment must not continue."
        )
    if apply:
        await _record_status("complete", stats, after=after)
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
