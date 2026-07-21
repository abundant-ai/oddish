# QA Analyzer — Plan C: Trajectory file-access parser + post-trial linkage

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Parse each trial trajectory for per-step file access, feed that plus the task's pre-trial action items into the per-trial classifier, and have it emit (a) an exploited/causal assessment for each pre-trial item and (b) new trajectory-derived action items — then elevate ("doubly note") any pre-trial item that was exploited.

**Architecture:** A pure `parse_trajectory_file_access(trial_dir)` re-walks the agent JSONL (`claude-code.txt`) using the same walk as the probe analyzer. `TrialClassificationModel`/`TrialClassification` gain `action_items` + `exploitation` fields, so the existing `--json-schema`-enforced classifier returns them in one call. `classify_trial_and_store` loads `task.pre_trial` items + computes file-access, writes both to a JSON file mounted via `--add-dir`, and the prompt instructs the model to assess exploitation. Results land in the `trial.analysis` JSONB blob (no migration). A task-level aggregation stamps `exploited` back onto `task.pre_trial` items.

**Tech Stack:** Python 3.11, Claude Code CLI classifier, pydantic v2, SQLAlchemy JSONB, pytest.

## Global Constraints

- Depends on Plan A (`ActionItem`, `ActionItemSource`, `Dimension`, `ProblemType`, `ActionTier`) and Plan B (`task.pre_trial` column populated). Those must be available.
- Reuse the probe analyzer's JSONL walk verbatim: `_find_first(root, "claude-code.txt")` + the `event["type"]=="assistant"` → `content` → `tool_use` traversal (`oddish/src/oddish/worker/probe_analysis.py:199-212, 241-273`). The parsed `agent_messages` dicts drop the raw `input`, so re-walk the raw JSONL to read `c["input"]` directly.
- Tool → file mapping: `Read`/`Edit`/`Write`/`MultiEdit` use `input["file_path"]`; `Glob` uses `input["pattern"]`; `Bash` uses `input["command"]`. `step_index` = 1-based enumeration over content blocks (matches probe `step_indices`).
- New per-trial output goes into the existing `trial.analysis` JSONB dict (the `classification_result` at `analysis_handler.py:207-215`) — no DB migration. If `_classifications_from_trials` (`qa_handler.py:122-145`) must expose the new keys to the verdict step, update that reader too.
- `--json-schema` enforces `TrialClassificationModel.model_json_schema()` (`classifier.py:311`); adding fields to that model is what makes the CLI emit them. Keep the additions OPTIONAL (defaults) so existing behavior/parity holds when there are no pre-trial items.
- Extra readable roots pass via additional `--add-dir` (multiple allowed); prompt placeholders extend `classify_prompt.txt` + the `.format(...)` at `classifier.py:232-237`.
- DB tests need real Postgres (see Plan A).

## Interfaces produced (cross-task contract)

- `TrajectoryFileAccess` dataclass + `parse_trajectory_file_access(trial_dir: Path) -> list[TrajectoryFileAccess]` (`oddish/src/oddish/analyze/trajectory_files.py`)
- `ExploitationAssessment(BaseModel)` — `{ links_to: str, exploited: bool, exploit_evidence: str | None, causal: bool }`
- `TrialClassificationModel` gains `action_items: list[ActionItem] = []`, `exploitation: list[ExploitationAssessment] = []`
- `TrialClassification` dataclass gains the same two fields; `from_model` carries them
- `classify_trial_and_store(..., pre_trial_items=None)` still callable with the old signature (new arg optional / loaded internally)
- `aggregate_exploited_into_pre_trial(task_id)` → stamps `exploited`/`exploit_evidence` back onto `task.pre_trial` items

---

### Task 1: `parse_trajectory_file_access`

**Files:**
- Create: `oddish/src/oddish/analyze/trajectory_files.py`
- Test: `oddish/tests/analyze/test_trajectory_files.py`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/analyze/test_trajectory_files.py
import json
from pathlib import Path

from oddish.analyze.trajectory_files import parse_trajectory_file_access, TrajectoryFileAccess


def _write_log(dir_: Path, events: list[dict]) -> None:
    (dir_ / "claude-code.txt").write_text("\n".join(json.dumps(e) for e in events))


def _assistant(tool, inp):
    return {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": tool, "input": inp}]}}


def test_extracts_reads_writes_and_commands(tmp_path):
    _write_log(tmp_path, [
        _assistant("Read", {"file_path": "verifier.py"}),
        _assistant("Edit", {"file_path": "solution.py"}),
        _assistant("Glob", {"pattern": "tests/**"}),
        _assistant("Bash", {"command": "grep -n foo verifier.py"}),
    ])
    steps = parse_trajectory_file_access(tmp_path)
    assert isinstance(steps[0], TrajectoryFileAccess)
    assert steps[0].files_read == ["verifier.py"]
    assert steps[1].files_written == ["solution.py"]
    assert steps[2].files_read == ["tests/**"]  # Glob pattern treated as a read target
    assert steps[3].commands == ["grep -n foo verifier.py"]
    assert [s.step_index for s in steps] == [1, 2, 3, 4]


def test_missing_log_returns_empty(tmp_path):
    assert parse_trajectory_file_access(tmp_path) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/analyze/test_trajectory_files.py -v`
Expected: FAIL (`ModuleNotFoundError`).

- [ ] **Step 3: Implement**

```python
# oddish/src/oddish/analyze/trajectory_files.py
"""Per-step file-access metadata parsed from a trial's agent JSONL, so the
post-trial classifier can match pre-trial action-item file refs structurally."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from oddish.worker.probe_analysis import _find_first

_READ_TOOLS = {"Read", "View", "Cat"}
_WRITE_TOOLS = {"Edit", "Write", "MultiEdit"}


@dataclass
class TrajectoryFileAccess:
    step_index: int
    tool: str
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)


def _iter_tool_uses(log_path: Path):
    step = 0
    for line in log_path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if event.get("type") != "assistant":
            continue
        content = (event.get("message") or {}).get("content") or []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            step += 1
            yield step, block.get("name", "?"), block.get("input") or {}


def parse_trajectory_file_access(trial_dir: Path) -> list[TrajectoryFileAccess]:
    log_path = _find_first(Path(trial_dir), "claude-code.txt")
    if log_path is None:
        return []
    out: list[TrajectoryFileAccess] = []
    for step, tool, inp in _iter_tool_uses(log_path):
        access = TrajectoryFileAccess(step_index=step, tool=tool)
        if tool in _READ_TOOLS and isinstance(inp.get("file_path"), str):
            access.files_read.append(inp["file_path"])
        elif tool == "Glob" and isinstance(inp.get("pattern"), str):
            access.files_read.append(inp["pattern"])
        elif tool in _WRITE_TOOLS and isinstance(inp.get("file_path"), str):
            access.files_written.append(inp["file_path"])
        elif tool == "Bash" and isinstance(inp.get("command"), str):
            access.commands.append(inp["command"])
        out.append(access)
    return out
```

Confirm `_find_first` is importable from `oddish.worker.probe_analysis` (it is defined there ~line 199); if it is private-by-convention only, either import it as shown or copy its 12-line body into this module.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/analyze/test_trajectory_files.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/analyze/trajectory_files.py oddish/tests/analyze/test_trajectory_files.py
git commit -m "feat(analyze): trajectory file-access parser"
```

---

### Task 2: Extend classification models with action items + exploitation

**Files:**
- Modify: `oddish/src/oddish/analyze/models.py` (add `ExploitationAssessment`; extend `TrialClassificationModel`, `TrialClassification`, `from_model`)
- Test: `oddish/tests/analyze/test_classification_extension.py`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/analyze/test_classification_extension.py
from oddish.analyze.models import (
    ActionItem, ActionItemSource, Dimension, ProblemType, ActionTier,
    ExploitationAssessment, TrialClassification, TrialClassificationModel,
)


def test_model_defaults_are_empty():
    m = TrialClassificationModel(
        classification="BAD_SUCCESS", subtype="Oracle Copying",
        evidence="e", root_cause="rc", recommendation="rec",
    )
    assert m.action_items == []
    assert m.exploitation == []


def test_from_model_carries_new_fields():
    item = ActionItem(
        source=ActionItemSource.POST_TRIAL, problem_type=ProblemType.MISMATCH,
        dimension=Dimension.VERIFIER, file="verifier.py", line_start=1, line_end=1,
        title="t", detail="d", recommendation="r", tier=ActionTier.MUST_FIX,
    )
    assessment = ExploitationAssessment(links_to="abc123", exploited=True, exploit_evidence="step 4", causal=True)
    m = TrialClassificationModel(
        classification="BAD_SUCCESS", subtype="Oracle Copying",
        evidence="e", root_cause="rc", recommendation="rec",
        action_items=[item], exploitation=[assessment],
    )
    tc = TrialClassification.from_model(trial_name="t1", model=m, reward=1.0)
    assert tc.action_items[0].file == "verifier.py"
    assert tc.exploitation[0].exploited is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/analyze/test_classification_extension.py -v`
Expected: FAIL (`ImportError: ExploitationAssessment`).

- [ ] **Step 3: Implement**

In `oddish/src/oddish/analyze/models.py`:

Add (after `ActionItem`):

```python
class ExploitationAssessment(BaseModel):
    """Whether a pre-trial action item was exploited by this trial."""

    links_to: str = Field(description="Pre-trial ActionItem.id this assesses")
    exploited: bool = Field(description="Did the trajectory exploit this weakness?")
    exploit_evidence: str | None = Field(
        default=None, description="Quote or step index showing exploitation"
    )
    causal: bool = Field(
        default=False, description="Did trajectory behavior result from this weakness?"
    )
```

Extend `TrialClassificationModel` (add two fields):

```python
    action_items: list[ActionItem] = Field(
        default_factory=list,
        description="New trajectory-derived action items (source=post_trial)",
    )
    exploitation: list[ExploitationAssessment] = Field(
        default_factory=list,
        description="Assessment of each provided pre-trial action item",
    )
```

Extend the `TrialClassification` dataclass (add two fields with defaults so existing constructions still work):

```python
    action_items: list[ActionItem] = field(default_factory=list)
    exploitation: list[ExploitationAssessment] = field(default_factory=list)
```

Extend `from_model` to pass them through:

```python
            action_items=list(model.action_items),
            exploitation=list(model.exploitation),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/analyze/test_classification_extension.py -v`
Expected: PASS. Also run the existing suite to confirm no regression: `cd oddish && uv run pytest tests/analyze/ -v`.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/analyze/models.py oddish/tests/analyze/test_classification_extension.py
git commit -m "feat(analyze): classification carries action items + exploitation"
```

---

### Task 3: Thread pre-trial items + file-access into the classifier

**Files:**
- Modify: `oddish/src/oddish/analyze/classifier.py` (accept extra inputs; write them to a mounted JSON; extra `--add-dir`; extend `.format`)
- Modify: `oddish/src/oddish/analyze/classify_prompt.txt` (new section + placeholders)
- Test: `oddish/tests/analyze/test_classifier_inputs.py`

**Design:** `classify_trial()` gains `pre_trial_items: list[dict] | None` and `file_access: list[dict] | None`. When present, it writes them to `<trial_dir>/.qa_context/pre_trial.json` and `.../file_access.json`, adds that dir via `--add-dir`, and fills two new prompt placeholders (`{pre_trial_context}`, `{file_access_context}`) that point the model at those files and instruct populating `action_items` + `exploitation`. When absent, placeholders render as `"(none)"` and behavior is unchanged.

- [ ] **Step 1: Write the failing test** (unit-test the prompt/placeholder wiring, not the subprocess)

```python
# oddish/tests/analyze/test_classifier_inputs.py
from oddish.analyze.classifier import build_classify_prompt


def test_placeholders_render_context_when_present():
    prompt = build_classify_prompt(
        result_str="{}", task_dir="/task", trial_dir="/trial",
        trial_agent_context="", pre_trial_context="PRE", file_access_context="FA",
    )
    assert "PRE" in prompt
    assert "FA" in prompt


def test_placeholders_render_none_when_absent():
    prompt = build_classify_prompt(
        result_str="{}", task_dir="/task", trial_dir="/trial",
        trial_agent_context="", pre_trial_context=None, file_access_context=None,
    )
    assert "(none)" in prompt
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/analyze/test_classifier_inputs.py -v`
Expected: FAIL (`ImportError: build_classify_prompt`).

- [ ] **Step 3a: Extract a testable prompt builder**

In `oddish/src/oddish/analyze/classifier.py`, replace the inline `.format(...)` (lines 232-237) with a module-level function and call it:

```python
def build_classify_prompt(
    *, result_str, task_dir, trial_dir, trial_agent_context,
    pre_trial_context=None, file_access_context=None,
) -> str:
    return _CLASSIFY_PROMPT.format(
        result=result_str,
        task_dir=str(task_dir),
        trial_dir=str(trial_dir),
        trial_agent_context=trial_agent_context,
        pre_trial_context=pre_trial_context or "(none)",
        file_access_context=file_access_context or "(none)",
    )
```

- [ ] **Step 3b: Add the placeholders to the prompt**

In `oddish/src/oddish/analyze/classify_prompt.txt`, add a section (before the JSON-output block) such as:

```
## PRE-TRIAL ACTION ITEMS (known task weaknesses)
{pre_trial_context}

## TRAJECTORY FILE ACCESS (files each step read/wrote)
{file_access_context}

If pre-trial action items are provided, populate `exploitation` with one entry
per item you can assess: set `links_to` to its id, `exploited` true if the agent
took advantage of that weakness, `exploit_evidence` with a step index/quote, and
`causal` true if the agent's behavior resulted from it. Use the file/line refs
and the file-access metadata to check whether the agent touched the implicated
files. Add any NEW weaknesses you find to `action_items` (source="post_trial").
```

Confirm the `TrialClassificationModel` JSON schema (now with `action_items`/`exploitation`) is what `--json-schema` serializes — no code change needed there beyond Task 2.

- [ ] **Step 3c: Wire the inputs through `classify_trial`**

In `classify_trial()` / `_run_claude_cli`, accept `pre_trial_items` + `file_access`, write them to `<trial_dir>/.qa_context/*.json`, add `--add-dir <that dir>` to the command, and build the prompt via `build_classify_prompt(..., pre_trial_context=..., file_access_context=...)` where the context strings tell the model the JSON file paths (e.g. `"See .qa_context/pre_trial.json"`). Keep both optional with `None` defaults.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/analyze/test_classifier_inputs.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/analyze/classifier.py oddish/src/oddish/analyze/classify_prompt.txt \
        oddish/tests/analyze/test_classifier_inputs.py
git commit -m "feat(analyze): thread pre-trial items + file-access into classifier"
```

---

### Task 4: Store post-trial items in `trial.analysis` + expose to verdict

**Files:**
- Modify: `oddish/src/oddish/workers/queue/analysis_handler.py` (compute file-access, load `task.pre_trial`, pass to classifier, add keys to `classification_result`)
- Modify: `oddish/src/oddish/workers/queue/qa_handler.py` (`_classifications_from_trials` reads new keys)
- Test: `backend/tests/test_analysis_handler_action_items.py` (real DB) or a focused unit if the handler can be exercised without full infra

- [ ] **Step 1: Write the failing test** (unit-level: the result-dict builder includes the new keys)

Refactor the `classification_result` dict construction (`analysis_handler.py:207-215`) into a pure helper and test it:

```python
# oddish/tests/analyze/test_classification_result_dict.py
from oddish.analyze.models import (
    ActionItem, ActionItemSource, Dimension, ProblemType, ActionTier,
    ExploitationAssessment, Classification, TrialClassification,
)
from oddish.workers.queue.analysis_handler import classification_to_result_dict


def test_result_dict_includes_action_items_and_exploitation():
    item = ActionItem(
        source=ActionItemSource.POST_TRIAL, problem_type=ProblemType.MISMATCH,
        dimension=Dimension.VERIFIER, file="verifier.py", line_start=1, line_end=1,
        title="t", detail="d", recommendation="r", tier=ActionTier.MUST_FIX,
    )
    tc = TrialClassification(
        trial_name="t1", classification=Classification.BAD_SUCCESS, subtype="Oracle Copying",
        evidence="e", root_cause="rc", recommendation="rec", reward=1.0,
        action_items=[item], exploitation=[ExploitationAssessment(links_to="x", exploited=True)],
    )
    d = classification_to_result_dict(tc)
    assert d["action_items"][0]["file"] == "verifier.py"
    assert d["exploitation"][0]["exploited"] is True
    assert d["classification"] == "BAD_SUCCESS"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/analyze/test_classification_result_dict.py -v`
Expected: FAIL (`ImportError: classification_to_result_dict`).

- [ ] **Step 3: Implement**

In `analysis_handler.py`, extract and extend the result-dict builder:

```python
def classification_to_result_dict(classification) -> dict:
    return {
        "trial_name": classification.trial_name,
        "classification": classification.classification.value,
        "subtype": classification.subtype,
        "evidence": classification.evidence,
        "root_cause": classification.root_cause,
        "recommendation": classification.recommendation,
        "reward": classification.reward,
        "action_items": [i.model_dump(mode="json") for i in classification.action_items],
        "exploitation": [e.model_dump(mode="json") for e in classification.exploitation],
    }
```

Use it where `classification_result` is built (lines 207-215). Before calling the classifier, compute file access and load pre-trial items:

```python
    from oddish.analyze.trajectory_files import parse_trajectory_file_access

    file_access = [fa.__dict__ for fa in parse_trajectory_file_access(trial_dir_to_use)]
    pre_trial_items = (task.pre_trial or {}).get("items") if task.pre_trial else None
    classification = await classifier.classify_trial(
        trial_dir=trial_dir_to_use, task_dir=task_dir_to_use, trial_agent=trial_agent,
        pre_trial_items=pre_trial_items, file_access=file_access,
    )
```

(Load `task` in this handler if not already loaded — the trial's `task_id` is available; fetch `TaskModel.pre_trial`.)

In `qa_handler.py::_classifications_from_trials` (lines 122-145), pass the new keys through when reconstructing `TrialClassification` from `trial.analysis` (so the verdict step can see them if needed) — read `analysis.get("action_items", [])` / `analysis.get("exploitation", [])` and hydrate via `ActionItem(**x)` / `ExploitationAssessment(**x)`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/analyze/test_classification_result_dict.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/workers/queue/analysis_handler.py oddish/src/oddish/workers/queue/qa_handler.py \
        oddish/tests/analyze/test_classification_result_dict.py
git commit -m "feat(qa): store post-trial action items + exploitation on trial.analysis"
```

---

### Task 5: Elevate exploited items back onto `task.pre_trial`

**Files:**
- Modify: `oddish/src/oddish/core/verdict_sync.py` (add `aggregate_exploited_into_pre_trial`)
- Modify: `oddish/src/oddish/workers/queue/qa_handler.py` (call it after the per-trial loop, before/with verdict persist)
- Test: `backend/tests/test_exploited_aggregation.py` (real DB)

**Design:** After all trials are classified, union the `exploitation` assessments across trials; for each pre-trial item whose id was `exploited` in any trial, set `exploited=true` + attach `exploit_evidence` (and which trial) on the stored `task.pre_trial` item. This is the "doubly note" elevation.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_exploited_aggregation.py
import uuid

import pytest

from oddish.core.verdict_sync import aggregate_exploited_into_pre_trial
from oddish.db import get_session
from oddish.db.models import TaskModel, TrialModel


@pytest.mark.asyncio
async def test_exploited_flag_propagates_to_pre_trial_item():
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(TaskModel(
            id=task_id, status="running",
            pre_trial={"items": [{"id": "item1", "file": "verifier.py", "exploited": False}]},
        ))  # adjust required NOT-NULL fields to real TaskModel
        session.add(TrialModel(
            id=f"trial_{uuid.uuid4().hex[:8]}", task_id=task_id, status="success",
            analysis={"exploitation": [{"links_to": "item1", "exploited": True, "exploit_evidence": "step 4"}]},
        ))  # adjust required fields
        await session.commit()
    try:
        await aggregate_exploited_into_pre_trial(task_id)
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            item = task.pre_trial["items"][0]
            assert item["exploited"] is True
            assert "step 4" in (item.get("exploit_evidence") or "")
    finally:
        async with get_session() as session:
            await session.execute(TrialModel.__table__.delete().where(TrialModel.task_id == task_id))
            await session.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
            await session.commit()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_exploited_aggregation.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement**

Append to `oddish/src/oddish/core/verdict_sync.py`:

```python
async def aggregate_exploited_into_pre_trial(task_id: str) -> None:
    """Stamp exploited=true (+ evidence) onto task.pre_trial items whose id was
    exploited in any trial. The 'doubly note' elevation. Idempotent."""
    from sqlalchemy import select

    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if task is None or not task.pre_trial:
            return
        trials = (await session.execute(
            select(TrialModel).where(TrialModel.task_id == task_id)
        )).scalars().all()

        exploited: dict[str, str] = {}
        for trial in trials:
            for a in (trial.analysis or {}).get("exploitation", []):
                if a.get("exploited") and a.get("links_to"):
                    exploited.setdefault(a["links_to"], a.get("exploit_evidence") or "")

        items = task.pre_trial.get("items", [])
        changed = False
        for item in items:
            if item.get("id") in exploited:
                item["exploited"] = True
                if exploited[item["id"]]:
                    item["exploit_evidence"] = exploited[item["id"]]
                changed = True
        if changed:
            task.pre_trial = {**task.pre_trial, "items": items}
            await session.commit()
```

In `qa_handler.py`, after the per-trial loop completes and trials are reloaded (~line 305), call `await aggregate_exploited_into_pre_trial(task_id)` (guard with try/except so it never blocks the verdict).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_exploited_aggregation.py -v`
Expected: PASS. (Adjust fixture rows to the real required columns of `TaskModel`/`TrialModel`.)

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/verdict_sync.py oddish/src/oddish/workers/queue/qa_handler.py \
        backend/tests/test_exploited_aggregation.py
git commit -m "feat(qa): elevate exploited pre-trial items after classification"
```

---

## Self-Review

**Spec coverage (Components 3 + 4):**
- Trajectory file-inspection metadata parser → Task 1. ✓
- Classifier consumes pre-trial items + file-access; emits exploited/causal + new items → Tasks 2, 3. ✓
- Per-trial storage of post-trial items + exploitation → Task 4. ✓
- Exploited items double-flagged/elevated → Task 5. ✓
- Uses file/line refs + structured file-access to match (not fuzzy grep only) → Tasks 1, 3 (metadata) with the prompt still permitting grep as fallback. ✓

**Placeholder scan:** No code placeholders. Fixture rows in the DB tests are marked "adjust required fields to real TaskModel/TrialModel" — a real, explicit step (the exact NOT-NULL set is repo state), not a silent TODO. The `_find_first` import is flagged with a copy-body fallback.

**Type consistency:** `ExploitationAssessment` fields (`links_to`/`exploited`/`exploit_evidence`/`causal`) are identical in the model (Task 2), the result dict (Task 4), and the aggregation reader (Task 5). `action_items` are `ActionItem`s throughout, `model_dump(mode="json")` on write, `ActionItem(**x)` on read. `classification_to_result_dict` keys match what `_classifications_from_trials` and `aggregate_exploited_into_pre_trial` read back.

**Carried assumption:** post-trial `ActionItem.id` is computed the same way as pre-trial (Plan A `compute_action_item_id`); if the classifier returns items without ids, compute them in `classification_to_result_dict` (add `i.id = i.id or compute_action_item_id(i)` before dump).
