# Handoff — extend live-tailing to other harnesses

Uncommitted working note (repo policy: ad-hoc docs stay uncommitted; do NOT commit
this file). For the engineer adding live-tail support beyond Claude Code on
`feat/live-streaming-mvp` (PR #612). Assumes the S1-S5 tailer is merged. All
`file:line` refs are against the branch at time of writing; re-confirm before editing.

Companion research (verdicts + primary sources) is in the session; the load-bearing
conclusions are inlined below.

## 0. TL;DR

The tailer is ~90% format-agnostic. Adding a harness = supply three things behind a
one-method adapter: (a) the in-sandbox log path, (b) a `feed_line` parser producing the
existing `{kind, payload}` events, (c) a per-turn usage fold with a model string for
pricing. Do the **adapter-registry refactor first** (the spec promised it,
`docs/superpowers/specs/harbor-live-streaming-mvp.md:166-172`, the code never built it),
then land harnesses cheapest-first: **Codex → mini-swe-agent → Gemini → (Cursor,
Terminus as transcript-only)**. Codex is nearly free because its parser already exists
in-repo (`oddish/src/oddish/workers/agents/codex_stdout_trajectory.py`), just invoked
post-hoc today.

## 1. The seam as it exists today

Generic, reuse untouched (`oddish/src/oddish/workers/harbor/live_tail.py`):
- Poll/transport: `_tick` (`live_tail.py:229`) execs `tail -c +offset | head -c N |
  base64` through Harbor's `environment.exec` (obtained from the AGENT_START hook,
  `trial_handler.py:1002-1009`). Byte offsets + `split_lines` carry buffer
  (`live_tail.py:26`, `:248`).
- Storage/idempotency: `_buffer_events`/`_flush_events` → `trial_events` with
  `ON CONFLICT DO NOTHING` (`live_tail.py:250-284`); 5000-event cap + visible marker.
- Cost checkpoint: monotone `max(prev,new)`, unpriceable tick reuses last, write gated
  `WHERE finished_at IS NULL` (`live_tail.py:284-322`).
- Pricing: `price_totals` → `estimate_cost_usd(model, ...)` (`live_tail.py:145`), model-keyed, generic.
- Lifecycle: registry, `replaced` fencing, `start`/`shutdown`, retry inheritance
  (`live_tail.py:326-406`).
- Endpoint + both clients (CLI `oddish logs`, dashboard Live tab): speak the
  `(attempt, seq)` cursor contract, agent-agnostic. **Zero changes needed downstream.**

Claude-Code-specific, exactly three spots:
1. `CLAUDE_LOG_PATH = "/logs/agent/claude-code.txt"` (`live_tail.py:18`). Exists only
   because Harbor's stock ClaudeCode agent tees `claude --output-format stream-json`
   there. `/logs/agent/` is Harbor's fixed `EnvironmentPaths.agent_dir`.
2. `ClaudeUsageFold.feed_line` (`live_tail.py:108-130`) + the render helpers
   `_render_assistant_blocks`/`_render_tool_results`/`_clipped_payload`
   (`live_tail.py:44-93`): parse Anthropic-shaped stream-json (`type`
   assistant/user/result; `message.id`; `message.usage` with
   `input_tokens`/`output_tokens`/`cache_read_input_tokens`/`cache_creation_input_tokens`),
   fold **last-usage-per-message.id** (cumulative within an id, summing events
   multiply-counts — spec §5).
3. Dispatch: `supports(agent)` is a literal `"claude-code" in agent.lower()`
   (`live_tail.py:328`), called from `start()` (`live_tail.py:340`), which is called
   from `trial_handler.py:1002` with `agent=live_tail_spawn[1]` (the trial's agent name
   flows in already — `trial_handler.py:936`).

The event contract every adapter must emit (what `feed_line` returns, what
`_buffer_events` stamps a `seq` onto and stores): a list of
`{"kind": <"message"|"tool_use"|"tool_result"|"summary">, "payload": {...}}`. Payloads
are clipped via `_clipped_payload(key, value, **extra)` (sets `truncated: True` past
`PAYLOAD_CLIP_CHARS=2048`). The four kinds and payload shapes the clients already render
(CLI `_render_event`, frontend `LiveEventRow`):
- `message` → `{text}`
- `tool_use` → `{name, input}` (input is a JSON-stringified clip)
- `tool_result` → `{content, is_error?}`
- `summary` → `{text}` (also the transcript-cap marker; always render)

`UsageTotals` (`live_tail.py:35`): `input_tokens`, `cache_tokens`, `cache_write_tokens`,
`output_tokens`, `model`. `totals()` folds `usage_by_id` into it; `price_totals` reads
`totals.model`.

## 2. Step 1 — the adapter registry (do this first, ~1 small PR)

Replace the three hardcoded spots with a table. Keep the fold **stateful/incremental** (a
new instance per tailer, accumulating across ticks) — do NOT adopt the spec's batch
`parse(lines)` shape; the tailer feeds lines one at a time and usage accumulates across
ticks.

Suggested shape (in `live_tail.py`, terse, comment-free per repo style):

```python
class Fold(Protocol):
    def feed_line(self, line: bytes) -> list[dict[str, Any]]: ...
    def totals(self) -> UsageTotals: ...

@dataclass(frozen=True)
class Adapter:
    matches: Callable[[str], bool]
    log_path: str
    make_fold: Callable[[str | None], Fold]

ADAPTERS = [
    Adapter(lambda a: "claude-code" in a, "/logs/agent/claude-code.txt",
            lambda m: ClaudeUsageFold(model=m)),
    # Adapter(lambda a: a == "codex", "/logs/agent/codex.txt",
    #         lambda m: CodexUsageFold(model=m)),  # step 2
]

def _adapter_for(agent: str | None) -> Adapter | None:
    a = (agent or "").strip().lower()
    return next((ad for ad in ADAPTERS if ad.matches(a)), None) if a else None

def supports(agent: str | None) -> bool:
    return _adapter_for(agent) is not None
```

`LiveTailer.__init__` takes `log_path` + `fold` from the adapter instead of hardcoding
`CLAUDE_LOG_PATH` / `ClaudeUsageFold`; `_tick`'s command interpolates `self.log_path`;
`start()` resolves `ad = _adapter_for(agent)` and passes `ad.log_path`,
`ad.make_fold(model)`. Same-attempt replacement inheritance (`live_tail.py:355-363`)
already copies `self.fold` wholesale, so it stays correct as long as the successor uses
the same adapter (it will — same agent). Tests: the existing `test_live_tail.py` fold
tests become the Claude adapter's tests; add a registry-dispatch test (`supports` /
`_adapter_for`).

Rename `ClaudeUsageFold` stays fine; the render helpers
(`_render_assistant_blocks` etc.) are Claude-format and should move into or beside the
Claude fold so the next fold doesn't accidentally reuse Anthropic block shapes.

## 3. Per-harness playbook

Confirm every in-sandbox path and field name by the test recipe (§4) before coding —
these CLIs move fast and the researched byte shapes were not run in-session.

### Codex — cheapest, do first (verdict: full, cumulative asterisk)

- Harbor already runs `codex exec --json ... | tee <agent_dir>/codex.txt` — the exact
  tee-a-JSONL pattern the tailer wants. Confirm the absolute path (likely
  `/logs/agent/codex.txt`).
- **Parser already exists**: `codex_stdout_trajectory.py:36`
  (`convert_codex_stdout_jsonl_to_trajectory`) reads `thread.started` / `item.completed`
  (`reasoning`, message, tool items) / `turn.completed`, and the usage fields
  (`input_tokens` / `output_tokens` / `cached_input_tokens` / `reasoning_output_tokens`
  / `total_tokens`, `codex_stdout_trajectory.py:215-240`). Lift that event-typing into a
  `CodexUsageFold.feed_line` emitting the four `{kind,payload}` kinds, and fold usage.
- **Usage is cumulative across the whole session** (openai#17539, won't-fix): each
  `turn.completed.usage` is a running total, not a per-turn delta. So the fold keeps the
  **last** `turn.completed.usage` (not a sum, not per-id). Right for a "cost so far"
  meter; do not try to reconstruct per-call deltas.
- Map Codex item types → kinds: assistant text → `message`; tool/command execution →
  `tool_use` (name = command/tool) and `tool_result` (its output); `reasoning` → either
  a `message` or drop for MVP; final agent message / `turn.completed` summary →
  `summary`.
- Watch: open Codex `--json` bugs that can break a tee-tailer depending on the pinned
  `@openai/codex` version — #19945 (crash when stdio detached from TTY, 0.124.0+),
  #15451 (malformed JSON with MCP tools), #10141 (dropped command_execution output).
  Pin and test against the version Harbor installs.

### mini-swe-agent — full, but different write mechanism

- Emits a structured trajectory with per-message `extra.response.usage` + `cost` at a
  fixed path (`minisweagent/agents/default.py` `save()`), plus a plain-text `.txt` tee
  (no JSON — ignore the .txt).
- **Blocker: `save()` rewrites the WHOLE file every step** (`write_text` in a `finally`),
  not append-only. So `tail -c +offset` does not apply. This adapter needs a different
  tick: `stat` the file for mtime/size, reread the whole JSON, diff the `messages` array
  against a stored count, emit only the new tail. Tolerate torn reads (non-atomic write)
  with a `JSONDecodeError` → skip-this-tick retry.
  - Cleanest integration: generalize `_tick` so an adapter can declare its read mode
    (`append` vs `whole-file`), or give the fold a `feed_whole(bytes) -> events` variant.
    A `whole-file` adapter also changes offset semantics (offset becomes "messages
    consumed", not bytes). Scope this deliberately; it is the one adapter that touches
    `_tick`, not just adds a fold.
- Normalize two usage shapes: chat-completions (`prompt_tokens`/`completion_tokens`) vs
  Responses API (`usage.input_tokens`/`output_tokens`). Harbor ships a `_message_usage()`
  helper doing exactly this — copy its logic.
- Cost caveat: mini-swe `cost` can be silently `0.0` for models unlisted in litellm's
  price table (Harbor sets `cost_tracking=ignore_errors`); prefer feeding token counts
  through `estimate_cost_usd` yourself rather than trusting the harness `cost`.

### Gemini CLI — full, medium confidence, two-file correlation

- `--output-format stream-json` gives a tee-able JSONL transcript (fold like Claude), but
  **per-turn usage lives only in the OpenTelemetry channel** (`gemini_cli.api_response`:
  `input_token_count`/`output_token_count`/`cached_content_token_count`), which is off by
  default (`target=local` + outfile) and lands in a **separate** NDJSON `telemetry.log`.
  The stream-json `result.stats` block is end-only aggregate — do NOT use it for live
  usage.
- So this adapter tails TWO files and correlates by `session_id`/`prompt_id`. Meaningful
  extra plumbing. If live cost isn't worth the correlation, ship it transcript-only
  (partial) first.
- Field *spelling* for stream-json came from a PR + third-party cheatsheet, not
  doc-quoted — verify by running the CLI. stream-json landed ~v0.11.0.

### Cursor CLI — transcript-only (verdict: partial)

- `agent -p ... --output-format stream-json` gives a clean tee-able JSONL transcript
  (`system`/`user`/`assistant`/`tool_call`/`result`) — fold to the four kinds, live view
  is straightforward.
- **No token/cost field in the current (Jul 2026) schema.** Checkpoint cost `null`;
  backfill via a post-hoc extractor or Cursor's out-of-band admin usage API (out of
  scope). This is the verdict most likely to flip to full — re-check the usage field
  before committing to transcript-only permanently.

### Terminus — transcript-only + a structural catch (verdict: partial)

- Structured per-turn logs are live-observable (new `agent-logs/episode-N/{debug,
  response}.json` per turn + continuously-appended `sessions/agent.cast` asciinema), so a
  directory-poll transcript view is feasible (a new tick mode: `ls` the episode dir, read
  new episodes).
- **Usage is a litellm `token_counter` estimate**, never written per-episode, surfaced
  once at the end in `results.json` — no live cost without oddish re-estimating tokens
  itself.
- **Structural blocker to verify first**: Terminus runs in the terminal-bench *harness*
  process, not inside the graded container, so `agent-logs/` may live on the harness FS,
  not a sandbox oddish can `environment.exec` into. Confirm exec-reachability before any
  code — if unreachable, verdict drops to none (post-hoc extractor only).

## 4. How to test any harness (do this before coding each adapter)

1. Run it once on a trivial Harbor task locally (Docker backend fastest):
   `oddish run <task> --agent X`. Add a `sleep` step or an AGENT_START-hook breakpoint so
   you can catch it mid-run.
2. Read `harbor/agents/installed/<x>.py`; grep the argv for `tee`/`>`/`2>&1`/writes into
   `/logs/agent/`. If output only materializes at exit → **none**, stop (best you can do
   is a post-hoc extractor like `codex_stdout_trajectory.py`).
3. Exec into the live sandbox mid-run:
   `await env.exec("ls -la /logs/agent/; tail -c 500 /logs/agent/<candidate>")`, or fastest,
   `docker run -it` the same image and `watch -n1 'tail -c 500 /logs/agent/<file>'`.
   **The load-bearing check: does the file grow monotonically during the run?** Rewritten
   whole-file (mini-swe) or only-at-exit (grok headless) changes the tick mode.
4. Shape: is each line self-delimiting JSON (`split(b"\n")` + `json.loads` works)?
   Pretty-printed multi-line JSON / ANSI text → framing is hard, likely "no" for MVP.
5. Usage: cumulative-per-id (Claude → dedup-by-id), single-cumulative-running-total
   (Codex → keep last), per-message (mini-swe → normalize + sum), or none. Confirm a
   `model` string is present for `estimate_cost_usd`.
6. Verdict → **full** (incremental + parseable + fold-able usage): add adapter with a
   fold. **partial** (incremental + parseable, no usage): adapter with cost checkpointed
   null, backfill post-hoc. **none** (exit-only): `supports` false, post-hoc extractor.

## 5. Do NOT

- **Do not adopt Daytona native log-streaming** (`get_session_command_logs_async` /
  WebSocket `follow=true`). It streams the stdout of a process *you launched via the
  Daytona SDK*, keyed by its command id; it cannot follow an arbitrary file, and oddish
  does not launch the agent (Harbor does), so oddish holds no command id. It is also
  Daytona-only, breaking the docker/modal backends. Both fallbacks (launch the agent
  yourself; get Harbor to expose a log callback) are the paths spec §12 already rejected.
  If per-tick exec latency ever proves costly (the open S1 exit criterion), the right fix
  is a Harbor change: an optional `environment.stream_logs()` implemented per-backend
  (Daytona SDK stream / `tail -f` elsewhere), keeping one-transport-three-backends. Not
  MVP.
- **Do not sum usage events** for cumulative-per-id formats (Claude, and Codex which is
  cumulative-per-session). Multiply-counts. Fold correctly per §1.
- **Do not churn committed formatting** in touched files; terse, comment-free new code.
- **Do not touch** the tailer invariants (monotone cost, `replaced` fencing, `shutdown`
  owns the registry pop, purge-at-terminal). See the session memory
  `live-streaming-mvp-pr-612` for the full deliberate-invariant list.

## 6. Suggested order & sizing

1. Adapter registry refactor (§2) — small, unblocks everything, no new harness.
2. Codex fold (§3) — small; parser exists; ships full live cost. Highest value/effort.
3. mini-swe-agent (§3) — medium; needs the whole-file tick mode in `_tick`.
4. Gemini (§3) — medium; ship transcript-only first, add telemetry correlation later.
5. Cursor, Terminus — transcript-only adapters; defer live cost.

Each of 2-5 is independently shippable behind the registry; a harness with no adapter
already degrades to today's stage-only view.

## 7. References

Local: `live_tail.py`, `codex_stdout_trajectory.py`, `grok_build_trajectory.py`,
`trial_handler.py:833-1009`, spec §5/§8/§12 + adapter promise `:166-172`.
External (per-harness, verify against current versions):
- mini-swe-agent: github.com/SWE-agent/mini-swe-agent `.../agents/default.py`,
  `.../models/litellm_model.py`; mini-swe-agent.com/latest/usage/{output_files,trajectories}
- Codex: developers.openai.com/codex/{noninteractive,cli/reference};
  github.com/openai/codex/issues/{17539,10141,15451,19945}
- Cursor: cursor.com/docs/cli/{reference/output-format,headless};
  forum.cursor.com/t/{146980,156872}
- Gemini: github.com/google-gemini/gemini-cli/blob/main/docs/cli/{headless.md,telemetry.md}; pull/10883
- Terminus: github.com/harbor-framework/terminal-bench/blob/main/terminal_bench/agents/{terminus_1.py,terminus_2/terminus_2.py}
