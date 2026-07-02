# Collect-by-task Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let users build a published, read-only collection from the latest trials of one or more tasks' current versions, via `oddish collect --task <task…>`.

**Architecture:** Extend the existing reference-based collections primitive (`create_trial_collection_core`, which links trials into an `is_collection` experiment — no copy). Add a `task_ids` mode that queries the DB for each task's current-version terminal trials, wire it through the backend route, and add a `collect` CLI that publishes by default.

**Tech Stack:** Python 3.13, FastAPI (`backend/` = deployed server), SQLAlchemy async, Pydantic v2, Typer (CLI), pytest/pytest-asyncio + httpx ASGITransport.

## Global Constraints

- Deployed server is **`backend/`** (`api.app:create_app`). The secondary `oddish/src/oddish/server/` is out of scope.
- Shared core lives in `oddish/src/oddish/core/endpoints/collections.py`; schemas in `oddish/src/oddish/schemas.py`.
- Selection filter (must match `ls`'s `latest_trials`): `(task_id, task_version_id) == (task.id, task.current_version_id)`, `superseded_by_trial_id IS NULL`, `status IN {SUCCESS, FAILED}`, `is_probe IS NOT TRUE`, `org_id == org_id`.
- Collections are **reference-based** (link via `experiment_trials` / `task_experiments`); never copy trial rows or artifacts.
- Additive only: `trial_ids`-only callers, `combine`, and `experiment create` must behave exactly as before.
- Publish is CLI-orchestrated via the existing `POST /experiments/{id}/publish`; publish-by-default with `--no-publish`.
- Run oddish-core tests from `oddish/`: `uv run pytest …`. Run backend tests from `backend/`: `set -a && source .env && set +a && uv run pytest …`.

---

### Task 1: Extend `TrialCollectionRequest`/`Response` schema

**Files:**
- Modify: `oddish/src/oddish/schemas.py:560` (`TrialCollectionRequest`), `:1045` (`TrialCollectionResponse`)
- Test: `oddish/tests/test_trial_collection_schema.py` (create)

**Interfaces:**
- Produces: `TrialCollectionRequest{name: str, trial_ids: list[str]=[], task_ids: list[str]=[]}` with a validator requiring ≥1 across `trial_ids`+`task_ids`; `TrialCollectionResponse` gains `trials_from_tasks: int=0`, `tasks_skipped_empty: int=0`.

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/test_trial_collection_schema.py
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.schemas import TrialCollectionRequest


def test_accepts_task_ids_only():
    req = TrialCollectionRequest(name="c", task_ids=["taskA", "taskA", " taskB "])
    assert req.trial_ids == []
    assert req.task_ids == ["taskA", "taskB"]  # deduped + stripped


def test_accepts_trial_ids_only():
    req = TrialCollectionRequest(name="c", trial_ids=["t1", "t1"])
    assert req.trial_ids == ["t1"]
    assert req.task_ids == []


def test_rejects_empty_sources():
    with pytest.raises(ValueError):
        TrialCollectionRequest(name="c")


def test_rejects_blank_name():
    with pytest.raises(ValueError):
        TrialCollectionRequest(name="  ", trial_ids=["t1"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_trial_collection_schema.py -v`
Expected: FAIL — `test_accepts_task_ids_only` errors (`task_ids` unknown / `trial_ids` required).

- [ ] **Step 3: Write minimal implementation**

Replace `TrialCollectionRequest` (schemas.py:560) with:

```python
class TrialCollectionRequest(BaseModel):
    """Request to gather existing trials into a new read-only collection."""

    name: str
    trial_ids: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("name must not be empty")
        return stripped

    @model_validator(mode="after")
    def _validate_sources(self) -> "TrialCollectionRequest":
        self.trial_ids = list(
            dict.fromkeys(s.strip() for s in self.trial_ids if s and s.strip())
        )
        self.task_ids = list(
            dict.fromkeys(s.strip() for s in self.task_ids if s and s.strip())
        )
        if not self.trial_ids and not self.task_ids:
            raise ValueError("provide at least one trial id or task id")
        return self
```

Add the new fields to `TrialCollectionResponse` (schemas.py:1045):

```python
class TrialCollectionResponse(BaseModel):
    """Result of gathering trials into a new read-only collection."""

    id: str
    name: str
    trials_linked: int
    tasks_linked: int
    trials_from_tasks: int = 0
    tasks_skipped_empty: int = 0
```

(Ensure `model_validator` is imported at the top of schemas.py — it already is, used by `ExperimentCombineRequest`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/test_trial_collection_schema.py -v`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/schemas.py oddish/tests/test_trial_collection_schema.py
git commit -m "feat(collections): add task_ids to TrialCollectionRequest + response counts"
```

---

### Task 2: Add task-based selection to `create_trial_collection_core`

**Files:**
- Modify: `oddish/src/oddish/core/endpoints/collections.py`
- Test: `oddish/tests/test_create_trial_collection.py` (append tests)

**Interfaces:**
- Consumes: `TrialCollectionRequest` fields from Task 1.
- Produces: `create_trial_collection_core(session, *, name, trial_ids=None, task_ids=None, org_id) -> TrialCollectionResponse` — resolves each task to its `current_version_id`, selects current-version terminal non-superseded non-probe trials, unions with explicit `trial_ids` (deduped), links them; returns counts including `trials_from_tasks` and `tasks_skipped_empty`.

- [ ] **Step 1: Write the failing test** (append to `oddish/tests/test_create_trial_collection.py`)

```python
from datetime import datetime, timezone

from oddish.db.models import TaskVersionModel, TrialStatus


def _version(task, n: int) -> TaskVersionModel:
    return TaskVersionModel(
        id=f"{task.id}-v{n}", task_id=task.id, version=n, task_path=f"s3://t/{task.id}/v{n}"
    )


def _ver_trial(task, home, version_id, *, status=TrialStatus.SUCCESS,
               is_probe=False, superseded=None, org_id="org1") -> TrialModel:
    t = _trial(task, home, org_id=org_id)
    t.task_version_id = version_id
    t.status = status
    t.is_probe = is_probe
    t.superseded_by_trial_id = superseded
    return t


@pytest.mark.asyncio
async def test_task_mode_links_only_current_version_terminal_trials(session):
    task = _task("cbt-task")
    session.add(task)
    await session.flush()
    v1, v2 = _version(task, 1), _version(task, 2)
    session.add_all([v1, v2])
    await session.flush()
    task.current_version_id = v2.id
    await session.flush()

    home = _experiment("home")
    session.add(home)
    await session.flush()

    keep_a = _ver_trial(task, home, v2.id, status=TrialStatus.SUCCESS)
    keep_b = _ver_trial(task, home, v2.id, status=TrialStatus.FAILED)
    old = _ver_trial(task, home, v1.id, status=TrialStatus.SUCCESS)       # old version
    pending = _ver_trial(task, home, v2.id, status=TrialStatus.PENDING)   # not terminal
    probe = _ver_trial(task, home, v2.id, is_probe=True)                  # probe
    sup = _ver_trial(task, home, v2.id, superseded="whatever")           # superseded
    session.add_all([keep_a, keep_b, old, pending, probe, sup])
    await session.flush()

    resp = await create_trial_collection_core(
        session, name="c", task_ids=[task.name], org_id="org1"
    )
    await session.flush()

    linked = set((await session.execute(
        select(experiment_trials.c.trial_id).where(
            experiment_trials.c.experiment_id == resp.id
        )
    )).scalars().all())
    assert linked == {keep_a.id, keep_b.id}
    assert resp.trials_linked == 2
    assert resp.trials_from_tasks == 2


@pytest.mark.asyncio
async def test_task_not_found_raises_404(session):
    with pytest.raises(HTTPException) as ei:
        await create_trial_collection_core(
            session, name="c", task_ids=["nope"], org_id="org1"
        )
    assert ei.value.status_code == 404


@pytest.mark.asyncio
async def test_empty_task_is_skipped_and_counted(session):
    task = _task("empty-task")
    session.add(task)
    await session.flush()
    v1 = _version(task, 1)
    session.add(v1)
    await session.flush()
    task.current_version_id = v1.id
    await session.flush()
    # no trials for v1 -> whole set empty -> 400
    with pytest.raises(HTTPException) as ei:
        await create_trial_collection_core(
            session, name="c", task_ids=[task.name], org_id="org1"
        )
    assert ei.value.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_create_trial_collection.py -v -k "task_mode or task_not_found or empty_task"`
Expected: FAIL — `create_trial_collection_core` has no `task_ids` parameter (TypeError).

- [ ] **Step 3: Write minimal implementation**

Rewrite `oddish/src/oddish/core/endpoints/collections.py` as:

```python
from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import insert, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import ExperimentModel, TrialModel, experiment_trials, utcnow
from oddish.db.models import TaskModel, TrialStatus
from oddish.schemas import TrialCollectionResponse

_TERMINAL = (TrialStatus.SUCCESS, TrialStatus.FAILED)


async def create_trial_collection_core(
    session: AsyncSession,
    *,
    name: str,
    trial_ids: list[str] | None = None,
    task_ids: list[str] | None = None,
    org_id: str | None,
) -> TrialCollectionResponse:
    """Gather existing trials into a new read-only collection experiment.

    Trials keep their home experiment; a fresh ``is_collection`` experiment is
    created and the trials are linked into it via ``experiment_trials`` /
    ``task_experiments`` (no copy). ``trial_ids`` links those exact trials;
    ``task_ids`` links each task's current-version terminal, non-superseded,
    non-probe trials. The caller's session context manager commits.
    """
    from oddish.queue import _link_task_to_experiment

    explicit_ids = list(
        dict.fromkeys(t.strip() for t in (trial_ids or []) if t and t.strip())
    )
    task_idents = list(
        dict.fromkeys(t.strip() for t in (task_ids or []) if t and t.strip())
    )
    if not explicit_ids and not task_idents:
        raise HTTPException(status_code=400, detail="Provide at least one trial id or task id")

    # 1. Explicit trials (existing behavior).
    explicit_rows: list[TrialModel] = []
    if explicit_ids:
        rows = (
            await session.execute(
                select(TrialModel).where(
                    TrialModel.id.in_(explicit_ids),
                    TrialModel.org_id == org_id,
                )
            )
        ).scalars().all()
        found = {t.id: t for t in rows}
        missing = [i for i in explicit_ids if i not in found]
        if missing:
            raise HTTPException(
                status_code=404, detail=f"Trials not found in org: {', '.join(missing)}"
            )
        explicit_rows = [found[i] for i in explicit_ids]

    # 2. Task-sourced trials (current version only).
    tasks_skipped_empty = 0
    task_rows: list[TrialModel] = []
    if task_idents:
        pairs: list[tuple[str, str]] = []
        for ident in task_idents:
            task = (
                await session.execute(
                    select(TaskModel).where(
                        or_(TaskModel.id == ident, TaskModel.name == ident),
                        TaskModel.org_id == org_id,
                    )
                )
            ).scalars().first()
            if task is None:
                raise HTTPException(status_code=404, detail=f"Task {ident} not found")
            if task.current_version_id is None:
                tasks_skipped_empty += 1
                continue
            pairs.append((task.id, task.current_version_id))

        if pairs:
            task_rows = (
                await session.execute(
                    select(TrialModel)
                    .where(
                        tuple_(TrialModel.task_id, TrialModel.task_version_id).in_(pairs),
                        TrialModel.superseded_by_trial_id.is_(None),
                        TrialModel.status.in_(_TERMINAL),
                        TrialModel.is_probe.isnot(True),
                        TrialModel.org_id == org_id,
                    )
                    .order_by(TrialModel.task_id, TrialModel.created_at)
                )
            ).scalars().all()
            contributed = {t.task_id for t in task_rows}
            tasks_skipped_empty += sum(1 for tid, _ in pairs if tid not in contributed)

    # 3. Union + dedupe (explicit first).
    seen: set[str] = set()
    trials: list[TrialModel] = []
    for t in (*explicit_rows, *task_rows):
        if t.id in seen:
            continue
        seen.add(t.id)
        trials.append(t)
    if not trials:
        raise HTTPException(status_code=400, detail="resulting trial set is empty")

    explicit_id_set = {t.id for t in explicit_rows}
    trials_from_tasks = sum(1 for t in trials if t.id not in explicit_id_set)

    # 4. Create the collection experiment and link additively.
    last_activity = max((t.created_at for t in trials), default=None) or utcnow()
    result = ExperimentModel(
        name=name.strip() or "collection",
        org_id=org_id,
        is_collection=True,
        last_activity_at=last_activity,
    )
    session.add(result)
    await session.flush()

    linked_task_ids = list(dict.fromkeys(t.task_id for t in trials))
    for task_id in linked_task_ids:
        await _link_task_to_experiment(session, task_id=task_id, experiment_id=result.id)

    await session.execute(
        insert(experiment_trials),
        [{"experiment_id": result.id, "trial_id": t.id} for t in trials],
    )

    return TrialCollectionResponse(
        id=result.id,
        name=result.name,
        trials_linked=len(trials),
        tasks_linked=len(linked_task_ids),
        trials_from_tasks=trials_from_tasks,
        tasks_skipped_empty=tasks_skipped_empty,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/test_create_trial_collection.py -v`
Expected: PASS (existing trial-id tests + 3 new task-mode tests).

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/core/endpoints/collections.py oddish/tests/test_create_trial_collection.py
git commit -m "feat(collections): task_ids selection of current-version trials"
```

---

### Task 3: Wire `task_ids` through the backend route

**Files:**
- Modify: `backend/api/routers/tasks.py:773` (`create_trial_collection`)
- Test: `backend/tests/test_collections_route.py` (append a task-mode test)

**Interfaces:**
- Consumes: `TrialCollectionRequest.task_ids` (Task 1), `create_trial_collection_core(..., task_ids=...)` (Task 2).

- [ ] **Step 1: Write the failing test** (append to `backend/tests/test_collections_route.py`, mirroring the file's existing fixture/cleanup style: create an org + API key with `TASKS` scope, a task with a current version, and two current-version SUCCESS trials, then POST with `task_ids`)

```python
@pytest.mark.asyncio
async def test_collection_from_task_ids(client_and_key):
    client, headers, ctx = client_and_key  # existing fixture: authed client + seeded ids
    resp = await client.post(
        "/experiments/collections",
        headers=headers,
        json={"name": "from-task", "task_ids": [ctx["task_name"]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trials_linked"] == ctx["current_version_trial_count"]
    assert body["trials_from_tasks"] == ctx["current_version_trial_count"]
```

> Note for the implementer: `backend/tests/test_collections_route.py` already builds an authed client, org, API key (`APIKeyScope.TASKS`), task, and trials in its fixtures/helpers. Extend the seed to add a `TaskVersionModel`, set `task.current_version_id`, and give the trials `task_version_id = current_version_id`, `status = TrialStatus.SUCCESS`. Reuse the file's `_cleanup(...)` teardown for the new rows.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_collections_route.py::test_collection_from_task_ids -v`
Expected: FAIL — route ignores `task_ids`; `trials_linked` is 0 / 400 "resulting trial set is empty".

- [ ] **Step 3: Write minimal implementation** — add the passthrough at `backend/api/routers/tasks.py:786`:

```python
        result = await create_trial_collection_core(
            session,
            name=payload.name,
            trial_ids=payload.trial_ids,
            task_ids=payload.task_ids,
            org_id=auth.org_id,
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env && set +a && uv run pytest tests/test_collections_route.py -v`
Expected: PASS (existing route tests + new task-mode test).

- [ ] **Step 5: Commit**

```bash
git add backend/api/routers/tasks.py backend/tests/test_collections_route.py
git commit -m "feat(collections): route passes task_ids to core"
```

---

### Task 4: `oddish collect` CLI command (publish by default)

**Files:**
- Create: `oddish/src/oddish/cli/collect.py`
- Modify: `oddish/src/oddish/cli/__init__.py`
- Test: `oddish/tests/test_cli_collect.py` (create)

**Interfaces:**
- Consumes: `POST /experiments/collections` (Task 3) and existing `POST /experiments/{id}/publish`.
- Produces: `collect(tasks, trial_ids, name, publish=True, json_output=False, api_url=None)` registered as `app.command()(collect)`.

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/test_cli_collect.py
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.collect import _build_payload, _guard_sources


def test_build_payload_tasks_and_trials():
    payload = _build_payload(name="c", tasks=["taskA", "taskA"], trial_ids=["t1"])
    assert payload == {"name": "c", "task_ids": ["taskA"], "trial_ids": ["t1"]}


def test_guard_requires_a_source():
    assert _guard_sources(tasks=[], trial_ids=[]) is False
    assert _guard_sources(tasks=["a"], trial_ids=[]) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/test_cli_collect.py -v`
Expected: FAIL — `oddish.cli.collect` does not exist (ImportError).

- [ ] **Step 3: Write minimal implementation** — create `oddish/src/oddish/cli/collect.py`:

```python
from __future__ import annotations

from typing import Annotated, Optional

import httpx
import typer
from rich.console import Console

from oddish.cli.config import (
    get_api_url,
    get_auth_headers,
    get_dashboard_url,
    require_api_key,
)

console = Console()


def _dedupe(raw: list[str]) -> list[str]:
    return list(dict.fromkeys(s.strip() for s in raw if s and s.strip()))


def _guard_sources(*, tasks: list[str], trial_ids: list[str]) -> bool:
    return bool(_dedupe(tasks) or _dedupe(trial_ids))


def _build_payload(*, name: str, tasks: list[str], trial_ids: list[str]) -> dict:
    return {"name": name, "task_ids": _dedupe(tasks), "trial_ids": _dedupe(trial_ids)}


def collect(
    trial_ids: Annotated[
        Optional[list[str]],
        typer.Argument(help="Trial IDs to include (optional; combine with --task)."),
    ] = None,
    tasks: Annotated[
        Optional[list[str]],
        typer.Option("--task", "-t", help="Task id/name; links its current-version trials. Repeatable."),
    ] = None,
    name: Annotated[
        Optional[str],
        typer.Option("--name", "-n", help="Name for the collection."),
    ] = None,
    publish: Annotated[
        bool,
        typer.Option("--publish/--no-publish", help="Publish a public read-only link (default: publish)."),
    ] = True,
    json_output: Annotated[bool, typer.Option("--json", help="Print raw JSON.")] = False,
    api_url: Annotated[
        Optional[str],
        typer.Option("--api-url", "-u", help="API URL (uses configured URL if unset)."),
    ] = None,
):
    """Gather the latest trials of one or more tasks into a read-only collection.

        oddish collect --task activiti-spring-boot-3-upgrade --task struts-rest-showcase-to-spring-mvc
        oddish collect --task my-task --no-publish -n "my rollup"
    """
    tasks = tasks or []
    trial_ids = trial_ids or []
    if not _guard_sources(tasks=tasks, trial_ids=trial_ids):
        console.print("[red]Provide at least one --task or trial id.[/red]")
        raise typer.Exit(1)

    if not api_url:
        api_url = get_api_url()
    require_api_key(api_url)

    coll_name = (name or "").strip() or "collection"
    payload = _build_payload(name=coll_name, tasks=tasks, trial_ids=trial_ids)

    with httpx.Client(timeout=120.0, headers=get_auth_headers()) as client:
        try:
            resp = client.post(f"{api_url}/experiments/collections", json=payload)
        except httpx.RequestError as e:
            console.print(f"[red]Failed to connect to API:[/red] {e}")
            raise typer.Exit(1)
        if resp.status_code != 200:
            console.print(f"[red]Collect failed:[/red] {resp.status_code} - {resp.text}")
            raise typer.Exit(1)
        data = resp.json()

        public_url = None
        if publish and data.get("id"):
            pub = client.post(f"{api_url}/experiments/{data['id']}/publish")
            if pub.status_code == 200:
                public_url = pub.json().get("public_url") or pub.json().get("url")
            else:
                console.print(f"[yellow]Created, but publish failed:[/yellow] {pub.text}")

    if json_output:
        console.print_json(data={**data, "public_url": public_url})
        return

    console.print(f"[green]Created collection {data.get('id')}[/green] ({data.get('name')})")
    console.print(f"  Trials linked:      {data.get('trials_linked', 0)}")
    console.print(f"  From tasks:         {data.get('trials_from_tasks', 0)}")
    skipped = data.get("tasks_skipped_empty", 0)
    if skipped:
        console.print(f"  Tasks skipped (empty): {skipped}")
    if public_url:
        console.print("[bold]This is a public, read-only link:[/bold]")
        console.print(f"  {public_url}")
    else:
        exp_id = data.get("id")
        if exp_id:
            console.print(f"  View: {get_dashboard_url(api_url)}/experiments/{exp_id}")
```

Register in `oddish/src/oddish/cli/__init__.py`:

```python
from oddish.cli.collect import collect
# ...
app.command()(collect)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd oddish && uv run pytest tests/test_cli_collect.py -v`
Expected: PASS. Also verify wiring: `cd oddish && uv run oddish collect --help` shows the command.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/cli/collect.py oddish/src/oddish/cli/__init__.py oddish/tests/test_cli_collect.py
git commit -m "feat(cli): oddish collect — task-based read-only collections, publish by default"
```

---

## Self-Review

**Spec coverage:**
- task_ids selection → Task 2. Schema → Task 1. Route passthrough → Task 3. `collect` CLI + publish-by-default → Task 4. Selection filter → Task 2 (`_TERMINAL`, superseded, is_probe, current_version_id). Publish-orchestration → Task 4. All spec sections mapped.

**Placeholder scan:** none — every step has concrete code/commands. (Task 3's test relies on the existing fixture in `test_collections_route.py`; the note tells the implementer exactly what to seed, since that file's fixture isn't reproduced here.)

**Type consistency:** `create_trial_collection_core(..., task_ids=..., org_id=...)` signature matches between Task 2 (definition), Task 3 (call). `TrialCollectionResponse` fields (`trials_from_tasks`, `tasks_skipped_empty`) defined in Task 1, populated in Task 2, read in Task 3/4. CLI helpers `_build_payload`/`_guard_sources` defined and tested in Task 4.

## Verification

Manual end-to-end against a real key (after deploy of Tasks 1–3):
```bash
export ODDISH_API_KEY=…
oddish collect --task struts-rest-showcase-to-spring-mvc --task spring-petclinic-rest-jakarta-upgrade -n "ent smoke"
# expect: Created collection …, Trials linked > 0, public read-only link printed
```
