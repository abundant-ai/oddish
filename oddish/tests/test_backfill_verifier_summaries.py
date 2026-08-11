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

    async def list_objects_all(self, prefix: str) -> list[dict]:
        return self.objects.get(prefix, [])

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
    completion = []

    async def candidate_page(after: str):
        return candidates if not after else []

    async def write_summaries(updates):
        writes.extend(updates)
        return len(updates)

    async def record_status(status, stats):
        completion.append((status, dict(stats)))

    async def no_completion():
        return None

    monkeypatch.setattr(backfill, "_candidate_page", candidate_page)
    monkeypatch.setattr(backfill, "_write_summaries", write_summaries)
    monkeypatch.setattr(backfill, "_record_status", record_status)
    monkeypatch.setattr(backfill, "_completion_payload", no_completion)
    monkeypatch.setattr(backfill, "get_storage_client", lambda: storage)

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
    assert completion == [("complete", stats)]
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

    async def record_status(status, stats):
        completion.append((status, dict(stats)))

    monkeypatch.setattr(backfill, "_candidate_page", candidate_page)
    monkeypatch.setattr(backfill, "_completion_payload", no_completion)
    monkeypatch.setattr(backfill, "_record_status", record_status)
    monkeypatch.setattr(backfill, "get_storage_client", lambda: storage)

    with pytest.raises(RuntimeError, match="1 unreadable trial report"):
        await backfill.run_backfill(apply=True)

    assert completion == [
        (
            "failed",
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
