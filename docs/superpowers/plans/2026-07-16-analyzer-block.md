# AnalyzerBlock Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a standalone, chainable `AnalyzerBlock` primitive that runs one LLM job through a swappable backend (Daytona sandbox or direct Anthropic API), streams the output, and guarantees a save to S3 + Postgres on every exit path (success, exception, cancellation) with all logging keyed by analyzer type.

**Architecture:** A new `analyzer_blocks` table stores each block's lifecycle + input/output. `AnalyzerBlock` (in `backend/api/services/analyzer_block.py`) drives an injectable `AnalyzerLLMClient` (in `backend/api/services/analyzer_llm_client.py`) — `ApiAnalyzerLLMClient` (AsyncAnthropic streaming), `SandboxAnalyzerLLMClient` (Daytona + `ClaudeCodeRuntime.stream_chat`), or `FakeAnalyzerLLMClient` (tests). The block's `run()` uses a `try/except BaseException/finally` with `asyncio.shield(_persist())` so persistence survives cancellation, and `save_to_s3`/`save_to_db` are each independently wrapped so one failing never blocks the other.

**Tech Stack:** Python 3.11+/3.13, SQLAlchemy 2 async ORM (`Mapped`/`mapped_column`), Alembic, `anthropic==0.76.0` (`AsyncAnthropic`), asyncio, pytest.

## Global Constraints

- **Never commit/push to `main`.** Work stays on the current feature branch `kate/save_analyer_logs`.
- **DB writes** go through `async with get_session()` (from `oddish.db import get_session`) — it commits on success, rolls back on any `BaseException`. No explicit `.commit()`.
- **S3 writes** go through `get_storage_client()` (from `oddish.db.storage import get_storage_client`); `await ...upload_bytes(data: bytes, s3_key: str, *, content_type: str | None = None)`.
- **Timestamps** use `utcnow()` from `oddish.db.models` (timezone-aware UTC).
- **New table reuses the existing `jobstatus` Postgres enum** — reference it with `create_type=False`; do NOT create a new enum type. Store `type` and `llm_client_type` as plain `String(64)` (the enum `.value`), so no new pg enum types are introduced and `models.py` gains no dependency on the backend package.
- **`metadata` is a reserved attribute** on SQLAlchemy's declarative `Base`: the Python attribute is `block_metadata`, mapped to a DB column literally named `metadata`.
- **Backend tests that need Postgres** must use a throwaway, freshly-migrated DB — never the shared local DB. The tests in this plan are written to avoid a live DB by patching `get_session`/`get_storage_client`; the model test is pure ORM introspection.
- **Default API model:** `claude-opus-4-8` (per repo convention for Claude runs).
- Spec: `docs/superpowers/specs/2026-07-16-analyzer-block-design.md`.

---

### Task 1: `AnalyzerBlockModel` table + migration

**Files:**
- Modify: `oddish/src/oddish/db/models.py` (add model near `AnalyzerModel` ~line 585; add to `register_soft_delete_models(...)` call ~line 1994)
- Modify: `oddish/src/oddish/db/__init__.py` (import + `__all__` export, alongside `AnalyzerModel`)
- Create: `oddish/alembic/versions/analyzers_007_add_analyzer_blocks.py`
- Test: `backend/tests/test_analyzer_block_model.py`

**Interfaces:**
- Produces: `AnalyzerBlockModel` (ORM model, table `analyzer_blocks`) with columns `id, created_at, updated_at, deleted_at, analyzer_id, type, key_prefix, llm_client_type, prompt, input, output, status, error, job_started_at, job_ended_at, job_duration_seconds` and Python attribute `block_metadata` → DB column `metadata`. Importable as `from oddish.db.models import AnalyzerBlockModel` and `from oddish.db import AnalyzerBlockModel`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_analyzer_block_model.py`:

```python
from oddish.db.models import AnalyzerBlockModel, JobStatus


def test_tablename_and_columns():
    cols = set(AnalyzerBlockModel.__table__.columns.keys())
    assert AnalyzerBlockModel.__tablename__ == "analyzer_blocks"
    # DB column is literally "metadata", not "block_metadata".
    assert "metadata" in cols
    assert "block_metadata" not in cols
    expected = {
        "id", "created_at", "updated_at", "deleted_at",
        "analyzer_id", "type", "key_prefix", "llm_client_type",
        "prompt", "input", "output", "status", "error",
        "job_started_at", "job_ended_at", "job_duration_seconds", "metadata",
    }
    assert expected <= cols


def test_metadata_attribute_maps_to_metadata_column():
    # The Python attribute is block_metadata (metadata is reserved on Base).
    assert AnalyzerBlockModel.block_metadata.property.columns[0].name == "metadata"


def test_status_reuses_jobstatus_enum():
    status_col = AnalyzerBlockModel.__table__.columns["status"]
    assert status_col.type.name == "jobstatus"
    # Must not try to CREATE TYPE — the type already exists.
    assert status_col.type.create_type is False


def test_importable_from_db_package():
    from oddish.db import AnalyzerBlockModel as Exported
    assert Exported is AnalyzerBlockModel
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_block_model.py -v`
Expected: FAIL with `ImportError: cannot import name 'AnalyzerBlockModel'`.

- [ ] **Step 3: Add the model to `oddish/src/oddish/db/models.py`**

Insert immediately after the `AnalyzerModel` class (after ~line 585, before `class TaskModel`). All imports used (`String, Text, Integer, Float, DateTime, Index, text, SQLEnum, JSONB, Mapped, mapped_column, generate_id, utcnow, JobStatus, TimestampedMixin, Base, datetime`) already exist at the top of this file.

```python
class AnalyzerBlockModel(TimestampedMixin, Base):
    """One run of a single composable analyzer block.

    Standalone primitive (not part of ``run_analyzer_generation_job``): many
    blocks chain arbitrarily in test scripts. ``type`` / ``llm_client_type`` are
    the ``.value`` of the ``AnalyzerType`` / ``LLMClientType`` enums defined in
    ``backend/api/services`` -- stored as plain strings so this module stays free
    of any backend-package dependency. Raw streamed output lives in S3 at
    ``{key_prefix}/{id}``; ``output`` here is the accumulated/parsed result.
    """

    __tablename__ = "analyzer_blocks"
    __table_args__ = (
        Index(
            "idx_analyzer_blocks_analyzer_id_live",
            "analyzer_id",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True, default=generate_id)
    analyzer_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    type: Mapped[str] = mapped_column(String(64), nullable=False)
    key_prefix: Mapped[str] = mapped_column(Text, nullable=False)
    llm_client_type: Mapped[str] = mapped_column(String(64), nullable=False)

    prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # input/output are arbitrary JSON (the block's I/O are typed ``any``).
    input: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    output: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    status: Mapped[JobStatus] = mapped_column(
        SQLEnum(JobStatus, name="jobstatus", create_type=False),
        default=JobStatus.PENDING,
        nullable=False,
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    job_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    job_ended_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    job_duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)

    # ``metadata`` is reserved on the declarative Base, so the attribute is
    # ``block_metadata`` while the DB column is literally named ``metadata``.
    block_metadata: Mapped[dict | None] = mapped_column("metadata", JSONB, nullable=True)
```

Then add `AnalyzerBlockModel` to the `register_soft_delete_models(...)` call (~line 1994), right after `AnalyzerModel,`:

```python
register_soft_delete_models(
    ExperimentModel,
    AnalyzerModel,
    AnalyzerBlockModel,
    TaskModel,
    ...
)
```

- [ ] **Step 4: Export from `oddish/src/oddish/db/__init__.py`**

In the ORM-models import block (~line 27, where `AnalyzerModel,` is imported from `.models`) add `AnalyzerBlockModel,`, and in `__all__` (~line 106, next to `"AnalyzerModel",`) add `"AnalyzerBlockModel",`.

- [ ] **Step 5: Run the model tests to verify they pass**

Run: `cd backend && uv run pytest tests/test_analyzer_block_model.py -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Write the Alembic migration**

Create `oddish/alembic/versions/analyzers_007_add_analyzer_blocks.py`:

```python
"""add analyzer_blocks table

Standalone table for the composable AnalyzerBlock primitive. Reuses the existing
``jobstatus`` enum (create_type=False -- do not CREATE TYPE). ``000_initial_schema``
runs ``create_all()``, so on a fresh DB the table already exists before this
migration runs -- hence the inspector guard.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "analyzers_007"
down_revision: Union[str, Sequence[str], None] = "analyzers_006"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    if "analyzer_blocks" in sa.inspect(bind).get_table_names():
        return
    jobstatus = postgresql.ENUM(name="jobstatus", create_type=False)
    op.create_table(
        "analyzer_blocks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("analyzer_id", sa.String(64), nullable=True),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("key_prefix", sa.Text, nullable=False),
        sa.Column("llm_client_type", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text, nullable=True),
        sa.Column("input", JSONB, nullable=True),
        sa.Column("output", JSONB, nullable=True),
        sa.Column("status", jobstatus, nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column("job_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("job_duration_seconds", sa.Float, nullable=True),
        sa.Column("metadata", JSONB, nullable=True),
    )
    op.create_index(
        "idx_analyzer_blocks_analyzer_id_live",
        "analyzer_blocks",
        ["analyzer_id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_analyzer_blocks_analyzer_id", "analyzer_blocks", ["analyzer_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_analyzer_blocks_analyzer_id", table_name="analyzer_blocks")
    op.drop_index("idx_analyzer_blocks_analyzer_id_live", table_name="analyzer_blocks")
    op.drop_table("analyzer_blocks")
```

Note: confirm `analyzers_006` is still the head of this lineage before committing — `cd oddish && uv run alembic heads` should list it (or a merge head). If a newer head exists, set `down_revision` to that head instead.

- [ ] **Step 7: Verify the migration is chained and applies**

Run: `cd oddish && uv run alembic history | head -20`
Expected: `analyzers_007` appears with `analyzers_006` as its down-revision, no "multiple heads" error introduced.

(If a throwaway test DB is available: `uv run alembic upgrade head` then `alembic downgrade -1` should both succeed. Skip if no DB is wired up locally — the inspector guard + `create_type=False` are the correctness-critical parts.)

- [ ] **Step 8: Commit**

```bash
git add oddish/src/oddish/db/models.py oddish/src/oddish/db/__init__.py \
        oddish/alembic/versions/analyzers_007_add_analyzer_blocks.py \
        backend/tests/test_analyzer_block_model.py
git commit -m "feat: add analyzer_blocks table for AnalyzerBlock primitive"
```

---

### Task 2: Enums, I/O dataclasses, and prefix-bound logging

**Files:**
- Rewrite: `backend/api/services/analyzer_block.py` (replace the current scaffold's `AnalyzerType`, `AnalyzerInput`, `AnalyzerOutput`; the `AnalyzerLLMClient`/`LLMClientType` scaffold in this file moves to Task 3's new file)
- Test: `backend/tests/test_analyzer_block.py`

**Interfaces:**
- Produces:
  - `class AnalyzerType(str, enum.Enum)` with `TRAJECTORY_FAILURE_ANALYSIS`, `HEADROOM_ANALYSIS`, `SCALING_ANALYSIS`.
  - `@dataclass AnalyzerInput: input: Any`
  - `@dataclass AnalyzerOutput: output: Any`
  - `block_key_prefix(analyzer_type: AnalyzerType) -> str` returning `f"analyzer/{analyzer_type.value}"`.
  - `block_logger(key_prefix: str) -> logging.LoggerAdapter` — every emitted record is prefixed with `[{key_prefix}]`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_analyzer_block.py`:

```python
import logging

from api.services.analyzer_block import (
    AnalyzerType,
    AnalyzerInput,
    AnalyzerOutput,
    block_key_prefix,
    block_logger,
)


def test_key_prefix_uses_enum_value():
    assert block_key_prefix(AnalyzerType.HEADROOM_ANALYSIS) == "analyzer/headroom_analysis"


def test_io_dataclasses_accept_any():
    assert AnalyzerInput(input={"a": 1}).input == {"a": 1}
    assert AnalyzerOutput(output="text").output == "text"


def test_block_logger_prepends_prefix(caplog):
    log = block_logger("analyzer/scaling_analysis")
    with caplog.at_level(logging.INFO):
        log.info("hello")
    assert "[analyzer/scaling_analysis] hello" in caplog.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py -v`
Expected: FAIL (`ImportError` for `block_key_prefix`/`block_logger`, or attribute errors).

- [ ] **Step 3: Write the module scaffold**

Replace the entire contents of `backend/api/services/analyzer_block.py` with (execution logic comes in later tasks — this task establishes types + logging only):

```python
from __future__ import annotations

import enum
import logging
from dataclasses import dataclass
from typing import Any


class AnalyzerType(str, enum.Enum):
    TRAJECTORY_FAILURE_ANALYSIS = "trajectory_failure_analysis"
    HEADROOM_ANALYSIS = "headroom_analysis"
    SCALING_ANALYSIS = "scaling_analysis"


@dataclass
class AnalyzerInput:
    input: Any


@dataclass
class AnalyzerOutput:
    output: Any


def block_key_prefix(analyzer_type: AnalyzerType) -> str:
    """S3 prefix / log tag for a block, keyed by its analyzer type."""
    return f"analyzer/{analyzer_type.value}"


class _PrefixAdapter(logging.LoggerAdapter):
    def process(self, msg: str, kwargs: dict) -> tuple[str, dict]:
        return f"[{self.extra['prefix']}] {msg}", kwargs


def block_logger(key_prefix: str) -> logging.LoggerAdapter:
    """A logger whose every record (including exceptions) is tagged with the
    block's key_prefix, so all of one block's output is greppable by type."""
    return _PrefixAdapter(logging.getLogger("oddish.analyzer_block"), {"prefix": key_prefix})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/analyzer_block.py backend/tests/test_analyzer_block.py
git commit -m "feat: AnalyzerBlock types + prefix-bound logger"
```

---

### Task 3: `AnalyzerLLMClient` protocol, Fake + API clients

**Files:**
- Create: `backend/api/services/analyzer_llm_client.py`
- Test: `backend/tests/test_analyzer_llm_client.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `class LLMClientType(str, enum.Enum)` with `SANDBOX = "Sandbox"`, `API = "Api"`.
  - `class AnalyzerLLMClient(Protocol)` with `def stream(self, prompt: str) -> AsyncIterator[str]` and `async def aclose(self) -> None`.
  - `class FakeAnalyzerLLMClient` — constructed with `chunks: list[str]` (or `exc: BaseException`); `stream` yields each chunk (or raises `exc`), `aclose` is a no-op.
  - `class ApiAnalyzerLLMClient` — `stream` uses `AsyncAnthropic().messages.stream(...)` yielding text deltas; `__init__(self, *, model: str = "claude-opus-4-8", max_tokens: int = 4096)`.

- [ ] **Step 1: Write the failing test**

Create `backend/tests/test_analyzer_llm_client.py`:

```python
import pytest

from api.services.analyzer_llm_client import (
    LLMClientType,
    FakeAnalyzerLLMClient,
    ApiAnalyzerLLMClient,
)


async def _collect(client, prompt):
    out = []
    async for chunk in client.stream(prompt):
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_fake_client_yields_chunks():
    client = FakeAnalyzerLLMClient(chunks=["a", "b", "c"])
    assert await _collect(client, "p") == ["a", "b", "c"]
    await client.aclose()


@pytest.mark.asyncio
async def test_fake_client_raises_when_configured():
    client = FakeAnalyzerLLMClient(exc=RuntimeError("boom"))
    with pytest.raises(RuntimeError, match="boom"):
        await _collect(client, "p")


@pytest.mark.asyncio
async def test_api_client_streams_text_deltas(monkeypatch):
    # Fake the AsyncAnthropic streaming context manager.
    class _AStream:
        def __init__(self, parts):
            self._parts = parts
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        @property
        def text_stream(self):
            async def gen():
                for p in self._parts:
                    yield p
            return gen()

    class _FakeMessages:
        def stream(self, **kwargs):
            assert kwargs["model"] == "claude-opus-4-8"
            assert kwargs["messages"][0]["content"] == "hi"
            return _AStream(["Hel", "lo"])

    class _FakeAnthropic:
        def __init__(self, *a, **k):
            self.messages = _FakeMessages()

    monkeypatch.setattr(
        "api.services.analyzer_llm_client.AsyncAnthropic", _FakeAnthropic
    )
    client = ApiAnalyzerLLMClient()
    assert await _collect(client, "hi") == ["Hel", "lo"]
    await client.aclose()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_llm_client.py -v`
Expected: FAIL (`ModuleNotFoundError: api.services.analyzer_llm_client`).

- [ ] **Step 3: Write the client module (Fake + API)**

Create `backend/api/services/analyzer_llm_client.py`:

```python
from __future__ import annotations

import enum
import logging
from typing import AsyncIterator, Protocol, runtime_checkable

from anthropic import AsyncAnthropic

log = logging.getLogger("oddish.analyzer_block.client")

_DEFAULT_MODEL = "claude-opus-4-8"


class LLMClientType(str, enum.Enum):
    SANDBOX = "Sandbox"
    API = "Api"


@runtime_checkable
class AnalyzerLLMClient(Protocol):
    def stream(self, prompt: str) -> AsyncIterator[str]: ...
    async def aclose(self) -> None: ...


class FakeAnalyzerLLMClient:
    """Test double: yields canned chunks, or raises a configured exception."""

    def __init__(
        self,
        *,
        chunks: list[str] | None = None,
        exc: BaseException | None = None,
    ) -> None:
        self._chunks = chunks or []
        self._exc = exc

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        for chunk in self._chunks:
            yield chunk
        if self._exc is not None:
            raise self._exc

    async def aclose(self) -> None:
        return None


class ApiAnalyzerLLMClient:
    """Direct Anthropic API backend: streams text deltas for a single prompt."""

    def __init__(self, *, model: str = _DEFAULT_MODEL, max_tokens: int = 4096) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._inner = AsyncAnthropic()

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        async with self._inner.messages.stream(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            async for text in stream.text_stream:
                yield text

    async def aclose(self) -> None:
        return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_analyzer_llm_client.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/analyzer_llm_client.py backend/tests/test_analyzer_llm_client.py
git commit -m "feat: AnalyzerLLMClient protocol with Fake + Api backends"
```

---

### Task 4: Sandbox client + async factory

**Files:**
- Modify: `backend/api/services/analyzer_llm_client.py`
- Test: `backend/tests/test_analyzer_llm_client.py`

**Interfaces:**
- Consumes: `LLMClientType`, `AnalyzerLLMClient` (Task 3); `Provisioner` (`backend/api/services/cc_chat/provisioner.py`), `RealDaytonaClient` (`backend/api/services/cc_chat/daytona_client.py`), `ClaudeCodeRuntime` (`backend/api/services/cc_chat/claude_code_runtime.py`), `settings` (`oddish.config`), `generate_id` (`oddish.db`).
- Produces:
  - `class SandboxAnalyzerLLMClient` — holds a provisioned `sandbox`, a `DaytonaClient`, a `ClaudeCodeRuntime`, and a `daytona_session_id`; `stream(prompt)` drives `runtime.stream_chat(...)` and yields one JSON string per stream-json dict; `aclose()` deletes the sandbox (best-effort).
  - `async def create_llm_client(llm_client_type: LLMClientType) -> AnalyzerLLMClient` — `API` → `ApiAnalyzerLLMClient()`; `SANDBOX` → provisions and returns a `SandboxAnalyzerLLMClient`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_analyzer_llm_client.py`:

```python
from api.services.analyzer_llm_client import (
    SandboxAnalyzerLLMClient,
    create_llm_client,
)


@pytest.mark.asyncio
async def test_create_llm_client_api_branch():
    client = await create_llm_client(LLMClientType.API)
    assert isinstance(client, ApiAnalyzerLLMClient)
    await client.aclose()


@pytest.mark.asyncio
async def test_sandbox_client_streams_json_lines_and_closes():
    sent = {}

    class _FakeSandbox:
        id = "sbx-1"

    class _FakeRuntime:
        async def stream_chat(self, client, sandbox, *, content, claude_session_id,
                              daytona_session_id="cc", system_prompt=None):
            sent["content"] = content
            for d in [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]:
                yield d

    class _FakeDaytona:
        def __init__(self):
            self.deleted = False
        async def delete_sandbox(self, sandbox):
            self.deleted = True

    daytona = _FakeDaytona()
    client = SandboxAnalyzerLLMClient(
        sandbox=_FakeSandbox(),
        daytona_client=daytona,
        runtime=_FakeRuntime(),
        daytona_session_id="analyzer",
    )
    out = []
    async for chunk in client.stream("my prompt"):
        out.append(chunk)
    assert sent["content"] == "my prompt"
    assert [__import__("json").loads(c) for c in out] == [
        {"type": "text", "text": "one"},
        {"type": "text", "text": "two"},
    ]
    await client.aclose()
    assert daytona.deleted is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_llm_client.py -k "sandbox or api_branch" -v`
Expected: FAIL (`ImportError` for `SandboxAnalyzerLLMClient` / `create_llm_client`).

- [ ] **Step 3: Add the sandbox client + factory**

Add these imports to the top of `backend/api/services/analyzer_llm_client.py`:

```python
import json
import os

from oddish.config import settings
from oddish.db import generate_id
from api.services.cc_chat.claude_code_runtime import ClaudeCodeRuntime
from api.services.cc_chat.daytona_client import CreatedSandbox, DaytonaClient, RealDaytonaClient
from api.services.cc_chat.provisioner import Provisioner, delete_sandbox_quietly
```

Append to the module:

```python
_DAYTONA_SESSION_ID = "analyzer"
_AUTO_STOP_MINUTES = 15
_AUTO_DELETE_MINUTES = 30


class SandboxAnalyzerLLMClient:
    """Daytona-sandbox backend: runs claude-code and yields one JSON string per
    stream-json event. Provisioning happens in ``create_llm_client`` (an async
    factory) -- constructors cannot be awaited."""

    def __init__(
        self,
        *,
        sandbox: CreatedSandbox,
        daytona_client: DaytonaClient,
        runtime: ClaudeCodeRuntime,
        daytona_session_id: str = _DAYTONA_SESSION_ID,
    ) -> None:
        self._sandbox = sandbox
        self._client = daytona_client
        self._runtime = runtime
        self._session_id = daytona_session_id

    async def stream(self, prompt: str) -> AsyncIterator[str]:
        async for event in self._runtime.stream_chat(
            self._client,
            self._sandbox,
            content=prompt,
            claude_session_id=None,
            daytona_session_id=self._session_id,
        ):
            yield json.dumps(event)

    async def aclose(self) -> None:
        await delete_sandbox_quietly(self._client, self._sandbox)


async def create_llm_client(llm_client_type: LLMClientType) -> AnalyzerLLMClient:
    if llm_client_type == LLMClientType.API:
        return ApiAnalyzerLLMClient()

    if llm_client_type == LLMClientType.SANDBOX:
        daytona_client = RealDaytonaClient(api_key=os.environ["DAYTONA_API_KEY"])
        sandbox = await Provisioner(client=daytona_client).create(
            env_vars={"ANTHROPIC_API_KEY": settings.anthropic_api_key or ""},
            auto_stop_minutes=_AUTO_STOP_MINUTES,
            auto_delete_minutes=_AUTO_DELETE_MINUTES,
            labels={"app": "analyzer", "session_id": generate_id()},
            daytona_session_id=_DAYTONA_SESSION_ID,
        )
        runtime = ClaudeCodeRuntime()
        await runtime.install(daytona_client, sandbox)
        return SandboxAnalyzerLLMClient(
            sandbox=sandbox, daytona_client=daytona_client, runtime=runtime
        )

    raise ValueError(f"unknown llm_client_type: {llm_client_type!r}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_analyzer_llm_client.py -v`
Expected: PASS (5 tests total).

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/analyzer_llm_client.py backend/tests/test_analyzer_llm_client.py
git commit -m "feat: SandboxAnalyzerLLMClient + create_llm_client factory"
```

---

### Task 5: `AnalyzerBlock` persistence — `save_to_s3` + `save_to_db`

**Files:**
- Modify: `backend/api/services/analyzer_block.py`
- Test: `backend/tests/test_analyzer_block.py`

**Interfaces:**
- Consumes: `AnalyzerType`, `AnalyzerInput`, `AnalyzerOutput`, `block_key_prefix`, `block_logger` (Task 2); `LLMClientType` (Task 3); `AnalyzerBlockModel`, `JobStatus`, `utcnow` (`oddish.db.models`), `get_session` (`oddish.db`), `get_storage_client` (`oddish.db.storage`).
- Produces:
  - `class AnalyzerBlock` with `__init__(self, *, analyzer_type, llm_client_type, input, prompt, analyzer_id=None, block_metadata=None, client=None)` that sets `self.id = generate_id()`, `self.key_prefix`, `self.log`, and lifecycle fields (`status=PENDING`, `output=None`, `error=None`, timestamps `None`).
  - `async def save_to_s3(self, raw: bytes) -> None` — uploads to `f"{self.key_prefix}/{self.id}"`, `content_type="application/x-ndjson"`; catches + logs its own exceptions (never raises).
  - `async def save_to_db(self) -> None` — inserts one `AnalyzerBlockModel` row; catches + logs its own exceptions (never raises).

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_analyzer_block.py`:

```python
import pytest

from api.services.analyzer_block import AnalyzerBlock
from api.services.analyzer_llm_client import LLMClientType
from oddish.db.models import JobStatus, utcnow


def _make_block(**over):
    kw = dict(
        analyzer_type=AnalyzerType.HEADROOM_ANALYSIS,
        llm_client_type=LLMClientType.API,
        input=AnalyzerInput(input={"x": 1}),
        prompt="do the thing",
    )
    kw.update(over)
    return AnalyzerBlock(**kw)


def test_block_init_sets_prefix_and_ids():
    b = _make_block()
    assert b.key_prefix == "analyzer/headroom_analysis"
    assert b.id and isinstance(b.id, str)
    assert b.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_save_to_s3_uses_prefix_key(monkeypatch):
    calls = {}

    class _FakeStorage:
        async def upload_bytes(self, data, s3_key, *, content_type=None):
            calls["data"] = data
            calls["key"] = s3_key
            calls["ct"] = content_type

    monkeypatch.setattr(
        "api.services.analyzer_block.get_storage_client", lambda: _FakeStorage()
    )
    b = _make_block()
    await b.save_to_s3(b"raw-bytes")
    assert calls["key"] == f"analyzer/headroom_analysis/{b.id}"
    assert calls["data"] == b"raw-bytes"
    assert calls["ct"] == "application/x-ndjson"


@pytest.mark.asyncio
async def test_save_to_s3_swallows_and_logs_errors(monkeypatch, caplog):
    class _BoomStorage:
        async def upload_bytes(self, *a, **k):
            raise RuntimeError("s3 down")

    monkeypatch.setattr(
        "api.services.analyzer_block.get_storage_client", lambda: _BoomStorage()
    )
    b = _make_block()
    await b.save_to_s3(b"x")  # must NOT raise
    assert "s3 down" in caplog.text or "save_to_s3" in caplog.text


@pytest.mark.asyncio
async def test_save_to_db_adds_row(monkeypatch):
    added = {}

    class _FakeSession:
        def add(self, obj):
            added["obj"] = obj
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(
        "api.services.analyzer_block.get_session", lambda: _FakeSession()
    )
    b = _make_block(block_metadata={"k": "v"})
    b.status = JobStatus.SUCCESS
    b.output = AnalyzerOutput(output="result-text")
    b.job_started_at = utcnow()
    b.job_ended_at = b.job_started_at
    b.job_duration_seconds = 0.0
    await b.save_to_db()
    row = added["obj"]
    assert row.id == b.id
    assert row.type == "headroom_analysis"
    assert row.llm_client_type == "Api"
    assert row.key_prefix == "analyzer/headroom_analysis"
    assert row.prompt == "do the thing"
    assert row.input == {"x": 1}
    assert row.output == "result-text"
    assert row.status == JobStatus.SUCCESS
    assert row.block_metadata == {"k": "v"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py -k "save or init" -v`
Expected: FAIL (`ImportError: cannot import name 'AnalyzerBlock'`).

- [ ] **Step 3: Add `AnalyzerBlock` init + persistence to `analyzer_block.py`**

Add these imports at the top of `backend/api/services/analyzer_block.py`:

```python
from oddish.db import generate_id, get_session
from oddish.db.models import AnalyzerBlockModel, JobStatus, utcnow
from oddish.db.storage import get_storage_client

from api.services.analyzer_llm_client import (
    AnalyzerLLMClient,
    LLMClientType,
    create_llm_client,
)
```

Append the class (execution/`run` comes in Task 6):

```python
class AnalyzerBlock:
    """One composable analyzer job. Runs a prompt through a swappable backend,
    streams the output, and persists to S3 + DB on every exit path."""

    def __init__(
        self,
        *,
        analyzer_type: AnalyzerType,
        llm_client_type: LLMClientType,
        input: AnalyzerInput,
        prompt: str,
        analyzer_id: str | None = None,
        block_metadata: dict | None = None,
        client: AnalyzerLLMClient | None = None,
    ) -> None:
        self.id = generate_id()
        self.analyzer_type = analyzer_type
        self.llm_client_type = llm_client_type
        self.input = input
        self.prompt = prompt
        self.analyzer_id = analyzer_id
        self.block_metadata = block_metadata
        self._client = client

        self.key_prefix = block_key_prefix(analyzer_type)
        self.log = block_logger(self.key_prefix)

        self.status: JobStatus = JobStatus.PENDING
        self.output: AnalyzerOutput | None = None
        self.error: str | None = None
        self.job_started_at = None
        self.job_ended_at = None
        self.job_duration_seconds: float | None = None

    @property
    def s3_key(self) -> str:
        return f"{self.key_prefix}/{self.id}"

    async def save_to_s3(self, raw: bytes) -> None:
        """Upload the raw streamed bytes. Never raises -- an S3 failure must not
        block the DB write in _persist."""
        try:
            await get_storage_client().upload_bytes(
                raw, self.s3_key, content_type="application/x-ndjson"
            )
            self.log.info("saved %dB to s3 key=%s", len(raw), self.s3_key)
        except Exception:
            self.log.exception("save_to_s3 failed for key=%s", self.s3_key)

    async def save_to_db(self) -> None:
        """Insert the block row. Never raises -- persistence is best-effort on
        the failure path, and the caller has already logged the primary error."""
        try:
            async with get_session() as session:
                session.add(
                    AnalyzerBlockModel(
                        id=self.id,
                        analyzer_id=self.analyzer_id,
                        type=self.analyzer_type.value,
                        key_prefix=self.key_prefix,
                        llm_client_type=self.llm_client_type.value,
                        prompt=self.prompt,
                        input=self.input.input,
                        output=self.output.output if self.output else None,
                        status=self.status,
                        error=self.error,
                        job_started_at=self.job_started_at,
                        job_ended_at=self.job_ended_at,
                        job_duration_seconds=self.job_duration_seconds,
                        block_metadata=self.block_metadata,
                    )
                )
            self.log.info("saved block row id=%s status=%s", self.id, self.status.value)
        except Exception:
            self.log.exception("save_to_db failed for id=%s", self.id)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py -k "save or init" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/api/services/analyzer_block.py backend/tests/test_analyzer_block.py
git commit -m "feat: AnalyzerBlock save_to_s3 + save_to_db (each failure-isolated)"
```

---

### Task 6: `run()` / `stream_output()` lifecycle with guaranteed save

**Files:**
- Modify: `backend/api/services/analyzer_block.py`
- Test: `backend/tests/test_analyzer_block.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5, plus `asyncio`.
- Produces:
  - `async def stream_output(self) -> AsyncIterator[str]` — async generator that lazily creates the client (or uses the injected one), yields each chunk to the caller, and accumulates chunks into `self._chunks`.
  - `async def run(self) -> AnalyzerOutput` — drives `stream_output()` to completion; sets `status`/timestamps/`duration`; on any `BaseException` (incl. `CancelledError`) records `FAILED` + `error` and re-raises; in a `finally`, runs `asyncio.shield(self._persist())` so the save happens on success, failure, and cancellation. `_persist` calls `save_to_s3(raw)` then `save_to_db()`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_analyzer_block.py`:

```python
import asyncio

from api.services.analyzer_llm_client import FakeAnalyzerLLMClient


def _patch_persistence(monkeypatch):
    """Capture save_to_s3 raw + save_to_db without touching S3/DB."""
    saved = {"s3": None, "db": 0}

    async def fake_s3(self, raw):
        saved["s3"] = raw

    async def fake_db(self):
        saved["db"] += 1
        saved["status"] = self.status
        saved["output"] = self.output
        saved["error"] = self.error
        saved["duration"] = self.job_duration_seconds

    monkeypatch.setattr(AnalyzerBlock, "save_to_s3", fake_s3)
    monkeypatch.setattr(AnalyzerBlock, "save_to_db", fake_db)
    return saved


@pytest.mark.asyncio
async def test_run_success_persists_output(monkeypatch):
    saved = _patch_persistence(monkeypatch)
    b = _make_block(client=FakeAnalyzerLLMClient(chunks=["foo", "bar"]))
    out = await b.run()
    assert out.output == "foobar"
    assert saved["s3"] == b"foobar"
    assert saved["db"] == 1
    assert saved["status"] == JobStatus.SUCCESS
    assert saved["error"] is None
    assert saved["duration"] is not None and saved["duration"] >= 0


@pytest.mark.asyncio
async def test_run_failure_persists_failed_and_reraises(monkeypatch):
    saved = _patch_persistence(monkeypatch)
    b = _make_block(client=FakeAnalyzerLLMClient(chunks=["partial"], exc=RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await b.run()
    assert saved["db"] == 1
    assert saved["status"] == JobStatus.FAILED
    assert "boom" in saved["error"]
    # Partial stream still reaches S3.
    assert saved["s3"] == b"partial"


@pytest.mark.asyncio
async def test_run_cancellation_still_persists(monkeypatch):
    saved = _patch_persistence(monkeypatch)

    class _HangingClient:
        async def stream(self, prompt):
            yield "first"
            await asyncio.sleep(3600)
        async def aclose(self):
            return None

    b = _make_block(client=_HangingClient())
    task = asyncio.create_task(b.run())
    await asyncio.sleep(0.05)  # let it yield "first", then hang
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert saved["db"] == 1
    assert saved["status"] == JobStatus.FAILED
    assert saved["s3"] == b"first"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py -k run -v`
Expected: FAIL (`AttributeError: 'AnalyzerBlock' object has no attribute 'run'`).

- [ ] **Step 3: Add `stream_output`, `run`, and `_persist`**

Add `import asyncio` to the top of `backend/api/services/analyzer_block.py`, then add these methods to `AnalyzerBlock`. Also add `self._chunks: list[str] = []` at the end of `__init__`.

```python
    async def stream_output(self):
        """Yield each output chunk to the caller and accumulate it. Lazily
        provisions the backend client (or uses the injected one)."""
        client = self._client or await create_llm_client(self.llm_client_type)
        try:
            async for chunk in client.stream(self.prompt):
                self._chunks.append(chunk)
                self.log.debug("chunk %d (len=%d)", len(self._chunks), len(chunk))
                yield chunk
        finally:
            # Only close a client we created; an injected one is the caller's.
            if self._client is None:
                await client.aclose()

    async def run(self) -> AnalyzerOutput:
        """Drive the stream to completion, persisting on every exit path."""
        self.job_started_at = utcnow()
        self.status = JobStatus.RUNNING
        self.log.info("block starting (llm_client_type=%s)", self.llm_client_type.value)
        try:
            async for _ in self.stream_output():
                pass
            self.output = AnalyzerOutput(output="".join(self._chunks))
            self.status = JobStatus.SUCCESS
            self.log.info("block succeeded (%d chunk(s))", len(self._chunks))
            return self.output
        except BaseException as exc:  # incl. asyncio.CancelledError
            self.status = JobStatus.FAILED
            self.error = repr(exc)
            self.log.exception("block failed")
            raise
        finally:
            self.job_ended_at = utcnow()
            if self.job_started_at is not None:
                self.job_duration_seconds = (
                    self.job_ended_at - self.job_started_at
                ).total_seconds()
            # Guarantee the save runs to completion even when run() is being
            # cancelled. A bare ``await asyncio.shield(...)`` is not enough: if
            # our await is itself cancelled, the shielded task keeps running but
            # we'd unwind before it finishes -- dropping the save. So hold the
            # task handle and, on cancellation, wait for it before re-raising.
            persist = asyncio.ensure_future(self._persist())
            try:
                await asyncio.shield(persist)
            except asyncio.CancelledError:
                await persist
                raise

    async def _persist(self) -> None:
        """S3 first, then DB. Each is failure-isolated inside its own method, so
        an S3 outage still lets the DB row land (and vice versa)."""
        raw = "".join(self._chunks).encode("utf-8")
        await self.save_to_s3(raw)
        await self.save_to_db()
```

Note on the accumulation: for `API` the chunks are text deltas (join → the full text); for `SANDBOX` each chunk is a JSON line, so the joined `output` is the concatenated JSON stream and S3 holds the same bytes. Since block I/O is typed `any`, downstream test scripts parse further as needed — this task does not parse stream-json.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py -k run -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Run the whole suite for the feature**

Run: `cd backend && uv run pytest tests/test_analyzer_block.py tests/test_analyzer_llm_client.py tests/test_analyzer_block_model.py -v`
Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/api/services/analyzer_block.py backend/tests/test_analyzer_block.py
git commit -m "feat: AnalyzerBlock.run/stream_output with shielded save on all exit paths"
```

---

## Notes for the executor

- `pytest.mark.asyncio` requires `pytest-asyncio` (already used across `backend/tests`). If the repo uses `anyio` mode instead, match the existing test files' async marker convention (check a sibling test in `backend/tests/`).
- The current `backend/api/services/analyzer_block.py` is an uncommitted scaffold with syntax errors (`def __init__(analyzer_type: AnalyzerType, input: )`); Task 2 replaces the whole file, so nothing needs salvaging.
- Import style: `backend/` code imports `api.services.*` and `oddish.*` (not `backend.api.*`). The existing scaffold's `from backend.api...` import (`daytona_client`) is the wrong prefix — Task 4 uses `from api.services.cc_chat...` to match the rest of the backend package.
- Do not wire the block into routers or the worker queue — it is driven from test scripts for now (per the spec's non-goals).
