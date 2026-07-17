# Design: Migrate the sandbox cohort analyzer onto the `AnalyzerBlock` primitive

**Date:** 2026-07-17
**Status:** Approved (design)
**Branch:** `kate/analyzer-cohort-use-block` (based on `main`)

## Context

The `AnalyzerBlock` primitive (already merged to `main` via #768) is a
composable, self-persisting analyzer unit: one prompt → swappable backend
(`ApiAnalyzerLLMClient` / `SandboxAnalyzerLLMClient`) → streamed output →
shielded persist to S3 + the `analyzer_blocks` table on every exit path. It
was built standalone ("many blocks chain arbitrarily in test scripts") and is
not yet wired into the production analyzer.

The production analyzer has **two** map/reduce implementations behind the
`eval_rows_fn` seam (`workers/queue/analyzer_handler.py:298`):

- **API path** (`default_eval_rows` → `run_analyzer_eval`, `evals/analyzer/core.py`):
  direct Anthropic SDK, non-streaming, fan-out one `messages.create` per trial
  under `Semaphore(16)` + one reduce call. Already single-prompt-per-call.
- **Sandbox path** (`sandbox_eval_rows` → `run_cohort`,
  `api/services/cc_chat/analyzer_cohort.py`): one Daytona sandbox per cohort
  (`bad`/`good`), reused across N MAP batches + one REDUCE turn, each a *fresh*
  claude-code process (`claude_session_id=None`). State handed off through the
  sandbox filesystem (`findings.jsonl`); the host downloads `reduce.json` +
  per-batch `findings-*.jsonl` at the end and parses them via
  `parse_cohort_result`. This is the path swapped in when
  `settings.analyzer_sandbox_enabled`.

## Goal / scope

Rewire the **sandbox cohort path** to execute its MAP and REDUCE turns as
`AnalyzerBlock`s, so that each turn self-persists an `analyzer_blocks` row
(per-turn observability + S3 archival) and reuses the primitive's
stream/shielded-persist machinery instead of the hand-rolled `_turn` loop.

**Out of scope:** the API path (already single-prompt; needs none of the new
capabilities), the batching strategy, the prompt builders, `parse_cohort_result`,
the `AnalyzerModel` schema and its `_store` rollup, and the `eval_rows_fn` seam
itself. `AnalyzerModel` remains the source of truth for the analyzer's sections
+ findings; blocks are the new execution substrate and additive observability,
not a replacement.

## Why the primitive needs three extensions

The sandbox path uses capabilities the primitive does not yet expose:

| Need in cohort today | Where | Missing in primitive |
|---|---|---|
| Pull results out of the sandbox (`reduce.json`, `findings-*.jsonl`) | `analyzer_cohort.py:182-193` via `client.download_file` | Protocol is stream-only; no download escape hatch |
| Per-turn system prompt (MAP gets `build_system_prompt`, REDUCE none) | `analyzer_cohort.py:169,177` | `SandboxAnalyzerLLMClient.stream` drops `system_prompt`, though `stream_chat` accepts it |
| Haiku, CLI upload, probe creds, `TRAJ_TAIL_BYTES` provisioning | `analyzer_cohort.py:120-140` | `create_llm_client` hardcodes Opus 4.8, one label, no CLI |

Note the model default in the primitive is **Opus 4.8**
(`analyzer_llm_client.py:_DEFAULT_MODEL`), not Sonnet. The cohort forces Haiku
via `ANTHROPIC_MODEL` env for cost; making model a param lets the sandbox
backend drop to Haiku.

## Design

### 1. Path-based I/O contract on the block dataclasses

The shared sandbox filesystem is the medium between blocks; paths are the
interface. A block consumes by path and advertises by path, so the orchestrator
can wire one block's output to the next without either knowing the other's
internals.

```python
@dataclass
class AnalyzerInput:
    input: Any
    files_to_download: list[str] | None = None   # pull OUT of the sandbox, post-stream, pre-aclose

@dataclass
class AnalyzerOutput:
    output: Any
    files_written: list[str] | None = None        # signpost: paths this block produced in the sandbox
```

- `files_to_download` — after the stream drains, the block downloads each path
  and sets `output.output` to a `{path: content}` map (also mirrored to S3 under
  the block key). **Sandbox-only**: on the API backend (no filesystem) this is a
  loud rejection.
- `files_written` — a **declared** signpost (the caller built the prompt telling
  the agent where to write, so the caller supplies the expected path; the block
  echoes it on success). A missing file already degrades gracefully — `_download`
  returns `b""` (`analyzer_cohort.py:88-93`). We choose *declared* over
  *glob-discovered* to keep the block a pure prompt→stream→collect unit.

### 2. Download timing and idempotency

Downloads run inside `run()`, **after** the stream completes and **before** any
`aclose` (which deletes the sandbox — `analyzer_llm_client.py:110`). They must
**not** run in `__init__`, because:

1. `__init__` is synchronous and `_download_file` is async — the same reason the
   sandbox client is already built by an async factory ("constructors cannot be
   awaited").
2. Produce-then-download files (e.g. the reduce block's own `reduce.json`) do not
   exist until after that block's stream.
3. The client may be lazily provisioned inside `stream_output`
   (`analyzer_block.py:129`).

Idempotency via a **per-instance** dict (never a class attribute — that would
bleed downloads across block instances):

```python
def __init__(self, ...):
    ...
    self._downloaded_files: dict[str, bytes] = {}

async def run(self):
    ...
    async for _ in self.stream_output():
        pass
    self.output = AnalyzerOutput(output="".join(self._chunks))
    for p in self.input.files_to_download or []:
        if p not in self._downloaded_files:                 # skip already-fetched → partial-retry safe
            self._downloaded_files[p] = await self._client._download_file(p)
    if self.input.files_to_download:
        self.output = AnalyzerOutput(
            output=dict(self._downloaded_files),
            files_written=self.output.files_written if self.output else None,
        )
    self.status = JobStatus.SUCCESS
    ...
    # finally: _persist (S3 + DB), then aclose only if self-owned client
```

The `p not in self._downloaded_files` guard is defensive against an orchestrator
retry/re-entry; under single-use `run()` it never triggers, but it is cheap and
makes partial failure re-fetch only the missing paths.

**Constraint: `files_to_download` requires an *injected* client.** A
self-provisioned block closes its client inside `stream_output`'s `finally`
(`analyzer_block.py:133-135`) — i.e. the sandbox is deleted before `run()` reaches
the download step. So the download reads `self._client`, which is only set on the
injected path. The cohort always injects one shared client, so this holds; a block
that both self-provisions *and* sets `files_to_download` is a misconfiguration we
reject (alongside the API-backend rejection).

### 3. `system_prompt` and `model` as optional block params

- Protocol widens to `stream(prompt, *, system_prompt=None)`. Sandbox forwards to
  `stream_chat(..., system_prompt=...)`; API forwards as `system=` to
  `messages.stream`.
- `create_llm_client(type, *, model=None)`: API → constructor arg; sandbox →
  `ANTHROPIC_MODEL` env var.
- `AnalyzerBlock` gains optional `system_prompt` and `model`. `model` is recorded
  in `block_metadata` for reproducibility (no schema change).

### 4. `SandboxAnalyzerLLMClient` becomes reusable + downloadable

- Add `async def _download_file(self, path) -> bytes` wrapping
  `daytona.download_file` (`daytona_client.py:267`).
- Parameterize provisioning for the cohort: Haiku via `ANTHROPIC_MODEL`,
  `oddish-query` CLI upload, `ODDISH_QUERY_*` creds, `TRAJ_TAIL_BYTES`,
  `mkdir OUT_DIR`.
- `aclose` still deletes the sandbox, but is now called **once by the
  orchestrator**, not per block. The block only closes a client it created
  itself (existing `self._client is None` seam, `analyzer_block.py:133-135`).

### 5. Rewired cohort runner (`run_cohort`)

```
provision one shared SandboxAnalyzerLLMClient (cohort-provisioned)
  → for each MAP batch i:  AnalyzerBlock(
        analyzer_type=TRAJECTORY_FAILURE_ANALYSIS, llm_client_type=SANDBOX,
        prompt=build_map_batch_prompt(...), system_prompt=build_system_prompt(...),
        model=HAIKU, input=AnalyzerInput(input=..., files_to_download=None),
        analyzer_id=<parent AnalyzerModel.id>, client=<shared>)
        → run()   # output.files_written = ["findings-{i}.jsonl"]
  → REDUCE:  AnalyzerBlock(
        prompt=build_reduce_only_prompt(...), system_prompt=None, model=HAIKU,
        input=AnalyzerInput(input=..., files_to_download=["reduce.json", *map files_written]),
        client=<shared>)
        → run()   # downloads reduce.json + findings-*.jsonl into output.output
  → orchestrator aclose()s the shared client once
  → parse_cohort_result(reduce bytes, findings bytes, stream fallback) → (findings, sections)   # UNCHANGED
```

Each block persists its own `analyzer_blocks` row keyed by
`analyzer_id = parent AnalyzerModel.id`, giving per-turn rows without touching
`AnalyzerModel`. The reduce block's `{path: content}` output feeds the existing
`parse_cohort_result` unchanged.

Sandbox lifecycle / teardown safety currently in `run_cohort` (the
`return_exceptions=True` gather, the `finally: delete_sandbox`,
`COHORT_TIMEOUT_SECONDS`) is preserved by the orchestrator; only the per-turn
`_turn` calls become block `run()`s.

## Error handling

- The primitive already fails a block (`status=FAILED`, `error=repr(exc)`) and
  shield-persists on any exit path including cancellation — reused as-is.
- A non-empty cohort producing no findings still raises before persisting blank
  sections (existing guard, `analyzer_sandbox.py:177-181`).
- Download failures: a missing file yields `b""` (graceful), consistent with
  today. An API backend receiving `files_to_download` raises immediately
  (misconfiguration).

## Testing

Extend `backend/tests/test_analyzer_block.py`:
- `files_to_download` → `_download_file` looped, `output.output` is the
  `{path: content}` map (fake client returns canned files).
- Idempotency: second `run()`/re-entry does not re-fetch already-downloaded paths.
- `system_prompt` and `model` passthrough to the (fake) client / `stream_chat`.
- API backend + `files_to_download` → loud rejection.

Cohort rewrite reuses `FakeDaytonaClient` for a full map+reduce block sequence
against one shared sandbox, asserting `parse_cohort_result` still produces the
same `(findings, sections)`.

## What does not change

Batching (`batches()`, `MAP_BATCH_SIZE=10`), all prompt builders,
`parse_cohort_result`, `AnalyzerModel` schema + `_store`, the `eval_rows_fn`
seam, and the API path.
