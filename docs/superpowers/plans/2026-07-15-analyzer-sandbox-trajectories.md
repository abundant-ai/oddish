# Analyzer Sandbox Trajectory Persistence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Upload every analyzer sandbox turn's raw stream-json trajectory to S3, so a suspect finding can be traced back to how the agent reached it.

**Architecture:** A new `backend/api/services/cc_chat/analyzer_trajectory.py` owns key derivation and a best-effort upload. `run_cohort`'s inner `_turn` collects the raw `evt` dicts it already iterates and calls `persist_turn` when the stream closes — one JSONL object per turn at `analyzers/<id>/<bucket>/<slug>.jsonl`. Always on; no DB column, no migration, no read path.

**Tech Stack:** Python 3.13, pytest + pytest-asyncio, `StorageClient.upload_bytes` (aioboto3) via the `get_storage_client()` singleton seam.

**Spec:** `docs/superpowers/specs/2026-07-15-analyzer-sandbox-trajectories-design.md`

## Global Constraints

- Run tests with `pytest` from the `backend/` directory.
- Best-effort upload: a failure logs a warning and MUST NOT fail the analyzer job.
- Catch `Exception`, never `BaseException`. This code runs inside `asyncio.timeout(COHORT_TIMEOUT_SECONDS)`; swallowing `CancelledError` would break the cohort timeout.
- No `moto`. Tests use hand-rolled fakes + `monkeypatch` over the `get_storage_client` seam — the established house style.
- Package boundary: `backend/` may import `oddish` (AGENTS.md:188-194). Never the reverse.
- Comments: sparingly, and only for non-obvious *why*. Match the density of the file being edited.
- Never commit to `main`. Work on the `analyzer-sandbox-trajectories` branch.

---

### Task 1: `analyzer_trajectory` module

Key derivation plus the best-effort upload, isolated so it is testable without provisioning a sandbox.

**Files:**
- Create: `backend/api/services/cc_chat/analyzer_trajectory.py`
- Test: `backend/tests/cc_chat/test_analyzer_trajectory.py`

**Interfaces:**
- Consumes: `oddish.db.storage.get_storage_client()` → object with `async upload_bytes(data: bytes, s3_key: str, *, content_type: str | None = None) -> None`; `oddish.config.settings.s3_bucket` (log messages only).
- Produces:
  - `trajectory_key(analyzer_id: str, bucket: str, slug: str) -> str`
  - `map_slug(batch_no: int) -> str`
  - `REDUCE_SLUG: str = "reduce"`
  - `async persist_turn(*, analyzer_id: str, bucket: str, slug: str, events: list[dict]) -> None`

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/cc_chat/test_analyzer_trajectory.py`:

```python
import asyncio
import json

import pytest

from api.services.cc_chat import analyzer_trajectory as at

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """Records uploads. Mirrors StorageClient.upload_bytes' signature only."""

    def __init__(self, exc=None):
        self.calls = []
        self._exc = exc

    async def upload_bytes(self, data, s3_key, *, content_type=None):
        self.calls.append({"data": data, "s3_key": s3_key,
                           "content_type": content_type})
        if self._exc:
            raise self._exc


def _install(monkeypatch, storage):
    monkeypatch.setattr(at, "get_storage_client", lambda: storage)


def test_trajectory_key_layout():
    assert at.trajectory_key("a1", "bad", "map-01") == \
        "analyzers/a1/bad/map-01.jsonl"


def test_map_slug_is_zero_padded():
    # Unpadded, map-10 would sort before map-2.
    assert at.map_slug(1) == "map-01"
    assert at.map_slug(2) == "map-02"
    assert at.map_slug(10) == "map-10"
    assert sorted([at.map_slug(2), at.map_slug(10)]) == ["map-02", "map-10"]


def test_reduce_slug():
    assert at.REDUCE_SLUG == "reduce"


async def test_persist_turn_uploads_jsonl(monkeypatch):
    storage = _FakeStorage()
    _install(monkeypatch, storage)
    events = [{"type": "system", "subtype": "init"}, {"type": "result"}]

    await at.persist_turn(analyzer_id="a1", bucket="good", slug="map-01",
                          events=events)

    assert len(storage.calls) == 1
    call = storage.calls[0]
    assert call["s3_key"] == "analyzers/a1/good/map-01.jsonl"
    assert call["content_type"] == "application/x-ndjson"
    lines = call["data"].decode().split("\n")
    assert [json.loads(x) for x in lines] == events


async def test_persist_turn_preserves_events_verbatim(monkeypatch):
    """The record must be raw, not rendered: render_event truncates tool
    inputs to 200 chars, which would hide whether --tail-bytes was widened."""
    storage = _FakeStorage()
    _install(monkeypatch, storage)
    command = "node /home/daytona/workspace/oddish-query trials logs t1 " \
              "--trajectory --tail-bytes 40000" + " # pad" * 100
    events = [{"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": "Bash", "input": {"command": command}}]}}]

    await at.persist_turn(analyzer_id="a1", bucket="bad", slug="map-01",
                          events=events)

    line = json.loads(storage.calls[0]["data"].decode())
    assert line["message"]["content"][0]["input"]["command"] == command


async def test_persist_turn_skips_empty_events(monkeypatch):
    storage = _FakeStorage()
    _install(monkeypatch, storage)

    await at.persist_turn(analyzer_id="a1", bucket="bad", slug="reduce",
                          events=[])

    assert storage.calls == []


async def test_persist_turn_swallows_upload_failure(monkeypatch):
    storage = _FakeStorage(exc=RuntimeError("s3 down"))
    _install(monkeypatch, storage)

    # Must not raise: the findings are the primary product.
    await at.persist_turn(analyzer_id="a1", bucket="bad", slug="map-01",
                          events=[{"type": "result"}])


async def test_persist_turn_lets_cancellation_propagate(monkeypatch):
    """CancelledError is a BaseException; swallowing it would break
    run_cohort's asyncio.timeout."""
    storage = _FakeStorage(exc=asyncio.CancelledError())
    _install(monkeypatch, storage)

    with pytest.raises(asyncio.CancelledError):
        await at.persist_turn(analyzer_id="a1", bucket="bad", slug="map-01",
                              events=[{"type": "result"}])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/cc_chat/test_analyzer_trajectory.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'api.services.cc_chat.analyzer_trajectory'`

- [ ] **Step 3: Write the implementation**

Create `backend/api/services/cc_chat/analyzer_trajectory.py`:

```python
"""Persist one analyzer sandbox turn's raw trajectory to S3.

Raw stream-json events, not render_event's output: rendering truncates tool
inputs to 200 chars and drops _stderr/_invalid_json frames entirely, so the
evidence for whether an agent widened --tail-bytes survives only in the raw
event. Rendering can be regenerated from raw; not the reverse.
"""

from __future__ import annotations

import json
import logging

from oddish.config import settings
from oddish.db.storage import get_storage_client

logger = logging.getLogger(__name__)

REDUCE_SLUG = "reduce"


def trajectory_key(analyzer_id: str, bucket: str, slug: str) -> str:
    return f"analyzers/{analyzer_id}/{bucket}/{slug}.jsonl"


def map_slug(batch_no: int) -> str:
    # Zero-padded for the reason findings_path already documents: unpadded, 10
    # sorts before 2.
    return f"map-{batch_no:02d}"


async def persist_turn(
    *,
    analyzer_id: str,
    bucket: str,
    slug: str,
    events: list[dict],
) -> None:
    """Best-effort upload of one turn's raw events.

    A failure logs a warning but must NOT fail the analyzer job -- the findings
    are the primary product. Catches Exception, never BaseException: this runs
    inside asyncio.timeout(COHORT_TIMEOUT_SECONDS), and swallowing
    CancelledError would break the cohort timeout.
    """
    if not events:
        logger.warning(
            "analyzer-trajectory: %s/%s produced no events; nothing to upload",
            bucket, slug,
        )
        return
    key = trajectory_key(analyzer_id, bucket, slug)
    try:
        data = "\n".join(json.dumps(e) for e in events).encode("utf-8")
        await get_storage_client().upload_bytes(
            data, key, content_type="application/x-ndjson"
        )
        logger.info(
            "analyzer-trajectory: saved %d events to s3://%s/%s",
            len(events), settings.s3_bucket, key,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "analyzer-trajectory: failed to save %d events to s3://%s/%s: %s",
            len(events), settings.s3_bucket, key, exc,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/cc_chat/test_analyzer_trajectory.py -v`
Expected: PASS — 7 passed

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/cc_chat/analyzer_trajectory.py backend/tests/cc_chat/test_analyzer_trajectory.py
git commit -m "feat(analyzer): add best-effort S3 upload for a sandbox turn's raw trajectory

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Wire persistence into `run_cohort`

`_turn` already iterates every event to render and log it. Collect the raw dict alongside, and upload when the stream closes.

**Files:**
- Modify: `backend/api/services/cc_chat/analyzer_cohort.py:142-180`
- Test: `backend/tests/cc_chat/test_analyzer_cohort.py`

**Interfaces:**
- Consumes: `trajectory_key`, `map_slug`, `REDUCE_SLUG`, `persist_turn` from Task 1.
- Produces: no new public surface. `run_cohort`'s signature is unchanged — it already receives `analyzer_id` and `bucket`.

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/cc_chat/test_analyzer_cohort.py`. The file already has `_FakeRuntime`, `_good_files()`, `_kwargs()`, `COHORT`, and imports `analyzer_cohort as ac`:

```python
class _RecordingStorage:
    def __init__(self, exc=None):
        self.calls = []
        self._exc = exc

    async def upload_bytes(self, data, s3_key, *, content_type=None):
        self.calls.append({"data": data, "s3_key": s3_key})
        if self._exc:
            raise self._exc


async def test_each_turn_uploads_its_trajectory(monkeypatch):
    storage = _RecordingStorage()
    monkeypatch.setattr(at, "get_storage_client", lambda: storage)
    events = [{"type": "system", "subtype": "init", "model": "m"},
              {"type": "result", "total_cost_usd": 0.01}]
    runtime = _FakeRuntime(events, files=_good_files())

    await ac.run_cohort(FakeDaytonaClient(), runtime, **_kwargs())

    # One map batch (1-trial cohort) + one reduce.
    assert [c["s3_key"] for c in storage.calls] == [
        "analyzers/a1/bad/map-01.jsonl",
        "analyzers/a1/bad/reduce.jsonl",
    ]
    assert [json.loads(x) for x in
            storage.calls[0]["data"].decode().split("\n")] == events


async def test_trajectory_is_bucket_scoped(monkeypatch):
    storage = _RecordingStorage()
    monkeypatch.setattr(at, "get_storage_client", lambda: storage)
    runtime = _FakeRuntime([{"type": "result"}], files={
        REDUCE_PATH: json.dumps({"good_failure_content": "# Good"}).encode(),
        findings_path(1): (json.dumps({
            "trial_id": "bad-1", "bucket": "good", "subcategory": "3a",
            "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
            "headroom_signal": "h", "trajectory_link": "junk",
        }) + "\n").encode(),
    })

    await ac.run_cohort(FakeDaytonaClient(), runtime, **_kwargs(bucket="good"))

    assert all("/good/" in c["s3_key"] for c in storage.calls)


async def test_upload_failure_does_not_fail_cohort(monkeypatch):
    storage = _RecordingStorage(exc=RuntimeError("s3 down"))
    monkeypatch.setattr(at, "get_storage_client", lambda: storage)
    runtime = _FakeRuntime([{"type": "result"}], files=_good_files())

    findings, sections = await ac.run_cohort(
        FakeDaytonaClient(), runtime, **_kwargs()
    )

    assert findings  # the primary product survives a dead S3
    assert sections["bad_failure_content"] == "# Bad"
```

Add to the file's imports:

```python
from api.services.cc_chat import analyzer_trajectory as at
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/cc_chat/test_analyzer_cohort.py -k trajectory -v`
Expected: FAIL — `assert [] == ['analyzers/a1/bad/map-01.jsonl', ...]`; nothing uploads yet.

- [ ] **Step 3: Wire it into `_turn`**

In `backend/api/services/cc_chat/analyzer_cohort.py`, add to the imports:

```python
from api.services.cc_chat.analyzer_trajectory import (
    REDUCE_SLUG,
    map_slug,
    persist_turn,
)
```

Replace `_turn` (currently at :142-154) with:

```python
            async def _turn(prompt: str, label: str, slug: str,
                            system_prompt=None) -> None:
                # claude_session_id=None every time: a fresh process with a
                # fresh context is the whole point. Passing --resume here would
                # chain contexts and reintroduce the linear growth.
                events: list[dict] = []
                async for evt in runtime.stream_chat(
                    client, sandbox, content=prompt,
                    claude_session_id=None, daytona_session_id="cc",
                    system_prompt=system_prompt,
                ):
                    events.append(evt)
                    line = render_event(evt)
                    if line:
                        stream_lines.append(line)
                        logger.info("%s[%s] %s", tag, label, line)
                # Per turn, not per cohort: a cohort that later times out still
                # keeps every completed turn's record.
                await persist_turn(
                    analyzer_id=analyzer_id, bucket=bucket, slug=slug,
                    events=events,
                )
```

Update the map call (currently :163-170) to pass the slug:

```python
                await _turn(
                    build_map_batch_prompt(
                        bucket, batch, roster, oracle_by_trial, i, len(plan),
                        TRAJ_TAIL_BYTES,
                    ),
                    f"map {i}/{len(plan)}",
                    map_slug(i),
                    system_prompt=build_system_prompt(TRAJ_TAIL_BYTES),
                )
```

Update the reduce call (currently :177-180):

```python
            await _turn(
                build_reduce_only_prompt(bucket, counts, len(plan), models_by_task),
                "reduce",
                REDUCE_SLUG,
            )
```

- [ ] **Step 4: Run the new tests**

Run: `cd backend && pytest tests/cc_chat/test_analyzer_cohort.py -k trajectory -v`
Expected: PASS — 3 passed

- [ ] **Step 5: Run the full cohort + trajectory suites for regressions**

Run: `cd backend && pytest tests/cc_chat/test_analyzer_cohort.py tests/cc_chat/test_analyzer_trajectory.py -v`
Expected: PASS — all tests, including the pre-existing timeout/parse tests, still pass.

The pre-existing tests construct `_FakeRuntime` without any storage patch, so
`persist_turn` will call the real `get_storage_client()`. That is fine — it is
best-effort and its failure is swallowed to a warning — but if any pre-existing
test fails or hangs on a network call, stop and report rather than weakening
`persist_turn`. The fix is a module-scoped autouse fixture patching
`at.get_storage_client`, not a change to the production code.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/cc_chat/analyzer_cohort.py backend/tests/cc_chat/test_analyzer_cohort.py
git commit -m "feat(analyzer): persist each sandbox turn's trajectory to S3

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Correct the stale "never persisted" comments

Two comments now assert the opposite of what the code does. Left alone they would mislead the next reader, and they are the exact comments that made this gap findable in the first place.

**Files:**
- Modify: `backend/api/services/cc_chat/analyzer_cohort.py:4`, `:116`
- Modify: `backend/worker/analyzer_sandbox.py:7-8`

- [ ] **Step 1: Update the `analyzer_cohort` module docstring**

The docstring (:1-5) says "nothing here reads S3", which is still true of reads but is now easy to misread. Replace:

```python
"""Run one cohort's MAP -> REDUCE inside a Daytona sandbox.

One runner, parameterized by bucket. The agent pulls trajectories itself via the
oddish-query CLI, so nothing here reads S3; it only writes each turn's own
trajectory back to S3 for later debugging.
"""
```

- [ ] **Step 2: Correct the `stream_lines` comment**

At :116, `# Retained only to serve the parse-fallback; never persisted.` is now false — the raw events are persisted; the rendered lines still are not. Replace:

```python
    # Rendered lines: the parse-fallback's input, never persisted. The raw
    # events behind them go to S3 per turn (persist_turn).
    stream_lines: list[str] = []
```

- [ ] **Step 3: Verify no other stale claim remains**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish-present-2/oddish && grep -rn "never persisted\|nothing here reads S3" backend/ --include="*.py" | grep -v ".venv"`
Expected: only the two updated comments appear, both now accurate.

Check `backend/worker/analyzer_sandbox.py:7-8` ("Unlike the core path this never reads S3") — still true (it reads nothing), so leave it unless the grep shows it claiming *no S3 use at all*.

- [ ] **Step 4: Run the suite**

Run: `cd backend && pytest tests/cc_chat/ -v`
Expected: PASS — comment-only changes; no behavior change.

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/cc_chat/analyzer_cohort.py
git commit -m "docs(analyzer): correct comments that said trajectories are never persisted

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Verification

- [ ] Run the full backend suite: `cd backend && pytest -q`. Expected: no new failures versus `main`.
- [ ] Confirm the spec's out-of-scope list held: no DB column, no migration, no API endpoint, no `save_*` flag was added. `git diff main --stat` should touch only `analyzer_trajectory.py`, `analyzer_cohort.py`, their two test files, and the docs.
- [ ] Open a PR against `main`. Do not merge without review.

## Notes for the implementer

- **Do not** add `asyncio.shield` around the upload to rescue a turn cancelled mid-flight by the cohort timeout. It was considered and deliberately deferred (spec, "Known tradeoff").
- **Do not** add `default=str` to `json.dumps`. Every event `stream_chat` yields is either `json.loads` output or a synthesized dict (`_stderr`, `_invalid_json`), so all are serializable by construction (`claude_code_runtime.py:285-303`).
- The two cohorts run concurrently via `asyncio.gather`, but each writes under its own `<bucket>/` prefix, so there is no shared mutable state and no lock is needed.
- `test_trajectory_is_bucket_scoped` plants only `good_failure_content`, even though the good bucket owns three section keys. That is deliberate and verified: `_sections_from` (`analyzer_parse.py:76-85`) raises only when *no* key has non-blank content, so one is enough.
