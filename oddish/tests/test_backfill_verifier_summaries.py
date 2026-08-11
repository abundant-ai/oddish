import asyncio
import json

import pytest

from oddish import backfill_verifier_summaries as backfill
from oddish.db.storage import resolve_trial_s3_prefix


def _ctrf(*, passed: int, failed: int) -> bytes:
    return json.dumps(
        {
            "results": {
                "tool": {"name": "pytest"},
                "summary": {
                    "tests": passed + failed,
                    "passed": passed,
                    "failed": failed,
                    "skipped": 0,
                    "pending": 0,
                    "other": 0,
                },
            }
        }
    ).encode()


class _Storage:
    def __init__(
        self,
        objects: dict[str, list[dict]],
        documents: dict[str, bytes | Exception],
    ):
        self.objects = objects
        self.documents = documents
        self.closed = False
        self.list_calls: list[tuple[str, str | None]] = []

    async def list_objects(
        self,
        prefix: str,
        *,
        max_keys: int,
        continuation_token: str | None = None,
    ) -> dict:
        self.list_calls.append((prefix, continuation_token))
        objects = sorted(self.objects.get(prefix, []), key=lambda item: item["key"])
        start = int(continuation_token or 0)
        page = objects[start : start + max_keys]
        next_index = start + len(page)
        truncated = next_index < len(objects)
        return {
            "objects": page,
            "is_truncated": truncated,
            "next_token": str(next_index) if truncated else None,
        }

    async def download_bytes(self, key: str, _max_bytes: int) -> bytes:
        document = self.documents[key]
        if isinstance(document, Exception):
            raise document
        return document

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_backfill_persists_valid_reports_and_records_nonfatal_skips(
    monkeypatch,
):
    candidates = [
        backfill._Candidate("task-1", None, None),
        backfill._Candidate("task-2", None, None),
        backfill._Candidate("task-3", None, None),
    ]
    report_prefix = resolve_trial_s3_prefix(
        "task-1", trial_s3_key=None, trial_result_path=None
    )
    bad_key = f"{report_prefix}a/verifier/ctrf.json"
    good_key = f"{report_prefix}z/verifier/ctrf.json"
    oversized_prefix = resolve_trial_s3_prefix(
        "task-3", trial_s3_key=None, trial_result_path=None
    )
    oversized_key = f"{oversized_prefix}verifier/ctrf.json"
    storage = _Storage(
        {
            report_prefix: [
                {"key": good_key, "size": 200},
                {"key": bad_key, "size": 2},
            ],
            oversized_prefix: [
                {
                    "key": oversized_key,
                    "size": backfill.VERIFIER_CTRF_MAX_BYTES + 1,
                }
            ],
        },
        {bad_key: b"{}", good_key: _ctrf(passed=3, failed=1)},
    )
    writes = []
    checkpoints = []
    completion = []

    async def candidate_page(after: str):
        return candidates if not after else []

    async def commit_page(updates, *, after, stats):
        writes.extend(updates)
        stats["updated"] += len(updates)
        checkpoints.append((after, dict(stats)))

    async def record_status(status, stats, *, after):
        completion.append((status, after, dict(stats)))

    async def no_completion():
        return None

    monkeypatch.setattr(backfill, "_candidate_page", candidate_page)
    monkeypatch.setattr(backfill, "_commit_page", commit_page)
    monkeypatch.setattr(backfill, "_record_status", record_status)
    monkeypatch.setattr(backfill, "_completion_payload", no_completion)
    monkeypatch.setattr(backfill, "get_storage_client", lambda: storage)
    monkeypatch.setattr(backfill, "_LIST_PAGE_SIZE", 1)

    stats = await backfill.run_backfill(apply=True)

    assert stats == {
        "scanned": 3,
        "found": 1,
        "missing": 1,
        "oversized": 1,
        "unreadable": 0,
        "updated": 1,
    }
    assert writes == [
        (
            "task-1",
            {
                "format": "ctrf",
                "tests": 4,
                "passed": 3,
                "failed": 1,
                "skipped": 0,
                "pending": 0,
                "other": 0,
                "report_path": "z/verifier/ctrf.json",
                "tool": "pytest",
            },
        )
    ]
    assert checkpoints == [("task-3", stats)]
    assert completion == [("complete", "task-3", stats)]
    assert storage.list_calls == [
        (report_prefix, None),
        (report_prefix, "1"),
        (
            resolve_trial_s3_prefix(
                "task-2", trial_s3_key=None, trial_result_path=None
            ),
            None,
        ),
        (oversized_prefix, None),
    ]
    assert storage.closed


@pytest.mark.asyncio
async def test_backfill_refuses_completion_when_a_report_is_unreadable(monkeypatch):
    candidate = backfill._Candidate("task-1", None, None)
    prefix = resolve_trial_s3_prefix(
        candidate.id, trial_s3_key=None, trial_result_path=None
    )
    key = f"{prefix}verifier/ctrf.json"
    storage = _Storage(
        {prefix: [{"key": key, "size": 10}]},
        {key: OSError("S3 read failed")},
    )
    completion = []

    async def candidate_page(after: str):
        return [candidate] if not after else []

    async def no_completion():
        return None

    async def commit_page(updates, *, after, stats):
        assert updates == []

    async def record_status(status, stats, *, after):
        completion.append((status, after, dict(stats)))

    monkeypatch.setattr(backfill, "_candidate_page", candidate_page)
    monkeypatch.setattr(backfill, "_completion_payload", no_completion)
    monkeypatch.setattr(backfill, "_commit_page", commit_page)
    monkeypatch.setattr(backfill, "_record_status", record_status)
    monkeypatch.setattr(backfill, "get_storage_client", lambda: storage)

    with pytest.raises(RuntimeError, match="1 unreadable trial report"):
        await backfill.run_backfill(apply=True)

    assert completion == [
        (
            "failed",
            "task-1",
            {
                "scanned": 1,
                "found": 0,
                "missing": 0,
                "oversized": 0,
                "unreadable": 1,
                "updated": 0,
            },
        )
    ]
    assert storage.closed


@pytest.mark.asyncio
async def test_backfill_resumes_from_the_last_committed_page(monkeypatch):
    candidate = backfill._Candidate("task-2", None, None)
    prefix = resolve_trial_s3_prefix(
        candidate.id, trial_s3_key=None, trial_result_path=None
    )
    storage = _Storage({prefix: []}, {})
    pages = []
    checkpoints = []
    completion = []

    async def running_checkpoint():
        return {
            "status": "running",
            "after": "task-1",
            "scanned": 1,
            "found": 1,
            "missing": 0,
            "oversized": 0,
            "unreadable": 0,
            "updated": 1,
        }

    async def candidate_page(after: str):
        pages.append(after)
        return [candidate] if after == "task-1" else []

    async def commit_page(updates, *, after, stats):
        assert updates == []
        checkpoints.append((after, dict(stats)))

    async def record_status(status, stats, *, after):
        completion.append((status, after, dict(stats)))

    monkeypatch.setattr(backfill, "_completion_payload", running_checkpoint)
    monkeypatch.setattr(backfill, "_candidate_page", candidate_page)
    monkeypatch.setattr(backfill, "_commit_page", commit_page)
    monkeypatch.setattr(backfill, "_record_status", record_status)
    monkeypatch.setattr(backfill, "get_storage_client", lambda: storage)

    stats = await backfill.run_backfill(apply=True)

    assert stats == {
        "scanned": 2,
        "found": 1,
        "missing": 1,
        "oversized": 0,
        "unreadable": 0,
        "updated": 1,
    }
    assert pages == ["task-1", "task-2"]
    assert checkpoints == [("task-2", stats)]
    assert completion == [("complete", "task-2", stats)]
    assert storage.closed


@pytest.mark.asyncio
async def test_candidate_storage_work_is_bounded(monkeypatch):
    candidate = backfill._Candidate("task-1", None, None)
    storage = _Storage({}, {})

    async def never_finishes(_storage, _candidate):
        await asyncio.Event().wait()

    monkeypatch.setattr(backfill, "_read_summary", never_finishes)
    monkeypatch.setattr(backfill, "_CANDIDATE_TIMEOUT_SECONDS", 0.001)

    status, summary = await backfill._read_candidate(
        storage, asyncio.Semaphore(1), candidate
    )

    assert status == "unreadable"
    assert summary is None
