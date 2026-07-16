# Persist analyzer sandbox trajectories to S3

## Goal

Save the trajectory of every analyzer sandbox run to S3, so that when a finding
looks wrong we can see how the agent reached it — which trajectories it pulled,
whether it widened `--tail-bytes`, what it discarded — and so under-fetching can
be measured after the fact.

## Background

- The sandboxed analyzer runs one Daytona sandbox **per cohort** (`bad`/`good`),
  the two concurrently via `asyncio.gather`
  (`backend/worker/analyzer_sandbox.py:162`).
- Within a cohort, each turn runs in a **fresh claude process**
  (`claude_session_id=None`, `analyzer_cohort.py:142-154`): N map batches, then
  one reduce. Context never carries across turns — only disk does.
- Today the agent's transcript is **never persisted**. `stream_lines`
  (`analyzer_cohort.py:116`) is annotated *"Retained only to serve the
  parse-fallback; never persisted"*: it is held in memory so
  `parse_cohort_result` can scrape `MAP FINDING:` / `REDUCE RESULT:` if the
  files don't come back, and echoed to `logger.info`. Nothing else.
- Only `reduce.json` and `findings-*.jsonl` leave the sandbox before it is
  deleted in a `finally` (`analyzer_cohort.py:198-203`). The reasoning goes with
  the sandbox.
- This matters because `build_system_prompt` (`analyzer_prompt.py:49-78`) exists
  specifically to counter silent under-fetching, and its failure mode is
  invisible: *"the finding looks fine, it is just shallower than the evidence
  supports."* The one signal that would show whether that prompt works is
  currently discarded.
- `runtime.stream_chat` is typed `AsyncIterator[dict]`
  (`claude_code_runtime.py:209-218`) and yields raw Claude Code
  `--output-format=stream-json` events — already JSON-serializable.
- `render_event` (`stream_render.py:29`) is **lossy**: it truncates tool results
  to 600 chars and tool inputs to 200, and drops non-init system frames. The
  untruncated Bash command — which is what proves whether the agent passed
  `--tail-bytes` — survives only in the raw event.
- Prior art for an analyzer S3 write: `_trial_analyses_s3_key(analyzer_id)`
  returns `analyzers/{analyzer_id}/trial_analyses.json`
  (`workers/queue/analyzer_handler.py:65`), and `_maybe_save_trial_analyses`
  (:69-102) uploads best-effort — key derived from the id, never a stored
  pointer.
- `StorageClient.upload_bytes(data, s3_key, *, content_type)`
  (`oddish/src/oddish/db/storage.py:1210`) is the primitive; `get_storage_client()`
  (:1526) is the module-level seam every test monkeypatches. The bucket comes
  from `settings.s3_bucket` and is never passed per call.

## Decisions

- **Capture raw, not rendered.** One raw `evt` per JSONL line. Rendered text is
  lossy in exactly the dimension we need (tool inputs), and rendering can always
  be regenerated from raw — not the reverse.
- **One object per turn**, not per cohort or per run. Mirrors reality: one
  process, one context, one trajectory — the same reasoning that already gives
  each map batch its own `findings-NN.jsonl`. A failed turn is simply a missing
  object.
- **Always on.** No opt-in flag. A debug record you must opt into ahead of time
  is the one you won't have when a finding looks wrong. The events already exist
  in memory; the cost is S3 storage plus one PUT per turn.
- **Upload per turn, immediately after its stream closes.** A cohort that later
  times out or crashes still keeps every completed turn's record — and a timeout
  is exactly when the trajectory is most wanted. Also bounds memory: events
  flush per turn instead of accumulating across the cohort.
- **No DB column, no migration.** The key is fully derivable from
  `analyzer_id`, and `list_keys("analyzers/{id}/")` enumerates a run. Per the
  existing convention, only add a `*_s3_key` column when the key cannot be
  derived.
- **No read path.** Objects are read with the AWS CLI/console when debugging. An
  endpoint would pull in auth/org-scoping and public-view probe-visibility
  rules; not warranted yet.

## Design

### New module: `backend/api/services/cc_chat/analyzer_trajectory.py`

Kept out of `analyzer_cohort.py`, which is already dense and has no S3
dependency today.

Importing `oddish.db.storage` from `backend/` respects the package boundary:
AGENTS.md:188-194 constrains the direction only one way (`oddish` must not
import from `backend/`), and prescribes wrapping neutral `oddish` primitives
from `backend/` — which is what this does.

```python
def trajectory_key(analyzer_id: str, bucket: str, slug: str) -> str:
    return f"analyzers/{analyzer_id}/{bucket}/{slug}.jsonl"

async def persist_turn(analyzer_id, bucket, slug, events: list[dict]) -> None:
    """Best-effort: never fail the analyzer job."""
```

`persist_turn` serializes `"\n".join(json.dumps(e) for e in events)` and calls
`upload_bytes(..., content_type="application/x-ndjson")`. If `events` is empty
it logs a warning and skips the upload rather than writing an empty object.

### Key layout

```
analyzers/{analyzer_id}/{bucket}/map-01.jsonl
analyzers/{analyzer_id}/{bucket}/map-02.jsonl
analyzers/{analyzer_id}/{bucket}/reduce.jsonl
```

Sits alongside the existing `analyzers/{analyzer_id}/trial_analyses.json`. Turn
numbers are zero-padded for the reason already documented on `findings_path`
(`analyzer_prompt.py:43-46`): unpadded, `map-10` sorts before `map-2`.

### Wiring in `run_cohort`

`_turn` (`analyzer_cohort.py:142`) collects each raw `evt` into a list while it
renders and logs, exactly as now. When the `async for` completes it calls
`persist_turn`. `_turn` gains a `slug` parameter (`map-01`, `reduce`) distinct
from its existing display `label` (`map 1/3`). `run_cohort` already receives
`analyzer_id`, so no signature changes propagate upward.

### Error handling

Best-effort, matching `_maybe_save_trial_analyses`: catch `Exception` → log a
warning → continue. The analyzer job must not fail because an upload failed.

`except Exception`, deliberately **not** `BaseException`: this code runs inside
`asyncio.timeout(COHORT_TIMEOUT_SECONDS)`, and swallowing `CancelledError` would
break the cohort timeout. (`CancelledError` escaping `except Exception` is
already pinned by a test elsewhere —
`oddish/tests/workers/test_analyzer_handler.py:507`.)

**Known tradeoff:** completed turns are safe, but a turn cancelled mid-flight by
the cohort timeout loses its partial record, because the upload's `await` is
cancelled too. Preserving partials would need `asyncio.shield`; deferred as
unjustified complexity.

## Testing

House style: hand-rolled fakes + `monkeypatch` over the `get_storage_client`
seam. There is no `moto` in this repo. `backend/tests/cc_chat/test_analyzer_cohort.py`
already has `stream_chat` fakes (:43, :61, :238) to build on.

- Each turn uploads to its expected key: `map-01`, `map-02`, `reduce`.
- Every line round-trips `json.loads` back to the original event — proves the
  record is raw, not rendered.
- An upload that raises does not fail the cohort: findings still return.
- Zero-padding holds past 10 batches (`map-10` sorts after `map-02`).
- `bad` and `good` cohorts write to separate prefixes.
- A turn yielding no events skips the upload and warns.

## Out of scope

- Any read path: API endpoint, CLI command, or dashboard viewer.
- A DB pointer column or migration.
- Preserving the partial trajectory of a turn cancelled by the cohort timeout.
- Persisting trajectories for the non-sandbox (API) analyzer path.
- Retention/lifecycle policy for the new objects.
