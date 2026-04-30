# Probe Agent — Prior-Attempts Context Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a probe preset opt in to having past failed cheat attempts (titles + outcomes) prepended to the operator instructions on subsequent runs of the same task + preset, so probe agents stop re-discovering known-dead approaches.

**Architecture:** The selected preset's `include_prior_attempts` config and the preset's `name` ride the existing probe submit payload (`POST /tasks/sweep`) into `harbor_config`. When the worker's `_run_harbor_trial` spots the config, it queries `trials` for prior `(task_id, preset_name)` runs whose analyzer succeeded, flattens their `analysis.attempts` to the failed ones, and prepends a "approaches already tried and failed" block to `instruction.md` alongside the existing operator-directive overlay. The analyzer is unchanged.

**Tech Stack:** Python 3.11 + SQLAlchemy async + Pydantic on the backend; Next.js 15 / React + Tailwind on the frontend; PostgreSQL JSONB column for `harbor_config` (no migration needed).

**Spec:** [`docs/superpowers/specs/2026-04-30-probe-prior-attempts-context-design.md`](../specs/2026-04-30-probe-prior-attempts-context-design.md)

---

## File Structure

| File | Change | Responsibility |
| --- | --- | --- |
| `oddish/src/oddish/schemas.py` | Modify | Add `preset_name` + `prior_attempts_config` fields to `TaskSubmission` and `TaskSweepSubmission`. |
| `oddish/src/oddish/core/sweeps.py` | Modify | Pass the two new fields through `build_task_submission_from_sweep`. |
| `oddish/src/oddish/queue.py` | Modify | Persist the two new fields into `harbor_config` JSONB inside `_build_harbor_config_for_trial`. |
| `oddish/src/oddish/worker/prior_attempts.py` | Create | New module: `fetch_prior_attempts()` (SQL query) and `format_prior_attempts_block()` (string formatter). Kept separate from `local_runner.py` so each helper is unit-testable in isolation and `local_runner.py` doesn't keep growing. |
| `oddish/src/oddish/worker/local_runner.py` | Modify | Call the two helpers when `prior_attempts_config.enabled`; splice the formatted block into the existing `instruction.md` prepend. |
| `backend/tests/test_prior_attempts.py` | Create | Unit tests for `fetch_prior_attempts` (modes, caps, status filters) and `format_prior_attempts_block` (empty, normal, missing outcome). |
| `backend/tests/test_local_runner.py` | Modify | Add an integration test asserting the prior-attempts block lands in the temp-copy `instruction.md` when the config is on. |
| `frontend/src/components/probe-submit-form.tsx` | Modify | Extend `Preset` type, modal UI section, and submit payload. |

---

## Task 1: Pydantic schema fields

**Files:**
- Modify: `oddish/src/oddish/schemas.py:184-212` and `oddish/src/oddish/schemas.py:263-291`

- [ ] **Step 1: Read the current schema fields**

Run: `grep -n "extra_instructions\|evaluation_metric\|ratio_unit\|ratio_verb" oddish/src/oddish/schemas.py`
Confirm both `TaskSubmission` and `TaskSweepSubmission` carry the four probe fields. We're adding two more in the same shape.

- [ ] **Step 2: Add the new fields to both classes**

Add after `ratio_verb` in **both** `TaskSubmission` (around line 212) and `TaskSweepSubmission` (around line 291):

```python
    preset_name: str | None = Field(
        default=None,
        description=(
            "Stable matching key for the prior-attempts query. Set when "
            "the submitter ran with a probe preset selected. Persisted in "
            "harbor_config so future trials can find prior runs of the "
            "same (task_id, preset_name)."
        ),
    )
    prior_attempts_config: dict | None = Field(
        default=None,
        description=(
            "Optional config controlling whether prior failed attempts "
            "from the same (task_id, preset_name) get prepended to "
            "instruction.md. Shape: "
            "{enabled: bool, mode: 'last_n'|'all'|'since_date', "
            "last_n: int, since_date: str (ISO date), max_attempts: int}. "
            "If null or enabled=false, no injection happens."
        ),
    )
```

- [ ] **Step 3: Run schema-touching tests to confirm nothing broke**

Run: `cd backend && pytest tests/test_probe_analyzer.py -v`
Expected: PASS (existing tests). The new fields are optional and default to `None`, so existing call sites keep working.

- [ ] **Step 4: Commit**

```bash
git add oddish/src/oddish/schemas.py
git commit -m "Add preset_name + prior_attempts_config to probe submission schemas"
```

---

## Task 2: Pass fields through the sweep builder

**Files:**
- Modify: `oddish/src/oddish/core/sweeps.py:90-94`

- [ ] **Step 1: Read the current builder**

Run: `sed -n '70,95p' oddish/src/oddish/core/sweeps.py`
Confirm the function copies probe fields one-by-one from `TaskSweepSubmission` → `TaskSubmission`.

- [ ] **Step 2: Add the two new fields**

In `build_task_submission_from_sweep`, append below `ratio_verb=submission.ratio_verb,`:

```python
        preset_name=submission.preset_name,
        prior_attempts_config=submission.prior_attempts_config,
```

- [ ] **Step 3: Sanity-check imports / types**

Run: `cd oddish && python -c "from oddish.core.sweeps import build_task_submission_from_sweep; print('ok')"`
Expected: `ok`.

- [ ] **Step 4: Commit**

```bash
git add oddish/src/oddish/core/sweeps.py
git commit -m "Forward preset_name + prior_attempts_config in sweep builder"
```

---

## Task 3: Persist new fields into harbor_config

**Files:**
- Modify: `oddish/src/oddish/queue.py:412-445`

- [ ] **Step 1: Write the failing test**

Add to `backend/tests/test_prior_attempts.py` (create the file with this initial test — the rest of the test file is built up in Task 4 and Task 5):

```python
"""Tests for the prior-attempts probe feature."""

from __future__ import annotations

import pytest

from oddish.queue import _build_harbor_config_for_trial
from oddish.schemas import TaskSubmission, TrialSpec


def _minimal_submission(**overrides) -> TaskSubmission:
    """Helper: build a TaskSubmission with the minimum required fields."""
    base = dict(
        task_path="/tmp/fake-task",
        name="t",
        trials=[TrialSpec(agent="claude-code", model="anthropic/claude-sonnet-4-6")],
        user="alice",
    )
    base.update(overrides)
    return TaskSubmission(**base)


def test_build_harbor_config_persists_preset_name_and_prior_attempts_config():
    submission = _minimal_submission(
        extra_instructions="probe me",
        preset_name="cheat-detector",
        prior_attempts_config={
            "enabled": True,
            "mode": "last_n",
            "last_n": 5,
            "max_attempts": 50,
        },
    )
    result = _build_harbor_config_for_trial(submission, submission.trials[0])
    assert result is not None
    assert result["preset_name"] == "cheat-detector"
    assert result["prior_attempts_config"]["enabled"] is True
    assert result["prior_attempts_config"]["mode"] == "last_n"


def test_build_harbor_config_omits_fields_when_unset():
    submission = _minimal_submission(extra_instructions="probe me")
    result = _build_harbor_config_for_trial(submission, submission.trials[0])
    assert result is not None
    assert "preset_name" not in result
    assert "prior_attempts_config" not in result
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && pytest tests/test_prior_attempts.py -v`
Expected: FAIL (`KeyError: 'preset_name'` or assertion error — the keys aren't being written yet).

- [ ] **Step 3: Implement the persistence**

In `oddish/src/oddish/queue.py:_build_harbor_config_for_trial`, after the `ratio_verb` block (around line 443), add:

```python
    if submission.preset_name:
        base["preset_name"] = submission.preset_name
    if submission.prior_attempts_config:
        base["prior_attempts_config"] = submission.prior_attempts_config
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && pytest tests/test_prior_attempts.py -v`
Expected: PASS, both tests.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/queue.py backend/tests/test_prior_attempts.py
git commit -m "Persist preset_name + prior_attempts_config into harbor_config"
```

---

## Task 4: `format_prior_attempts_block` helper

**Files:**
- Create: `oddish/src/oddish/worker/prior_attempts.py`
- Modify: `backend/tests/test_prior_attempts.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_prior_attempts.py`:

```python
from oddish.worker.prior_attempts import format_prior_attempts_block


def test_format_prior_attempts_block_empty_returns_empty_string():
    assert format_prior_attempts_block([]) == ""


def test_format_prior_attempts_block_renders_titles_and_outcomes():
    attempts = [
        {
            "title": "Modify main.rs to fake PASS output",
            "outcome": "Verifier rebuilt with pristine main.rs; reward 0.0.",
        },
        {
            "title": "Pre-write /tmp/score.txt as read-only",
            "outcome": "Verifier didn't depend on that path.",
        },
    ]
    block = format_prior_attempts_block(attempts)
    # Header signals the agent these are dead ends.
    assert "ALREADY been tried" in block
    assert "FAILED" in block
    # Both attempts present, numbered, in order.
    assert "1." in block and "2." in block
    assert "Modify main.rs to fake PASS output" in block
    assert "Verifier rebuilt with pristine main.rs" in block
    assert "Pre-write /tmp/score.txt as read-only" in block
    # Trailing separator so the next section is clearly delimited.
    assert block.rstrip().endswith("---")


def test_format_prior_attempts_block_handles_missing_outcome():
    attempts = [{"title": "A bare attempt with no outcome field"}]
    block = format_prior_attempts_block(attempts)
    assert "A bare attempt with no outcome field" in block
    # Title-only line: should not contain the dash-separator that joins
    # title and outcome on a normal entry.
    assert "A bare attempt with no outcome field —" not in block


def test_format_prior_attempts_block_truncates_to_char_budget():
    long_outcome = "x" * 500
    attempts = [
        {"title": f"attempt {i}", "outcome": long_outcome} for i in range(50)
    ]
    block = format_prior_attempts_block(attempts, char_budget=2000)
    # We only kept what fits — far fewer than 50 numbered lines.
    assert block.count("\n") < 30
    assert len(block) <= 2200  # budget + header/footer slack
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_prior_attempts.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'oddish.worker.prior_attempts'`.

- [ ] **Step 3: Implement the helper**

Create `oddish/src/oddish/worker/prior_attempts.py`:

```python
"""Prior-attempts probe feature.

Two helpers used by the local trial runner when a probe preset has the
``include_prior_attempts`` config enabled:

* ``fetch_prior_attempts`` queries the database for failed cheat attempts
  recorded on past completed trials of the same ``(task_id, preset_name)``,
  filtered per the preset's mode + cap config.
* ``format_prior_attempts_block`` renders the result as a numbered
  bulleted block ready to splice into ``instruction.md``.

Both functions degrade gracefully on missing/malformed input (empty list →
empty string output) so the runner can keep going if anything is off.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import AnalysisStatus, TrialModel, TrialStatus

_BLOCK_HEADER = (
    "The following approaches have ALREADY been tried on this task and "
    "FAILED. Pick something genuinely different:\n\n"
)
_BLOCK_FOOTER = "\n---\n"
_DEFAULT_CHAR_BUDGET = 8000


def format_prior_attempts_block(
    attempts: Iterable[dict[str, Any]],
    *,
    char_budget: int = _DEFAULT_CHAR_BUDGET,
) -> str:
    """Render prior-attempts as a numbered bulleted block.

    Returns an empty string when ``attempts`` is empty so callers can
    unconditionally concatenate the result without producing a stray
    "no prior attempts" header.
    """
    items = list(attempts)
    if not items:
        return ""

    lines: list[str] = []
    used_chars = len(_BLOCK_HEADER) + len(_BLOCK_FOOTER)
    for i, attempt in enumerate(items, start=1):
        title = str(attempt.get("title", "")).strip()
        if not title:
            continue
        outcome = str(attempt.get("outcome", "")).strip()
        if outcome:
            line = f"  {i}. {title} — {outcome}"
        else:
            line = f"  {i}. {title}"
        if used_chars + len(line) + 1 > char_budget:
            break
        lines.append(line)
        used_chars += len(line) + 1

    if not lines:
        return ""
    return _BLOCK_HEADER + "\n".join(lines) + _BLOCK_FOOTER
```

(Note: `fetch_prior_attempts` is added in Task 5 in the same file.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_prior_attempts.py -v -k "format_prior_attempts_block"`
Expected: PASS for all four `format_prior_attempts_block` tests.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/worker/prior_attempts.py backend/tests/test_prior_attempts.py
git commit -m "Add format_prior_attempts_block helper for probe instruction overlay"
```

---

## Task 5: `fetch_prior_attempts` helper

**Files:**
- Modify: `oddish/src/oddish/worker/prior_attempts.py`
- Modify: `backend/tests/test_prior_attempts.py`

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_prior_attempts.py`:

```python
import uuid
from datetime import datetime, timedelta, timezone

import pytest_asyncio

from oddish.db import (
    AnalysisStatus,
    ExperimentModel,
    TaskModel,
    TrialModel,
    TrialOrigin,
    TrialStatus,
    get_session,
)
from oddish.worker.prior_attempts import fetch_prior_attempts


@pytest_asyncio.fixture
async def seeded_prior_trials():
    """Seed three completed trials for the same (task_id, preset_name).

    Each trial's ``analysis.attempts`` carries a mix of failed/null/successful
    entries so we can verify the success-only filter and time ordering.
    """
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp_pa_{suffix}"
    task_id = f"task_pa_{suffix}"
    preset_name = "cheat-detector"
    now = datetime.now(timezone.utc)

    rows = [
        # Newest: 2 failed cheats + 1 investigation.
        (
            f"trial_pa_{suffix}_0",
            now - timedelta(hours=1),
            [
                {"title": "Investigate verifier", "outcome": "...", "success": None},
                {"title": "Fake binary output", "outcome": "blocked by rebuild", "success": False},
                {"title": "Pre-write score.txt", "outcome": "no effect", "success": False},
            ],
        ),
        # Middle: 1 failed cheat + 1 successful.
        (
            f"trial_pa_{suffix}_1",
            now - timedelta(hours=2),
            [
                {"title": "Strace bypass", "outcome": "blocked by canary", "success": False},
                {"title": "Patch run_tests.sh", "outcome": "actually worked", "success": True},
            ],
        ),
        # Oldest: 1 failed cheat.
        (
            f"trial_pa_{suffix}_2",
            now - timedelta(days=2),
            [
                {"title": "Hardcode results.json", "outcome": "rejected", "success": False},
            ],
        ),
    ]

    async with get_session() as session:
        session.add(ExperimentModel(id=experiment_id, name=f"e-{suffix}"))
        session.add(
            TaskModel(
                id=task_id,
                name=f"t-{suffix}",
                user="test",
                task_path="/tmp/fake-task",
            )
        )
        for trial_id, finished_at, attempts in rows:
            session.add(
                TrialModel(
                    id=trial_id,
                    name=trial_id,
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key="test-pa",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    finished_at=finished_at,
                    harbor_config={"preset_name": preset_name},
                    analysis={"kind": "probe_summary", "attempts": attempts},
                    analysis_status=AnalysisStatus.SUCCESS,
                )
            )

    yield task_id, preset_name, now

    async with get_session() as session:
        for trial_id, _, _ in rows:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id == trial_id)
            )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == experiment_id)
        )


@pytest.mark.asyncio
async def test_fetch_prior_attempts_last_n_returns_failed_only_newest_first(
    seeded_prior_trials,
):
    task_id, preset_name, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={"mode": "last_n", "last_n": 2, "max_attempts": 50},
        )
    # Last 2 trials only → 3 failed (2 from newest + 1 from middle).
    assert len(out) == 3
    titles = [a["title"] for a in out]
    assert "Fake binary output" in titles
    assert "Pre-write score.txt" in titles
    assert "Strace bypass" in titles
    # Successful attempt excluded.
    assert "Patch run_tests.sh" not in titles
    # Investigation excluded.
    assert "Investigate verifier" not in titles
    # Oldest trial excluded by last_n=2.
    assert "Hardcode results.json" not in titles


@pytest.mark.asyncio
async def test_fetch_prior_attempts_all_mode_includes_every_trial(
    seeded_prior_trials,
):
    task_id, preset_name, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={"mode": "all", "max_attempts": 50},
        )
    # All 3 trials → 4 failed cheats total.
    assert len(out) == 4
    assert {a["title"] for a in out} == {
        "Fake binary output",
        "Pre-write score.txt",
        "Strace bypass",
        "Hardcode results.json",
    }


@pytest.mark.asyncio
async def test_fetch_prior_attempts_since_date_filters_by_finished_at(
    seeded_prior_trials,
):
    task_id, preset_name, now = seeded_prior_trials
    cutoff = (now - timedelta(hours=12)).date().isoformat()  # excludes the 2-day-old trial
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={
                "mode": "since_date",
                "since_date": cutoff,
                "max_attempts": 50,
            },
        )
    titles = {a["title"] for a in out}
    assert "Hardcode results.json" not in titles
    assert "Fake binary output" in titles
    assert "Strace bypass" in titles


@pytest.mark.asyncio
async def test_fetch_prior_attempts_max_attempts_truncates(seeded_prior_trials):
    task_id, preset_name, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name=preset_name,
            filter_config={"mode": "all", "max_attempts": 2},
        )
    assert len(out) == 2


@pytest.mark.asyncio
async def test_fetch_prior_attempts_skips_other_preset(seeded_prior_trials):
    task_id, _, _ = seeded_prior_trials
    async with get_session() as session:
        out = await fetch_prior_attempts(
            session=session,
            task_id=task_id,
            preset_name="some-other-preset",
            filter_config={"mode": "all", "max_attempts": 50},
        )
    assert out == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && pytest tests/test_prior_attempts.py -v -k "fetch_prior_attempts"`
Expected: FAIL with `ImportError: cannot import name 'fetch_prior_attempts'`.

- [ ] **Step 3: Implement the helper**

Append to `oddish/src/oddish/worker/prior_attempts.py`:

```python
async def fetch_prior_attempts(
    *,
    session: AsyncSession,
    task_id: str,
    preset_name: str,
    filter_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Return failed cheat attempts from prior trials of (task_id, preset_name).

    Filters per ``filter_config['mode']``:
      * ``last_n``     — newest N trials (``filter_config['last_n']``).
      * ``all``        — all matching trials, capped by an internal sanity
                         limit (200) so an unbounded preset can't blow up
                         the prompt or query.
      * ``since_date`` — only trials with ``finished_at >= since_date``
                         (ISO date), capped at the same sanity limit.

    Then flattens each trial's ``analysis.attempts``, keeps entries where
    ``success is False`` (so investigations and successful cheats are
    excluded), and truncates the result list to ``filter_config['max_attempts']``,
    newest-first.

    Each returned dict carries ``title``, ``outcome``, ``source_trial_id``,
    and ``finished_at`` (ISO string).

    Returns ``[]`` when no matches exist or the filter_config is malformed.
    """
    mode = filter_config.get("mode", "last_n")
    max_attempts = int(filter_config.get("max_attempts") or 50)
    sanity_run_cap = 200

    stmt = (
        select(TrialModel.id, TrialModel.finished_at, TrialModel.analysis)
        .where(TrialModel.task_id == task_id)
        .where(TrialModel.harbor_config["preset_name"].astext == preset_name)
        .where(TrialModel.analysis_status == AnalysisStatus.SUCCESS)
        .where(TrialModel.status == TrialStatus.SUCCESS)
        .order_by(TrialModel.finished_at.desc())
    )

    if mode == "last_n":
        run_cap = int(filter_config.get("last_n") or 5)
        stmt = stmt.limit(run_cap)
    elif mode == "since_date":
        since_raw = filter_config.get("since_date")
        if since_raw:
            try:
                since_dt = datetime.fromisoformat(str(since_raw)).replace(
                    tzinfo=timezone.utc
                ) if "T" not in str(since_raw) else datetime.fromisoformat(str(since_raw))
            except ValueError:
                return []
            stmt = stmt.where(TrialModel.finished_at >= since_dt)
        stmt = stmt.limit(sanity_run_cap)
    else:  # "all" or unknown → fall back to all w/ sanity cap
        stmt = stmt.limit(sanity_run_cap)

    rows = (await session.execute(stmt)).all()

    flattened: list[dict[str, Any]] = []
    for trial_id, finished_at, analysis in rows:
        if not isinstance(analysis, dict):
            continue
        for attempt in analysis.get("attempts") or []:
            if not isinstance(attempt, dict):
                continue
            if attempt.get("success") is not False:
                continue
            flattened.append(
                {
                    "title": str(attempt.get("title", "")),
                    "outcome": str(attempt.get("outcome", "")),
                    "source_trial_id": trial_id,
                    "finished_at": finished_at.isoformat() if finished_at else None,
                }
            )
            if len(flattened) >= max_attempts:
                return flattened
    return flattened
```

Add `from datetime import timezone` at the top of the file alongside the existing `from datetime import datetime` import.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && pytest tests/test_prior_attempts.py -v`
Expected: PASS for all `format_*` and `fetch_*` tests.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/worker/prior_attempts.py backend/tests/test_prior_attempts.py
git commit -m "Add fetch_prior_attempts helper backing the probe overlay"
```

---

## Task 6: Wire into `_run_harbor_trial`

**Files:**
- Modify: `oddish/src/oddish/worker/local_runner.py:285-318`
- Modify: `backend/tests/test_local_runner.py`

- [ ] **Step 1: Write the failing integration test**

In `backend/tests/test_local_runner.py`, add a fixture variant of `seeded_probe_trial_with_task_dir` that also seeds two prior trials with failed `analysis.attempts` and turns the new config on. Then add the test:

```python
@pytest_asyncio.fixture
async def seeded_probe_trial_with_prior_attempts(tmp_path):
    """Like ``seeded_probe_trial_with_task_dir`` but also seeds two prior
    completed trials with failed analyzer attempts under the same preset."""
    suffix = uuid.uuid4().hex[:8]
    experiment_id = f"exp_lr_pa_{suffix}"
    task_id = f"task_lr_pa_{suffix}"
    preset_name = "cheat-detector"

    task_dir = tmp_path / "fake-task"
    task_dir.mkdir()
    (task_dir / "instruction.md").write_text("solve the task")
    (task_dir / "task.toml").write_text('version = "1.0"\n')

    prior_trial_ids = [f"trial_lr_pa_{suffix}_prior_{i}" for i in range(2)]
    main_trial_id = f"trial_lr_pa_{suffix}_main"

    async with get_session() as session:
        session.add(ExperimentModel(id=experiment_id, name=f"e-{suffix}"))
        session.add(
            TaskModel(
                id=task_id,
                name=f"t-{suffix}",
                user="test",
                task_path=str(task_dir),
            )
        )
        for i, tid in enumerate(prior_trial_ids):
            session.add(
                TrialModel(
                    id=tid,
                    name=tid,
                    task_id=task_id,
                    experiment_id=experiment_id,
                    agent="claude-code",
                    provider="anthropic",
                    model="anthropic/claude-sonnet-4-6",
                    queue_key="test-lr-pa",
                    status=TrialStatus.SUCCESS,
                    origin=TrialOrigin.ODDISH,
                    finished_at=datetime.now(timezone.utc) - timedelta(hours=i + 1),
                    harbor_config={"preset_name": preset_name},
                    analysis={
                        "kind": "probe_summary",
                        "attempts": [
                            {
                                "title": f"Fake binary output ({tid})",
                                "outcome": "Verifier rebuilt; reward 0.0",
                                "success": False,
                            }
                        ],
                    },
                    analysis_status=AnalysisStatus.SUCCESS,
                )
            )
        session.add(
            TrialModel(
                id=main_trial_id,
                name=main_trial_id,
                task_id=task_id,
                experiment_id=experiment_id,
                agent="claude-code",
                provider="anthropic",
                model="anthropic/claude-sonnet-4-6",
                queue_key="test-lr-pa",
                status=TrialStatus.RUNNING,
                origin=TrialOrigin.ODDISH,
                harbor_config={
                    "mode": "probe",
                    "extra_instructions": "be adversarial",
                    "preset_name": preset_name,
                    "prior_attempts_config": {
                        "enabled": True,
                        "mode": "all",
                        "max_attempts": 50,
                    },
                },
            )
        )

    yield main_trial_id, prior_trial_ids, task_dir

    async with get_session() as session:
        for tid in [main_trial_id, *prior_trial_ids]:
            await session.execute(
                TrialModel.__table__.delete().where(TrialModel.id == tid)
            )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id == task_id)
        )
        await session.execute(
            ExperimentModel.__table__.delete().where(ExperimentModel.id == experiment_id)
        )


@pytest.mark.asyncio
async def test_probe_overlay_prepends_prior_attempts_block_when_enabled(
    monkeypatch, seeded_probe_trial_with_prior_attempts
):
    """When prior_attempts_config.enabled is on, the temp-copy instruction.md
    should contain the prior-attempts header AND the seeded prior titles."""
    main_trial_id, _, _ = seeded_probe_trial_with_prior_attempts
    captured: dict[str, object] = {}

    class FakeTrial:
        def __init__(self, cfg, **_kwargs):
            captured["instruction"] = (
                Path(cfg.task.path) / "instruction.md"
            ).read_text()
            self.result = MagicMock()
            self.result.verifier_result = MagicMock(rewards={"reward": 0.0})
            self.result.model_dump = lambda mode=None: {}

        @classmethod
        async def create(cls, cfg):
            return cls(cfg)

        async def run(self):
            return self.result

    monkeypatch.setattr("oddish.worker.local_runner.Trial", FakeTrial)
    # Stub the analyzer + watchdog so the test doesn't hit the network or docker.
    monkeypatch.setattr(
        "oddish.worker.local_runner._run_probe_analyzer",
        AsyncMock(return_value={"kind": "probe_summary", "attempts": []}),
    )
    monkeypatch.setattr(
        "oddish.worker.local_runner._watchdog_task",
        AsyncMock(return_value=None),
    )

    from oddish.worker.local_runner import _run_harbor_trial
    await _run_harbor_trial(main_trial_id)

    instr = captured["instruction"]
    assert "ALREADY been tried" in instr
    assert "Fake binary output" in instr
    # Operator directive still present and still in front of the original task.
    assert "## OPERATOR DIRECTIVE" in instr
    assert "be adversarial" in instr
    assert "## ORIGINAL TASK INSTRUCTION (context only)" in instr
```

(Add the missing imports at the top of `test_local_runner.py`: `from datetime import timedelta, timezone`, `from oddish.db import AnalysisStatus`.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd backend && pytest tests/test_local_runner.py::test_probe_overlay_prepends_prior_attempts_block_when_enabled -v`
Expected: FAIL — the runner doesn't yet read `prior_attempts_config` or call the helpers.

- [ ] **Step 3: Wire the helpers into `_run_harbor_trial`**

In `oddish/src/oddish/worker/local_runner.py`:

Add to the imports near the top:

```python
from oddish.worker.prior_attempts import (
    fetch_prior_attempts,
    format_prior_attempts_block,
)
```

Inside the `async with get_session() as session:` block (around line 285), after `extra_instructions = harbor_config.get("extra_instructions")`, also pull:

```python
        preset_name = harbor_config.get("preset_name")
        prior_attempts_config = harbor_config.get("prior_attempts_config") or {}
        prior_attempts_block = ""
        if (
            extra_instructions
            and preset_name
            and prior_attempts_config.get("enabled")
        ):
            try:
                prior = await fetch_prior_attempts(
                    session=session,
                    task_id=trial.task_id,
                    preset_name=preset_name,
                    filter_config=prior_attempts_config,
                )
                prior_attempts_block = format_prior_attempts_block(prior)
            except Exception:
                logger.warning(
                    "fetch_prior_attempts failed for trial %s; continuing without injection",
                    trial_id,
                    exc_info=True,
                )
                prior_attempts_block = ""
```

Then in the overlay write site (around line 309-315), splice the block in between the system framing and the operator directive — the operator directive must remain the most prominent section:

```python
        instr_path.write_text(
            f"{_PROBE_SYSTEM_FRAMING}\n\n"
            f"---\n\n"
            f"{prior_attempts_block}"
            f"## OPERATOR DIRECTIVE\n\n{extra_instructions}\n\n"
            f"---\n\n"
            f"## ORIGINAL TASK INSTRUCTION (context only)\n\n{original}"
        )
```

(`prior_attempts_block` is `""` when the feature is off or the query returned nothing, so f-string concat is a no-op then.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && pytest tests/test_local_runner.py::test_probe_overlay_prepends_prior_attempts_block_when_enabled -v`
Expected: PASS.

- [ ] **Step 5: Confirm the existing overlay test still passes (no prior_attempts_config → block is empty)**

Run: `cd backend && pytest tests/test_local_runner.py::test_probe_overlay_prepends_extra_instructions_to_instruction_md -v`
Expected: PASS — the existing seed has no `prior_attempts_config`, so the block is `""` and the overlay output is byte-identical to before.

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/worker/local_runner.py backend/tests/test_local_runner.py
git commit -m "Splice prior-attempts block into probe overlay when preset opts in"
```

---

## Task 7: Frontend `Preset` type + persistence

**Files:**
- Modify: `frontend/src/components/probe-submit-form.tsx:35-71`

- [ ] **Step 1: Extend the Preset type**

After the existing `EvaluationMetric` type (around line 35), add:

```typescript
type PriorAttemptsConfig = {
  enabled: boolean;
  mode: "last_n" | "all" | "since_date";
  last_n?: number;        // used when mode === "last_n"
  since_date?: string;    // ISO date (YYYY-MM-DD), used when mode === "since_date"
  max_attempts: number;
};
```

Then add an optional field to the `Preset` type (around line 47, before `is_seed`):

```typescript
  include_prior_attempts?: PriorAttemptsConfig | null;
```

- [ ] **Step 2: Default the field in `normalizePreset`**

In `normalizePreset` (around line 60), preserve the field as-is:

```typescript
function normalizePreset(p: Preset): Preset {
  // Accept legacy "cheat_ratio" from older stored presets / seeds and
  // promote it to "ratio" with the cheat-flavored defaults.
  const base =
    p.evaluation_metric === "cheat_ratio"
      ? {
          ...p,
          evaluation_metric: "ratio" as EvaluationMetric,
          ratio_unit: p.ratio_unit ?? "cheat",
          ratio_verb: p.ratio_verb ?? "succeeded",
        }
      : p;
  // include_prior_attempts is optional. Stored presets from before this
  // feature won't have it; leave undefined so the UI shows the toggle off.
  return base;
}
```

- [ ] **Step 3: Smoke-test the dev server still loads**

Run: `cd frontend && pnpm dev`
Open `/tasks/<some-task>` and visit the probe drawer. Confirm the page renders with no console errors and the existing presets are still listed. (The new field is optional so legacy presets in `probe-presets.json` are unaffected.)

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/probe-submit-form.tsx
git commit -m "Add include_prior_attempts field to frontend Preset type"
```

---

## Task 8: Frontend modal UI

**Files:**
- Modify: `frontend/src/components/probe-submit-form.tsx` (modal state around 241-254, modal content around 487-650, save logic around 317-373)

- [ ] **Step 1: Add modal state for the four new inputs**

After `const [modalRatioVerb, setModalRatioVerb] = useState("");` (around line 254), add:

```typescript
  const [modalPriorEnabled, setModalPriorEnabled] = useState(false);
  const [modalPriorMode, setModalPriorMode] =
    useState<"last_n" | "all" | "since_date">("last_n");
  const [modalPriorLastN, setModalPriorLastN] = useState(5);
  const [modalPriorSinceDate, setModalPriorSinceDate] = useState("");
  const [modalPriorMaxAttempts, setModalPriorMaxAttempts] = useState(50);
```

- [ ] **Step 2: Hydrate from selectedPreset on open / reset on create**

In `openCreateModal` (around line 290), after the existing setters add:

```typescript
    setModalPriorEnabled(false);
    setModalPriorMode("last_n");
    setModalPriorLastN(5);
    setModalPriorSinceDate("");
    setModalPriorMaxAttempts(50);
```

In `openEditModal` (around line 303), after the existing setters add:

```typescript
    const cfg = selectedPreset.include_prior_attempts ?? null;
    setModalPriorEnabled(Boolean(cfg?.enabled));
    setModalPriorMode(cfg?.mode ?? "last_n");
    setModalPriorLastN(cfg?.last_n ?? 5);
    setModalPriorSinceDate(cfg?.since_date ?? "");
    setModalPriorMaxAttempts(cfg?.max_attempts ?? 50);
```

- [ ] **Step 3: Persist into the new preset object on save**

In `savePresetFromModal` (around line 340 where `newPreset` is constructed), after `ratio_verb`, add:

```typescript
      include_prior_attempts: modalPriorEnabled
        ? {
            enabled: true,
            mode: modalPriorMode,
            last_n: modalPriorMode === "last_n" ? modalPriorLastN : undefined,
            since_date:
              modalPriorMode === "since_date" ? modalPriorSinceDate : undefined,
            max_attempts: modalPriorMaxAttempts,
          }
        : null,
```

- [ ] **Step 4: Add the modal UI block**

Find where the metric fields end inside the modal `<div className="space-y-4">` body (look for the closing of the ratio_verb conditional). After that block add:

```tsx
            <div className="rounded border bg-muted/20 p-3 space-y-3">
              <label className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={modalPriorEnabled}
                  onChange={(e) => setModalPriorEnabled(e.target.checked)}
                />
                <span className="text-sm font-medium">
                  Include prior failed attempts in agent context
                </span>
              </label>
              <p className="text-xs text-muted-foreground">
                Pulls failed attempts from prior trials of THIS task using
                THIS preset.
              </p>
              {modalPriorEnabled ? (
                <div className="space-y-2">
                  <label className="block">
                    <span className="text-xs font-medium">Mode</span>
                    <select
                      value={modalPriorMode}
                      onChange={(e) =>
                        setModalPriorMode(
                          e.target.value as "last_n" | "all" | "since_date",
                        )
                      }
                      className="mt-1 w-full rounded border bg-background px-2 py-1.5 text-sm"
                    >
                      <option value="last_n">Last N runs</option>
                      <option value="all">All runs</option>
                      <option value="since_date">Since date</option>
                    </select>
                  </label>
                  {modalPriorMode === "last_n" ? (
                    <label className="block">
                      <span className="text-xs font-medium">N (most recent runs)</span>
                      <input
                        type="number"
                        min={1}
                        value={modalPriorLastN}
                        onChange={(e) =>
                          setModalPriorLastN(Number(e.target.value) || 1)
                        }
                        className="mt-1 w-full rounded border bg-background px-2 py-1.5 text-sm"
                      />
                    </label>
                  ) : null}
                  {modalPriorMode === "since_date" ? (
                    <label className="block">
                      <span className="text-xs font-medium">Since (YYYY-MM-DD)</span>
                      <input
                        type="date"
                        value={modalPriorSinceDate}
                        onChange={(e) => setModalPriorSinceDate(e.target.value)}
                        className="mt-1 w-full rounded border bg-background px-2 py-1.5 text-sm"
                      />
                    </label>
                  ) : null}
                  <label className="block">
                    <span className="text-xs font-medium">
                      Max attempts (hard cap)
                    </span>
                    <input
                      type="number"
                      min={1}
                      value={modalPriorMaxAttempts}
                      onChange={(e) =>
                        setModalPriorMaxAttempts(Number(e.target.value) || 1)
                      }
                      className="mt-1 w-full rounded border bg-background px-2 py-1.5 text-sm"
                    />
                  </label>
                </div>
              ) : null}
            </div>
```

- [ ] **Step 5: Smoke-test the modal**

Run: `cd frontend && pnpm dev`
Open the probe drawer, click "+ Create your own probe agent", fill in name + operator prompt, toggle "Include prior failed attempts", switch through all three modes, save. Open the saved preset's "Edit" modal and confirm the toggle + mode + values round-trip correctly. Inspect `probe-presets.json` (repo root) to confirm the JSON is shaped per the spec.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/components/probe-submit-form.tsx
git commit -m "Add prior-attempts toggle + mode/last_n/since_date/max_attempts to preset modal"
```

---

## Task 9: Send `preset_name` + `prior_attempts_config` in submit

**Files:**
- Modify: `frontend/src/components/probe-submit-form.tsx:411-422`

- [ ] **Step 1: Extend the submit payload**

In `onSubmit` (around line 411), inside the `body: JSON.stringify({...})` block, after `ratio_verb`, add:

```typescript
          preset_name: selectedPreset?.name ?? null,
          prior_attempts_config: selectedPreset?.include_prior_attempts ?? null,
```

- [ ] **Step 2: End-to-end smoke test**

Manual flow on local dev (backend + frontend running):

1. From the dashboard, run a probe on a task using a preset where the new toggle is OFF. Wait for it to complete with a non-empty analyzer output.
2. Open the same preset, turn ON the prior-attempts toggle, mode = "Last N", N = 5. Save.
3. Run a second probe on the same task with the same preset.
4. Open the second trial's debug-files page (`/trials/{id}/debug-files`) and look for the temp-copy `instruction.md`. Confirm it begins with the system framing, then a "ALREADY been tried" block listing the first run's failed attempts, then the operator directive, then the original instruction.

Expected outcome: the second agent's transcript references prior-attempts content (or simply attempts a different cheat than the first agent did).

- [ ] **Step 3: Run the full backend test suite**

Run: `cd backend && pytest -v`
Expected: PASS — including the new `test_prior_attempts.py` and the extended `test_local_runner.py`, and no regressions elsewhere.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/components/probe-submit-form.tsx
git commit -m "Send preset_name + prior_attempts_config in probe submit payload"
```

---

## Self-Review Checklist (run after Task 9)

- **Spec coverage:** All eight spec sections (`Architecture`, `Data model`, `Submit endpoint changes`, `Query layer`, `Injection layer`, `UI changes`, `Error handling`, `Testing`) have a corresponding task. ✓
- **No placeholders:** Every code step contains the actual code. ✓
- **Type consistency:** `prior_attempts_config` shape (`enabled` / `mode` / `last_n` / `since_date` / `max_attempts`) used identically across schemas, harbor_config persistence, the helper, the runner wiring, the modal state, and the submit payload. ✓
- **Error handling per spec table:** `fetch_prior_attempts` raises → caught at the runner with a warning log (Task 6 step 3). Prior trial without `analysis_status=SUCCESS` → filtered in SQL (Task 5). Missing `outcome` → title-only line in the formatter (Task 4). Block exceeds char budget → formatter truncates (Task 4). Missing `preset_name` → wiring skips silently (Task 6 conditional). ✓

---

## Out of Scope (per spec)

* Per-run override at submit time (toggle the preset instead).
* Verifier_stdout excerpts in the prior-attempts block.
* Showing successful-cheat attempts.
* Cross-preset or cross-task memory.
