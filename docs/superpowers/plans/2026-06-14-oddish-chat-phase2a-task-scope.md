# Oddish Chat — Phase 2a: Task-Scope Version-Aware Trial Logs — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `task` chat scope that loads a task's trial-log files from S3 into the Claude Code sandbox, organized by task version, defaulting attention to the current version while keeping past-version folders available.

**Architecture:** Builds on Phase 1 (PR #306). `orchestrator.start()` for the new `task` scope queries the task's trials grouped by version (Postgres), downloads each trial's files from object storage, uploads them into the sandbox workspace under `jobs/v{version}/{trial_id}/…`, and renders a version-aware `CLAUDE.md` that points Claude at the current version by default. "On-demand retrieval" is satisfied by Claude reading workspace files with its own tools (not prompt-stuffing); a total-bytes cap guards against oversized uploads.

**Tech Stack:** async SQLAlchemy, oddish `StorageClient` (S3), Daytona SDK, pytest-asyncio against local Postgres + a fake storage client.

**Spec:** `docs/superpowers/specs/2026-06-14-oddish-chat-design.md` (Scope 2). **Phase 1 plan:** `docs/superpowers/plans/2026-06-14-oddish-chat-phase1-backbone.md`.

---

## Conventions (same as Phase 1)
- `backend/` is on sys.path: `from models import ...`, `from api.services.cc_chat... import ...`. Core: `from oddish.db import get_session`, `from oddish.db.storage import get_storage_client, resolve_trial_s3_prefix`, `from oddish.core.endpoints import get_task_for_org_core`.
- **Test DB (throwaway, native Postgres):** `cd backend && ODDISH_DATABASE_URL='postgresql+asyncpg://oddish:oddish@localhost:5432/oddish_chat_test' uv run pytest <path> -v`. Tests `drop schema public cascade` — never the dev DB.
- Enums are plain `String` columns; adding an enum value needs NO migration.

## File structure
```
backend/
├── models.py                                   # MODIFY: add `task` to ChatScopeKind
├── api/services/cc_chat/
│   ├── file_loader.py                          # NEW (ported): upload_files
│   ├── task_files.py                           # NEW: collect_task_version_files
│   ├── claude_md.py                            # MODIFY: add render_task_chat_claude_md
│   └── orchestrator.py                         # MODIFY: start() handles scope_kind == "task"
└── tests/cc_chat/
    ├── conftest.py                             # MODIFY: add seed_task_with_trials helper
    ├── test_task_files.py                      # NEW
    ├── test_claude_md_task.py                  # NEW
    └── test_orchestrator_start_task.py         # NEW
```

---

## Task 1: Port `file_loader.py` (upload_files)

**Files:** Create `backend/api/services/cc_chat/file_loader.py`

- [ ] **Step 1: Copy + adapt**
```bash
cp /Users/kateyeh/Developer/os_repos/agent-sandbox-service/src/agent_sandbox/services/sandbox/file_loader.py \
   /Users/kateyeh/Developer/os_repos/oddish/backend/api/services/cc_chat/file_loader.py
```
Edit the one import: `from agent_sandbox.services.sandbox.daytona_client import CreatedSandbox, DaytonaClient` → `from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient`. Everything else (stdlib `asyncio`, `Iterable`) is unchanged. The function is `async def upload_files(client, sandbox, *, files: Iterable[tuple[str, bytes]], workspace_root: str, concurrency: int = 8) -> None`.

- [ ] **Step 2: Verify import**
Run: `cd /Users/kateyeh/Developer/os_repos/oddish/backend && uv run python -c "from api.services.cc_chat.file_loader import upload_files; print('ok')"` → `ok`.

- [ ] **Step 3: Commit**
```bash
cd /Users/kateyeh/Developer/os_repos/oddish
git add backend/api/services/cc_chat/file_loader.py
git commit -m "feat(cc_chat): port upload_files helper"
```

---

## Task 2: Add `task` scope to ChatScopeKind

**Files:** Modify `backend/models.py` (the `ChatScopeKind` enum from Phase 1)

- [ ] **Step 1: Add the enum value**
In `backend/models.py`, change `ChatScopeKind` to add a `task` member (keep the existing lowercase-member comment):
```python
class ChatScopeKind(str, Enum):
    experiment = "experiment"
    task_probes = "task_probes"
    task = "task"
```

- [ ] **Step 2: Verify (no migration needed — scope_kind is a String column)**
Run: `cd /Users/kateyeh/Developer/os_repos/oddish/backend && uv run python -c "import models; print(models.ChatScopeKind.task.value)"` → `task`.

- [ ] **Step 3: Commit**
```bash
cd /Users/kateyeh/Developer/os_repos/oddish
git add backend/models.py
git commit -m "feat(cc_chat): add 'task' chat scope kind"
```

---

## Task 3: `collect_task_version_files` — trials grouped by version + their S3 files

**Files:** Create `backend/api/services/cc_chat/task_files.py`; modify `backend/tests/cc_chat/conftest.py`; create `backend/tests/cc_chat/test_task_files.py`

- [ ] **Step 1: Add a seed helper to `conftest.py`**
Append to `backend/tests/cc_chat/conftest.py`:
```python
async def seed_task_with_trials(maker, *, task_id="task_1", versions=(1, 2), trials_per_version=1):
    """Seed a task, its task_versions, current_version pointer, and trials.
    Returns {version_int: [trial_id, ...]}. Trials get trial_s3_key=None so
    resolve_trial_s3_prefix falls back to tasks/{task_id}/trials/{trial_id}/."""
    from oddish.db.models import TaskModel, TaskVersionModel, TrialModel
    out: dict[int, list[str]] = {}
    async with maker() as s:
        s.add(TaskModel(id=task_id, name="demo-task", org_id=ORG, status="completed"))
        await s.flush()
        latest_version_id = None
        for v in versions:
            vid = f"{task_id}-v{v}"
            s.add(TaskVersionModel(id=vid, task_id=task_id, version=v))
            latest_version_id = vid if (latest_version_id is None or v == max(versions)) else latest_version_id
            ids = []
            for i in range(trials_per_version):
                tid = f"{task_id}-{v}{i}"
                s.add(TrialModel(
                    id=tid, name=tid, task_id=task_id, task_version_id=vid,
                    org_id=ORG, status="success",
                ))
                ids.append(tid)
            out[v] = ids
        # point current_version at the max version
        max_v = max(versions)
        await s.flush()
        task = await s.get(TaskModel, task_id)
        task.current_version_id = f"{task_id}-v{max_v}"
        await s.commit()
    return out
```
NOTE for the implementer: `TaskModel`/`TrialModel` have several columns with DB-level requirements. Before relying on the snippet above, run a quick check that these minimal inserts satisfy NOT NULL constraints — `cd backend && ODDISH_DATABASE_URL='...oddish_chat_test' uv run python -c "..."` constructing one row — and add any missing required fields (e.g. `TaskModel` may need a `user` or `task_path`; inspect `oddish/src/oddish/db/models.py:409` `TaskModel` for `nullable=False` columns without a default and set them). Mirror the raw-INSERT seeding in `backend/tests/test_browse_search.py` if ORM seeding is fiddly. Keep the returned shape `{version: [trial_id,...]}`.

- [ ] **Step 2: Write the failing test — `backend/tests/cc_chat/test_task_files.py`**
```python
import pytest
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from api.services.cc_chat.task_files import collect_task_version_files

pytestmark = pytest.mark.asyncio


class _FakeStorage:
    """Returns one file per trial prefix."""
    def __init__(self):
        self.downloads = 0
    async def list_keys(self, prefix):
        return [f"{prefix}trial.log", f"{prefix}agent/output.txt"]
    async def download_bytes(self, key):
        self.downloads += 1
        return b"x" * 10


async def test_collects_trials_grouped_by_version_with_files(db):
    seeded = await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)
    storage = _FakeStorage()
    async with db() as s:
        current_version, version_trials, files, truncated = await collect_task_version_files(
            s, storage, task_id="task_1", org_id=ORG,
        )
    assert current_version == 2
    assert set(version_trials.keys()) == {1, 2}
    # each trial contributes 2 files, organized under jobs/v{version}/{trial_id}/
    paths = {rel for rel, _ in files}
    assert any(p.startswith("jobs/v2/") and p.endswith("trial.log") for p in paths)
    assert any(p.startswith("jobs/v1/") for p in paths)
    assert truncated is False


async def test_respects_byte_cap(db):
    await seed_task_with_trials(db, versions=(1,), trials_per_version=1)
    storage = _FakeStorage()
    async with db() as s:
        _, _, files, truncated = await collect_task_version_files(
            s, storage, task_id="task_1", org_id=ORG, max_total_bytes=5,
        )
    assert truncated is True
    assert len(files) == 0  # cap hit before the first 10-byte file
```

- [ ] **Step 3: Run — expect ImportError**
Run: `cd backend && ODDISH_DATABASE_URL='postgresql+asyncpg://oddish:oddish@localhost:5432/oddish_chat_test' uv run pytest tests/cc_chat/test_task_files.py -v` → FAIL (module missing).

- [ ] **Step 4: Implement `backend/api/services/cc_chat/task_files.py`**
```python
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.core.endpoints import get_task_for_org_core
from oddish.db.models import TaskVersionModel, TrialModel
from oddish.db.storage import resolve_trial_s3_prefix

# Heavy/binary noise that wastes upload time and adds no chat value.
_SKIP_PARTS = frozenset({"node_modules", "__pycache__", "sessions", "backups", "skills"})


def _should_skip(rel: str) -> bool:
    return any(part in _SKIP_PARTS for part in rel.split("/"))


async def collect_task_version_files(
    session: AsyncSession,
    storage,
    *,
    task_id: str,
    org_id: str | None,
    max_total_bytes: int = 50_000_000,
) -> tuple[int | None, dict[int, list[str]], list[tuple[str, bytes]], bool]:
    """Return (current_version, {version: [trial_id,...]}, files, truncated).

    `files` are (workspace_rel_path, bytes) tuples laid out as
    jobs/v{version}/{trial_id}/{rel}. Org-scoped. Stops once the running byte
    total would exceed max_total_bytes (sets truncated=True)."""
    task = await get_task_for_org_core(
        session, task_id=task_id, org_id=org_id, load_current_version=True
    )
    current_version = task.current_version.version if task.current_version else None

    version_rows = (
        await session.execute(
            select(TaskVersionModel)
            .where(TaskVersionModel.task_id == task.id)
            .order_by(TaskVersionModel.version.desc())
        )
    ).scalars().all()

    version_trials: dict[int, list[str]] = {}
    files: list[tuple[str, bytes]] = []
    total = 0
    truncated = False

    for v in version_rows:
        trial_q = select(TrialModel).where(
            TrialModel.task_id == task.id,
            TrialModel.task_version_id == v.id,
            TrialModel.superseded_by_trial_id.is_(None),
        )
        if org_id is not None:
            trial_q = trial_q.where(TrialModel.org_id == org_id)
        trials = (await session.execute(trial_q)).scalars().all()
        version_trials[v.version] = [t.id for t in trials]

        for t in trials:
            prefix = resolve_trial_s3_prefix(
                t.id, trial_s3_key=t.trial_s3_key, trial_result_path=t.harbor_result_path
            )
            for key in await storage.list_keys(prefix):
                rel = key[len(prefix):]
                if not rel or _should_skip(rel):
                    continue
                if total >= max_total_bytes:
                    truncated = True
                    break
                data = await storage.download_bytes(key)
                total += len(data)
                files.append((f"jobs/v{v.version}/{t.id}/{rel}", data))
            if truncated:
                break
        if truncated:
            break

    return current_version, version_trials, files, truncated
```

- [ ] **Step 5: Run — expect 2 passed**
Run: `cd backend && ODDISH_DATABASE_URL='postgresql+asyncpg://oddish:oddish@localhost:5432/oddish_chat_test' uv run pytest tests/cc_chat/test_task_files.py -v`

- [ ] **Step 6: Commit**
```bash
cd /Users/kateyeh/Developer/os_repos/oddish
git add backend/api/services/cc_chat/task_files.py backend/tests/cc_chat/conftest.py backend/tests/cc_chat/test_task_files.py
git commit -m "feat(cc_chat): collect task trials grouped by version with S3 files"
```

---

## Task 4: `render_task_chat_claude_md` (version-aware template)

**Files:** Modify `backend/api/services/cc_chat/claude_md.py`; create `backend/tests/cc_chat/test_claude_md_task.py`

- [ ] **Step 1: Write the failing test — `backend/tests/cc_chat/test_claude_md_task.py`**
```python
from api.services.cc_chat.claude_md import render_task_chat_claude_md


def test_task_chat_claude_md_focuses_current_version_and_lists_all():
    out = render_task_chat_claude_md(
        task_name="rust-compiler",
        current_version=2,
        version_trials={2: ["task_1-20"], 1: ["task_1-10"]},
    )
    assert "rust-compiler" in out
    assert "v2" in out and "v1" in out
    assert "task_1-20" in out and "task_1-10" in out
    # current version is called out as the default focus
    assert "current" in out.lower()
```

- [ ] **Step 2: Run — expect ImportError/fail**
Run: `cd backend && uv run pytest tests/cc_chat/test_claude_md_task.py -v` (no DB needed).

- [ ] **Step 3: Implement — append to `backend/api/services/cc_chat/claude_md.py`**
```python
_TASK_CHAT_TEMPLATE = """# Task chat — {task_name}

You are helping investigate the trial runs for this task. The trial logs are
already in your workspace under `jobs/v<version>/<trial_id>/...`.

**Current version: v{current_version} — focus here by default.** Past versions
are also available in their own `jobs/v<N>/` folders; only look at them if the
user asks about earlier runs or a comparison across versions.

Each `jobs/v<version>/<trial_id>/` folder contains the usual Harbor trial tree
(`config.json`, `result.json`, `trial.log`, `exception.txt`, `agent/`,
`verifier/`). Read files on demand rather than assuming their contents.

## Versions and trials in this workspace
{version_block}
"""


def render_task_chat_claude_md(
    *, task_name: str, current_version: int | None, version_trials: dict[int, list[str]]
) -> str:
    lines: list[str] = []
    for v in sorted(version_trials, reverse=True):
        tag = " (current)" if v == current_version else ""
        lines.append(f"- **v{v}**{tag}:")
        trials = sorted(version_trials[v])
        if trials:
            lines.extend(f"  - `{tid}`" for tid in trials)
        else:
            lines.append("  - _(no trials)_")
    version_block = "\n".join(lines) if lines else _EMPTY_TRIAL_BLOCK
    return _TASK_CHAT_TEMPLATE.format(
        task_name=task_name,
        current_version=current_version if current_version is not None else "?",
        version_block=version_block,
    )
```

- [ ] **Step 4: Run — expect PASS**
Run: `cd backend && uv run pytest tests/cc_chat/test_claude_md_task.py -v`

- [ ] **Step 5: Commit**
```bash
cd /Users/kateyeh/Developer/os_repos/oddish
git add backend/api/services/cc_chat/claude_md.py backend/tests/cc_chat/test_claude_md_task.py
git commit -m "feat(cc_chat): version-aware task chat CLAUDE.md template"
```

---

## Task 5: Wire `orchestrator.start()` for the `task` scope

**Files:** Modify `backend/api/services/cc_chat/orchestrator.py`; create `backend/tests/cc_chat/test_orchestrator_start_task.py`

- [ ] **Step 1: Update `start()` to handle `scope_kind == "task"`**
At the top of `start()` (replacing the current `if scope_kind == "experiment": ... else: render_task_probes...` block that renders with `trial_ids=[]`), branch into three scopes. For the `task` scope, collect version-organized files + render the version-aware CLAUDE.md; build a `files` list to upload. Add imports at the top of the file:
```python
from api.services.cc_chat.claude_md import (
    render_experiment_claude_md,
    render_task_chat_claude_md,
    render_task_probes_claude_md,
)
from api.services.cc_chat.file_loader import upload_files
from api.services.cc_chat.task_files import collect_task_version_files
```
Replace the scope/CLAUDE.md block with:
```python
        files: list[tuple[str, bytes]] = []
        if scope_kind == "experiment":
            claude_md = render_experiment_claude_md(experiment_id=scope_id, trial_ids=[])
        elif scope_kind == "task":
            async with self._db(db_session_factory) as db:
                current_version, version_trials, files, truncated = await collect_task_version_files(
                    db, self._blob, task_id=scope_id, org_id=org_id,
                )
            if truncated:
                log.warning("cc_chat task-scope upload truncated at byte cap: task=%s", scope_id)
            claude_md = render_task_chat_claude_md(
                task_name=scope_id,
                current_version=current_version,
                version_trials=version_trials,
            )
        else:  # task_probes
            claude_md = render_task_probes_claude_md(task_name=scope_id, trial_ids=[])
```
Then, in the provisioning `try:` block (where it currently uploads only `CLAUDE.md`), upload the collected files BEFORE the CLAUDE.md write:
```python
            await self._runtime.install(self._daytona, sandbox)
            if files:
                await upload_files(
                    self._daytona, sandbox, files=files, workspace_root=WORKSPACE_ROOT,
                )
            await self._daytona.upload_file(
                sandbox,
                dest_path=f"{WORKSPACE_ROOT}/CLAUDE.md",
                content=claude_md.encode("utf-8"),
            )
```
Remove the now-obsolete `# TODO(phase2)` comment about file sync.

- [ ] **Step 2: Write the integration test — `backend/tests/cc_chat/test_orchestrator_start_task.py`**
Uses fakes for daytona + the same `_FakeStorage` shape as Task 3, against the real DB.
```python
import pytest
from contextlib import asynccontextmanager
from tests.cc_chat.conftest import seed_task_with_trials, ORG
from api.services.cc_chat.orchestrator import ChatOrchestrator
from api.services.cc_chat.transcript_buffer import SessionTranscriptBuffer

pytestmark = pytest.mark.asyncio


class _FakeSandbox:
    id = "sbx_task"


class _FakeDaytona:
    def __init__(self):
        self.uploaded: list[str] = []
    async def create_sandbox(self, *, env_vars, auto_stop_minutes):
        return _FakeSandbox()
    async def create_session(self, sandbox, *, session_id):
        return None
    async def upload_file(self, sandbox, *, dest_path, content):
        self.uploaded.append(dest_path)
    async def delete_sandbox(self, sandbox):
        return None


class _FakeRuntime:
    async def install(self, daytona, sandbox):
        return None


class _FakeStorage:
    async def list_keys(self, prefix):
        return [f"{prefix}trial.log"]
    async def download_bytes(self, key):
        return b"log-bytes"


async def test_start_task_scope_uploads_versioned_files_and_claude_md(db):
    await seed_task_with_trials(db, versions=(1, 2), trials_per_version=1)

    def factory():
        @asynccontextmanager
        async def _cm():
            async with db() as s:
                yield s
        return _cm()

    daytona = _FakeDaytona()
    orch = ChatOrchestrator(
        daytona=daytona,
        runtime=_FakeRuntime(),
        transcript_buffer=SessionTranscriptBuffer(),
        anthropic_api_key="test",
        blob_store=_FakeStorage(),
    )
    session_id = await orch.start(
        org_id=ORG, user_id="u1", scope_kind="task", scope_id="task_1",
        db_session_factory=factory,
    )
    assert session_id

    # CLAUDE.md and at least the v2 + v1 trial logs were uploaded
    assert any(p.endswith("/CLAUDE.md") for p in daytona.uploaded)
    assert any("jobs/v2/" in p for p in daytona.uploaded)
    assert any("jobs/v1/" in p for p in daytona.uploaded)

    # session row is active
    from models import ChatSession
    async with db() as s:
        row = await s.get(ChatSession, session_id)
        assert row.status == "active" and row.scope_kind == "task"
```

- [ ] **Step 3: Run — expect 1 passed**
Run: `cd backend && ODDISH_DATABASE_URL='postgresql+asyncpg://oddish:oddish@localhost:5432/oddish_chat_test' uv run pytest tests/cc_chat/test_orchestrator_start_task.py -v`
(If `upload_files` calls `client.upload_file` with the workspace-joined dest path, the assertions on `jobs/v2/` substrings hold because the dest is `f"{workspace_root}/{rel}"`.)

- [ ] **Step 4: Full suite green**
Run: `cd backend && ODDISH_DATABASE_URL='postgresql+asyncpg://oddish:oddish@localhost:5432/oddish_chat_test' uv run pytest tests/cc_chat -q` — expect all prior tests + the new ones pass.

- [ ] **Step 5: Commit**
```bash
cd /Users/kateyeh/Developer/os_repos/oddish
git add backend/api/services/cc_chat/orchestrator.py backend/tests/cc_chat/test_orchestrator_start_task.py
git commit -m "feat(cc_chat): start() loads version-organized trial logs for task scope"
```

---

## Definition of done (Phase 2a)
- A `task`-scope chat session provisions a sandbox with the task's trial logs laid out under `jobs/v{version}/{trial_id}/…`, current version flagged in `CLAUDE.md`, past versions present but de-emphasized.
- Upload is byte-capped with a logged warning on truncation.
- All cc_chat tests green.

**Deferred:** the frontend task-detail entry point + chat panel + proxy routes → **Phase 2b** (separate plan; shared with Phase 3's global scope). The `global` scope + `POST /tasks/query` + broker token → **Phase 3**. Reconciling/retiring the older `task_probes` scope vs the new `task` scope → revisit once both are exercised in the UI.

## Self-review notes
- Spec coverage: "default latest version, see past versions" → current-version flag in CLAUDE.md + all versions preloaded as labeled folders (Task 4/5). "On-demand retrieval" → Claude reads workspace files on demand; byte cap bounds the upload (Task 3). Org-scoping → `collect_task_version_files` filters `TrialModel.org_id` and goes through `get_task_for_org_core` (Task 3).
- Open risk: the `seed_task_with_trials` helper must satisfy `TaskModel`/`TrialModel` NOT-NULL columns — Task 3 Step 1 calls this out explicitly for the implementer to verify against `oddish/src/oddish/db/models.py`.
