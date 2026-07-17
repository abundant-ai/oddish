# AnalyzerBlock — a composable, self-persisting analyzer primitive

**Date:** 2026-07-16
**Status:** Approved (design)

## Purpose

A standalone, chainable unit for running analyzer LLM jobs. Many `AnalyzerBlock`s
will be composed into arbitrary chains and exercised in test scripts to try out
different configurations. Each block:

- takes an `AnalyzerInput` (typed `any` for now) and a prompt,
- runs one LLM job through a **swappable backend** — `SANDBOX` (Daytona +
  `ClaudeCodeRuntime`) or `API` (direct `AsyncAnthropic`),
- streams the output,
- **guarantees a save to both S3 and DB on every exit path** — success,
  exception, or cancellation,
- logs everything through a logger bound to `key_prefix = f"analyzer/{type}"`,
  including all exception paths.

It is a **new standalone primitive**. It does NOT go through the existing
`run_analyzer_generation_job` / `analyzer_handler` pipeline. It reuses only
low-level building blocks: `get_storage_client`, `get_session`, `DaytonaClient` /
`RealDaytonaClient`, `Provisioner`, `ClaudeCodeRuntime`, and `AsyncAnthropic`.

## Non-goals

- Not replacing or wrapping the production analyzer pipeline.
- Not wiring blocks into the worker queue or routers (blocks are driven from
  test scripts for now).
- No chain-orchestration engine yet — blocks expose a clean run/stream API;
  composing them is the caller's job in test scripts.

## New table: `AnalyzerBlockModel` (`analyzer_blocks`)

Added to `oddish/src/oddish/db/models.py`, mirroring `AnalyzerModel`'s style:
inherits `TimestampedMixin` (`id`, `created_at`, `updated_at`, `deleted_at`) and
`Base`, with `id` defaulting to `generate_id`.

| column | type | notes |
|---|---|---|
| `id` | String(64) PK | from `TimestampedMixin`, `default=generate_id` |
| `analyzer_id` | String(64), indexed, nullable | ties a block to a chain/run |
| `type` | SQLEnum(`AnalyzerType`) | analyzer type |
| `key_prefix` | Text | `analyzer/{type}` |
| `llm_client_type` | SQLEnum(`LLMClientType`) | Sandbox vs Api |
| `prompt` | Text, nullable | prompt sent to the LLM |
| `input` | JSONB, nullable | `input` is `any` |
| `output` | JSONB, nullable | parsed/accumulated result |
| `status` | SQLEnum(`JobStatus`), not null, default `PENDING` | lifecycle / failure recording |
| `error` | Text, nullable | exception repr on failure |
| `job_started_at` | DateTime(tz=True), nullable | job start |
| `job_ended_at` | DateTime(tz=True), nullable | job end |
| `job_duration_seconds` | Float, nullable | derived from start/end |
| `block_metadata` | JSONB, nullable | **Python attribute `block_metadata`, mapped to DB column named `metadata`** — `metadata` is a reserved name on SQLAlchemy's declarative `Base`, so the attribute cannot literally be `metadata` |

`status` and `error` were added beyond the caller's listed columns because
"cover all failure paths" requires a place to record failure. Reuses the
existing `JobStatus` enum (`oddish/src/oddish/db/models.py:91`).

**Migration:** `oddish/alembic/versions/analyzers_007_add_analyzer_blocks.py`
(down-revision = the current head in that lineage, `analyzers_006_add_models_by_task`).
Creates the table + index on `analyzer_id`, and any new enum types needed
(`AnalyzerType`, `LLMClientType`) if they aren't already DB enums.

## S3 vs DB split

- **S3** (`get_storage_client().upload_bytes`): the raw streamed output bytes,
  at key `f"{key_prefix}/{id}"` (matching the `analyzer_parse.py` convention),
  `content_type="application/x-ndjson"` (or `application/json`). This is the
  bulky raw stream.
- **DB**: structured `input` / `output` / `prompt` / `block_metadata` plus
  lifecycle (`status` / `error` / timestamps / `job_duration_seconds`). `output`
  in DB is the parsed/accumulated result; S3 holds the full raw stream.

## Class structure

```
AnalyzerLLMClient (Protocol / ABC)   →  async stream(prompt) -> AsyncIterator[str | dict]
  ├─ SandboxAnalyzerLLMClient        →  Daytona + ClaudeCodeRuntime.stream_chat
  ├─ ApiAnalyzerLLMClient            →  AsyncAnthropic streaming
  └─ FakeAnalyzerLLMClient (tests)   →  yields canned chunks, no network
```

### `AnalyzerLLMClient`

- A `Protocol` (or ABC) with `async def stream(self, prompt: str) -> AsyncIterator[...]`.
- Provisioning cannot live in `async def __init__` (constructors aren't
  awaitable — the current sketch's `async def __init__` is a bug). Instead:
  a sync `__init__` plus an **async factory** `classmethod create(llm_client_type,
  ...) -> AnalyzerLLMClient` that provisions the Daytona sandbox for the
  `SANDBOX` case via `Provisioner(...).create(...)` and installs the runtime via
  `ClaudeCodeRuntime().install(...)`.
- `SandboxAnalyzerLLMClient` drives `ClaudeCodeRuntime.stream_chat(...)`, which
  yields parsed stream-json dicts.
- `ApiAnalyzerLLMClient` uses `AsyncAnthropic` streaming and yields text/event
  chunks.
- Provisioned resources (sandbox) get an `aclose()` for cleanup.

### `AnalyzerBlock`

```python
AnalyzerBlock(
    analyzer_type: AnalyzerType,
    llm_client_type: LLMClientType,
    input: AnalyzerInput,
    prompt: str,
    analyzer_id: str | None = None,
    block_metadata: dict | None = None,
    client: AnalyzerLLMClient | None = None,   # injectable for tests
)
```

On construction: computes `key_prefix = f"analyzer/{analyzer_type}"`, builds
`self.log` — a prefix-binding logger helper (`LoggerAdapter` or a small wrapper)
that prepends `key_prefix` to every message so all logs (including exception
paths) are keyed by type.

## Run lifecycle — guaranteed save

```python
async def run(self) -> AnalyzerOutput:
    self.job_started_at = utcnow()
    self.status = JobStatus.RUNNING
    self.log.info("starting block")
    chunks = []
    try:
        client = self.client or await AnalyzerLLMClient.create(self.llm_client_type, ...)
        async for chunk in client.stream(self.prompt):   # stream_output() under the hood
            chunks.append(chunk)
            self.log.debug("received chunk %d", len(chunks))
        self.output = self._accumulate(chunks)
        self.status = JobStatus.SUCCESS
        return self.output
    except BaseException as exc:            # includes asyncio.CancelledError
        self.status = JobStatus.FAILED
        self.error = repr(exc)
        self.log.exception("block failed")
        raise
    finally:
        self.job_ended_at = utcnow()
        self.job_duration_seconds = (self.job_ended_at - self.job_started_at).total_seconds()
        await asyncio.shield(self._persist(raw_chunks=chunks))
```

- `stream_output()` is the public streaming entry — an async generator that
  yields chunks to a caller **and** accumulates them; `run()` is the
  drive-to-completion entry that consumes it. (Blocks can be run either way.)
- `_persist` calls `save_to_s3` then `save_to_db`, **each wrapped in its own
  try/except** so an S3 failure still lets the DB write happen (and vice versa);
  every failure is logged with the prefix.
- `_persist` is wrapped in `asyncio.shield` in the `finally`, so a cancellation
  mid-run still persists the partial output and the `FAILED` status.
- `save_to_db` follows the canonical pattern: `async with get_session()`,
  `session.add(AnalyzerBlockModel(...))` (or re-fetch `with_for_update=True` for
  an update), commit-on-context-exit.
- `save_to_s3` uploads the joined raw chunk bytes to `f"{key_prefix}/{id}"`.

## Error-handling matrix (all covered)

| failure point | status | error col | S3 save | DB save | logged w/ prefix |
|---|---|---|---|---|---|
| client provisioning fails | FAILED | yes | attempted (empty ok) | yes | yes |
| stream raises mid-way | FAILED | yes | partial stream | yes | yes |
| task cancelled | FAILED | yes | partial (shielded) | yes (shielded) | yes |
| S3 upload fails | (unchanged) | — | logged failure | still runs | yes |
| DB write fails | (unchanged) | — | already done | logged failure | yes |
| success | SUCCESS | null | yes | yes | yes |

## Testing

- `AnalyzerLLMClient` is a Protocol so a `FakeAnalyzerLLMClient` yielding canned
  chunks can be injected via the `client=` constructor arg — chain tests need no
  Daytona/network.
- Tests assert: a block persists on success; persists (with `FAILED` + `error`)
  when the injected client raises; persists when cancelled; S3 failure doesn't
  block DB write; `key_prefix` / S3 key derive from `AnalyzerType`; logs carry
  the prefix; `job_duration_seconds` is populated.
- DB tests use a throwaway freshly-migrated Postgres (per the repo's backend
  test-DB gotcha), not the shared local DB.

## Files

- `oddish/src/oddish/db/models.py` — add `AnalyzerBlockModel`.
- `oddish/alembic/versions/analyzers_007_add_analyzer_blocks.py` — migration.
- `backend/api/services/analyzer_block.py` — flesh out `AnalyzerBlock`,
  `AnalyzerLLMClient` + `SandboxAnalyzerLLMClient` / `ApiAnalyzerLLMClient`,
  `AnalyzerInput` / `AnalyzerOutput`, enums.
- (optional) `backend/api/services/analyzer_llm_client.py` — split the client
  classes out if `analyzer_block.py` grows too large.
- tests under `backend/tests/` (or the repo's analyzer test location).
