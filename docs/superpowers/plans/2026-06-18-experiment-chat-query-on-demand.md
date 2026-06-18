# Experiment-scope chat → query-on-demand Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop mounting an experiment's entire artifact tree into the cc_chat sandbox; instead give the experiment chat the `oddish-query` CLI (the proven `global`-scope pattern) so it fetches trial data on demand.

**Architecture:** Add a lean `GET /experiments/{id}/trials` endpoint + helper, extend the `oddish-query` CLI with experiment/trial subcommands (including a client-side trajectory `--summary` that computes step counts), and rewire the orchestrator's experiment scope to mint a read-only key + upload the CLI + render a query-style CLAUDE.md instead of mounting files. Delete the now-dead `collect_experiment_files`.

**Tech Stack:** Python 3.11+/3.13, FastAPI, SQLAlchemy async, Pydantic, pytest/pytest-asyncio. CLI is stdlib-only (urllib).

## Global Constraints

- **Never commit to `main`.** Work on branch `fix/experiment-chat-no-task-cap` (already checked out).
- **No Claude co-author / Generated-with trailers** in commit messages.
- The CLI (`oddish/src/oddish/cc_chat_query_cli.py`) is **stdlib-only** — no third-party imports.
- CLI output stays byte-budgeted: reuse the existing `MAX_BYTES = 16000`, `LOG_HEAD/LOG_TAIL = 4000`, `_emit_rows`, and `{"_truncated": true}`/`{"_has_more": true}` markers.
- Backend cc_chat tests require `ODDISH_DATABASE_URL` (the `db` fixture skips without it). Run from `backend/` via `uv run pytest`.
- CLI tests run from `oddish/` via `uv run pytest`.
- Do NOT change `global`, `task`, or `task_probes` scope behavior.

## Preamble: discard the superseded cap-removal edits

The working tree currently holds uncommitted byte-cap-removal edits to `experiment_files.py`, `orchestrator.py`, and `test_experiment_files.py` from an earlier pass. This plan deletes/rewrites those paths, so discard them first for a clean base.

- [ ] **Step 0: Reset the superseded edits**

```bash
cd /Users/kateyeh/Developer/os_repos/oddish
git checkout -- backend/api/services/cc_chat/experiment_files.py \
                backend/api/services/cc_chat/orchestrator.py \
                backend/tests/cc_chat/test_experiment_files.py
git status   # expect: only untracked docs/ specs+plans, no staged changes to those 3 files
```

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `oddish/src/oddish/schemas.py` | shared Pydantic schemas | + `ExperimentTrialSummary` |
| `oddish/src/oddish/core/public_helpers.py` | DB query helpers behind the public API | + `list_experiment_trials` |
| `backend/api/routers/trials.py` | trial/experiment HTTP endpoints | + `GET /experiments/{id}/trials` |
| `backend/tests/cc_chat/test_experiment_trials_query.py` | helper test | **create** |
| `oddish/src/oddish/cc_chat_query_cli.py` | in-sandbox read CLI | + `experiments trials`, `trials result/files/file/trajectory[--summary]` |
| `oddish/tests/test_cc_chat_query_cli.py` | CLI tests | + new-command tests |
| `backend/api/services/cc_chat/claude_md.py` | CLAUDE.md templates | rewrite `render_experiment_claude_md` |
| `backend/tests/cc_chat/test_claude_md.py` | template tests | update experiment test |
| `backend/api/services/cc_chat/orchestrator.py` | sandbox provisioning | extend key/CLI gates to `experiment`; drop experiment mount |
| `backend/tests/cc_chat/test_global_scope.py` | provisioning tests | repoint no-mint test; + experiment-mint test |
| `backend/tests/cc_chat/test_orchestrator_start_experiment.py` | experiment provisioning test | rewrite for no-mount + CLI |
| `backend/api/services/cc_chat/experiment_files.py` | (dead) mount collector | **delete** |
| `backend/tests/cc_chat/test_experiment_files.py` | (dead) collector test | **delete** |

---

### Task 1: Backend — `GET /experiments/{id}/trials` + lean helper + schema

**Files:**
- Modify: `oddish/src/oddish/schemas.py`
- Modify: `oddish/src/oddish/core/public_helpers.py`
- Modify: `backend/api/routers/trials.py`
- Test: `backend/tests/cc_chat/test_experiment_trials_query.py` (create)

**Interfaces:**
- Produces: `ExperimentTrialSummary` (Pydantic) with fields `trial_id: str, task_name: str, status: str, reward: float | None, is_probe: bool, input_tokens: int | None, output_tokens: int | None, cost_usd: float | None, phase_timing: dict | None, has_trajectory: bool, started_at: datetime | None, finished_at: datetime | None`.
- Produces: `async def list_experiment_trials(session, experiment_id: str, *, org_id: str) -> list[ExperimentTrialSummary]`.
- Produces: HTTP `GET /experiments/{experiment_id}/trials` → `list[ExperimentTrialSummary]` (READ scope). Consumed by the CLI in Task 2.

- [ ] **Step 1: Write the failing helper test**

Create `backend/tests/cc_chat/test_experiment_trials_query.py`:

```python
import pytest
from sqlalchemy import text
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from oddish.core.public_helpers import list_experiment_trials

pytestmark = pytest.mark.asyncio


async def test_lists_all_experiment_trials_with_summary_fields(db):
    # seed_task_with_trials creates experiment "exp_task_1" with task "demo-task".
    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=2)
    async with db() as s:
        rows = await list_experiment_trials(s, "exp_task_1", org_id=ORG)
    assert {r.trial_id for r in rows} == {
        "task_1-10", "task_1-11", "task_1-20", "task_1-21",
    }
    r = next(r for r in rows if r.trial_id == "task_1-10")
    assert r.task_name == "demo-task"
    assert r.is_probe is False
    assert r.has_trajectory is False


async def test_excludes_other_orgs(db):
    await seed_task_with_trials(db, versions=(1,), trials_per_version=1)
    async with db() as s:
        rows = await list_experiment_trials(s, "exp_task_1", org_id="org_other")
    assert rows == []


async def test_flags_probe_trials(db):
    seeded = await seed_task_with_trials(db, versions=(1,), trials_per_version=2)
    probe_id = seeded[1][0]  # first trial of version 1 == "task_1-10"
    async with db() as s:
        await s.execute(
            text("update trials set is_probe = true where id = :id"), {"id": probe_id}
        )
        await s.commit()
    async with db() as s:
        rows = await list_experiment_trials(s, "exp_task_1", org_id=ORG)
    assert {r.trial_id for r in rows if r.is_probe} == {probe_id}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd backend && uv run pytest tests/cc_chat/test_experiment_trials_query.py -q`
Expected: FAIL with `ImportError: cannot import name 'list_experiment_trials'`.

- [ ] **Step 3: Add the `ExperimentTrialSummary` schema**

In `oddish/src/oddish/schemas.py`, ensure `from datetime import datetime` is present (add if missing), then add near `TrialResponse`:

```python
class ExperimentTrialSummary(BaseModel):
    """Lean per-trial summary for experiment-scope chat (cheap stored columns only)."""
    trial_id: str
    task_name: str
    status: str
    reward: float | None = None
    is_probe: bool = False
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    phase_timing: dict | None = None
    has_trajectory: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
```

- [ ] **Step 4: Add the `list_experiment_trials` helper**

In `oddish/src/oddish/core/public_helpers.py`, add the import (extend the existing schemas import) `from oddish.schemas import ExperimentTrialSummary` and the helper. `TrialModel`, `TaskModel`, `select`, and `AsyncSession` are already imported in this module (used by `list_task_trials_for_task`).

```python
async def list_experiment_trials(
    session: AsyncSession, experiment_id: str, *, org_id: str
) -> list[ExperimentTrialSummary]:
    """Lean list of an experiment's non-superseded trials, org-scoped.

    Same predicate the cc_chat mount collector used (experiment_id FK,
    superseded hidden), projected to cheap stored columns so the experiment
    chat can list trials over HTTP without downloading any artifacts.
    """
    rows = (
        await session.execute(
            select(TrialModel, TaskModel.name)
            .join(TaskModel, TaskModel.id == TrialModel.task_id)
            .where(
                TrialModel.experiment_id == experiment_id,
                TrialModel.org_id == org_id,
                TrialModel.superseded_by_trial_id.is_(None),
            )
            .order_by(TrialModel.created_at.asc())
        )
    ).all()
    return [
        ExperimentTrialSummary(
            trial_id=t.id,
            task_name=name,
            status=getattr(t.status, "value", t.status),
            reward=t.reward,
            is_probe=t.is_probe,
            input_tokens=t.input_tokens,
            output_tokens=t.output_tokens,
            cost_usd=t.cost_usd,
            phase_timing=t.phase_timing,
            has_trajectory=t.has_trajectory,
            started_at=t.started_at,
            finished_at=t.finished_at,
        )
        for t, name in rows
    ]
```

- [ ] **Step 5: Run the helper test to verify it passes**

Run: `cd backend && uv run pytest tests/cc_chat/test_experiment_trials_query.py -q`
Expected: PASS (3 passed).

- [ ] **Step 6: Add the HTTP endpoint**

In `backend/api/routers/trials.py`: extend the existing `from oddish.core.public_helpers import (...)` block to also import `list_experiment_trials`, and the `from oddish.schemas import (...)` block to also import `ExperimentTrialSummary`. Then add, right after the `list_task_trials` endpoint (after line ~93):

```python
@router.get(
    "/experiments/{experiment_id}/trials",
    response_model=list[ExperimentTrialSummary],
)
async def list_experiment_trials_endpoint(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> list[ExperimentTrialSummary]:
    """List an experiment's non-superseded trials (org-scoped, lean summary)."""
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        return await list_experiment_trials(
            session, experiment_id, org_id=auth.org_id
        )
```

- [ ] **Step 7: Verify the router imports cleanly**

Run: `cd backend && uv run python -c "import api.routers.trials; print('ok')"`
Expected: `ok`.

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/schemas.py oddish/src/oddish/core/public_helpers.py \
        backend/api/routers/trials.py backend/tests/cc_chat/test_experiment_trials_query.py
git commit -m "feat(cc_chat): add GET /experiments/{id}/trials lean listing"
```

---

### Task 2: CLI — experiment listing + result/files/file commands

**Files:**
- Modify: `oddish/src/oddish/cc_chat_query_cli.py`
- Test: `oddish/tests/test_cc_chat_query_cli.py`

**Interfaces:**
- Consumes: `GET /experiments/{id}/trials` (Task 1); existing `GET /trials/{id}/result`, `/files`, `/files/{path}`.
- Produces CLI commands: `experiments trials <exp_id>`, `trials result <trial_id>`, `trials files <trial_id> [--prefix P] [--recursive]`, `trials file <trial_id> <path>`.

- [ ] **Step 1: Write the failing tests**

Append to `oddish/tests/test_cc_chat_query_cli.py`:

```python
def test_experiment_trials_projects_rows(monkeypatch):
    data = [
        {"trial_id": "tr1", "task_name": "demo", "status": "SUCCESS",
         "reward": 1.0, "is_probe": False, "has_trajectory": True,
         "phase_timing": {"x": 1}, "cost_usd": 0.2},
    ]
    seen = {}

    def fake_get(path, params=None):
        seen["path"] = path
        return data

    out = _run(monkeypatch, ["experiments", "trials", "exp1"], fake_get)
    assert seen["path"] == "/experiments/exp1/trials"
    row = json.loads(out.splitlines()[0])
    assert row == {
        "trial_id": "tr1", "task": "demo", "status": "SUCCESS",
        "reward": 1.0, "probe": False, "has_trajectory": True,
    }
    assert "phase_timing" not in out


def test_result_budgets_output(monkeypatch):
    out = _run(monkeypatch, ["trials", "result", "tr1"],
               lambda p, params=None: {"reward": 1, "blob": "y" * 50000})
    assert len(out) <= cli.MAX_BYTES


def test_files_maps_recursive_flag(monkeypatch):
    seen = {}

    def fake_get(path, params=None):
        seen["path"] = path
        seen["params"] = params
        return {"files": []}

    _run(monkeypatch, ["trials", "files", "tr1", "--prefix", "agent/", "--recursive"], fake_get)
    assert seen["path"] == "/trials/tr1/files"
    assert seen["params"]["prefix"] == "agent/"
    assert seen["params"]["recursive"] == "true"


def test_file_fetches_by_path(monkeypatch):
    seen = {}

    def fake_get(path, params=None):
        seen["path"] = path
        return {"content": "hello"}

    _run(monkeypatch, ["trials", "file", "tr1", "agent/trajectory.json"], fake_get)
    assert seen["path"] == "/trials/tr1/files/agent/trajectory.json"
```

- [ ] **Step 2: Run to verify failure**

Run: `cd oddish && uv run pytest tests/test_cc_chat_query_cli.py -q -k "experiment_trials or result_budgets or files_maps or file_fetches"`
Expected: FAIL (argparse exits / commands not defined).

- [ ] **Step 3: Implement the command handlers**

In `oddish/src/oddish/cc_chat_query_cli.py`, add these handlers after `_cmd_logs` (before `def main`):

```python
def _cmd_experiment_trials(a) -> None:
    data = _get(f"/experiments/{a.exp_id}/trials")
    rows = data if isinstance(data, list) else (data.get("items") or [])
    _emit_rows([
        {
            "trial_id": t.get("trial_id"),
            "task": t.get("task_name"),
            "status": t.get("status"),
            "reward": t.get("reward"),
            "probe": t.get("is_probe"),
            "has_trajectory": t.get("has_trajectory"),
        }
        for t in rows
    ])


def _cmd_result(a) -> None:
    _print(json.dumps(_get(f"/trials/{a.trial_id}/result"), separators=(",", ":"))[:MAX_BYTES])


def _cmd_files(a) -> None:
    data = _get(f"/trials/{a.trial_id}/files", {
        "prefix": a.prefix,
        "recursive": "true" if a.recursive else None,
    })
    _print(json.dumps(data, separators=(",", ":"))[:MAX_BYTES])


def _cmd_file(a) -> None:
    data = _get(f"/trials/{a.trial_id}/files/{a.path}")
    text = data if isinstance(data, str) else json.dumps(data)
    if len(text) > LOG_HEAD + LOG_TAIL:
        text = text[:LOG_HEAD] + "\n…[truncated]…\n" + text[-LOG_TAIL:]
    _print(text)
```

- [ ] **Step 4: Register the subparsers**

In `main()`, extend the existing `trials` subparser group and add an `experiments` group. After the `lg = trials.add_parser("logs")` block:

```python
    rs = trials.add_parser("result")
    rs.add_argument("trial_id")
    rs.set_defaults(func=_cmd_result)
    fls = trials.add_parser("files")
    fls.add_argument("trial_id")
    fls.add_argument("--prefix", default=None)
    fls.add_argument("--recursive", action="store_true")
    fls.set_defaults(func=_cmd_files)
    fl = trials.add_parser("file")
    fl.add_argument("trial_id")
    fl.add_argument("path")
    fl.set_defaults(func=_cmd_file)

    experiments = sub.add_parser("experiments").add_subparsers(dest="cmd", required=True)
    et = experiments.add_parser("trials")
    et.add_argument("exp_id")
    et.set_defaults(func=_cmd_experiment_trials)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_cc_chat_query_cli.py -q`
Expected: PASS (all, including the four new tests).

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/cc_chat_query_cli.py oddish/tests/test_cc_chat_query_cli.py
git commit -m "feat(cc_chat): add experiments/trials + result/files/file query commands"
```

---

### Task 3: CLI — `trials trajectory [--summary]` with client-side step count

**Files:**
- Modify: `oddish/src/oddish/cc_chat_query_cli.py`
- Test: `oddish/tests/test_cc_chat_query_cli.py`

**Interfaces:**
- Consumes: existing `GET /trials/{id}/trajectory` (returns the raw ATIF dict with a top-level `steps` list).
- Produces CLI command: `trials trajectory <trial_id> [--summary]`. Default: head/tail-truncated dump. `--summary`: prints `{"num_steps", "num_tool_calls", "final_metrics"}` computed client-side.

- [ ] **Step 1: Write the failing tests**

Append to `oddish/tests/test_cc_chat_query_cli.py`:

```python
def test_trajectory_summary_counts_steps(monkeypatch):
    traj = {
        "steps": [
            {"tool_calls": [{"name": "a"}, {"name": "b"}]},
            {"tool_calls": [{"name": "c"}]},
            {"tool_calls": []},
        ],
        "final_metrics": {"total_cost_usd": 0.5},
    }
    out = _run(monkeypatch, ["trials", "trajectory", "tr1", "--summary"],
               lambda p, params=None: traj)
    payload = json.loads(out.splitlines()[0])
    assert payload == {
        "num_steps": 3,
        "num_tool_calls": 3,
        "final_metrics": {"total_cost_usd": 0.5},
    }


def test_trajectory_summary_tolerates_missing_steps(monkeypatch):
    out = _run(monkeypatch, ["trials", "trajectory", "tr1", "--summary"],
               lambda p, params=None: {})
    payload = json.loads(out.splitlines()[0])
    assert payload == {"num_steps": 0, "num_tool_calls": 0, "final_metrics": None}


def test_trajectory_full_truncates(monkeypatch):
    big = {"steps": [{"x": "A" * 12000}]}
    out = _run(monkeypatch, ["trials", "trajectory", "tr1"], lambda p, params=None: big)
    assert "[truncated]" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `cd oddish && uv run pytest tests/test_cc_chat_query_cli.py -q -k trajectory`
Expected: FAIL (`trajectory` command not defined).

- [ ] **Step 3: Implement the handler**

In `oddish/src/oddish/cc_chat_query_cli.py`, add after `_cmd_file`:

```python
def _cmd_trajectory(a) -> None:
    data = _get(f"/trials/{a.trial_id}/trajectory")
    if a.summary:
        traj = data if isinstance(data, dict) else {}
        steps = traj.get("steps")
        steps = steps if isinstance(steps, list) else []
        num_tool_calls = sum(
            len(s.get("tool_calls") or []) for s in steps if isinstance(s, dict)
        )
        _print(json.dumps({
            "num_steps": len(steps),
            "num_tool_calls": num_tool_calls,
            "final_metrics": traj.get("final_metrics"),
        }, separators=(",", ":")))
        return
    text = data if isinstance(data, str) else json.dumps(data)
    if len(text) > LOG_HEAD + LOG_TAIL:
        text = text[:LOG_HEAD] + "\n…[truncated]…\n" + text[-LOG_TAIL:]
    _print(text)
```

- [ ] **Step 4: Register the subparser**

In `main()`, after the `trials file` block:

```python
    tj = trials.add_parser("trajectory")
    tj.add_argument("trial_id")
    tj.add_argument("--summary", action="store_true")
    tj.set_defaults(func=_cmd_trajectory)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_cc_chat_query_cli.py -q`
Expected: PASS (all).

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/cc_chat_query_cli.py oddish/tests/test_cc_chat_query_cli.py
git commit -m "feat(cc_chat): add trials trajectory --summary client-side step count"
```

---

### Task 4: Rewrite `render_experiment_claude_md` for the query workflow

**Files:**
- Modify: `backend/api/services/cc_chat/claude_md.py`
- Test: `backend/tests/cc_chat/test_claude_md.py`

**Interfaces:**
- Produces: `def render_experiment_claude_md(*, experiment_id: str) -> str` (drops the old `trial_ids` parameter). Consumed by the orchestrator in Task 5.

- [ ] **Step 1: Update the failing test**

In `backend/tests/cc_chat/test_claude_md.py`, replace `test_experiment_claude_md_mentions_experiment_id_and_trials` with:

```python
def test_experiment_claude_md_documents_query_cli():
    out = render_experiment_claude_md(experiment_id="exp_abc")
    assert "exp_abc" in out
    assert "oddish-query experiments trials" in out
    assert "trajectory --summary" in out
    # query-on-demand, not a mounted tree
    assert "jobs/" not in out
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/cc_chat/test_claude_md.py -q -k experiment`
Expected: FAIL (`TypeError` on missing `trial_ids` / old assertions).

- [ ] **Step 3: Rewrite the template and function**

In `backend/api/services/cc_chat/claude_md.py`, replace the `_TEMPLATE` constant (the experiment one, lines ~4-43), the `_EMPTY_TRIAL_BLOCK` usage in this function, and `render_experiment_claude_md` (lines ~48-55) with:

```python
_TEMPLATE = """\
# Experiment {experiment_id}

You are a Claude Code agent helping a user reason about an Oddish experiment.
You have a read-only CLI, `oddish-query`, that queries the oddish backend for
this experiment's trials. Nothing is mounted — fetch only what the user's
question needs. Scope is this experiment; you cannot write anything.

## Tool: `oddish-query` (call via Bash)

Run it from the workspace dir as `./oddish-query` (it lives in the current directory).

Start with the trial list, then drill into individual trials on demand:

- `./oddish-query experiments trials {experiment_id}`
  One row per trial: trial_id, task, status, reward, probe, has_trajectory. **Start here.**
  Probe trials are flagged `probe: true` — treat them as a distinct category.
- `./oddish-query trials result <trial_id>` — a trial's structured result/verdict.
- `./oddish-query trials trajectory <trial_id> --summary` — `{num_steps, num_tool_calls, final_metrics}`.
  Use this for step/tool-call counts (the full trajectory is large; `--summary` is exact and cheap).
- `./oddish-query trials trajectory <trial_id>` — the full action trajectory (large; one trial at a time).
- `./oddish-query trials logs <trial_id>` — a single trial's logs (large; one trial at a time).
- `./oddish-query trials files <trial_id> [--prefix P] [--recursive]` — list a trial's artifact tree.
- `./oddish-query trials file <trial_id> <path>` — fetch one artifact by path.

## Discipline

- ALWAYS begin with `experiments trials {experiment_id}`; judge relevance from the rows yourself.
- Only call `result`/`trajectory`/`logs`/`files` once the user has zoomed into specific trials.
- For experiment-wide derived stats (e.g. average step count), loop `trajectory --summary`
  over the relevant trials — fetch only the trials the question needs, not all of them.
- Output is capped per call; if you see `{"_truncated": true}`, narrow rather than widen.
"""


def render_experiment_claude_md(*, experiment_id: str) -> str:
    return _TEMPLATE.format(experiment_id=experiment_id)
```

(Leave `_EMPTY_TRIAL_BLOCK` in place — it is still used by `render_task_probes_claude_md` and `render_task_chat_claude_md`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd backend && uv run pytest tests/cc_chat/test_claude_md.py -q`
Expected: PASS (all).

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/cc_chat/claude_md.py backend/tests/cc_chat/test_claude_md.py
git commit -m "feat(cc_chat): experiment CLAUDE.md documents query CLI, not mounted tree"
```

---

### Task 5: Orchestrator — query-on-demand experiment scope; delete the mount

**Files:**
- Modify: `backend/api/services/cc_chat/orchestrator.py`
- Modify: `backend/tests/cc_chat/test_global_scope.py`
- Rewrite: `backend/tests/cc_chat/test_orchestrator_start_experiment.py`
- Delete: `backend/api/services/cc_chat/experiment_files.py`
- Delete: `backend/tests/cc_chat/test_experiment_files.py`

**Interfaces:**
- Consumes: `render_experiment_claude_md(experiment_id=...)` (Task 4); the experiment key/CLI wiring mirrors the existing `global` path (`create_api_key`, `extra_env`, `upload_query_cli`).

- [ ] **Step 1: Rewrite the experiment provisioning test (failing)**

Replace the entire body of `backend/tests/cc_chat/test_orchestrator_start_experiment.py` with:

```python
from contextlib import asynccontextmanager

import pytest

from tests.cc_chat.conftest import seed_task_with_trials, ORG

pytestmark = pytest.mark.asyncio


class _FakeRuntime:
    async def install(self, client, sandbox):
        return None


def _factory(db):
    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()
    return factory


async def test_experiment_scope_mints_key_uploads_cli_and_mounts_no_files(db, monkeypatch):
    from api.services.cc_chat import orchestrator as orchestrator_module
    from api.services.cc_chat.daytona_client import FakeDaytonaClient
    from api.services.cc_chat.orchestrator import ChatOrchestrator
    from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer
    from models import APIKeyModel, APIKeyScope, ChatSession, generate_id

    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)

    minted: dict[str, object] = {}

    def fake_create_api_key(
        org_id, name, scope=APIKeyScope.FULL, created_by_user_id=None,
        expires_at=None, is_internal=False,
    ):
        model = APIKeyModel(
            id=generate_id(), org_id=org_id, name=name,
            key_prefix="ok_testkey", key_hash=f"hash_{generate_id()}",
            scope=scope, created_by_user_id=None,
            expires_at=expires_at, is_internal=is_internal,
        )
        minted["model"] = model
        minted["scope"] = scope
        return model, "ok_rawsecretkey"

    monkeypatch.setattr(orchestrator_module, "create_api_key", fake_create_api_key)

    fake = FakeDaytonaClient()
    orch = ChatOrchestrator(
        daytona=fake,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        public_api_base_url="https://api.oddish.example",
    )

    session_id = await orch.start(
        org_id=ORG, user_id=None,
        scope_kind="experiment", scope_id="exp_task_1",
        db_session_factory=_factory(db),
    )
    assert session_id

    rec = next(iter(fake.sandboxes.values()))
    uploaded = list(rec["files"].keys())
    # CLI + CLAUDE.md uploaded; NO jobs/ artifacts mounted
    assert any(p.endswith("oddish-query") for p in uploaded)
    assert any(p.endswith("/CLAUDE.md") for p in uploaded)
    assert not any("jobs/" in p for p in uploaded)
    # read-only query credential injected
    assert minted["scope"] == APIKeyScope.READ
    assert rec["env"]["ODDISH_API_KEY"] == "ok_rawsecretkey"
    assert rec["env"]["ODDISH_API_BASE_URL"] == "https://api.oddish.example"

    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.status == "active" and row.scope_kind == "experiment"
        assert row.query_api_key_id == minted["model"].id
```

- [ ] **Step 2: Run to verify failure**

Run: `cd backend && uv run pytest tests/cc_chat/test_orchestrator_start_experiment.py -q`
Expected: FAIL (experiment scope still mounts files / mints no key — `oddish-query` not uploaded, `ODDISH_API_KEY` missing).

- [ ] **Step 3: Rewire `_resolve_scope_inputs` experiment branch**

In `backend/api/services/cc_chat/orchestrator.py`, replace the experiment branch (lines ~111-129) with:

```python
        if scope_kind == "experiment":
            claude_md = render_experiment_claude_md(experiment_id=scope_id)
```

Then remove the now-unused import `from api.services.cc_chat.experiment_files import collect_experiment_files` (line ~31).

- [ ] **Step 4: Extend the key-mint + CLI gates to experiment scope**

In the same file, change the three `global`-only gates to include `experiment`:

- In `start()` (line ~226): `if scope_kind == "global":` → `if scope_kind in ("global", "experiment"):`
- In `resume()` (line ~326): `if scope_kind == "global":` → `if scope_kind in ("global", "experiment"):`
- Both `_provision_sandbox(... upload_query_cli=scope_kind == "global")` calls (lines ~270 and ~366): `upload_query_cli=scope_kind in ("global", "experiment")`

(`close()` needs no change — it revokes whatever `query_api_key_id` the row carries.)

- [ ] **Step 5: Run the experiment provisioning test to verify it passes**

Run: `cd backend && uv run pytest tests/cc_chat/test_orchestrator_start_experiment.py -q`
Expected: PASS.

- [ ] **Step 6: Repoint the "no-mint" global-scope test**

`test_non_global_scope_mints_no_key` in `backend/tests/cc_chat/test_global_scope.py` currently proves *experiment* scope mints no key — that is now false. Repoint it to `task_probes` (which mounts nothing and mints nothing, needing no seeded data). In that test, change the `orch.start(...)` call (lines ~280-284) from:

```python
    await orch.start(
        org_id="org_cc_test", user_id=None,
        scope_kind="experiment", scope_id="exp_1",
        db_session_factory=_factory(db),
    )
```

to:

```python
    await orch.start(
        org_id="org_cc_test", user_id=None,
        scope_kind="task_probes", scope_id="tp_task",
        db_session_factory=_factory(db),
    )
```

(The assertions below it — `create_api_key` not called, no `oddish-query` uploaded — stay as-is and now describe `task_probes`.)

- [ ] **Step 7: Delete the dead mount collector and its test**

```bash
git rm backend/api/services/cc_chat/experiment_files.py \
       backend/tests/cc_chat/test_experiment_files.py
```

- [ ] **Step 8: Run the full cc_chat suite + import check**

Run:
```bash
cd backend && uv run python -c "import api.services.cc_chat.orchestrator; print('ok')" && \
uv run pytest tests/cc_chat/ -q
```
Expected: `ok` then PASS (no remaining reference to `collect_experiment_files`/`experiment_files`; all cc_chat tests green).

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat(cc_chat): experiment chat queries on demand; drop artifact mount"
```

---

### Task 6: Full-suite verification

**Files:** none (verification only).

- [ ] **Step 1: Run the backend + oddish suites**

Run:
```bash
cd backend && uv run pytest tests/cc_chat/ -q
cd ../oddish && uv run pytest tests/test_cc_chat_query_cli.py -q
```
Expected: both PASS.

- [ ] **Step 2: Grep for stragglers**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish && grep -rn "collect_experiment_files\|experiment_files" backend oddish --include='*.py'`
Expected: no matches.

- [ ] **Step 3: Confirm the branch state**

Run: `git log --oneline fix/experiment-chat-no-task-cap -6` and `git status`
Expected: the six task commits present, clean tree (untracked docs specs/plans aside).

---

## Self-Review

**Spec coverage:**
- New `GET /experiments/{id}/trials` (cheap columns) → Task 1. ✓
- CLI `experiments trials` + `trials result/files/file` → Task 2. ✓
- CLI `trials trajectory [--summary]` client-side step count → Task 3. ✓
- Existing `trials logs [--trajectory]` untouched (global contract) → not modified; verified by unchanged `test_logs_truncates_head_and_tail`. ✓
- Orchestrator: extend key-mint + CLI gates to experiment; stop mounting; no blob/DB needed at provision → Task 5. ✓
- Rewrite `render_experiment_claude_md` (drop `trial_ids`, query workflow) → Task 4. ✓
- `is_probe` surfaced via the listing → Task 1 schema + Task 2 projection (`probe`). ✓
- Delete `collect_experiment_files` + test; supersede cap removal → Preamble + Task 5. ✓
- Known limitation (experiment-wide derived aggregates loop `--summary`) → documented in the CLAUDE.md template (Task 4). ✓
- Testing: endpoint/helper (Task 1), CLI subcommands incl. `--summary` (Tasks 2–3), orchestrator no-mount + key + CLI (Task 5). ✓

**Placeholder scan:** No TBD/TODO/"add error handling"; every code step shows complete code. ✓

**Type consistency:** `ExperimentTrialSummary` field names (`trial_id`, `task_name`, `is_probe`, `has_trajectory`) match the CLI projection keys read in Task 2 (`t.get("trial_id")`, `t.get("task_name")`, `t.get("is_probe")`, `t.get("has_trajectory")`) and the helper's constructor kwargs in Task 1. `render_experiment_claude_md(experiment_id=...)` signature matches the orchestrator call in Task 5. ✓

**Note for implementer:** `TrialModel.status` may be a SQLAlchemy enum; the helper coerces via `getattr(t.status, "value", t.status)`, so `ExperimentTrialSummary.status: str` receives a plain string. If a future column is renamed, update both the helper and the schema together.
