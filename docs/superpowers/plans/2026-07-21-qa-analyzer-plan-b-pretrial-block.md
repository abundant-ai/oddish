# QA Analyzer — Plan B: Pre-trial block + sandbox provisioning + persistence

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A once-per-task pre-trial QA analyzer, built on `AnalyzerBlock`, whose agent runs `oddish pull` inside its sandbox to fetch task source, audits verifier completeness / oracle correctness / info leakage, and emits `ActionItem`s persisted to new `TaskModel` columns — wired into the QA job behind a settings gate.

**Architecture:** Mirror the verdict-analyzer-block seam exactly. New `AnalyzerType.PRE_TRIAL` + `PreTrialBlock(Block)` (output_schema = a list wrapper of `ActionItem`), fed by a DB-registry prompt (Plan A). A dedicated sandbox provisioner installs the `oddish` Python CLI and injects a minted READ key so `oddish pull` authenticates. A `pre_trial_synth_fn` injection seam (parallel to `verdict_synth_fn`) runs it before the per-trial loop; results persist via `sync_pre_trial_to_task` (which must NOT complete the task). Registration is gated on `settings.pre_trial_via_analyzer_block`.

**Tech Stack:** Python 3.11, SQLAlchemy/Alembic, AnalyzerBlock/Daytona sandbox, Claude Code CLI, pydantic v2, pytest-asyncio.

## Global Constraints

- Depends on Plan A: `ActionItem`, `ActionItemSource`, `compute_action_item_id` (`oddish/src/oddish/analyze/models.py`); `get_active_prompt_content` (`oddish/src/oddish/core/prompts.py`); seeded `pre_trial_qa` prompt. Plan A must be merged/available first.
- `oddish/` must not import `backend/`. The `pre_trial_synth_fn` seam lives in `oddish/`, its block-backed implementation in `backend/`.
- Status columns reuse `JobStatus` (aliased `VerdictStatus`) via `SQLEnum(JobStatus)` — no new DB enum.
- `sync_pre_trial_to_task` MUST NOT set `task.status = COMPLETED` or touch verdict columns (it runs before trials).
- Core migrations in `oddish/alembic/versions/`, resolve `down_revision` from `uv run alembic heads` (multiple heads → merge first). Include an inspector guard + `downgrade()`.
- Blocks record which prompt version they ran: add `prompt_key`/`prompt_version` to `analyzer_blocks` (Task 2) and populate them (Task 5).
- Registration must be a no-op when `pre_trial_via_analyzer_block` is False (default) — the legacy path is "pre-trial analysis simply does not run."
- DB tests need a real Postgres (see Plan A Global Constraints for the throwaway-DB recipe).

## Interfaces produced (cross-task contract)

- `AnalyzerType.PRE_TRIAL = "pre_trial"` (`backend/api/services/blocks/analyzer/analyzer_block.py`)
- `PreTrialActionItems(BaseModel)` — `{ items: list[ActionItem] }` (list wrapper; `AnalyzerBlock`/`Block.parse` needs a dict-shaped schema)
- `PreTrialBlock(Block)` with `output_schema = PreTrialActionItems`, `to_action_items(raw) -> dict`
- `pre_trial_section(task_id, trial_ids, prompt_template) -> str`
- `PreTrialSynthFn = Callable[[str, list[str], float], Awaitable[Any]]`, `default_pre_trial_synth` (no-op returning `None`)
- `build_pre_trial_payload(items: list[ActionItem]) -> dict`, `sync_pre_trial_to_task(task_id, *, payload, error, should_store=None) -> None`
- `provision_oddish_sandbox_client(*, org_id, model, api_key) -> SandboxAnalyzerLLMClient` (`backend/worker/pre_trial_sandbox.py`)
- `pre_trial_block_synth`, `PreTrialBlockQaJobHandler`, `install_pre_trial_block_qa_handler()` (`backend/worker/pre_trial_synth.py`)
- settings: `pre_trial_via_analyzer_block: bool = False`, `pre_trial_model: str`

---

### Task 1: Settings + `AnalyzerType.PRE_TRIAL` + output schema

**Files:**
- Modify: `oddish/src/oddish/config.py` (add `PRE_TRIAL_MODEL` const ~line 56; `pre_trial_via_analyzer_block` ~1107; `pre_trial_model` ~1182)
- Modify: `backend/api/services/blocks/analyzer/analyzer_block.py` (add enum member ~line 26)
- Modify: `oddish/src/oddish/analyze/models.py` (add `PreTrialActionItems` wrapper)
- Test: `oddish/tests/analyze/test_pre_trial_schema.py`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/analyze/test_pre_trial_schema.py
from oddish.analyze.models import ActionItem, ActionItemSource, Dimension, ProblemType, ActionTier, PreTrialActionItems


def test_wrapper_holds_items():
    item = ActionItem(
        source=ActionItemSource.PRE_TRIAL, problem_type=ProblemType.INCOMPLETENESS,
        dimension=Dimension.VERIFIER, file="verifier.py", line_start=1, line_end=2,
        title="t", detail="d", recommendation="r", tier=ActionTier.MUST_FIX,
    )
    wrapper = PreTrialActionItems(items=[item])
    assert wrapper.items[0].dimension == Dimension.VERIFIER
    assert PreTrialActionItems().items == []


def test_analyzer_type_has_pre_trial():
    from api.services.blocks.analyzer.analyzer_block import AnalyzerType
    assert AnalyzerType.PRE_TRIAL.value == "pre_trial"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/analyze/test_pre_trial_schema.py::test_wrapper_holds_items -v`
Expected: FAIL with `ImportError: cannot import name 'PreTrialActionItems'`.

- [ ] **Step 3: Implement**

Append to `oddish/src/oddish/analyze/models.py`:

```python
class PreTrialActionItems(BaseModel):
    """List wrapper so the block's output_schema is a dict-shaped model."""

    items: list[ActionItem] = Field(default_factory=list)
```

In `backend/api/services/blocks/analyzer/analyzer_block.py`, add to `AnalyzerType` (after line 26):

```python
    PRE_TRIAL = "pre_trial"
```

In `oddish/src/oddish/config.py`, near the model constants (~line 56) add:

```python
PRE_TRIAL_MODEL = ANALYSIS_MODEL
```

next to `verdict_via_analyzer_block` (~line 1107):

```python
    # AnalyzerBlock-backed pre-trial QA audit. Gates registration only; off ->
    # pre-trial analysis does not run and no task columns are written.
    pre_trial_via_analyzer_block: bool = False
```

and next to `verdict_model` (~line 1182):

```python
    pre_trial_model: str = PRE_TRIAL_MODEL
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd oddish && uv run pytest tests/analyze/test_pre_trial_schema.py::test_wrapper_holds_items -v`
Expected: PASS.
Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/analyze/test_pre_trial_schema.py::test_analyzer_type_has_pre_trial -v` (run from a context where `api` is importable; if `api` is only importable from `backend/`, place that second test in `backend/tests/` instead).
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add oddish/src/oddish/analyze/models.py oddish/src/oddish/config.py \
        backend/api/services/blocks/analyzer/analyzer_block.py \
        oddish/tests/analyze/test_pre_trial_schema.py
git commit -m "feat(pre-trial): settings, AnalyzerType.PRE_TRIAL, output schema"
```

---

### Task 2: TaskModel pre-trial columns + analyzer_blocks prompt-version columns + migration

**Files:**
- Modify: `oddish/src/oddish/db/models.py` (5 `pre_trial*` columns on `TaskModel` ~line 791; 2 columns on `AnalyzerBlockModel` ~line 620)
- Create: `oddish/alembic/versions/prompts_002_pre_trial_columns.py`
- Test: `oddish/tests/db/test_pre_trial_columns.py`

- [ ] **Step 1: Write the failing test**

```python
# oddish/tests/db/test_pre_trial_columns.py
from oddish.db import AnalyzerBlockModel
from oddish.db.models import TaskModel


def test_task_has_pre_trial_columns():
    cols = set(TaskModel.__table__.columns.keys())
    assert {
        "pre_trial", "pre_trial_status", "pre_trial_error",
        "pre_trial_started_at", "pre_trial_finished_at",
    } <= cols


def test_analyzer_block_records_prompt_version():
    cols = set(AnalyzerBlockModel.__table__.columns.keys())
    assert {"prompt_key", "prompt_version"} <= cols
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd oddish && uv run pytest tests/db/test_pre_trial_columns.py -v`
Expected: FAIL (missing columns → AssertionError).

- [ ] **Step 3: Add the columns**

In `oddish/src/oddish/db/models.py`, after the verdict columns (line 791) add:

```python
    # Pre-trial QA analysis (task-source audit; runs before trials)
    pre_trial: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    pre_trial_status: Mapped[VerdictStatus | None] = mapped_column(
        SQLEnum(VerdictStatus), nullable=True
    )
    pre_trial_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    pre_trial_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    pre_trial_finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
```

In `AnalyzerBlockModel` (after the `prompt` column ~line 620) add:

```python
    prompt_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    prompt_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
```

- [ ] **Step 4: Run model test to verify it passes**

Run: `cd oddish && uv run pytest tests/db/test_pre_trial_columns.py -v`
Expected: PASS.

- [ ] **Step 5: Write + apply the migration**

Resolve `down_revision` via `cd oddish && uv run alembic heads` (should be `prompts_001` from Plan A, unless other heads appeared — merge if so). Create `oddish/alembic/versions/prompts_002_pre_trial_columns.py`:

```python
"""add pre-trial task columns + analyzer_blocks prompt-version columns"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "prompts_002"
down_revision: Union[str, Sequence[str], None] = "prompts_001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_col(bind, table, col) -> bool:
    return col in {c["name"] for c in sa.inspect(bind).get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _has_col(bind, "tasks", "pre_trial"):
        op.add_column("tasks", sa.Column("pre_trial", sa.dialects.postgresql.JSONB, nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_status", sa.Enum(name="jobstatus", create_type=False), nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_error", sa.Text, nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_started_at", sa.DateTime(timezone=True), nullable=True))
        op.add_column("tasks", sa.Column("pre_trial_finished_at", sa.DateTime(timezone=True), nullable=True))
    if not _has_col(bind, "analyzer_blocks", "prompt_key"):
        op.add_column("analyzer_blocks", sa.Column("prompt_key", sa.String(128), nullable=True))
        op.add_column("analyzer_blocks", sa.Column("prompt_version", sa.Integer, nullable=True))


def downgrade() -> None:
    for col in ("pre_trial_finished_at", "pre_trial_started_at", "pre_trial_error", "pre_trial_status", "pre_trial"):
        op.drop_column("tasks", col)
    op.drop_column("analyzer_blocks", "prompt_version")
    op.drop_column("analyzer_blocks", "prompt_key")
```

Run: `cd oddish && uv run alembic upgrade head` → verify `uv run alembic current` shows `prompts_002`.

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/db/models.py oddish/alembic/versions/prompts_002_pre_trial_columns.py \
        oddish/tests/db/test_pre_trial_columns.py
git commit -m "feat(db): pre-trial task columns + analyzer_blocks prompt version"
```

---

### Task 3: `PreTrialBlock` + prompt section

**Files:**
- Create: `backend/api/services/blocks/analyzer/pre_trial/__init__.py`
- Create: `backend/api/services/blocks/analyzer/pre_trial/pre_trial_prompts.py`
- Create: `backend/api/services/blocks/analyzer/pre_trial/pre_trial_block.py`
- Test: `backend/tests/test_pre_trial_block.py`

**Interfaces:**
- Consumes: `Block`, `PreTrialActionItems`
- Produces: `pre_trial_section(task_id, trial_ids, prompt_template) -> str`; `PreTrialBlock(task_id, trial_ids, prompt_template)` with `to_action_items(raw) -> dict`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pre_trial_block.py
import json

from api.services.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock


def _block():
    return PreTrialBlock(
        task_id="task_abc",
        trial_ids=["t1", "t2"],
        prompt_template="Audit the task. Run: oddish pull {task_id} --type task --include-task-files -o ./task_src",
    )


def test_prompt_interpolates_task_id():
    prompt = _block().build_prompt()
    assert "task_abc" in prompt
    assert "oddish pull task_abc" in prompt


def test_to_action_items_parses_list_wrapper():
    raw = json.dumps({"items": [{
        "source": "pre_trial", "problem_type": "incompleteness", "dimension": "verifier",
        "file": "verifier.py", "line_start": 3, "line_end": 5,
        "title": "t", "detail": "d", "recommendation": "r", "tier": "must_fix",
    }]})
    out = _block().to_action_items(raw)
    assert out["items"][0]["file"] == "verifier.py"


def test_to_action_items_tolerates_code_fences():
    raw = "```json\n{\"items\": []}\n```"
    assert _block().to_action_items(raw) == {"items": []}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_pre_trial_block.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

```python
# backend/api/services/blocks/analyzer/pre_trial/__init__.py
```

```python
# backend/api/services/blocks/analyzer/pre_trial/pre_trial_prompts.py
"""Adapt the DB-registry pre-trial prompt into the Block section contract."""

from __future__ import annotations


def pre_trial_section(task_id: str, trial_ids: list[str], prompt_template: str) -> str:
    trials = ", ".join(trial_ids) if trial_ids else "(none yet)"
    return prompt_template.format(task_id=task_id, trial_ids=trials)
```

```python
# backend/api/services/blocks/analyzer/pre_trial/pre_trial_block.py
from __future__ import annotations

from pydantic import BaseModel

from api.services.blocks.block import Block
from oddish.analyze.models import PreTrialActionItems

from . import pre_trial_prompts as pp

_SECTION_NAME = "pre_trial"
_FALLBACK_SENTINEL = f"<{_SECTION_NAME}>[unavailable]</{_SECTION_NAME}>"


class _EmptyInput(BaseModel):
    pass


class PreTrialBlock(Block):
    """Audits task source for verifier/oracle/info-leakage defects and emits
    a list of ActionItems. The agent runs ``oddish pull`` in its sandbox."""

    output_schema = PreTrialActionItems

    def __init__(self, task_id: str, trial_ids: list[str], prompt_template: str) -> None:
        self.task_id = task_id
        self.trial_ids = trial_ids
        self.prompt_template = prompt_template

    def sections(self) -> list[dict]:
        return [
            {
                "name": _SECTION_NAME,
                "raw_input": {},
                "schema": _EmptyInput,
                "formatter": lambda _d: pp.pre_trial_section(
                    self.task_id, self.trial_ids, self.prompt_template
                ),
                "fallback": _FALLBACK_SENTINEL,
            }
        ]

    def build_prompt(self) -> str:
        prompt = super().build_prompt()
        if prompt == _FALLBACK_SENTINEL:
            raise RuntimeError("pre-trial prompt degraded to fallback sentinel")
        return prompt

    def to_action_items(self, raw: str) -> dict:
        return self.parse(raw).model_dump()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_pre_trial_block.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/blocks/analyzer/pre_trial/ backend/tests/test_pre_trial_block.py
git commit -m "feat(pre-trial): PreTrialBlock + prompt section"
```

---

### Task 4: Sandbox provisioner with `oddish` CLI + minted read key

**Files:**
- Create: `backend/worker/pre_trial_sandbox.py`
- Modify: `backend/api/services/cc_chat/claude_code_runtime.py` (add `install_oddish_cli(...)` helper — mirrors `_install_harbor`)
- Test: `backend/tests/test_pre_trial_sandbox.py`

**Interfaces:**
- Consumes: `Provisioner`, `ClaudeCodeRuntime`, `SandboxAnalyzerLLMClient`, `mint_internal_read_key`
- Produces: `provision_oddish_sandbox_client(*, org_id, model, api_key, db_session_factory) -> SandboxAnalyzerLLMClient`

**Design note:** The block factory's SANDBOX branch installs only claude-code + harbor and injects no oddish key. Because the agent must run `oddish pull`, we provision our own sandbox: install the `oddish` Python CLI (mirror `_install_harbor`'s pinned-`pip install --user`), inject `ODDISH_API_KEY` (minted READ key) + `ODDISH_API_BASE_URL` + `ANTHROPIC_API_KEY`, then wrap in `SandboxAnalyzerLLMClient` and pass it into `AnalyzerBlock(client=...)`.

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_pre_trial_sandbox.py
import pytest

import backend.worker.pre_trial_sandbox as mod


class _FakeRuntime:
    def __init__(self):
        self.installed = []
    async def install(self, client, sandbox):
        self.installed.append("base")
    async def install_oddish_cli(self, client, sandbox, *, api_key, api_base_url):
        self.installed.append(("oddish", api_key, api_base_url))


class _FakeProvisioner:
    def __init__(self, *a, **k): pass
    async def create(self, **kwargs):
        _FakeProvisioner.last_env = kwargs.get("env_vars")
        return object()


@pytest.mark.asyncio
async def test_provision_mints_key_and_installs_oddish(monkeypatch):
    async def fake_mint(session, *, org_id, name, ttl_minutes):
        return ("key_id", "ok_secret")

    monkeypatch.setattr(mod, "Provisioner", _FakeProvisioner)
    monkeypatch.setattr(mod, "ClaudeCodeRuntime", _FakeRuntime)
    monkeypatch.setattr(mod, "RealDaytonaClient", lambda **k: object())
    monkeypatch.setattr(mod, "mint_internal_read_key", fake_mint)
    monkeypatch.setattr(mod, "SandboxAnalyzerLLMClient", lambda **k: ("client", k))
    monkeypatch.setenv("DAYTONA_API_KEY", "x")

    class _Ctx:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(mod, "get_session", lambda: _Ctx())

    client = await mod.provision_oddish_sandbox_client(
        org_id="org_1", model="claude-sonnet-5", api_key=None,
        api_base_url="https://api.test",
    )
    assert client[0] == "client"
    assert _FakeProvisioner.last_env["ODDISH_API_KEY"] == "ok_secret"
    assert _FakeProvisioner.last_env["ODDISH_API_BASE_URL"] == "https://api.test"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_pre_trial_sandbox.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3a: Add `install_oddish_cli` to the runtime**

In `backend/api/services/cc_chat/claude_code_runtime.py`, add a method mirroring `_install_harbor` (which does `pip install --user --quiet <pinned>`). Install the `oddish` package the same way harbor is installed (resolve a pinned requirement if one exists; otherwise `pip install --user oddish`). Add:

```python
    async def install_oddish_cli(
        self, client, sandbox, *, api_key: str, api_base_url: str
    ) -> None:
        """Install the oddish Python CLI so the agent can run `oddish pull`.
        Mirrors _install_harbor's pinned pip install; best-effort."""
        # Reuse the same pinned-requirement mechanism harbor uses; fall back to
        # the plain package name if no pin is resolvable.
        try:
            from oddish.workers.agents.claude_code import _pinned_oddish_requirement
            requirement = _pinned_oddish_requirement()
        except Exception:
            requirement = "oddish"
        await client.exec_async(
            sandbox, command=f"pip install --user --quiet {requirement}"
        )
```

If `_pinned_oddish_requirement` does not exist, add one alongside `_pinned_harbor_requirement` in `oddish/src/oddish/workers/agents/claude_code.py` that returns the installable spec for the oddish package (e.g. a version pin or a VCS/wheel reference used in this deployment). Confirm the exact install spec against how harbor is pinned before finalizing.

- [ ] **Step 3b: Add the provisioner**

```python
# backend/worker/pre_trial_sandbox.py
"""Provision a sandbox that can run `oddish pull`: install the oddish CLI and
inject a short-lived READ key. Returns a SandboxAnalyzerLLMClient to pass into
AnalyzerBlock(client=...)."""

from __future__ import annotations

import os

from api.services.blocks.analyzer.analyzer_llm_client import (
    SandboxAnalyzerLLMClient,
    resolve_analyzer_api_key,
)
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.provisioning import Provisioner  # adjust import to real path
from daytona_client import RealDaytonaClient  # adjust import to real path
from oddish.core.api_keys import mint_internal_read_key
from oddish.db import get_session
from oddish.db.models import generate_id

_TTL_MINUTES = 45


async def provision_oddish_sandbox_client(
    *, org_id: str, model: str, api_key: str | None, api_base_url: str
) -> SandboxAnalyzerLLMClient:
    async with get_session() as session:
        _, raw_key = await mint_internal_read_key(
            session, org_id=org_id, name=f"pre-trial:{generate_id()}", ttl_minutes=_TTL_MINUTES
        )

    daytona_client = RealDaytonaClient(api_key=os.environ["DAYTONA_API_KEY"])
    env_vars = {
        "ANTHROPIC_API_KEY": resolve_analyzer_api_key(api_key) or "",
        "ODDISH_API_KEY": raw_key,
        "ODDISH_API_BASE_URL": api_base_url,
    }
    if model:
        env_vars["ANTHROPIC_MODEL"] = model
    sandbox = await Provisioner(client=daytona_client).create(
        env_vars=env_vars,
        auto_stop_minutes=15,
        auto_delete_minutes=30,
        labels={"app": "pre-trial", "session_id": generate_id()},
        daytona_session_id="pre-trial",
    )
    runtime = ClaudeCodeRuntime()
    await runtime.install(daytona_client, sandbox)
    await runtime.install_oddish_cli(
        daytona_client, sandbox, api_key=raw_key, api_base_url=api_base_url
    )
    return SandboxAnalyzerLLMClient(
        sandbox=sandbox, daytona_client=daytona_client, runtime=runtime
    )
```

Resolve the real import paths for `Provisioner` and `RealDaytonaClient` from `backend/api/services/blocks/analyzer/analyzer_llm_client.py` (the `create_llm_client` SANDBOX branch imports them) before running — adjust the two `# adjust import` lines to match.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_pre_trial_sandbox.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/worker/pre_trial_sandbox.py \
        backend/api/services/cc_chat/claude_code_runtime.py \
        backend/tests/test_pre_trial_sandbox.py
git commit -m "feat(pre-trial): sandbox provisioner with oddish CLI + minted key"
```

---

### Task 5: `pre_trial_block_synth` + persistence + handler

**Files:**
- Create: `backend/worker/pre_trial_synth.py`
- Modify: `oddish/src/oddish/core/verdict_sync.py` (add `build_pre_trial_payload` + `sync_pre_trial_to_task`)
- Modify: `oddish/src/oddish/workers/queue/qa_handler.py` (`PreTrialSynthFn` type, `default_pre_trial_synth`, thread through `run_task_qa_job`)
- Test: `backend/tests/test_pre_trial_sync.py` (real DB), `backend/tests/test_pre_trial_synth.py`

**Interfaces:**
- Produces: `build_pre_trial_payload(items) -> dict`; `sync_pre_trial_to_task(task_id, *, payload, error, should_store=None) -> None`; `PreTrialSynthFn`; `default_pre_trial_synth`; `pre_trial_block_synth(task_id, trial_ids, timeout) -> list[ActionItem] | None`; `PreTrialBlockQaJobHandler`; `install_pre_trial_block_qa_handler() -> bool`

- [ ] **Step 1: Write the failing tests**

```python
# backend/tests/test_pre_trial_sync.py
import uuid

import pytest

from oddish.analyze.models import ActionItem, ActionItemSource, Dimension, ProblemType, ActionTier
from oddish.core.verdict_sync import build_pre_trial_payload, sync_pre_trial_to_task
from oddish.db import get_session
from oddish.db.models import TaskModel, JobStatus


def _item():
    return ActionItem(
        source=ActionItemSource.PRE_TRIAL, problem_type=ProblemType.MISMATCH,
        dimension=Dimension.ORACLE, file="solution.py", line_start=1, line_end=1,
        title="t", detail="d", recommendation="r", tier=ActionTier.SHOULD_FIX,
    )


def test_payload_assigns_ids():
    payload = build_pre_trial_payload([_item()])
    assert payload["items"][0]["id"]  # computed
    assert payload["items"][0]["dimension"] == "oracle"


@pytest.mark.asyncio
async def test_sync_writes_columns_without_completing_task():
    task_id = f"task_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(TaskModel(id=task_id, status="running"))  # adjust required fields to real TaskModel
        await session.commit()
    try:
        await sync_pre_trial_to_task(task_id, payload=build_pre_trial_payload([_item()]), error=None)
        async with get_session() as session:
            task = await session.get(TaskModel, task_id)
            assert task.pre_trial_status == JobStatus.SUCCESS
            assert task.pre_trial["items"][0]["file"] == "solution.py"
            assert task.status != "completed"  # pre-trial must not complete the task
    finally:
        async with get_session() as session:
            await session.execute(TaskModel.__table__.delete().where(TaskModel.id == task_id))
            await session.commit()
```

```python
# backend/tests/test_pre_trial_synth.py
import backend.worker.pre_trial_synth as mod


def test_install_is_gated_off_by_default(monkeypatch):
    monkeypatch.setattr(mod.settings, "pre_trial_via_analyzer_block", False)
    assert mod.install_pre_trial_block_qa_handler() is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_pre_trial_sync.py tests/test_pre_trial_synth.py -v`
Expected: FAIL (import errors).

- [ ] **Step 3a: Persistence helpers**

Append to `oddish/src/oddish/core/verdict_sync.py` (reuse its existing imports: `get_session`, `TaskModel`, `VerdictStatus`, `utcnow`):

```python
def build_pre_trial_payload(items) -> dict:
    """Render the dict stored on tasks.pre_trial. Computes stable ids."""
    from oddish.analyze.models import compute_action_item_id

    out = []
    for item in items:
        item.id = item.id or compute_action_item_id(item)
        out.append(item.model_dump(mode="json"))
    return {"items": out}


async def sync_pre_trial_to_task(task_id, *, payload, error, should_store=None) -> None:
    """Write the pre-trial columns. Never completes the task or touches verdict."""
    async with get_session() as session:
        task = await session.get(TaskModel, task_id, with_for_update=True)
        if task is None:
            return
        if should_store is not None and not should_store():
            return
        if error is None:
            task.pre_trial = payload
            task.pre_trial_status = VerdictStatus.SUCCESS
            task.pre_trial_error = None
        else:
            task.pre_trial_status = VerdictStatus.FAILED
            task.pre_trial_error = str(error)
        task.pre_trial_finished_at = utcnow()
        await session.commit()
```

- [ ] **Step 3b: Injection seam in the QA handler**

In `oddish/src/oddish/workers/queue/qa_handler.py`, after `default_verdict_synth` (~line 58) add:

```python
PreTrialSynthFn = Callable[[str, list[str], float], Awaitable[Any]]


async def default_pre_trial_synth(task_id: str, trial_ids: list[str], timeout: float) -> Any:
    """Legacy default: pre-trial analysis does not run."""
    return None
```

Add a `pre_trial_synth_fn: PreTrialSynthFn = default_pre_trial_synth` parameter to `run_task_qa_job` (signature ~line 189-195). Inside the `try:` block, after `live_trials` is loaded (~line 231) and before the per-trial `to_classify` loop (~line 262), add:

```python
        pre_trial_items = await pre_trial_synth_fn(
            task_id, [t.id for t in live_trials], timeout
        )
        if pre_trial_items is not None:
            await sync_pre_trial_to_task(
                task_id,
                payload=build_pre_trial_payload(pre_trial_items),
                error=None,
                should_store=lambda: not _job_cancelled(queue_key),  # match existing cancel check
            )
```

Wrap in a local `try/except` that calls `sync_pre_trial_to_task(..., payload=None, error=exc)` on failure so a pre-trial error never blocks the verdict path. Import `build_pre_trial_payload, sync_pre_trial_to_task` at the top from `oddish.core.verdict_sync`. Also add a `pre_trial_synth_fn` class attribute to the `QaJobHandler` in `oddish/src/oddish/workers/jobs/handlers.py` (mirror how `verdict_synth_fn` is set and passed into `run_task_qa_job`).

- [ ] **Step 3c: Block-backed synth + handler**

```python
# backend/worker/pre_trial_synth.py
"""AnalyzerBlock-backed pre-trial synthesis, mirroring verdict_synth.py."""

from __future__ import annotations

import asyncio

from api.services.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from api.services.blocks.analyzer.analyzer_llm_client import LLMClientType
from api.services.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock
from backend.worker.pre_trial_sandbox import provision_oddish_sandbox_client
from oddish.analyze.models import ActionItem
from oddish.config import settings
from oddish.core.prompts import get_active_prompt_content
from oddish.db import get_session
from oddish.workers.jobs.handlers import QaJobHandler
from oddish.workers.queue.qa_handler import default_pre_trial_synth  # noqa: F401 (type anchor)


async def pre_trial_block_synth(task_id: str, trial_ids: list[str], timeout: float):
    async with get_session() as session:
        prompt_template = await get_active_prompt_content(session, "pre_trial_qa")

    block_obj = PreTrialBlock(task_id=task_id, trial_ids=trial_ids, prompt_template=prompt_template)
    client = await provision_oddish_sandbox_client(
        org_id=_resolve_org_id(task_id),  # implement: read task.org_id
        model=settings.pre_trial_model,
        api_key=None,
        api_base_url=settings.public_api_base_url,  # confirm the real settings attr
    )
    try:
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.PRE_TRIAL,
            llm_client_type=LLMClientType.SANDBOX,
            input=AnalyzerInput(input={"task_id": task_id, "trial_ids": trial_ids}),
            prompt=block_obj.build_prompt(),
            model=settings.pre_trial_model,
            output_transform=block_obj.to_action_items,
            client=client,
            block_metadata={"prompt_key": "pre_trial_qa"},
        )
        result = await asyncio.wait_for(block.run(), timeout=timeout)
    finally:
        await client.aclose()

    data = result.output or {"items": []}
    return [ActionItem(**it) for it in data.get("items", [])]


class PreTrialBlockQaJobHandler(QaJobHandler):
    pre_trial_synth_fn = staticmethod(pre_trial_block_synth)


def install_pre_trial_block_qa_handler() -> bool:
    if not settings.pre_trial_via_analyzer_block:
        return False
    from oddish.workers.jobs.handlers import register

    register(PreTrialBlockQaJobHandler(), override=True)
    return True
```

Implement `_resolve_org_id(task_id)` (load `TaskModel.org_id`) and confirm the settings attribute for the public API base URL (grep `public_api_base_url` in `config.py`/orchestrator). Record `prompt_version`: fetch the active version number alongside the content and set it on `block_metadata`/the block's `prompt_version` before `run()` (extend `AnalyzerBlock.save_to_db` to write `prompt_key`/`prompt_version` from `block_metadata`, or add explicit constructor args — smallest change: read them from `block_metadata` in `save_to_db`).

- [ ] **Step 3d: Register the handler**

In `backend/worker/functions.py`, next to the verdict handler registration (~line 123-126) add:

```python
from .pre_trial_synth import install_pre_trial_block_qa_handler

if install_pre_trial_block_qa_handler():
    console.print("[dim]qa: pre-trial-via-analyzer-block handler registered[/dim]")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_pre_trial_sync.py tests/test_pre_trial_synth.py -v`
Expected: PASS. (Adjust the `TaskModel(...)` construction in `test_pre_trial_sync.py` to whatever required NOT-NULL columns the real model has.)

- [ ] **Step 5: Commit**

```bash
git add backend/worker/pre_trial_synth.py oddish/src/oddish/core/verdict_sync.py \
        oddish/src/oddish/workers/queue/qa_handler.py oddish/src/oddish/workers/jobs/handlers.py \
        backend/worker/functions.py backend/tests/test_pre_trial_sync.py backend/tests/test_pre_trial_synth.py
git commit -m "feat(pre-trial): block synth, persistence, gated handler registration"
```

---

### Task 6: Record prompt version on the block row + end-to-end gate check

**Files:**
- Modify: `backend/api/services/blocks/analyzer/analyzer_block.py` (`save_to_db` writes `prompt_key`/`prompt_version` from `block_metadata`)
- Modify: `backend/worker/pre_trial_synth.py` (fetch + pass active version)
- Test: `backend/tests/test_analyzer_block_prompt_version.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_analyzer_block_prompt_version.py
from api.services.blocks.analyzer.analyzer_block import _block_row_kwargs  # add this pure helper


def test_block_row_includes_prompt_version():
    kwargs = _block_row_kwargs(
        block_metadata={"prompt_key": "pre_trial_qa", "prompt_version": 3, "model": "m"}
    )
    assert kwargs["prompt_key"] == "pre_trial_qa"
    assert kwargs["prompt_version"] == 3
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_analyzer_block_prompt_version.py -v`
Expected: FAIL (`ImportError`).

- [ ] **Step 3: Implement**

Refactor the `AnalyzerBlockModel(...)` construction inside `save_to_db` to build its kwargs via a small pure helper so it is unit-testable, and pull `prompt_key`/`prompt_version` out of `block_metadata`:

```python
def _block_row_kwargs(*, block_metadata: dict | None, **base) -> dict:
    md = block_metadata or {}
    base["prompt_key"] = md.get("prompt_key")
    base["prompt_version"] = md.get("prompt_version")
    base["block_metadata"] = block_metadata
    return base
```

Use it in `save_to_db` when constructing `AnalyzerBlockModel(**_block_row_kwargs(block_metadata=self.block_metadata, id=..., analyzer_id=..., ...))`. In `pre_trial_block_synth` (Task 5), fetch the active version number and put it in `block_metadata`:

```python
    async with get_session() as session:
        prompt, ver = await get_prompt_core(session, "pre_trial_qa")
        prompt_template = ver.content
        active_version = ver.version
    # ... block_metadata={"prompt_key": "pre_trial_qa", "prompt_version": active_version}
```

(Import `get_prompt_core` and drop the earlier `get_active_prompt_content` call.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && set -a && source .env.local && set +a && uv run pytest tests/test_analyzer_block_prompt_version.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/blocks/analyzer/analyzer_block.py backend/worker/pre_trial_synth.py \
        backend/tests/test_analyzer_block_prompt_version.py
git commit -m "feat(pre-trial): record prompt key+version on analyzer_blocks row"
```

---

## Self-Review

**Spec coverage (Component 2 + Component 6):**
- `AnalyzerType.PRE_TRIAL` + `PreTrialBlock` (output_schema = list wrapper) → Tasks 1, 3. ✓
- Agent runs `oddish pull` in a Bash sandbox with oddish CLI + scoped key → Task 4. ✓
- Prompt from the DB registry, active version recorded → Tasks 5, 6. ✓
- New TaskModel columns + own sync that does not complete the task → Tasks 2, 5. ✓
- `pre_trial_synth_fn` seam mirroring verdict, gated on a setting, runs once before the loop → Tasks 1, 5. ✓

**Placeholder scan:** Intentional fill-ins requiring live-repo confirmation (each with an explicit resolution step, not a silent TODO): the `oddish` install spec in `install_oddish_cli` (mirror harbor's pin), the `Provisioner`/`RealDaytonaClient` import paths (copy from `create_llm_client`), the public-API-base-URL settings attribute, and required NOT-NULL `TaskModel` fields for the sync test's fixture row. `down_revision` resolved via `alembic heads`.

**Type consistency:** `PreTrialSynthFn = Callable[[str, list[str], float], Awaitable[Any]]` matches the call in `run_task_qa_job` and `pre_trial_block_synth(task_id, trial_ids, timeout)`. `to_action_items`/`build_pre_trial_payload` both operate on `{"items": [...]}`. `pre_trial_via_analyzer_block`/`pre_trial_model` names consistent across config, synth, and handler.

**Open risk carried to review:** installing the full `oddish` Python CLI into the Daytona sandbox is the least-proven step (harbor is installed the same way, so the mechanism exists, but the exact pinned spec must be confirmed). If it proves impractical, the fallback is the handler pre-pulling task files and mounting them (Plan-time decision was agent-runs-pull; revisit only if install is blocked).
