# Trajectory Summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render an LLM-generated summary block (prose + 3–6 hot-linked "key moments") between the token/duration bars and the steps accordion in the Trajectory tab. Lazy-generated on first view; cached as an S3 sibling file.

**Architecture:** New `backend/api/services/summarize_trajectory.py` exposes `preprocess()` and `generate()`. New `read_trial_trajectory_summary()` / `write_trial_trajectory_summary()` in `oddish/src/oddish/core/trial_io.py` mirror the existing trajectory I/O (per-key lock, in-memory cache, S3 sibling file at `<trial_s3_prefix>/agent/trajectory_summary.json`). New endpoint `GET /trials/{trial_id}/trajectory/summary` does lazy-generate-then-write. Next.js proxy route + new `TrajectorySummary` component render it.

**Tech Stack:** Python 3.11 / FastAPI / asyncio / `anthropic` (`AsyncAnthropic`, `claude-sonnet-4-6`), Next.js 14 / SWR / shadcn/ui (Card, Button, Skeleton). Tests: pytest with `unittest.mock.patch("anthropic.AsyncAnthropic", ...)` and `pytest-asyncio`.

**Spec:** `docs/superpowers/specs/2026-04-30-trajectory-summary-design.md`

---

## File Structure

**Create:**
- `backend/api/services/summarize_trajectory.py` — `preprocess(trajectory)` and `generate(trajectory)`.
- `backend/tests/test_summarize_trajectory.py` — unit tests for both.
- `backend/tests/test_trajectory_summary_endpoint.py` — endpoint integration test.
- `frontend/src/app/api/trials/[trial_id]/trajectory/summary/route.ts` — Next.js proxy.
- `frontend/src/components/trajectory-summary.tsx` — summary UI.

**Modify:**
- `oddish/src/oddish/core/trial_io.py` — add `_TRAJECTORY_SUMMARY_CACHE`, `_TRAJECTORY_SUMMARY_LOCKS`, `read_trial_trajectory_summary`, `write_trial_trajectory_summary`, `_trajectory_summary_candidate_keys`.
- `backend/api/routers/trials.py` — add `GET /trials/{trial_id}/trajectory/summary` and import the new helpers.
- `frontend/src/lib/types.ts` — add `TrajectorySummary` and `TrajectoryHighlight` interfaces.
- `frontend/src/components/trajectory-viewer.tsx` — render `<TrajectorySummary>` between `<StepDurationBar>` and the accordion; map `step_id → array index` and pass through `handleStepClick`.

---

## Task 1: Preprocess function (strip images, truncate large text)

**Files:**
- Create: `backend/api/services/summarize_trajectory.py`
- Test: `backend/tests/test_summarize_trajectory.py`

- [ ] **Step 1.1: Write failing tests**

Create `backend/tests/test_summarize_trajectory.py`:

```python
"""Tests for backend.api.services.summarize_trajectory."""

from __future__ import annotations

from copy import deepcopy

import pytest

from backend.api.services.summarize_trajectory import (
    MAX_TEXT_CHARS,
    TRUNCATE_HEAD,
    TRUNCATE_TAIL,
    preprocess,
)


def _make_step(step_id: int, **overrides) -> dict:
    base: dict = {
        "step_id": step_id,
        "timestamp": "2026-04-30T12:00:00Z",
        "source": "agent",
        "model_name": "claude-sonnet-4-6",
        "message": "hello",
        "reasoning_content": None,
        "tool_calls": None,
        "observation": None,
        "metrics": None,
    }
    base.update(overrides)
    return base


def test_preprocess_leaves_small_fields_untouched():
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "claude-code", "version": "1", "model_name": "x"},
        "steps": [_make_step(1, message="short")],
        "notes": None,
        "final_metrics": None,
    }
    expected = deepcopy(trajectory)
    assert preprocess(trajectory) == expected


def test_preprocess_truncates_large_reasoning_content():
    long_text = "A" * 800 + "B" * 1500 + "C" * 500  # 2800 chars
    step = _make_step(1, reasoning_content=long_text)
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    rc = out["steps"][0]["reasoning_content"]
    assert rc.startswith("A" * TRUNCATE_HEAD)
    assert rc.endswith("C" * TRUNCATE_TAIL)
    assert "[...truncated" in rc
    assert len(rc) < len(long_text)


def test_preprocess_strips_image_content_parts():
    step = _make_step(
        1,
        message=[
            {"type": "text", "text": "look at this:"},
            {"type": "image", "source": {"media_type": "image/png", "path": "x.png"}},
            {"type": "text", "text": "thoughts?"},
        ],
        observation={
            "results": [
                {
                    "source_call_id": "c1",
                    "content": [
                        {"type": "image", "source": {"media_type": "image/png", "path": "y.png"}},
                    ],
                }
            ]
        },
    )
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    msg_parts = out["steps"][0]["message"]
    assert {p["type"] for p in msg_parts} == {"text"}
    assert any("[image omitted]" in p["text"] for p in msg_parts)
    obs_parts = out["steps"][0]["observation"]["results"][0]["content"]
    assert obs_parts[0]["type"] == "text"
    assert "[image omitted]" in obs_parts[0]["text"]


def test_preprocess_truncates_tool_call_argument_values():
    huge = "Z" * (MAX_TEXT_CHARS + 500)
    step = _make_step(
        1,
        tool_calls=[
            {
                "tool_call_id": "t1",
                "function_name": "edit_file",
                "arguments": {"path": "main.py", "content": huge},
            }
        ],
    )
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    args = out["steps"][0]["tool_calls"][0]["arguments"]
    assert args["path"] == "main.py"  # small string untouched
    assert "[...truncated" in args["content"]
    assert len(args["content"]) < len(huge)


def test_preprocess_truncates_observation_string_content():
    huge = "L" * (MAX_TEXT_CHARS + 1000)
    step = _make_step(
        1,
        observation={
            "results": [{"source_call_id": "c1", "content": huge}]
        },
    )
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    out = preprocess(trajectory)
    content = out["steps"][0]["observation"]["results"][0]["content"]
    assert "[...truncated" in content
    assert len(content) < len(huge)


def test_preprocess_does_not_mutate_input():
    huge = "Q" * (MAX_TEXT_CHARS + 100)
    step = _make_step(1, reasoning_content=huge)
    trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [step],
        "notes": None,
        "final_metrics": None,
    }
    snapshot = deepcopy(trajectory)
    preprocess(trajectory)
    assert trajectory == snapshot
```

- [ ] **Step 1.2: Run tests — confirm they fail**

```bash
cd backend && pytest tests/test_summarize_trajectory.py -v
```
Expected: every test fails with `ModuleNotFoundError: backend.api.services.summarize_trajectory` (or `ImportError`).

- [ ] **Step 1.3: Implement `preprocess`**

Create `backend/api/services/summarize_trajectory.py`:

```python
"""LLM-backed trajectory summarization.

Two pure-ish responsibilities:
  - ``preprocess`` strips image content parts and truncates large text fields
    so the token cost of the summary call is bounded.
  - ``generate`` calls the Anthropic API with a preprocessed trajectory and
    returns a persistable summary dict.

This module deliberately mirrors the JSON-parsing style of
``oddish.worker.local_runner._run_probe_analyzer`` rather than using tool-use
so the test patterns and prompt-shape conventions match the rest of the repo.
"""

from __future__ import annotations

import json
import logging
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

MAX_TEXT_CHARS = 2000
TRUNCATE_HEAD = 800
TRUNCATE_TAIL = 400
TRUNCATION_MARKER = "\n[...truncated {n} chars...]\n"
SCHEMA_VERSION = "1"
MODEL = "claude-sonnet-4-6"


def _truncate(text: str) -> str:
    if len(text) <= MAX_TEXT_CHARS:
        return text
    head = text[:TRUNCATE_HEAD]
    tail = text[-TRUNCATE_TAIL:]
    omitted = len(text) - TRUNCATE_HEAD - TRUNCATE_TAIL
    return head + TRUNCATION_MARKER.format(n=omitted) + tail


def _strip_images(parts: list[dict]) -> list[dict]:
    """Replace image parts with a single placeholder text part."""
    out: list[dict] = []
    skipped = 0
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "image":
            skipped += 1
            continue
        if isinstance(part, dict) and part.get("type") == "text":
            text = part.get("text") or ""
            out.append({"type": "text", "text": _truncate(text)})
        else:
            out.append(part)
    if skipped:
        out.append({"type": "text", "text": f"[image omitted x{skipped}]"})
    return out


def _process_content(value: Any) -> Any:
    """Process MessageContent / ObservationContent (string | list[ContentPart] | None)."""
    if value is None:
        return None
    if isinstance(value, str):
        return _truncate(value)
    if isinstance(value, list):
        return _strip_images(value)
    return value


def _process_tool_calls(tool_calls: list[dict] | None) -> list[dict] | None:
    if not tool_calls:
        return tool_calls
    out = []
    for call in tool_calls:
        new_call = dict(call)
        args = new_call.get("arguments")
        if isinstance(args, dict):
            new_call["arguments"] = {
                k: _truncate(v) if isinstance(v, str) else v
                for k, v in args.items()
            }
        out.append(new_call)
    return out


def _process_observation(obs: dict | None) -> dict | None:
    if obs is None:
        return None
    new_obs = dict(obs)
    new_results = []
    for result in obs.get("results") or []:
        new_result = dict(result)
        new_result["content"] = _process_content(result.get("content"))
        new_results.append(new_result)
    new_obs["results"] = new_results
    return new_obs


def preprocess(trajectory: dict) -> dict:
    """Return a copy of ``trajectory`` with images stripped and long text truncated."""
    out = deepcopy(trajectory)
    new_steps = []
    for step in out.get("steps") or []:
        new_step = dict(step)
        new_step["message"] = _process_content(step.get("message"))
        rc = step.get("reasoning_content")
        if isinstance(rc, str):
            new_step["reasoning_content"] = _truncate(rc)
        new_step["tool_calls"] = _process_tool_calls(step.get("tool_calls"))
        new_step["observation"] = _process_observation(step.get("observation"))
        new_steps.append(new_step)
    out["steps"] = new_steps
    return out
```

- [ ] **Step 1.4: Run tests — confirm they pass**

```bash
cd backend && pytest tests/test_summarize_trajectory.py -v
```
Expected: all 6 tests pass.

- [ ] **Step 1.5: Commit**

```bash
git add backend/api/services/summarize_trajectory.py backend/tests/test_summarize_trajectory.py
git commit -m "Add trajectory summary preprocessor"
```

---

## Task 2: Generate function (Claude call + JSON parse + step-id validation)

**Files:**
- Modify: `backend/api/services/summarize_trajectory.py`
- Modify: `backend/tests/test_summarize_trajectory.py`

- [ ] **Step 2.1: Add failing tests for `generate`**

Append to `backend/tests/test_summarize_trajectory.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch


def _trajectory_with_steps(step_ids: list[int]) -> dict:
    return {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [_make_step(sid) for sid in step_ids],
        "notes": None,
        "final_metrics": None,
    }


def _fake_client_returning(text: str) -> MagicMock:
    fake_response = MagicMock()
    fake_response.content = [MagicMock(text=text)]
    fake_client = MagicMock()
    fake_client.messages.create = AsyncMock(return_value=fake_response)
    return fake_client


@pytest.mark.asyncio
async def test_generate_returns_persistable_summary():
    from backend.api.services.summarize_trajectory import (
        MODEL,
        SCHEMA_VERSION,
        generate,
    )

    payload = json.dumps(
        {
            "summary": "Agent reproduced and fixed a flaky test.",
            "highlights": [
                {"step_id": 1, "title": "Reproduces failure", "why": "First confirmation."},
                {"step_id": 3, "title": "Lands the fix", "why": "Patch applied."},
            ],
        }
    )
    fake = _fake_client_returning(payload)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1, 2, 3]))

    assert result["schema_version"] == SCHEMA_VERSION
    assert result["model"] == MODEL
    assert "generated_at" in result
    assert result["summary"].startswith("Agent reproduced")
    assert [h["step_id"] for h in result["highlights"]] == [1, 3]


@pytest.mark.asyncio
async def test_generate_drops_highlights_with_unknown_step_ids():
    from backend.api.services.summarize_trajectory import generate

    payload = json.dumps(
        {
            "summary": "x",
            "highlights": [
                {"step_id": 1, "title": "ok", "why": "ok"},
                {"step_id": 999, "title": "bogus", "why": "model hallucinated"},
            ],
        }
    )
    fake = _fake_client_returning(payload)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1, 2, 3]))

    assert [h["step_id"] for h in result["highlights"]] == [1]


@pytest.mark.asyncio
async def test_generate_strips_code_fences_around_json():
    from backend.api.services.summarize_trajectory import generate

    body = json.dumps({"summary": "ok", "highlights": []})
    fenced = f"```json\n{body}\n```"
    fake = _fake_client_returning(fenced)
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        result = await generate(_trajectory_with_steps([1]))
    assert result["summary"] == "ok"
    assert result["highlights"] == []


@pytest.mark.asyncio
async def test_generate_raises_on_malformed_json():
    from backend.api.services.summarize_trajectory import (
        SummaryGenerationError,
        generate,
    )

    fake = _fake_client_returning("not json at all")
    with patch("anthropic.AsyncAnthropic", return_value=fake):
        with pytest.raises(SummaryGenerationError):
            await generate(_trajectory_with_steps([1]))
```

- [ ] **Step 2.2: Run tests — confirm new tests fail**

```bash
cd backend && pytest tests/test_summarize_trajectory.py -v
```
Expected: the 4 new tests fail (`ImportError` for `generate` / `SummaryGenerationError`).

- [ ] **Step 2.3: Implement `generate`**

Append to `backend/api/services/summarize_trajectory.py`:

```python
class SummaryGenerationError(RuntimeError):
    """Raised when the LLM returned content we could not turn into a summary."""


_PROMPT_HEADER = (
    "You are summarizing a recorded agent trajectory for a developer who "
    "wants a quick scan before diving into the per-step view. Produce a "
    "2-3 sentence summary covering what the agent set out to do and how "
    "it ended, then 3-6 pivotal 'key moments' with their step ids.\n\n"
    "Each highlight must reference a real `step_id` from the trajectory below. "
    "Pick steps where something genuinely shifted: a strategy was committed, "
    "a key tool call landed, an error redirected the work, or the final "
    "verdict was reached. Skip filler.\n\n"
    "Respond with ONLY a JSON object (no preamble, no code fences) matching "
    "this exact shape:\n"
    "{\n"
    '  "summary": "2-3 sentences",\n'
    '  "highlights": [\n'
    '    {"step_id": <int>, "title": "<short label>", "why": "<one sentence>"}\n'
    "  ]\n"
    "}\n"
    "Highlights must be ordered by step_id ascending.\n\n"
)


def _strip_code_fences(text: str) -> str:
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.lstrip().startswith("json"):
            raw = raw.split("\n", 1)[1] if "\n" in raw else raw
        raw = raw.rsplit("```", 1)[0]
    return raw.strip()


async def generate(trajectory: dict) -> dict:
    """Call Claude to produce a persistable summary dict for ``trajectory``.

    Raises ``SummaryGenerationError`` if the model returns malformed JSON or
    cannot be parsed. Highlights referencing step_ids that are not in the
    source trajectory are dropped silently.
    """
    from anthropic import AsyncAnthropic

    valid_step_ids = {
        step.get("step_id")
        for step in (trajectory.get("steps") or [])
        if isinstance(step.get("step_id"), int)
    }

    compact = preprocess(trajectory)
    prompt = _PROMPT_HEADER + "<trajectory>\n" + json.dumps(compact) + "\n</trajectory>"

    client = AsyncAnthropic()
    msg = await client.messages.create(
        model=MODEL,
        max_tokens=2048,
        messages=[{"role": "user", "content": prompt}],
    )

    raw_text = ""
    for block in msg.content:
        if hasattr(block, "text"):
            raw_text += block.text
    raw_text = _strip_code_fences(raw_text)
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError as e:
        raise SummaryGenerationError(f"Model returned non-JSON: {e}") from e

    summary = str(parsed.get("summary") or "").strip()
    raw_highlights = parsed.get("highlights") or []
    highlights: list[dict] = []
    if isinstance(raw_highlights, list):
        for entry in raw_highlights:
            if not isinstance(entry, dict):
                continue
            step_id = entry.get("step_id")
            if not isinstance(step_id, int) or step_id not in valid_step_ids:
                continue
            highlights.append(
                {
                    "step_id": step_id,
                    "title": str(entry.get("title") or "").strip(),
                    "why": str(entry.get("why") or "").strip(),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "model": MODEL,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": summary,
        "highlights": highlights,
    }
```

- [ ] **Step 2.4: Run tests — confirm pass**

```bash
cd backend && pytest tests/test_summarize_trajectory.py -v
```
Expected: all 10 tests pass.

- [ ] **Step 2.5: Commit**

```bash
git add backend/api/services/summarize_trajectory.py backend/tests/test_summarize_trajectory.py
git commit -m "Add trajectory summary generate() with JSON parse + step-id validation"
```

---

## Task 3: trial_io read/write helpers (S3 sibling file + cache)

**Files:**
- Modify: `oddish/src/oddish/core/trial_io.py`
- Test: `backend/tests/test_trajectory_summary_io.py`

- [ ] **Step 3.1: Write failing tests for read/write helpers**

Create `backend/tests/test_trajectory_summary_io.py`:

```python
"""Tests for trajectory_summary read/write helpers in oddish.core.trial_io."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _trial(trial_id: str = "t1", *, finished: bool = True) -> SimpleNamespace:
    """Lightweight TrialModel double — only the attributes trial_io reads."""
    return SimpleNamespace(
        id=trial_id,
        name="trial-0",
        trial_s3_key=f"trials/{trial_id}/",
        harbor_result_path=None,
        finished_at=datetime.now(timezone.utc) if finished else None,
    )


def _fake_storage_with_text(text: str | None) -> MagicMock:
    storage = MagicMock()
    if text is None:
        storage.download_text = AsyncMock(side_effect=Exception("no key"))
    else:
        storage.download_text = AsyncMock(return_value=text)
    storage.upload_bytes = AsyncMock()
    storage.list_keys = AsyncMock(return_value=[])
    return storage


@pytest.mark.asyncio
async def test_read_returns_existing_summary_from_s3():
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    payload = {"schema_version": "1", "summary": "x", "highlights": []}
    storage = _fake_storage_with_text(json.dumps(payload))

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage):
        result = await read_trial_trajectory_summary(_trial("t-existing"))
    assert result == payload


@pytest.mark.asyncio
async def test_read_returns_none_when_no_trajectory_to_summarize():
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    storage = _fake_storage_with_text(None)

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "oddish.core.trial_io._read_trial_trajectory_uncached", new=AsyncMock(return_value=None)
    ):
        result = await read_trial_trajectory_summary(_trial("t-no-traj"))
    assert result is None


@pytest.mark.asyncio
async def test_read_lazily_generates_writes_and_caches():
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    storage = _fake_storage_with_text(None)
    fake_trajectory = {
        "schema_version": "0.1",
        "session_id": "s1",
        "agent": {"name": "x", "version": "1", "model_name": None},
        "steps": [{"step_id": 1, "timestamp": None, "source": "agent",
                   "model_name": None, "message": "ok",
                   "reasoning_content": None, "tool_calls": None,
                   "observation": None, "metrics": None}],
        "notes": None,
        "final_metrics": None,
    }
    fake_summary = {
        "schema_version": "1",
        "model": "claude-sonnet-4-6",
        "generated_at": "2026-04-30T12:00:00+00:00",
        "summary": "ran one step",
        "highlights": [],
    }

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "oddish.core.trial_io._read_trial_trajectory_uncached",
        new=AsyncMock(return_value=fake_trajectory),
    ), patch(
        "backend.api.services.summarize_trajectory.generate",
        new=AsyncMock(return_value=fake_summary),
    ) as mock_gen:
        result = await read_trial_trajectory_summary(_trial("t-gen"))

    assert result == fake_summary
    assert mock_gen.await_count == 1
    storage.upload_bytes.assert_awaited_once()
    args, kwargs = storage.upload_bytes.await_args
    written_bytes, written_key = args[0], args[1]
    assert written_key == "trials/t-gen/agent/trajectory_summary.json"
    assert json.loads(written_bytes.decode("utf-8")) == fake_summary

    # Second call should hit the in-memory cache and not re-generate or re-write.
    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "backend.api.services.summarize_trajectory.generate",
        new=AsyncMock(return_value=fake_summary),
    ) as mock_gen2:
        result2 = await read_trial_trajectory_summary(_trial("t-gen"))
    assert result2 == fake_summary
    assert mock_gen2.await_count == 0


@pytest.mark.asyncio
async def test_write_failure_after_generate_still_returns_summary():
    """Best-effort persistence: if S3 write fails, return the generated summary
    so the user isn't shown an error for a successfully-generated summary."""
    from oddish.core.trial_io import (
        _TRAJECTORY_SUMMARY_CACHE,
        read_trial_trajectory_summary,
    )

    _TRAJECTORY_SUMMARY_CACHE.clear()
    storage = _fake_storage_with_text(None)
    storage.upload_bytes = AsyncMock(side_effect=RuntimeError("S3 down"))
    fake_trajectory = {"steps": [{"step_id": 1}], "schema_version": "0.1",
                       "session_id": "s", "agent": {"name": "x", "version": "1", "model_name": None},
                       "notes": None, "final_metrics": None}
    fake_summary = {"schema_version": "1", "model": "claude-sonnet-4-6",
                    "generated_at": "now", "summary": "x", "highlights": []}

    with patch("oddish.core.trial_io.get_storage_client", return_value=storage), patch(
        "oddish.core.trial_io._read_trial_trajectory_uncached",
        new=AsyncMock(return_value=fake_trajectory),
    ), patch(
        "backend.api.services.summarize_trajectory.generate",
        new=AsyncMock(return_value=fake_summary),
    ):
        result = await read_trial_trajectory_summary(_trial("t-s3-fail"))
    assert result == fake_summary
```

- [ ] **Step 3.2: Run tests — confirm failure**

```bash
cd backend && pytest tests/test_trajectory_summary_io.py -v
```
Expected: all four tests fail with ImportError on `_TRAJECTORY_SUMMARY_CACHE` / `read_trial_trajectory_summary`.

- [ ] **Step 3.3: Implement helpers in trial_io.py**

Edit `oddish/src/oddish/core/trial_io.py`. Add the new caches near the existing ones (after line 26, after `_TRAJECTORY_LOCKS`):

```python
_TRAJECTORY_SUMMARY_CACHE: dict[str, tuple[float, dict | None]] = {}
_TRAJECTORY_SUMMARY_LOCKS: dict[str, asyncio.Lock] = {}
```

Add the candidate-keys helper near `_trajectory_candidate_keys` (around line 149):

```python
def _trajectory_summary_candidate_keys(trial: TrialModel, s3_prefix: str) -> list[str]:
    """Return likely S3 keys for the cached trajectory summary."""
    candidates: list[str] = [f"{s3_prefix}agent/trajectory_summary.json"]
    if trial.name:
        candidates.append(f"{s3_prefix}{trial.name}/agent/trajectory_summary.json")
    candidates.append(f"{s3_prefix}trial-0/agent/trajectory_summary.json")
    return list(dict.fromkeys(candidates))
```

Add the read/write functions just below `read_trial_trajectory` (after line 523):

```python
async def _read_trial_trajectory_summary_uncached(trial: TrialModel) -> dict | None:
    """Read trajectory_summary.json from S3 (or local fallback)."""
    s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
    storage = get_storage_client()

    for key in _trajectory_summary_candidate_keys(trial, s3_prefix):
        try:
            content = await storage.download_text(key)
            if content:
                parsed: dict = _json.loads(content)
                return parsed
        except Exception:
            continue

    # Local-path fallback (parity with read_trial_trajectory).
    if not trial.harbor_result_path:
        return None
    trial_paths = _resolve_local_trial_paths(trial)
    if trial_paths is None:
        return None
    summary_path = trial_paths.agent_dir / "trajectory_summary.json"
    try:
        summary_path_resolved = summary_path.resolve()
    except Exception:
        return None
    if not summary_path_resolved.exists() or not summary_path_resolved.is_file():
        return None
    try:
        return _json.loads(summary_path_resolved.read_text(errors="replace"))
    except Exception:
        return None


async def _write_trial_trajectory_summary(
    trial: TrialModel, summary: dict
) -> None:
    """Best-effort persist of a generated summary to S3."""
    s3_prefix = trial.trial_s3_key or StorageClient._trial_prefix(trial.id)
    key = f"{s3_prefix}agent/trajectory_summary.json"
    storage = get_storage_client()
    payload = _json.dumps(summary).encode("utf-8")
    try:
        await storage.upload_bytes(payload, key, content_type="application/json")
    except Exception as e:
        logging.getLogger(__name__).warning(
            f"Failed to persist trajectory summary for {trial.id} at {key}: {e}"
        )


async def read_trial_trajectory_summary(trial: TrialModel) -> dict | None:
    """Read or lazily generate the trajectory summary for a trial.

    Returns ``None`` if the trial has no trajectory at all (nothing to
    summarize). Otherwise returns the persisted summary, generating and
    writing one on first access. Per-key locking prevents duplicate
    generation when multiple viewers arrive at once.
    """
    cache_key = trial.id
    if _should_cache_trial(trial):
        cached = _cache_get(_TRAJECTORY_SUMMARY_CACHE, cache_key)
        if cached is not None:
            return cached  # type: ignore[return-value]

    lock = _get_lock(_TRAJECTORY_SUMMARY_LOCKS, cache_key)
    async with lock:
        if _should_cache_trial(trial):
            cached = _cache_get(_TRAJECTORY_SUMMARY_CACHE, cache_key)
            if cached is not None:
                return cached  # type: ignore[return-value]

        existing = await _read_trial_trajectory_summary_uncached(trial)
        if existing is not None:
            if _should_cache_trial(trial):
                _cache_set(_TRAJECTORY_SUMMARY_CACHE, cache_key, existing)
            return existing

        # No persisted summary yet — generate.
        trajectory = await _read_trial_trajectory_uncached(trial)
        if trajectory is None:
            return None

        # Local import to avoid pulling backend.api.services into oddish at module load.
        from backend.api.services.summarize_trajectory import generate

        summary = await generate(trajectory)
        await _write_trial_trajectory_summary(trial, summary)
        if _should_cache_trial(trial):
            _cache_set(_TRAJECTORY_SUMMARY_CACHE, cache_key, summary)
        return summary
```

- [ ] **Step 3.4: Run tests — confirm pass**

```bash
cd backend && pytest tests/test_trajectory_summary_io.py -v
```
Expected: all four pass.

- [ ] **Step 3.5: Run the existing trial_io adjacent tests to ensure no regression**

```bash
cd backend && pytest tests/ -v -k "trial or trajectory"
```
Expected: all pass (no behavior changes to existing helpers).

- [ ] **Step 3.6: Commit**

```bash
git add oddish/src/oddish/core/trial_io.py backend/tests/test_trajectory_summary_io.py
git commit -m "Add trial_io read/write helpers for trajectory summary"
```

---

## Task 4: API endpoint

**Files:**
- Modify: `backend/api/routers/trials.py`
- Test: `backend/tests/test_trajectory_summary_endpoint.py`

- [ ] **Step 4.1: Write failing endpoint test**

Create `backend/tests/test_trajectory_summary_endpoint.py`:

```python
"""Endpoint test for GET /trials/{trial_id}/trajectory/summary.

We patch ``read_trial_trajectory_summary`` directly so this is a pure
router-shape test (auth wiring + status codes + response body), not an
integration test of the lazy-generate flow.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

# Reuse the existing API app factory used by other backend tests.
from backend.api.app import create_app  # type: ignore[attr-defined]


@pytest.fixture
def app_with_stub_auth():
    """Build the FastAPI app with `require_auth` overridden via dependency_overrides
    (the canonical FastAPI test pattern — monkeypatch can't intercept a Depends()
    reference that's already been captured at route registration time)."""
    from auth import APIKeyScope, AuthContext, require_auth

    fake_auth = AuthContext(
        org_id="org-1", user_id="u-1", scopes={APIKeyScope.READ}, is_admin=False
    )

    async def _fake_require_auth():
        return fake_auth

    app = create_app()
    app.dependency_overrides[require_auth] = _fake_require_auth
    return app


@pytest.fixture
def client(app_with_stub_auth):
    return TestClient(app_with_stub_auth)


@pytest.fixture
def fake_trial():
    return SimpleNamespace(id="t-1", name="trial-0", trial_s3_key="trials/t-1/")


def test_endpoint_returns_summary_when_present(client, fake_trial):
    summary = {"schema_version": "1", "summary": "ok", "highlights": []}
    with patch(
        "backend.api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "backend.api.routers.trials.read_trial_trajectory_summary",
        new=AsyncMock(return_value=summary),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 200
    assert resp.json() == summary


def test_endpoint_returns_404_when_no_trajectory(client, fake_trial):
    with patch(
        "backend.api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "backend.api.routers.trials.read_trial_trajectory_summary",
        new=AsyncMock(return_value=None),
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 404


def test_endpoint_returns_502_on_generation_error(client, fake_trial):
    from backend.api.services.summarize_trajectory import SummaryGenerationError

    async def _raise(_trial):
        raise SummaryGenerationError("model returned garbage")

    with patch(
        "backend.api.routers.trials._get_authorized_trial",
        new=AsyncMock(return_value=fake_trial),
    ), patch(
        "backend.api.routers.trials.read_trial_trajectory_summary", new=_raise
    ):
        resp = client.get("/trials/t-1/trajectory/summary")
    assert resp.status_code == 502
```

- [ ] **Step 4.2: Run — confirm failure**

```bash
cd backend && pytest tests/test_trajectory_summary_endpoint.py -v
```
Expected: 404 from FastAPI (route doesn't exist yet) on the success test → fails the assertion.

- [ ] **Step 4.3: Add the endpoint**

Edit `backend/api/routers/trials.py`. Add `read_trial_trajectory_summary` to the existing import block from `oddish.core.trial_io` (around line 14):

```python
from oddish.core.trial_io import (
    # ... existing imports ...
    read_trial_trajectory_summary,
    read_trial_trajectory,
    # ... existing imports ...
)
```

Add this endpoint immediately after `get_trial_trajectory` (line 259):

```python
@router.get("/trials/{trial_id}/trajectory/summary")
async def get_trial_trajectory_summary(
    trial_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Get a Claude-generated summary of the trajectory.

    Lazy-generated on first read and cached as an S3 sibling file. Returns
    404 when the trial has no trajectory to summarize, 502 if generation
    fails or the model returns malformed JSON.
    """
    from backend.api.services.summarize_trajectory import SummaryGenerationError

    auth.require_scope(APIKeyScope.READ)
    trial = await _get_authorized_trial(trial_id, auth)
    try:
        summary = await read_trial_trajectory_summary(trial)
    except SummaryGenerationError as e:
        raise HTTPException(
            status_code=502, detail=f"Summary generation failed: {e}"
        )
    if summary is None:
        raise HTTPException(
            status_code=404, detail="No trajectory available for this trial"
        )
    return summary
```

- [ ] **Step 4.4: Run — confirm pass**

```bash
cd backend && pytest tests/test_trajectory_summary_endpoint.py -v
```
Expected: all three pass.

- [ ] **Step 4.5: Run full backend test suite to catch regressions**

```bash
cd backend && pytest -x
```
Expected: all green. If the auth fixture pattern doesn't quite match the suite's existing convention, mirror whatever `test_probe_analyzer.py` / `test_local_runner.py` use for stub-auth and fix.

- [ ] **Step 4.6: Commit**

```bash
git add backend/api/routers/trials.py backend/tests/test_trajectory_summary_endpoint.py
git commit -m "Add GET /trials/{id}/trajectory/summary endpoint"
```

---

## Task 5: Frontend types + Next.js proxy route

**Files:**
- Create: `frontend/src/app/api/trials/[trial_id]/trajectory/summary/route.ts`
- Modify: `frontend/src/lib/types.ts`

- [ ] **Step 5.1: Add types**

Edit `frontend/src/lib/types.ts`. Append to the ATIF section (after the existing `Trajectory` interface around line 388):

```typescript
export interface TrajectoryHighlight {
  step_id: number;
  title: string;
  why: string;
}

export interface TrajectorySummary {
  schema_version: string;
  model: string;
  generated_at: string;
  summary: string;
  highlights: TrajectoryHighlight[];
}
```

- [ ] **Step 5.2: Create the proxy route**

Create `frontend/src/app/api/trials/[trial_id]/trajectory/summary/route.ts` (mirrors `frontend/src/app/api/trials/[trial_id]/trajectory/route.ts:1-42`):

```typescript
import { NextResponse } from "next/server";
import { auth } from "@clerk/nextjs/server";
import {
  getAuthHeaders,
  getBackendUrl,
  getClerkToken,
} from "@/lib/backend-config";

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ trial_id: string }> },
) {
  try {
    const { getToken } = await auth();
    const token = await getClerkToken(getToken);

    const { trial_id } = await params;

    const url = getBackendUrl("trials", `/${trial_id}/trajectory/summary`);
    const res = await fetch(url, {
      cache: "no-store",
      headers: getAuthHeaders(token),
    });

    const text = await res.text();
    const data = text ? JSON.parse(text) : null;

    if (!res.ok) {
      return NextResponse.json(data ?? { error: "Upstream error" }, {
        status: res.status,
      });
    }
    return NextResponse.json(data);
  } catch (error) {
    return NextResponse.json(
      { error: error instanceof Error ? error.message : "Unknown error" },
      { status: 503 },
    );
  }
}
```

- [ ] **Step 5.3: Verify it builds**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: no errors.

- [ ] **Step 5.4: Commit**

```bash
git add frontend/src/app/api/trials/[trial_id]/trajectory/summary/route.ts frontend/src/lib/types.ts
git commit -m "Add frontend types and proxy route for trajectory summary"
```

---

## Task 6: TrajectorySummary component

**Files:**
- Create: `frontend/src/components/trajectory-summary.tsx`

- [ ] **Step 6.1: Create the component**

Create `frontend/src/components/trajectory-summary.tsx`:

```tsx
"use client";

import useSWR from "swr";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Sparkles, ChevronRight, AlertCircle, Loader2 } from "lucide-react";
import { fetcher } from "@/lib/api";
import type { TrajectorySummary as TrajectorySummaryT } from "@/lib/types";

interface TrajectorySummaryProps {
  trialId: string;
  /**
   * Map a step_id from the summary to the array index used by the
   * accordion in TrajectoryViewer. Returns -1 if the step_id is unknown
   * (in which case the row is rendered non-clickable).
   */
  stepIdToIndex: (stepId: number) => number;
  onStepSelect: (index: number) => void;
  apiBaseUrl?: string;
}

export function TrajectorySummary({
  trialId,
  stepIdToIndex,
  onStepSelect,
  apiBaseUrl = "/api",
}: TrajectorySummaryProps) {
  const { data, error, isLoading, mutate } = useSWR<TrajectorySummaryT | null>(
    `${apiBaseUrl}/trials/${trialId}/trajectory/summary`,
    fetcher,
    { revalidateOnFocus: false },
  );

  if (isLoading) {
    return (
      <Card className="my-3">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <Sparkles className="h-4 w-4" />
            Summary
          </CardTitle>
        </CardHeader>
        <CardContent className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          Generating summary…
        </CardContent>
      </Card>
    );
  }

  if (error) {
    return (
      <Card className="my-3 border-red-200">
        <CardHeader className="pb-2">
          <CardTitle className="flex items-center gap-2 text-sm font-medium">
            <AlertCircle className="h-4 w-4 text-red-500" />
            Summary unavailable
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          <p className="text-xs text-muted-foreground">{error.message}</p>
          <Button size="sm" variant="outline" onClick={() => mutate()}>
            Retry
          </Button>
        </CardContent>
      </Card>
    );
  }

  if (!data) return null;

  return (
    <Card className="my-3">
      <CardHeader className="pb-2">
        <CardTitle className="flex items-center gap-2 text-sm font-medium">
          <Sparkles className="h-4 w-4" />
          Summary
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-3">
        {data.summary && (
          <p className="text-sm leading-relaxed text-foreground">
            {data.summary}
          </p>
        )}
        {data.highlights.length > 0 && (
          <ul className="space-y-1">
            {data.highlights.map((h) => {
              const index = stepIdToIndex(h.step_id);
              const disabled = index < 0;
              return (
                <li key={h.step_id}>
                  <button
                    type="button"
                    disabled={disabled}
                    onClick={() => !disabled && onStepSelect(index)}
                    className="group flex w-full items-start gap-2 rounded-md p-2 text-left text-sm hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
                  >
                    <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground group-hover:text-foreground" />
                    <span className="flex-1">
                      <span className="font-medium">
                        Step {h.step_id} · {h.title}
                      </span>
                      {h.why && (
                        <span className="block text-xs text-muted-foreground">
                          {h.why}
                        </span>
                      )}
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
```

- [ ] **Step 6.2: Verify it builds**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: no errors. (The component is unused at this point — the next task wires it in.)

- [ ] **Step 6.3: Commit**

```bash
git add frontend/src/components/trajectory-summary.tsx
git commit -m "Add TrajectorySummary component"
```

---

## Task 7: Wire TrajectorySummary into TrajectoryViewer

**Files:**
- Modify: `frontend/src/components/trajectory-viewer.tsx`

- [ ] **Step 7.1: Add the import**

In `frontend/src/components/trajectory-viewer.tsx`, after the existing imports near line 15:

```typescript
import { TrajectorySummary } from "@/components/trajectory-summary";
```

- [ ] **Step 7.2: Render the summary block between the duration bar and the accordion**

In the same file, the `TrajectoryViewer` return currently renders (around line 815):

```tsx
{/* Token Usage Bar */}
<TokenUsageBar metrics={trajectory.final_metrics} />

{/* Duration Bar */}
<StepDurationBar
  steps={trajectory.steps}
  ...
/>
```

Right after the `<StepDurationBar>` block (before the accordion that follows), insert:

```tsx
<TrajectorySummary
  trialId={trialId}
  stepIdToIndex={(stepId) =>
    trajectory.steps.findIndex((s) => s.step_id === stepId)
  }
  onStepSelect={handleStepClick}
  apiBaseUrl={apiBaseUrl}
/>
```

- [ ] **Step 7.3: Verify it builds and the dev server renders**

```bash
cd frontend && pnpm tsc --noEmit
```
Expected: no errors.

Then start the dev server and confirm the Summary card renders in the Trajectory tab:

```bash
cd frontend && pnpm dev
```

Open `http://localhost:3000/experiments/<some-id>?tab=trajectory` for a completed trial. Verify:
- "Generating summary…" appears briefly on first load.
- Once generated, the prose paragraph and the bulleted highlights render between the token/duration bars and the steps accordion.
- Clicking a highlight expands the corresponding accordion item and scrolls to it.
- Refreshing the page returns the cached summary instantly (no re-spinner).
- Trials with no trajectory show no summary card (falls through to the existing "No trajectory available" empty state).
- A trial whose summary fails to generate shows the error card with a working Retry button.

If any of those don't behave as expected, fix before committing.

- [ ] **Step 7.4: Commit**

```bash
git add frontend/src/components/trajectory-viewer.tsx
git commit -m "Render TrajectorySummary between token bars and steps accordion"
```

---

## Self-review checklist (run before final commit)

- [ ] All test files run green: `cd backend && pytest -x`.
- [ ] No frontend type errors: `cd frontend && pnpm tsc --noEmit`.
- [ ] Spec coverage:
  - Storage at S3 sibling — Task 3.
  - Lazy-generate on first view with per-key lock — Task 3 (`read_trial_trajectory_summary`).
  - Preprocessing (image strip + truncation) — Task 1.
  - Claude call + JSON parse + step-id validation — Task 2.
  - Endpoint with 404 / 502 — Task 4.
  - Next.js proxy + types — Task 5.
  - Loading / error / loaded UI — Task 6.
  - Placement between bars and accordion — Task 7.
- [ ] No placeholders or TODOs in committed code.
