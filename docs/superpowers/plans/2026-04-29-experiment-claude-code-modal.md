# Experiment Claude Code Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a "Chat with logs" modal on the experiment page that runs a real headless `claude` CLI inside a per-session Daytona sandbox, with the experiment's artifact tree uploaded as the working directory. Streams stream-json events back to the modal over SSE. Sessions are ephemeral.

**Architecture:** Sandbox-orchestrated headless Claude Code. Backend `CCChatOrchestrator` owns the lifecycle: creates a Daytona sandbox per session, populates it via an `ExperimentFileStore` (local for dev, S3 for prod), drops a curated `CLAUDE.md`, and runs `claude --print --output-format=stream-json --resume <sid>` per turn via `executeSessionCommand(runAsync=True)`, streaming logs over Daytona's WS-based `get_session_command_logs_async`. State is in-memory; close-modal deletes the sandbox.

**Tech Stack:** FastAPI + Daytona Python SDK + asyncio (backend), Next.js 15 App Router + shadcn `Dialog` + native `EventSource` (frontend), pytest (backend tests), `claude` CLI inside the sandbox.

**Spec:** `docs/superpowers/specs/2026-04-29-experiment-claude-code-modal-design.md`

---

## File Structure

### Created (backend)

- `backend/api/services/__init__.py` — namespace marker (only if `services/` doesn't exist yet).
- `backend/api/services/cc_chat/__init__.py` — re-exports public names.
- `backend/api/services/cc_chat/file_store.py` — `ExperimentFileStore` Protocol + `LocalFileStore` + `S3FileStore`.
- `backend/api/services/cc_chat/claude_md.py` — `render_claude_md(experiment_id, file_paths)` pure function.
- `backend/api/services/cc_chat/sessions.py` — `SessionState` dataclass, in-memory `sessions` dict, `IdleSweeper` task.
- `backend/api/services/cc_chat/daytona_client.py` — thin async wrapper around the Daytona SDK so we can fake it in tests.
- `backend/api/services/cc_chat/orchestrator.py` — `CCChatOrchestrator` class (`start`, `send`, `close`).
- `backend/api/routers/cc_chat.py` — three FastAPI endpoints.
- `backend/scripts/smoke_cc_chat.py` — pre-deploy smoke test.
- `backend/tests/cc_chat/__init__.py`
- `backend/tests/cc_chat/conftest.py` — shared fakes/fixtures.
- `backend/tests/cc_chat/test_local_file_store.py`
- `backend/tests/cc_chat/test_claude_md.py`
- `backend/tests/cc_chat/test_sessions.py`
- `backend/tests/cc_chat/test_orchestrator.py`
- `backend/tests/cc_chat/test_router.py`

### Modified (backend)

- `oddish/src/oddish/config.py` — add `daytona_api_key`, `cc_chat_local_jobs_dir` fields to `Settings`.
- `backend/api/app.py` — `from api.routers import cc_chat`; `api.include_router(cc_chat.router)`.

### Created (frontend)

- `frontend/src/components/cc-chat-modal.tsx` — the chat modal.
- `frontend/src/lib/cc-chat-stream.ts` — small helper that POSTs and consumes the SSE stream.

### Modified (frontend)

- `frontend/src/app/(app)/experiments/[experiment]/experiment-client.tsx` — add "Chat with logs" button + modal mount.

### Boundaries

- `file_store.py` knows nothing about Daytona; it just yields `(rel_path, bytes)` tuples for some uploader to consume.
- `daytona_client.py` knows nothing about experiments; it just exposes `create`, `upload_file`, `exec_session_async`, `stream_logs`, `delete`.
- `orchestrator.py` is the only module that combines `file_store` + `daytona_client`.
- `router.py` is a thin shim over `orchestrator`.

---

## Task 1: Add `daytona_api_key` and `cc_chat_local_jobs_dir` to settings

**Files:**
- Modify: `oddish/src/oddish/config.py` (add two fields to `Settings`)
- Test: `backend/tests/cc_chat/test_settings.py`

- [ ] **Step 1: Read the existing `Settings` class to confirm shape**

Run: `grep -n "anthropic_api_key\|env_prefix\|class Settings" /Users/kateyeh/Developer/os_repos/oddish/oddish/src/oddish/config.py`

Expected: confirms `class Settings(BaseSettings)`, `env_prefix="ODDISH_"`, and an existing `anthropic_api_key: str | None = Field(default=None, alias="ANTHROPIC_API_KEY")` line we'll mirror.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/cc_chat/__init__.py` (empty file) and `backend/tests/cc_chat/test_settings.py`:

```python
import os
from oddish.config import Settings


def test_daytona_api_key_reads_from_env(monkeypatch):
    monkeypatch.setenv("DAYTONA_API_KEY", "dt_test_value")
    s = Settings()
    assert s.daytona_api_key == "dt_test_value"


def test_daytona_api_key_defaults_to_none(monkeypatch):
    monkeypatch.delenv("DAYTONA_API_KEY", raising=False)
    s = Settings()
    assert s.daytona_api_key is None


def test_cc_chat_local_jobs_dir_reads_from_env(monkeypatch):
    monkeypatch.setenv("ODDISH_CC_CHAT_LOCAL_JOBS_DIR", "/tmp/fake/jobs")
    s = Settings()
    assert s.cc_chat_local_jobs_dir == "/tmp/fake/jobs"
```

- [ ] **Step 3: Run the test, expect failure**

Run: `pytest backend/tests/cc_chat/test_settings.py -v`
Expected: 3 failures with `AttributeError: 'Settings' object has no attribute 'daytona_api_key'`.

- [ ] **Step 4: Add the two fields to `Settings`**

In `oddish/src/oddish/config.py`, locate the existing `anthropic_api_key` line and add immediately after it:

```python
    daytona_api_key: str | None = Field(
        default=None, alias="DAYTONA_API_KEY"
    )
    cc_chat_local_jobs_dir: str | None = Field(default=None)
```

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_settings.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add oddish/src/oddish/config.py backend/tests/cc_chat/__init__.py backend/tests/cc_chat/test_settings.py
git commit -m "Add daytona_api_key and cc_chat_local_jobs_dir to Settings"
```

---

## Task 2: Implement `ExperimentFileStore` Protocol + `LocalFileStore`

`LocalFileStore` walks a base directory + experiment_id and yields `(rel_path, bytes)` pairs. The orchestrator does the uploading.

**Files:**
- Create: `backend/api/services/__init__.py` (empty if it doesn't exist)
- Create: `backend/api/services/cc_chat/__init__.py` (empty for now)
- Create: `backend/api/services/cc_chat/file_store.py`
- Test: `backend/tests/cc_chat/test_local_file_store.py`

- [ ] **Step 1: Confirm `services/` directory state**

Run: `ls /Users/kateyeh/Developer/os_repos/oddish/backend/api/services 2>&1 | head -5`
Expected: either an existing dir listing, or "No such file or directory". If missing, create it in step 4 alongside the other files.

- [ ] **Step 2: Write the failing test**

Create `backend/tests/cc_chat/test_local_file_store.py`:

```python
from pathlib import Path

import pytest

from api.services.cc_chat.file_store import LocalFileStore

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_BASE = REPO_ROOT / "jobs"
FIXTURE_EXPERIMENT_ID = "2026-04-26__16-45-36"


@pytest.mark.asyncio
async def test_local_file_store_yields_relative_paths_and_bytes():
    store = LocalFileStore(base_path=FIXTURE_BASE)
    files = []
    async for rel, content in store.iter_files(FIXTURE_EXPERIMENT_ID):
        files.append((rel, content))

    assert files, "expected at least one file"
    rel_paths = {rel for rel, _ in files}
    assert "hello-world__eU7yQqg/result.json" in rel_paths
    assert "hello-world__eU7yQqg/trial.log" in rel_paths

    # Content is bytes and matches the file on disk
    for rel, content in files:
        assert isinstance(content, bytes)
        on_disk = (FIXTURE_BASE / FIXTURE_EXPERIMENT_ID / rel).read_bytes()
        assert content == on_disk


@pytest.mark.asyncio
async def test_local_file_store_skips_dotfiles():
    store = LocalFileStore(base_path=FIXTURE_BASE)
    rel_paths = [rel async for rel, _ in store.iter_files(FIXTURE_EXPERIMENT_ID)]
    for rel in rel_paths:
        assert not any(part.startswith(".") for part in Path(rel).parts), (
            f"unexpected dotfile path: {rel}"
        )


@pytest.mark.asyncio
async def test_local_file_store_missing_experiment_yields_nothing():
    store = LocalFileStore(base_path=FIXTURE_BASE)
    files = [f async for f in store.iter_files("does-not-exist")]
    assert files == []
```

- [ ] **Step 3: Run the test, expect import error**

Run: `pytest backend/tests/cc_chat/test_local_file_store.py -v`
Expected: collection error / `ModuleNotFoundError: No module named 'api.services.cc_chat.file_store'`.

- [ ] **Step 4: Implement `LocalFileStore`**

Create `backend/api/services/__init__.py` (empty), `backend/api/services/cc_chat/__init__.py` (empty), and `backend/api/services/cc_chat/file_store.py`:

```python
from __future__ import annotations

from pathlib import Path
from typing import AsyncIterator, Protocol


class ExperimentFileStore(Protocol):
    async def iter_files(
        self, experiment_id: str
    ) -> AsyncIterator[tuple[str, bytes]]:
        """Yield (relative_path, file_bytes) for every artifact in the experiment."""
        ...


class LocalFileStore:
    """Reads experiment files from a local directory tree.

    Layout: <base_path>/<experiment_id>/<trial_id>/...
    Yields paths relative to <base_path>/<experiment_id>/.
    """

    def __init__(self, base_path: Path | str) -> None:
        self.base_path = Path(base_path)

    async def iter_files(
        self, experiment_id: str
    ) -> AsyncIterator[tuple[str, bytes]]:
        root = self.base_path / experiment_id
        if not root.is_dir():
            return
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if any(part.startswith(".") for part in path.relative_to(root).parts):
                continue
            rel = path.relative_to(root).as_posix()
            yield rel, path.read_bytes()
```

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_local_file_store.py -v`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/__init__.py backend/api/services/cc_chat/__init__.py backend/api/services/cc_chat/file_store.py backend/tests/cc_chat/test_local_file_store.py
git commit -m "Add ExperimentFileStore protocol and LocalFileStore"
```

---

## Task 3: Implement `render_claude_md`

A pure function that produces the curated CLAUDE.md content the agent reads on every chat. It takes the experiment id and a list of trial ids it found in the file tree, and returns the rendered markdown.

**Files:**
- Create: `backend/api/services/cc_chat/claude_md.py`
- Test: `backend/tests/cc_chat/test_claude_md.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cc_chat/test_claude_md.py`:

```python
from api.services.cc_chat.claude_md import render_claude_md


def test_render_claude_md_includes_experiment_id_and_layout():
    md = render_claude_md(
        experiment_id="exp-123",
        trial_ids=["trial-a", "trial-b"],
    )
    # Header tells the agent who it is
    assert "exp-123" in md
    # Layout description so the agent knows how to navigate
    assert "result.json" in md
    assert "trajectory.json" in md
    assert "verifier" in md
    # Lists the trials it can drill into
    assert "trial-a" in md
    assert "trial-b" in md
    # Steers the agent away from cat-ing every log
    assert "Glob" in md or "Grep" in md
    # No template placeholders left behind
    assert "{" not in md and "}" not in md


def test_render_claude_md_handles_empty_experiment():
    md = render_claude_md(experiment_id="exp-empty", trial_ids=[])
    assert "exp-empty" in md
    # Don't fail; tell the agent there's nothing yet
    assert "no trial" in md.lower()
```

- [ ] **Step 2: Run the test, expect failure**

Run: `pytest backend/tests/cc_chat/test_claude_md.py -v`
Expected: `ModuleNotFoundError: No module named 'api.services.cc_chat.claude_md'`.

- [ ] **Step 3: Implement `render_claude_md`**

Create `backend/api/services/cc_chat/claude_md.py`:

```python
from __future__ import annotations


_TEMPLATE = """\
# Experiment {experiment_id}

You are a Claude Code agent helping a user reason about the artifacts of
an Oddish experiment. The experiment's full artifact tree is mounted at
the current working directory under `jobs/{experiment_id}/`.

## Layout

```
jobs/{experiment_id}/
  <trial_id>/
    config.json          # trial config (task, agent, model, seed)
    result.json          # final reward, status, durations
    trial.log            # high-level trial events
    exception.txt        # present if the trial errored
    agent/
      claude-code.txt    # raw stdout/stderr from the agent
      trajectory.json    # parsed message+action timeline
      sessions/          # raw Claude Code session state
    verifier/
      ctrf.json          # structured test results (CTRF format)
      reward.txt         # numeric reward as a string
      test-stdout.txt    # raw verifier stdout
```

## How to navigate

- Start with `jobs/{experiment_id}/<trial_id>/result.json` and
  `jobs/{experiment_id}/<trial_id>/config.json` to get a trial's verdict
  and inputs without reading anything heavy.
- Use `Glob` and `Grep` to find things across trials. For example,
  `Grep --files-with-matches "FAIL" jobs/{experiment_id}/`.
- Only read full `trial.log` / `agent/claude-code.txt` / `agent/trajectory.json`
  when the user has zoomed into a specific trial. These can be large.
- `verifier/ctrf.json` is the canonical "did the test pass?" file.

## Trials in this experiment
{trial_list}
"""

_EMPTY_TRIAL_BLOCK = "_(no trial data available yet)_"


def render_claude_md(*, experiment_id: str, trial_ids: list[str]) -> str:
    if trial_ids:
        trial_list = "\n".join(f"- `{tid}`" for tid in sorted(trial_ids))
    else:
        trial_list = _EMPTY_TRIAL_BLOCK
    return _TEMPLATE.format(
        experiment_id=experiment_id, trial_list=trial_list
    )
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_claude_md.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/cc_chat/claude_md.py backend/tests/cc_chat/test_claude_md.py
git commit -m "Add render_claude_md template generator"
```

---

## Task 4: Implement `SessionState` and the in-memory `sessions` registry

Pure data types + a thread-safe-enough dict (single asyncio loop, no locking needed). No external deps yet.

**Files:**
- Create: `backend/api/services/cc_chat/sessions.py`
- Test: `backend/tests/cc_chat/test_sessions.py`

- [ ] **Step 1: Write the failing test**

Create `backend/tests/cc_chat/test_sessions.py`:

```python
from datetime import datetime, timedelta, timezone

from api.services.cc_chat.sessions import SessionRegistry, SessionState


def _now():
    return datetime.now(timezone.utc)


def test_register_and_get():
    reg = SessionRegistry()
    state = SessionState(
        session_id="sid-1",
        experiment_id="exp-1",
        org_id="org-1",
        sandbox_id="sbx-1",
        daytona_session_id="cc",
        created_at=_now(),
        last_activity=_now(),
        claude_session_id=None,
    )
    reg.put(state)
    assert reg.get("sid-1") is state
    assert reg.get("missing") is None


def test_pop_returns_and_removes():
    reg = SessionRegistry()
    state = SessionState(
        session_id="sid-1",
        experiment_id="exp-1",
        org_id="org-1",
        sandbox_id="sbx-1",
        daytona_session_id="cc",
        created_at=_now(),
        last_activity=_now(),
        claude_session_id=None,
    )
    reg.put(state)
    assert reg.pop("sid-1") is state
    assert reg.pop("sid-1") is None


def test_idle_returns_sessions_older_than_threshold():
    reg = SessionRegistry()
    now = _now()
    fresh = SessionState(
        session_id="fresh",
        experiment_id="e",
        org_id="o",
        sandbox_id="s",
        daytona_session_id="cc",
        created_at=now,
        last_activity=now,
        claude_session_id=None,
    )
    stale = SessionState(
        session_id="stale",
        experiment_id="e",
        org_id="o",
        sandbox_id="s",
        daytona_session_id="cc",
        created_at=now - timedelta(hours=1),
        last_activity=now - timedelta(minutes=45),
        claude_session_id=None,
    )
    reg.put(fresh)
    reg.put(stale)
    idle = list(reg.idle(now=now, max_idle=timedelta(minutes=30)))
    assert [s.session_id for s in idle] == ["stale"]


def test_touch_updates_last_activity():
    reg = SessionRegistry()
    now = _now()
    state = SessionState(
        session_id="sid",
        experiment_id="e",
        org_id="o",
        sandbox_id="s",
        daytona_session_id="cc",
        created_at=now - timedelta(hours=1),
        last_activity=now - timedelta(hours=1),
        claude_session_id=None,
    )
    reg.put(state)
    reg.touch("sid", now=now)
    assert reg.get("sid").last_activity == now
```

- [ ] **Step 2: Run the test, expect failure**

Run: `pytest backend/tests/cc_chat/test_sessions.py -v`
Expected: `ModuleNotFoundError`.

- [ ] **Step 3: Implement `SessionState` + `SessionRegistry`**

Create `backend/api/services/cc_chat/sessions.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterator


@dataclass
class SessionState:
    session_id: str
    experiment_id: str
    org_id: str
    sandbox_id: str
    daytona_session_id: str
    created_at: datetime
    last_activity: datetime
    claude_session_id: str | None
    broken: bool = False


class SessionRegistry:
    """In-memory session map. Single-replica only; documented limitation."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionState] = {}

    def put(self, state: SessionState) -> None:
        self._sessions[state.session_id] = state

    def get(self, session_id: str) -> SessionState | None:
        return self._sessions.get(session_id)

    def pop(self, session_id: str) -> SessionState | None:
        return self._sessions.pop(session_id, None)

    def touch(self, session_id: str, *, now: datetime) -> None:
        state = self._sessions.get(session_id)
        if state is not None:
            state.last_activity = now

    def idle(
        self, *, now: datetime, max_idle: timedelta
    ) -> Iterator[SessionState]:
        cutoff = now - max_idle
        for state in list(self._sessions.values()):
            if state.last_activity < cutoff:
                yield state
```

- [ ] **Step 4: Run tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_sessions.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/cc_chat/sessions.py backend/tests/cc_chat/test_sessions.py
git commit -m "Add SessionState and in-memory SessionRegistry"
```

---

## Task 5: Implement Daytona client wrapper

Thin async facade over the Daytona Python SDK. Existence solely so the orchestrator can be tested with a fake. Methods: `create_sandbox`, `upload_file`, `create_session`, `exec_async`, `stream_logs`, `delete_sandbox`.

**Files:**
- Create: `backend/api/services/cc_chat/daytona_client.py`

(No tests for this file directly — it's a pass-through. We test the orchestrator using a fake of this interface.)

- [ ] **Step 1: Verify `daytona` Python SDK is on the dependency list**

Run: `grep -n '"daytona' /Users/kateyeh/Developer/os_repos/oddish/backend/pyproject.toml || echo "NOT FOUND"`
Expected: either a hit (already installed) or `NOT FOUND`. If not found, add it in the next step.

- [ ] **Step 2: Add `daytona` to backend deps if missing**

If `NOT FOUND`, edit `backend/pyproject.toml` and add `"daytona>=0.20"` (or the latest current minor) to the `dependencies` list. Then:

Run: `cd /Users/kateyeh/Developer/os_repos/oddish/backend && uv sync`
Expected: lockfile updates with daytona installed.

- [ ] **Step 3: Implement the wrapper**

Create `backend/api/services/cc_chat/daytona_client.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable, Protocol

from daytona import AsyncDaytona, CreateSandboxParams, SessionExecuteRequest


@dataclass
class CreatedSandbox:
    """Just the bits the orchestrator needs to keep around."""

    id: str
    _sdk_handle: object  # opaque; used internally


class DaytonaClient(Protocol):
    async def create_sandbox(
        self, *, env_vars: dict[str, str], auto_stop_minutes: int
    ) -> CreatedSandbox: ...

    async def upload_file(
        self, sandbox: CreatedSandbox, *, dest_path: str, content: bytes
    ) -> None: ...

    async def create_session(
        self, sandbox: CreatedSandbox, *, session_id: str
    ) -> None: ...

    async def exec_async(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        command: list[str],
    ) -> str:
        """Returns the cmd_id."""
        ...

    async def stream_logs(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        cmd_id: str,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> None: ...

    async def delete_sandbox(self, sandbox: CreatedSandbox) -> None: ...


class RealDaytonaClient:
    """Production implementation backed by the Daytona Python SDK."""

    def __init__(self, *, api_key: str) -> None:
        self._daytona = AsyncDaytona(api_key=api_key)

    async def create_sandbox(
        self, *, env_vars: dict[str, str], auto_stop_minutes: int
    ) -> CreatedSandbox:
        sbx = await self._daytona.create(
            CreateSandboxParams(
                env_vars=env_vars,
                auto_stop_interval=auto_stop_minutes,
            )
        )
        return CreatedSandbox(id=sbx.id, _sdk_handle=sbx)

    async def upload_file(
        self, sandbox: CreatedSandbox, *, dest_path: str, content: bytes
    ) -> None:
        await sandbox._sdk_handle.fs.upload_file(content, dest_path)

    async def create_session(
        self, sandbox: CreatedSandbox, *, session_id: str
    ) -> None:
        await sandbox._sdk_handle.process.create_session(session_id)

    async def exec_async(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        command: list[str],
    ) -> str:
        result = await sandbox._sdk_handle.process.execute_session_command(
            daytona_session_id,
            SessionExecuteRequest(command=" ".join(command), run_async=True),
        )
        return result.cmd_id

    async def stream_logs(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        cmd_id: str,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> None:
        await sandbox._sdk_handle.process.get_session_command_logs_async(
            daytona_session_id,
            cmd_id,
            on_stdout,
            on_stderr,
        )

    async def delete_sandbox(self, sandbox: CreatedSandbox) -> None:
        await self._daytona.delete(sandbox._sdk_handle)
```

> **NOTE for the implementer:** the exact import paths (`AsyncDaytona`, `CreateSandboxParams`, `SessionExecuteRequest`) and method signatures may differ slightly between Daytona SDK versions. If imports fail, run `python -c "import daytona; print(dir(daytona))"` and adjust. The contract that matters is the `DaytonaClient` Protocol — keep that stable.

- [ ] **Step 4: Smoke-import**

Run: `python -c "from api.services.cc_chat.daytona_client import DaytonaClient, RealDaytonaClient, CreatedSandbox; print('ok')"` (with backend on `PYTHONPATH`)
Expected: `ok`.

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/cc_chat/daytona_client.py backend/pyproject.toml backend/uv.lock
git commit -m "Add DaytonaClient protocol and SDK-backed implementation"
```

---

## Task 6: Implement `CCChatOrchestrator.start`

This is the first orchestrator method: create sandbox, install `claude` CLI, populate files, write CLAUDE.md, create Daytona session, register in memory.

**Files:**
- Create: `backend/api/services/cc_chat/orchestrator.py`
- Modify: `backend/tests/cc_chat/conftest.py` (create with shared fakes)
- Test: `backend/tests/cc_chat/test_orchestrator.py`

- [ ] **Step 1: Create the test conftest with a `FakeDaytonaClient`**

Create `backend/tests/cc_chat/conftest.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable

import pytest

from api.services.cc_chat.daytona_client import CreatedSandbox


@dataclass
class FakeSandbox:
    id: str
    files: dict[str, bytes] = field(default_factory=dict)
    sessions: list[str] = field(default_factory=list)
    deleted: bool = False


class FakeDaytonaClient:
    """Records calls; lets tests assert on the resulting in-memory state."""

    def __init__(self) -> None:
        self.created: list[FakeSandbox] = []
        self.execs: list[dict] = []
        # Tests set this to control what stream_logs emits.
        self.canned_stdout_chunks: list[str] = []
        self.canned_stderr_chunks: list[str] = []

    async def create_sandbox(
        self, *, env_vars: dict[str, str], auto_stop_minutes: int
    ) -> CreatedSandbox:
        sbx = FakeSandbox(id=f"sbx-{len(self.created)}")
        self.created.append(sbx)
        sbx.env_vars = env_vars
        sbx.auto_stop_minutes = auto_stop_minutes
        return CreatedSandbox(id=sbx.id, _sdk_handle=sbx)

    async def upload_file(
        self, sandbox: CreatedSandbox, *, dest_path: str, content: bytes
    ) -> None:
        sandbox._sdk_handle.files[dest_path] = content

    async def create_session(
        self, sandbox: CreatedSandbox, *, session_id: str
    ) -> None:
        sandbox._sdk_handle.sessions.append(session_id)

    async def exec_async(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        command: list[str],
    ) -> str:
        cmd_id = f"cmd-{len(self.execs)}"
        self.execs.append(
            {
                "sandbox_id": sandbox.id,
                "session": daytona_session_id,
                "command": command,
                "cmd_id": cmd_id,
            }
        )
        return cmd_id

    async def stream_logs(
        self,
        sandbox: CreatedSandbox,
        *,
        daytona_session_id: str,
        cmd_id: str,
        on_stdout: Callable[[str], Awaitable[None]],
        on_stderr: Callable[[str], Awaitable[None]],
    ) -> None:
        for chunk in self.canned_stdout_chunks:
            await on_stdout(chunk)
        for chunk in self.canned_stderr_chunks:
            await on_stderr(chunk)

    async def delete_sandbox(self, sandbox: CreatedSandbox) -> None:
        sandbox._sdk_handle.deleted = True


@pytest.fixture
def fake_daytona() -> FakeDaytonaClient:
    return FakeDaytonaClient()
```

- [ ] **Step 2: Write the failing `start` test**

Create `backend/tests/cc_chat/test_orchestrator.py`:

```python
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.cc_chat.file_store import LocalFileStore
from api.services.cc_chat.orchestrator import CCChatOrchestrator

REPO_ROOT = Path(__file__).resolve().parents[3]
FIXTURE_BASE = REPO_ROOT / "jobs"
FIXTURE_EXPERIMENT_ID = "2026-04-26__16-45-36"


def _make_orchestrator(fake_daytona) -> CCChatOrchestrator:
    return CCChatOrchestrator(
        daytona=fake_daytona,
        file_store=LocalFileStore(base_path=FIXTURE_BASE),
        anthropic_api_key="sk-test",
        auto_stop_minutes=30,
    )


@pytest.mark.asyncio
async def test_start_creates_sandbox_and_uploads_files(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    assert sid

    # Exactly one sandbox created with the expected env
    assert len(fake_daytona.created) == 1
    sbx = fake_daytona.created[0]
    assert sbx.env_vars["ANTHROPIC_API_KEY"] == "sk-test"
    assert sbx.auto_stop_minutes == 30

    # CLAUDE.md was written
    assert "/workspace/CLAUDE.md" in sbx.files
    claude_md = sbx.files["/workspace/CLAUDE.md"].decode()
    assert FIXTURE_EXPERIMENT_ID in claude_md
    # At least one trial id appears
    assert "hello-world__eU7yQqg" in claude_md

    # Experiment files are under /workspace/jobs/<experiment_id>/
    expected_path = (
        f"/workspace/jobs/{FIXTURE_EXPERIMENT_ID}/"
        "hello-world__eU7yQqg/result.json"
    )
    assert expected_path in sbx.files
    # And the contents match disk
    on_disk = (
        FIXTURE_BASE
        / FIXTURE_EXPERIMENT_ID
        / "hello-world__eU7yQqg"
        / "result.json"
    ).read_bytes()
    assert sbx.files[expected_path] == on_disk

    # A daytona session named "cc" was created
    assert "cc" in sbx.sessions

    # Registry entry exists
    state = orch._sessions.get(sid)
    assert state is not None
    assert state.experiment_id == FIXTURE_EXPERIMENT_ID
    assert state.org_id == "org-1"
    assert state.claude_session_id is None


@pytest.mark.asyncio
async def test_start_installs_claude_cli(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    install_execs = [
        e for e in fake_daytona.execs
        if any("@anthropic-ai/claude-code" in arg for arg in e["command"])
    ]
    assert install_execs, (
        "expected an exec call that installs the claude CLI"
    )


@pytest.mark.asyncio
async def test_start_aborts_and_deletes_on_upload_failure(
    fake_daytona, monkeypatch
):
    orch = _make_orchestrator(fake_daytona)

    real_upload = fake_daytona.upload_file

    async def flaky_upload(sandbox, **kwargs):
        if "trial.log" in kwargs["dest_path"]:
            raise RuntimeError("boom")
        await real_upload(sandbox, **kwargs)

    monkeypatch.setattr(fake_daytona, "upload_file", flaky_upload)

    with pytest.raises(RuntimeError, match="boom"):
        await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")

    # Sandbox was created and then deleted
    assert len(fake_daytona.created) == 1
    assert fake_daytona.created[0].deleted is True
    # No session lingers in the registry
    assert list(orch._sessions._sessions.keys()) == []
```

- [ ] **Step 3: Run, expect collection or import failure**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v`
Expected: `ModuleNotFoundError: No module named 'api.services.cc_chat.orchestrator'`.

- [ ] **Step 4: Implement `CCChatOrchestrator` (start only for now)**

Create `backend/api/services/cc_chat/orchestrator.py`:

```python
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import PurePosixPath

from api.services.cc_chat.claude_md import render_claude_md
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient
from api.services.cc_chat.file_store import ExperimentFileStore
from api.services.cc_chat.sessions import SessionRegistry, SessionState


_DAYTONA_SESSION_NAME = "cc"
_WORKSPACE_ROOT = "/workspace"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _new_session_id() -> str:
    return f"cc-{secrets.token_urlsafe(12)}"


class CCChatOrchestrator:
    def __init__(
        self,
        *,
        daytona: DaytonaClient,
        file_store: ExperimentFileStore,
        anthropic_api_key: str,
        auto_stop_minutes: int = 30,
    ) -> None:
        self._daytona = daytona
        self._file_store = file_store
        self._anthropic_api_key = anthropic_api_key
        self._auto_stop_minutes = auto_stop_minutes
        self._sessions = SessionRegistry()
        self._sandbox_handles: dict[str, CreatedSandbox] = {}

    async def start(self, *, experiment_id: str, org_id: str) -> str:
        sandbox = await self._daytona.create_sandbox(
            env_vars={"ANTHROPIC_API_KEY": self._anthropic_api_key},
            auto_stop_minutes=self._auto_stop_minutes,
        )
        try:
            await self._daytona.create_session(
                sandbox, session_id=_DAYTONA_SESSION_NAME
            )
            await self._daytona.exec_async(
                sandbox,
                daytona_session_id=_DAYTONA_SESSION_NAME,
                command=[
                    "npm", "install", "-g", "@anthropic-ai/claude-code",
                ],
            )

            trial_ids: list[str] = []
            async for rel, content in self._file_store.iter_files(
                experiment_id
            ):
                trial_id = PurePosixPath(rel).parts[0]
                if trial_id not in trial_ids:
                    trial_ids.append(trial_id)
                dest = (
                    f"{_WORKSPACE_ROOT}/jobs/{experiment_id}/{rel}"
                )
                await self._daytona.upload_file(
                    sandbox, dest_path=dest, content=content
                )

            claude_md = render_claude_md(
                experiment_id=experiment_id, trial_ids=trial_ids
            )
            await self._daytona.upload_file(
                sandbox,
                dest_path=f"{_WORKSPACE_ROOT}/CLAUDE.md",
                content=claude_md.encode("utf-8"),
            )
        except Exception:
            await self._daytona.delete_sandbox(sandbox)
            raise

        session_id = _new_session_id()
        now = _now()
        self._sessions.put(
            SessionState(
                session_id=session_id,
                experiment_id=experiment_id,
                org_id=org_id,
                sandbox_id=sandbox.id,
                daytona_session_id=_DAYTONA_SESSION_NAME,
                created_at=now,
                last_activity=now,
                claude_session_id=None,
            )
        )
        self._sandbox_handles[session_id] = sandbox
        return session_id
```

- [ ] **Step 5: Run the start tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v -k start`
Expected: 3 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/cc_chat/orchestrator.py backend/tests/cc_chat/conftest.py backend/tests/cc_chat/test_orchestrator.py
git commit -m "Add CCChatOrchestrator.start with sandbox lifecycle and file upload"
```

---

## Task 7: Implement `CCChatOrchestrator.send`

Builds the `claude` command, runs it via `exec_async`, parses stream-json line by line, captures the `system/init` `session_id` on first turn, yields events outward.

**Files:**
- Modify: `backend/api/services/cc_chat/orchestrator.py` (add `send` method + small async-buffer helper)
- Modify: `backend/tests/cc_chat/test_orchestrator.py` (append send tests)

- [ ] **Step 1: Write the failing send tests**

Append to `backend/tests/cc_chat/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_send_first_turn_captures_claude_session_id(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")

    fake_daytona.canned_stdout_chunks = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "cc-uuid-1"}) + "\n",
        json.dumps({"type": "assistant", "delta": "Hello"}) + "\n",
        json.dumps({"type": "result", "stop_reason": "end_turn"}) + "\n",
    ]

    events = [event async for event in orch.send(session_id=sid, content="hi")]
    types = [e["type"] for e in events]
    assert types == ["system", "assistant", "result"]
    assert orch._sessions.get(sid).claude_session_id == "cc-uuid-1"


@pytest.mark.asyncio
async def test_send_second_turn_passes_resume(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")

    # Turn 1
    fake_daytona.canned_stdout_chunks = [
        json.dumps({"type": "system", "subtype": "init", "session_id": "cc-uuid-1"}) + "\n",
        json.dumps({"type": "result"}) + "\n",
    ]
    _ = [e async for e in orch.send(session_id=sid, content="first")]

    # Turn 2
    fake_daytona.execs.clear()
    fake_daytona.canned_stdout_chunks = [
        json.dumps({"type": "result"}) + "\n",
    ]
    _ = [e async for e in orch.send(session_id=sid, content="second")]

    claude_execs = [
        e for e in fake_daytona.execs
        if e["command"] and e["command"][0] == "claude"
    ]
    assert claude_execs, "expected a claude exec on the second turn"
    assert "--resume" in claude_execs[-1]["command"]
    assert "cc-uuid-1" in claude_execs[-1]["command"]


@pytest.mark.asyncio
async def test_send_unknown_session_raises(fake_daytona):
    from api.services.cc_chat.orchestrator import SessionNotFound
    orch = _make_orchestrator(fake_daytona)
    with pytest.raises(SessionNotFound):
        async for _ in orch.send(session_id="nope", content="hi"):
            pass
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v -k send`
Expected: 3 failures (`AttributeError: 'CCChatOrchestrator' object has no attribute 'send'` and similar).

- [ ] **Step 3: Implement `send`**

Add to the top of `backend/api/services/cc_chat/orchestrator.py`:

```python
import asyncio
import json
from typing import AsyncIterator
```

Add this exception class (near the imports):

```python
class SessionNotFound(Exception):
    pass
```

Add the `send` method to `CCChatOrchestrator`:

```python
    async def send(
        self, *, session_id: str, content: str
    ) -> AsyncIterator[dict]:
        state = self._sessions.get(session_id)
        if state is None or state.broken:
            raise SessionNotFound(session_id)
        sandbox = self._sandbox_handles[session_id]

        cmd: list[str] = [
            "claude",
            "--print",
            "--output-format=stream-json",
        ]
        if state.claude_session_id:
            cmd += ["--resume", state.claude_session_id]
        cmd += ["--", content]

        cmd_id = await self._daytona.exec_async(
            sandbox,
            daytona_session_id=state.daytona_session_id,
            command=cmd,
        )

        queue: asyncio.Queue[tuple[str, str] | None] = asyncio.Queue()

        async def on_stdout(chunk: str) -> None:
            await queue.put(("stdout", chunk))

        async def on_stderr(chunk: str) -> None:
            await queue.put(("stderr", chunk))

        stream_task = asyncio.create_task(
            self._daytona.stream_logs(
                sandbox,
                daytona_session_id=state.daytona_session_id,
                cmd_id=cmd_id,
                on_stdout=on_stdout,
                on_stderr=on_stderr,
            )
        )

        async def closer() -> None:
            try:
                await stream_task
            finally:
                await queue.put(None)

        closer_task = asyncio.create_task(closer())

        leftover = ""
        try:
            while True:
                item = await queue.get()
                if item is None:
                    break
                kind, chunk = item
                if kind == "stderr":
                    yield {
                        "type": "_stderr",
                        "text": chunk,
                    }
                    continue
                leftover += chunk
                while "\n" in leftover:
                    line, leftover = leftover.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        yield {"type": "_invalid_json", "raw": line}
                        continue
                    if (
                        event.get("type") == "system"
                        and event.get("subtype") == "init"
                        and "session_id" in event
                    ):
                        state.claude_session_id = event["session_id"]
                    state.last_activity = _now()
                    yield event
            if leftover.strip():
                try:
                    yield json.loads(leftover.strip())
                except json.JSONDecodeError:
                    yield {"type": "_invalid_json", "raw": leftover.strip()}
        finally:
            await closer_task
```

- [ ] **Step 4: Run send tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v -k send`
Expected: 3 passed.

- [ ] **Step 5: Run all orchestrator tests to confirm nothing regressed**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v`
Expected: 6 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/cc_chat/orchestrator.py backend/tests/cc_chat/test_orchestrator.py
git commit -m "Add CCChatOrchestrator.send with stream-json parsing and session resume"
```

---

## Task 8: Implement `CCChatOrchestrator.close`

**Files:**
- Modify: `backend/api/services/cc_chat/orchestrator.py`
- Modify: `backend/tests/cc_chat/test_orchestrator.py`

- [ ] **Step 1: Write the failing close tests**

Append to `backend/tests/cc_chat/test_orchestrator.py`:

```python
@pytest.mark.asyncio
async def test_close_deletes_sandbox_and_removes_state(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    sid = await orch.start(experiment_id=FIXTURE_EXPERIMENT_ID, org_id="org-1")
    assert orch._sessions.get(sid) is not None

    await orch.close(session_id=sid)

    assert orch._sessions.get(sid) is None
    assert fake_daytona.created[0].deleted is True


@pytest.mark.asyncio
async def test_close_unknown_session_is_idempotent(fake_daytona):
    orch = _make_orchestrator(fake_daytona)
    # No raise
    await orch.close(session_id="never-existed")
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v -k close`
Expected: 2 failures.

- [ ] **Step 3: Implement `close`**

Add to `CCChatOrchestrator`:

```python
    async def close(self, *, session_id: str) -> None:
        state = self._sessions.pop(session_id)
        if state is None:
            return
        sandbox = self._sandbox_handles.pop(session_id, None)
        if sandbox is not None:
            await self._daytona.delete_sandbox(sandbox)
```

- [ ] **Step 4: Run close tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v -k close`
Expected: 2 passed.

- [ ] **Step 5: Run all orchestrator tests**

Run: `pytest backend/tests/cc_chat/test_orchestrator.py -v`
Expected: 8 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/cc_chat/orchestrator.py backend/tests/cc_chat/test_orchestrator.py
git commit -m "Add CCChatOrchestrator.close with idempotent teardown"
```

---

## Task 9: Implement `S3FileStore`

Production implementation of `ExperimentFileStore` that reads from the existing oddish S3 storage layer.

**Files:**
- Modify: `backend/api/services/cc_chat/file_store.py` (add `S3FileStore`)
- Test: `backend/tests/cc_chat/test_s3_file_store.py`

- [ ] **Step 1: Confirm the existing storage helper signatures**

Run: `grep -n "list_trial_files\|download_trial_directory\|class StorageClient\|def get_storage_client" /Users/kateyeh/Developer/os_repos/oddish/oddish/src/oddish/db/storage.py | head -20`
Expected: confirms `class StorageClient`, `get_storage_client()` factory, `list_trial_files`, `download_trial_directory` (or similar) helpers are available.

- [ ] **Step 2: Write the failing test (uses a fake StorageClient)**

Create `backend/tests/cc_chat/test_s3_file_store.py`:

```python
import pytest

from api.services.cc_chat.file_store import S3FileStore


class FakeStorage:
    def __init__(self, files: dict[str, bytes]) -> None:
        # files keyed by full S3 key, e.g. "tasks/exp-1/trials/t-0/result.json"
        self._files = files

    async def list_keys_under(self, prefix: str) -> list[str]:
        return [k for k in self._files if k.startswith(prefix)]

    async def get_object(self, key: str) -> bytes:
        return self._files[key]


@pytest.mark.asyncio
async def test_s3_file_store_yields_relative_paths():
    storage = FakeStorage({
        "tasks/exp-1/trials/t-0/result.json": b'{"reward": 1}',
        "tasks/exp-1/trials/t-0/trial.log": b"hello\n",
        "tasks/exp-1/trials/t-1/result.json": b'{"reward": 0}',
    })
    store = S3FileStore(storage=storage, prefix_template="tasks/{experiment_id}/trials/")
    files = [(rel, content) async for rel, content in store.iter_files("exp-1")]
    rels = {rel for rel, _ in files}
    assert "t-0/result.json" in rels
    assert "t-0/trial.log" in rels
    assert "t-1/result.json" in rels
```

- [ ] **Step 3: Run, expect failure**

Run: `pytest backend/tests/cc_chat/test_s3_file_store.py -v`
Expected: `ImportError: cannot import name 'S3FileStore' ...`.

- [ ] **Step 4: Implement `S3FileStore`**

Append to `backend/api/services/cc_chat/file_store.py`:

```python
from typing import Awaitable, Callable, Protocol


class _StorageLike(Protocol):
    """Just the methods we need; lets us inject test doubles."""
    async def list_keys_under(self, prefix: str) -> list[str]: ...
    async def get_object(self, key: str) -> bytes: ...


class S3FileStore:
    """Reads experiment files from S3 via the oddish StorageClient.

    The S3 layout in production is `tasks/<experiment_id>/trials/<trial_id>/...`
    but is configurable via `prefix_template` for forward-compat.
    """

    def __init__(
        self,
        *,
        storage: _StorageLike,
        prefix_template: str = "tasks/{experiment_id}/trials/",
    ) -> None:
        self._storage = storage
        self._prefix_template = prefix_template

    async def iter_files(
        self, experiment_id: str
    ) -> AsyncIterator[tuple[str, bytes]]:
        prefix = self._prefix_template.format(experiment_id=experiment_id)
        keys = await self._storage.list_keys_under(prefix)
        for key in keys:
            rel = key[len(prefix):]
            if any(part.startswith(".") for part in rel.split("/") if part):
                continue
            content = await self._storage.get_object(key)
            yield rel, content
```

- [ ] **Step 5: Run tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_s3_file_store.py -v`
Expected: 1 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/cc_chat/file_store.py backend/tests/cc_chat/test_s3_file_store.py
git commit -m "Add S3FileStore implementation for production"
```

> **Implementer note:** wiring `S3FileStore` into the actual oddish `StorageClient` (which exposes `list_trial_files` / `download_trial_directory`, not `list_keys_under` / `get_object`) is done in Task 11 where we adapt the real storage interface to the `_StorageLike` shape.

---

## Task 10: Add the FastAPI router

Three endpoints. Org-scoped via `require_auth`.

**Files:**
- Create: `backend/api/routers/cc_chat.py`
- Test: `backend/tests/cc_chat/test_router.py`

- [ ] **Step 1: Write the failing router test**

Create `backend/tests/cc_chat/test_router.py`:

```python
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.routers import cc_chat as cc_chat_router
from api.services.cc_chat.orchestrator import SessionNotFound


class FakeOrchestrator:
    def __init__(self) -> None:
        self.start_calls: list[tuple[str, str]] = []
        self.send_calls: list[tuple[str, str]] = []
        self.close_calls: list[str] = []
        self.next_session_id = "sid-test"
        self.canned_events: list[dict] = []

    async def start(self, *, experiment_id: str, org_id: str) -> str:
        self.start_calls.append((experiment_id, org_id))
        return self.next_session_id

    async def send(self, *, session_id: str, content: str):
        self.send_calls.append((session_id, content))
        for ev in self.canned_events:
            yield ev

    async def close(self, *, session_id: str) -> None:
        self.close_calls.append(session_id)


@pytest.fixture
def app_with_fake(monkeypatch):
    fake = FakeOrchestrator()

    def get_orch_override():
        return fake

    monkeypatch.setattr(
        cc_chat_router, "get_orchestrator", get_orch_override
    )

    # Bypass auth: the router takes auth via Depends(require_auth);
    # we override the dep here.
    from auth import AuthContext, AuthMethod, APIKeyScope

    def fake_auth():
        return AuthContext(
            org_id="org-1",
            user_id="user-1",
            method=AuthMethod.API_KEY,
            scope=APIKeyScope.FULL,
        )

    app = FastAPI()
    app.include_router(cc_chat_router.router)

    from auth import require_auth
    app.dependency_overrides[require_auth] = fake_auth
    return app, fake


def test_post_session_returns_session_id(app_with_fake):
    app, fake = app_with_fake
    client = TestClient(app)
    r = client.post("/api/experiments/exp-1/cc-session")
    assert r.status_code == 200
    assert r.json() == {"session_id": "sid-test"}
    assert fake.start_calls == [("exp-1", "org-1")]


def test_post_message_streams_sse(app_with_fake):
    app, fake = app_with_fake
    fake.canned_events = [
        {"type": "system", "subtype": "init", "session_id": "cc-uuid-1"},
        {"type": "assistant", "delta": "hi"},
        {"type": "result"},
    ]
    client = TestClient(app)
    with client.stream(
        "POST",
        "/api/experiments/exp-1/cc-session/sid-test/messages",
        json={"content": "what failed?"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert "event: message" in body
    assert "event: done" in body
    # And the orchestrator was actually called
    assert fake.send_calls == [("sid-test", "what failed?")]


def test_post_message_unknown_session_404(app_with_fake):
    app, fake = app_with_fake

    async def boom(*, session_id, content):
        raise SessionNotFound(session_id)
        yield  # make it an async generator

    fake.send = boom  # type: ignore[assignment]
    client = TestClient(app)
    r = client.post(
        "/api/experiments/exp-1/cc-session/missing/messages",
        json={"content": "x"},
    )
    assert r.status_code == 404


def test_delete_session_calls_close(app_with_fake):
    app, fake = app_with_fake
    client = TestClient(app)
    r = client.delete("/api/experiments/exp-1/cc-session/sid-test")
    assert r.status_code == 204
    assert fake.close_calls == ["sid-test"]
```

- [ ] **Step 2: Run, expect failure**

Run: `pytest backend/tests/cc_chat/test_router.py -v`
Expected: `ModuleNotFoundError: No module named 'api.routers.cc_chat'`.

- [ ] **Step 3: Implement the router**

Create `backend/api/routers/cc_chat.py`:

```python
from __future__ import annotations

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from auth import APIKeyScope, AuthContext, require_auth

from api.services.cc_chat.orchestrator import (
    CCChatOrchestrator,
    SessionNotFound,
)


router = APIRouter(tags=["CC Chat"])


_orchestrator_singleton: CCChatOrchestrator | None = None


def get_orchestrator() -> CCChatOrchestrator:
    """Dependency-style accessor; constructed in app startup (Task 11)."""
    if _orchestrator_singleton is None:
        raise RuntimeError(
            "CCChatOrchestrator not initialized; call init_orchestrator()"
        )
    return _orchestrator_singleton


def init_orchestrator(orch: CCChatOrchestrator) -> None:
    global _orchestrator_singleton
    _orchestrator_singleton = orch


class StartResponse(BaseModel):
    session_id: str


class SendMessageRequest(BaseModel):
    content: str


@router.post(
    "/api/experiments/{experiment_id}/cc-session",
    response_model=StartResponse,
)
async def start_session(
    experiment_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> StartResponse:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()
    sid = await orch.start(experiment_id=experiment_id, org_id=auth.org_id)
    return StartResponse(session_id=sid)


@router.post(
    "/api/experiments/{experiment_id}/cc-session/{session_id}/messages",
)
async def send_message(
    experiment_id: str,
    session_id: str,
    body: SendMessageRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> StreamingResponse:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()

    async def event_stream():
        try:
            async for event in orch.send(
                session_id=session_id, content=body.content
            ):
                kind = "error" if event.get("type") == "_stderr" else "message"
                yield f"event: {kind}\ndata: {json.dumps(event)}\n\n"
        except SessionNotFound:
            yield (
                'event: error\n'
                'data: {"type": "session_not_found"}\n\n'
            )
            return
        yield "event: done\ndata: {}\n\n"

    # We have to detect SessionNotFound up-front for the 404 status code,
    # because once we've returned StreamingResponse, the status is locked.
    state = orch._sessions.get(session_id)  # type: ignore[attr-defined]
    if state is None:
        raise HTTPException(status_code=404, detail="session_not_found")

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.delete(
    "/api/experiments/{experiment_id}/cc-session/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def close_session(
    experiment_id: str,
    session_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> Response:
    auth.require_scope(APIKeyScope.READ)
    orch = get_orchestrator()
    await orch.close(session_id=session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
```

- [ ] **Step 4: Adjust the test to match the up-front 404 path**

The `test_post_message_unknown_session_404` test relies on the fake's send raising `SessionNotFound`. Because the router now checks the registry up-front, the fake also needs to expose `_sessions`. Update the fake in `test_router.py`:

In `FakeOrchestrator.__init__`, add:
```python
        self._sessions = type("Reg", (), {"get": lambda self_, sid: None})()
```

Then in the test, before invoking the request, populate it for sessions that *should* exist:
```python
    # In test_post_message_streams_sse, before the .stream(...) call:
    fake._sessions = type("Reg", (), {"get": lambda self_, sid: object()})()
```

(For the 404 test, leave `_sessions.get` returning `None`.)

- [ ] **Step 5: Run all router tests, expect pass**

Run: `pytest backend/tests/cc_chat/test_router.py -v`
Expected: 4 passed.

- [ ] **Step 6: Commit**

```bash
git add backend/api/routers/cc_chat.py backend/tests/cc_chat/test_router.py
git commit -m "Add cc_chat router with start/send/close endpoints"
```

---

## Task 11: Wire the orchestrator + router into `app.py`

**Files:**
- Modify: `backend/api/app.py`

- [ ] **Step 1: Read the existing `create_app` to find the include-router block**

Run: `grep -n "include_router\|from api.routers" /Users/kateyeh/Developer/os_repos/oddish/backend/api/app.py | head -20`
Expected: locates the import block and the sequence of `api.include_router(...)` calls.

- [ ] **Step 2: Add the orchestrator init + router include**

In `backend/api/app.py`:

1. Add to the import block alongside the existing routers:

```python
from api.routers import cc_chat
```

2. After the existing `lifespan` decorator definition, add a helper for orchestrator setup. Then call it from `create_app` before `include_router(cc_chat.router)`:

```python
def _init_cc_chat_orchestrator() -> None:
    from oddish.config import settings
    from api.services.cc_chat.daytona_client import RealDaytonaClient
    from api.services.cc_chat.file_store import LocalFileStore, S3FileStore
    from api.services.cc_chat.orchestrator import CCChatOrchestrator
    from api.routers.cc_chat import init_orchestrator

    if not settings.daytona_api_key:
        # Skip wiring; the endpoints will fail loudly with 500 when called.
        # We don't want the entire app to refuse to boot in environments
        # that don't use the chat feature.
        return
    if not settings.anthropic_api_key:
        return

    if settings.cc_chat_local_jobs_dir:
        from pathlib import Path
        file_store = LocalFileStore(
            base_path=Path(settings.cc_chat_local_jobs_dir)
        )
    else:
        # Adapt the real storage client to the _StorageLike protocol.
        from oddish.db.storage import get_storage_client

        class _OddishStorageAdapter:
            def __init__(self) -> None:
                self._client = get_storage_client()

            async def list_keys_under(self, prefix: str) -> list[str]:
                # The existing client exposes list_trial_files; use its
                # underlying paginator. If a more general listing helper
                # is added later, replace this body.
                # Implementer: confirm exact method name; this stub may
                # need updating against oddish/db/storage.py.
                raise NotImplementedError(
                    "Wire S3FileStore to oddish StorageClient; see comment."
                )

            async def get_object(self, key: str) -> bytes:
                raise NotImplementedError

        file_store = S3FileStore(storage=_OddishStorageAdapter())

    orch = CCChatOrchestrator(
        daytona=RealDaytonaClient(api_key=settings.daytona_api_key),
        file_store=file_store,
        anthropic_api_key=settings.anthropic_api_key,
        auto_stop_minutes=30,
    )
    init_orchestrator(orch)
```

3. Inside `create_app()`, add (placed alongside the other `include_router` calls):

```python
    _init_cc_chat_orchestrator()
    api.include_router(cc_chat.router)
```

- [ ] **Step 3: Run app boot smoke check**

Run:
```bash
cd /Users/kateyeh/Developer/os_repos/oddish/backend && \
  ODDISH_CC_CHAT_LOCAL_JOBS_DIR=/Users/kateyeh/Developer/os_repos/oddish/jobs \
  DAYTONA_API_KEY=fake-key \
  ANTHROPIC_API_KEY=fake-key \
  python -c "from api.app import create_app; app = create_app(); print('routes:', [r.path for r in app.routes if 'cc-session' in r.path])"
```
Expected: prints three paths containing `cc-session`.

- [ ] **Step 4: Commit**

```bash
git add backend/api/app.py
git commit -m "Wire CCChatOrchestrator and cc_chat router into app"
```

> **Implementer note:** the `_OddishStorageAdapter` body is an explicit stub. Before shipping to prod, replace its `list_keys_under` / `get_object` bodies with calls into `oddish.db.storage` (likely `StorageClient.list_trial_files` paginated, plus an S3 `get_object` via the existing aioboto3 client). That's a small follow-up commit; gate behind a manual test against staging.

---

## Task 12: Frontend — `cc-chat-stream.ts` SSE helper

**Files:**
- Create: `frontend/src/lib/cc-chat-stream.ts`

- [ ] **Step 1: Implement the helper**

Create `frontend/src/lib/cc-chat-stream.ts`:

```typescript
export type ChatStreamEvent =
  | { kind: "message"; data: unknown }
  | { kind: "error"; data: unknown }
  | { kind: "done" };

export async function streamCCChatMessage(
  url: string,
  body: { content: string },
  onEvent: (e: ChatStreamEvent) => void,
  signal?: AbortSignal,
): Promise<void> {
  const res = await fetch(url, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    throw new Error(`cc-chat send failed: ${res.status}`);
  }
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    // SSE frames are separated by a blank line.
    let blankIdx: number;
    while ((blankIdx = buffer.indexOf("\n\n")) !== -1) {
      const frame = buffer.slice(0, blankIdx);
      buffer = buffer.slice(blankIdx + 2);

      let event = "message";
      let dataLines: string[] = [];
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) {
          event = line.slice("event:".length).trim();
        } else if (line.startsWith("data:")) {
          dataLines.push(line.slice("data:".length).trim());
        }
      }
      const dataRaw = dataLines.join("\n");
      let parsed: unknown = dataRaw;
      try {
        parsed = JSON.parse(dataRaw);
      } catch {
        // leave as raw string
      }

      if (event === "done") {
        onEvent({ kind: "done" });
        return;
      } else if (event === "error") {
        onEvent({ kind: "error", data: parsed });
      } else {
        onEvent({ kind: "message", data: parsed });
      }
    }
  }
  onEvent({ kind: "done" });
}
```

- [ ] **Step 2: Type-check**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish/frontend && pnpm tsc --noEmit`
Expected: no errors related to this file.

- [ ] **Step 3: Commit**

```bash
git add frontend/src/lib/cc-chat-stream.ts
git commit -m "Add SSE helper for CC chat message streaming"
```

---

## Task 13: Frontend — `cc-chat-modal.tsx`

**Files:**
- Create: `frontend/src/components/cc-chat-modal.tsx`

- [ ] **Step 1: Implement the modal**

Create `frontend/src/components/cc-chat-modal.tsx`:

```typescript
"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { CodeBlock } from "@/components/code-block";
import { streamCCChatMessage } from "@/lib/cc-chat-stream";

type Phase = "creating" | "ready" | "thinking" | "idle" | "closed" | "error";

type ChatTurn =
  | { role: "user"; text: string }
  | { role: "assistant"; events: unknown[] };

export function CCChatModal({
  experimentId,
  open,
  onOpenChange,
}: {
  experimentId: string;
  open: boolean;
  onOpenChange: (next: boolean) => void;
}) {
  const [phase, setPhase] = useState<Phase>("creating");
  const [error, setError] = useState<string | null>(null);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [draft, setDraft] = useState("");
  const sendingRef = useRef(false);

  const start = useCallback(async () => {
    setPhase("creating");
    setError(null);
    try {
      const res = await fetch(
        `/api/experiments/${encodeURIComponent(experimentId)}/cc-session`,
        { method: "POST" },
      );
      if (!res.ok) throw new Error(`start ${res.status}`);
      const data = (await res.json()) as { session_id: string };
      setSessionId(data.session_id);
      setPhase("ready");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setPhase("error");
    }
  }, [experimentId]);

  useEffect(() => {
    if (!open) return;
    void start();
  }, [open, start]);

  // Close on unmount / open=false
  useEffect(() => {
    return () => {
      if (sessionId) {
        const url = `/api/experiments/${encodeURIComponent(
          experimentId,
        )}/cc-session/${encodeURIComponent(sessionId)}`;
        if (typeof navigator !== "undefined" && navigator.sendBeacon) {
          navigator.sendBeacon(url);
        } else {
          void fetch(url, { method: "DELETE", keepalive: true });
        }
      }
    };
  }, [experimentId, sessionId]);

  const send = useCallback(async () => {
    if (!sessionId || sendingRef.current || !draft.trim()) return;
    sendingRef.current = true;
    const content = draft;
    setDraft("");
    setTurns((prev) => [
      ...prev,
      { role: "user", text: content },
      { role: "assistant", events: [] },
    ]);
    setPhase("thinking");

    const url = `/api/experiments/${encodeURIComponent(
      experimentId,
    )}/cc-session/${encodeURIComponent(sessionId)}/messages`;

    try {
      await streamCCChatMessage(url, { content }, (e) => {
        if (e.kind === "message" || e.kind === "error") {
          setTurns((prev) => {
            const next = [...prev];
            const last = next[next.length - 1];
            if (last && last.role === "assistant") {
              last.events = [...last.events, e.data];
            }
            return next;
          });
        }
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setPhase("error");
    } finally {
      sendingRef.current = false;
      setPhase("idle");
    }
  }, [draft, experimentId, sessionId]);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Chat with experiment logs</DialogTitle>
          <DialogDescription>
            Status: <span className="font-mono text-xs">{phase}</span>
            {error ? (
              <span className="text-destructive ml-2">— {error}</span>
            ) : null}
          </DialogDescription>
        </DialogHeader>

        <div className="max-h-[60vh] overflow-y-auto space-y-4 py-2">
          {turns.map((turn, i) => (
            <div key={i}>
              {turn.role === "user" ? (
                <div className="rounded-md border bg-muted/50 px-3 py-2">
                  <div className="text-xs font-medium uppercase opacity-60">
                    you
                  </div>
                  <div className="whitespace-pre-wrap text-sm">{turn.text}</div>
                </div>
              ) : (
                <div className="rounded-md border px-3 py-2">
                  <div className="text-xs font-medium uppercase opacity-60">
                    claude
                  </div>
                  {turn.events.length === 0 ? (
                    <div className="text-sm italic opacity-60">thinking…</div>
                  ) : (
                    <CodeBlock
                      code={turn.events
                        .map((e) => JSON.stringify(e, null, 2))
                        .join("\n")}
                      language="json"
                    />
                  )}
                </div>
              )}
            </div>
          ))}
        </div>

        <form
          className="flex gap-2"
          onSubmit={(e) => {
            e.preventDefault();
            void send();
          }}
        >
          <Input
            placeholder="Ask about this experiment…"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            disabled={phase === "thinking" || phase === "creating" || phase === "error"}
          />
          <Button
            type="submit"
            disabled={phase === "thinking" || phase === "creating" || phase === "error" || !draft.trim()}
          >
            Send
          </Button>
        </form>
      </DialogContent>
    </Dialog>
  );
}
```

> **Implementer note:** the rendering of CC stream-json events is intentionally minimal in v1 (raw JSON in a `CodeBlock`). Pretty-rendering tool calls / text deltas is a v2 enhancement; the trajectory-viewer's `getTextFromContent` helper would be the starting point for that.

- [ ] **Step 2: Type-check**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish/frontend && pnpm tsc --noEmit`
Expected: no errors. If `@/components/code-block` exports a different prop shape, adjust the import and props to match (the code is at `frontend/src/components/code-block`).

- [ ] **Step 3: Commit**

```bash
git add frontend/src/components/cc-chat-modal.tsx
git commit -m "Add CCChatModal component for experiment-level chat with logs"
```

---

## Task 14: Frontend — wire the "Chat with logs" button

**Files:**
- Modify: `frontend/src/app/(app)/experiments/[experiment]/experiment-client.tsx`

- [ ] **Step 1: Locate the header insertion point**

Run: `grep -n "ExperimentShareButton\|experimentId\b" /Users/kateyeh/Developer/os_repos/oddish/frontend/src/app/\(app\)/experiments/\[experiment\]/experiment-client.tsx | head`
Expected: confirms `experimentId` is in scope and shows where `ExperimentShareButton` is rendered (the new button slots in next to it).

- [ ] **Step 2: Add the button + modal state**

In the file, near the top of the component body (alongside the other `useState` hooks), add:

```typescript
  const [isChatOpen, setIsChatOpen] = useState(false);
```

Add the import at the top:

```typescript
import { CCChatModal } from "@/components/cc-chat-modal";
import { Button } from "@/components/ui/button";
```

Then, next to the existing `<ExperimentShareButton ... />` in the header JSX, add:

```tsx
<Button variant="outline" onClick={() => setIsChatOpen(true)}>
  Chat with logs
</Button>
<CCChatModal
  experimentId={experimentId}
  open={isChatOpen}
  onOpenChange={setIsChatOpen}
/>
```

- [ ] **Step 3: Type-check**

Run: `cd /Users/kateyeh/Developer/os_repos/oddish/frontend && pnpm tsc --noEmit`
Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add frontend/src/app/\(app\)/experiments/\[experiment\]/experiment-client.tsx
git commit -m "Add 'Chat with logs' button to experiment page header"
```

---

## Task 15: Smoke test script

The smoke test runs the full path against real Daytona and the local `jobs/` fixtures. Catches regressions in the `claude --print --output-format=stream-json` envelope (esp. the `system/init session_id` field).

**Files:**
- Create: `backend/scripts/smoke_cc_chat.py`

- [ ] **Step 1: Implement the smoke script**

Create `backend/scripts/smoke_cc_chat.py`:

```python
"""End-to-end smoke test for the CC chat feature.

Requires DAYTONA_API_KEY and ANTHROPIC_API_KEY in env. Hits real Daytona
and the real Anthropic API. Designed to be run in CI / pre-deploy, not
on every commit.

Exits 0 on success, 1 on failure.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_BASE = REPO_ROOT / "jobs"
FIXTURE_EXPERIMENT_ID = "2026-04-26__16-45-36"


async def _run() -> int:
    from api.services.cc_chat.daytona_client import RealDaytonaClient
    from api.services.cc_chat.file_store import LocalFileStore
    from api.services.cc_chat.orchestrator import CCChatOrchestrator

    daytona_key = os.environ.get("DAYTONA_API_KEY")
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    if not daytona_key or not anthropic_key:
        print("skip: DAYTONA_API_KEY or ANTHROPIC_API_KEY missing")
        return 0

    orch = CCChatOrchestrator(
        daytona=RealDaytonaClient(api_key=daytona_key),
        file_store=LocalFileStore(base_path=FIXTURE_BASE),
        anthropic_api_key=anthropic_key,
        auto_stop_minutes=10,
    )

    sid = await orch.start(
        experiment_id=FIXTURE_EXPERIMENT_ID, org_id="smoke"
    )
    print(f"session: {sid}")

    saw_init = False
    saw_result = False
    async for ev in orch.send(
        session_id=sid,
        content="List the trial directories under jobs/ and tell me how many you found.",
    ):
        if ev.get("type") == "system" and ev.get("subtype") == "init":
            saw_init = True
            print(f"init session_id: {ev.get('session_id')}")
        if ev.get("type") == "result":
            saw_result = True
            print(f"result: {ev}")

    await orch.close(session_id=sid)

    if not saw_init:
        print("FAIL: never saw system/init event with session_id")
        return 1
    if not saw_result:
        print("FAIL: never saw result event")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
```

- [ ] **Step 2: Run the smoke test (only if env vars are present)**

Run:
```bash
cd /Users/kateyeh/Developer/os_repos/oddish && \
  PYTHONPATH=backend \
  python backend/scripts/smoke_cc_chat.py
```
Expected (if env vars set): `PASS` after a few minutes. If env vars are missing, prints `skip: ...` and exits 0.

- [ ] **Step 3: Commit**

```bash
git add backend/scripts/smoke_cc_chat.py
git commit -m "Add smoke test script for CC chat e2e"
```

---

## Task 16: Manual end-to-end QA

**Files:** none (manual)

- [ ] **Step 1: Start the backend with local fixtures**

Run:
```bash
cd /Users/kateyeh/Developer/os_repos/oddish/backend && \
  ODDISH_CC_CHAT_LOCAL_JOBS_DIR=/Users/kateyeh/Developer/os_repos/oddish/jobs \
  DAYTONA_API_KEY=<your-key> \
  ANTHROPIC_API_KEY=<your-key> \
  uvicorn api.app:create_app --factory --reload --port 8000
```
Expected: backend boots, prints something like `Uvicorn running on http://127.0.0.1:8000`.

- [ ] **Step 2: Start the frontend**

Run:
```bash
cd /Users/kateyeh/Developer/os_repos/oddish/frontend && pnpm dev
```
Expected: Next.js dev server boots on http://localhost:3000.

- [ ] **Step 3: Open the experiment page**

In a browser, navigate to a local experiment URL that resolves to the fixture id `2026-04-26__16-45-36`. (May require seeding the DB; if local DB doesn't have this experiment, run against a real staging experiment instead and adjust `ODDISH_CC_CHAT_LOCAL_JOBS_DIR` to a path that contains a matching subdirectory.)

- [ ] **Step 4: Click "Chat with logs"**

Expected: modal opens, status pill cycles `creating` → `ready`. (If creating takes >60s, sandbox cold-start is the likely cause — wait it out once.)

- [ ] **Step 5: Send a test message**

Type: `How many trials does this experiment have? List them.`

Expected: status becomes `thinking`, a series of stream-json events appear in the assistant bubble, status drops to `idle`. The answer should reference at least the fixture trial id `hello-world__eU7yQqg`.

- [ ] **Step 6: Send a second message to verify resume**

Type: `Did that trial pass the verifier?`

Expected: response references `verifier/ctrf.json` or `verifier/reward.txt` and gives the verdict. Looking at backend logs, you should see the second `claude` exec command include `--resume <session_id>`.

- [ ] **Step 7: Close the modal, watch the sandbox get deleted**

In the Daytona dashboard (or via SDK), confirm the sandbox associated with this session disappears within ~10 seconds.

- [ ] **Step 8: No commit (manual QA only)**

If anything failed, file the issue and stop. If everything passed, the feature is done for v1.

---

## Self-Review Notes

- **Spec coverage:**
  - Frontend modal + button: Tasks 12-14 ✓
  - Backend orchestrator (start/send/close): Tasks 6-8 ✓
  - LocalFileStore + S3FileStore: Tasks 2, 9 ✓
  - CLAUDE.md generator: Task 3 ✓
  - SessionRegistry + idle sweep data: Task 4 ✓ (sweeper task itself is documented in the spec but deferred — see follow-up)
  - Daytona client wrapper: Task 5 ✓
  - Router: Task 10 ✓
  - app.py wiring + settings: Tasks 1, 11 ✓
  - Smoke test: Task 15 ✓
  - Manual QA: Task 16 ✓
- **Idle sweeper task:** the spec calls for a 5-minute background sweep. Task 4 lays the data + `idle()` method; the actual `asyncio.create_task(loop)` is a one-screen addition that can be appended to `_init_cc_chat_orchestrator` in app.py once the rest is working. Documented as a follow-up in the implementer notes below, since `Daytona`'s own `auto_stop_interval` already covers leak protection in v1.
- **Naming consistency check:** `daytona_session_id` ("cc"), `claude_session_id` (UUID from CC), and `session_id` (our own opaque token) are three distinct names used consistently across orchestrator, registry, and router. Verified.
- **Type consistency:** `ExperimentFileStore.iter_files` yields `tuple[str, bytes]` everywhere; `DaytonaClient.create_sandbox` returns `CreatedSandbox` everywhere; `SessionState.claude_session_id` is `str | None` everywhere.

## Follow-ups (out of scope for this plan)

- Idle sweeper background task (small addition to app.py).
- Replace `_OddishStorageAdapter` stub bodies in app.py with real S3 calls.
- Pretty-render CC stream-json events in the modal (reuse `getTextFromContent` from trajectory-viewer.tsx).
- Conversation persistence across modal-close.
- Multi-replica session routing.
- BYO Anthropic API key per user.
