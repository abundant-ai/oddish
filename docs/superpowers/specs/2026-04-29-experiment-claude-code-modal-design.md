# Experiment Claude Code Modal — Design

**Status:** Draft
**Date:** 2026-04-29
**Owner:** Kate Yeh

## Summary

Add a "Chat with logs" modal on the experiment page that spins up a real headless Claude Code session in a Daytona sandbox, with the experiment's full artifact tree (`jobs/<experiment_id>/...`) uploaded as the working directory. The user gets a chat UI; behind it, `claude --print --output-format=stream-json --resume <sid>` runs per turn inside the sandbox, with stdout streamed back to the modal as Server-Sent Events.

Sessions are ephemeral: closing the modal deletes the sandbox and conversation history goes with it. A curated `CLAUDE.md` at the sandbox root steers the agent toward `result.json` and Grep/Glob navigation rather than `cat`-ing every trial log.

## Goals

- Let any user with access to an experiment ask natural-language questions about its trials and have a real Claude Code agent answer by reading the artifact tree directly.
- Use the genuine `claude` CLI inside an isolated cloud sandbox — same binary, same toolset, same behavior as a local Claude Code session.
- Trivial local dev: with `DAYTONA_API_KEY` and `ANTHROPIC_API_KEY` set, the feature works against the `jobs/` fixtures already checked into the repo. No S3, no FUSE, no extra infra.
- Single uniform code path for dev and prod, switched only by which `ExperimentFileStore` implementation is wired in.

## Non-Goals

- Conversation persistence across modal-close or page-reload. Each open is a fresh chat. (Possible v2.)
- Multi-replica session routing. v1 assumes the backend runs single-replica or with sticky routing keyed on `session_id`. (Possible v2.)
- Bring-your-own Anthropic API key per user. v1 uses one platform-owned `ANTHROPIC_API_KEY` from backend env. (Possible v2.)
- Token-cost attribution / billing per chat session. (Possible v2.)
- Optimizations for very large experiments (hundreds of trials, multi-GB logs). v1 just uploads everything and is slow if needed; we'll measure and revisit.
- Concurrent in-flight messages on one session. The frontend disables input while a stream is active; the backend 409s if a second message lands.

## Architecture

```
┌────────────────────────┐                ┌────────────────────────┐
│ Frontend (Next.js)     │                │ Backend (FastAPI)      │
│                        │                │                        │
│ Experiment page        │                │ /api/experiments/{id}/ │
│ ┌──────────────────┐   │ POST start     │   cc-session           │
│ │ "Chat" button    │──────────────────▶ │   ┌────────────────┐  │
│ └──────────────────┘   │                │   │ Orchestrator   │  │
│                        │                │   │ - create sbx   │  │
│ ┌──────────────────┐   │ POST msg (SSE) │   │ - upload files │  │
│ │ ChatModal        │◀───────────────────│   │ - exec claude  │  │
│ │ - input box      │   │  stream-json   │   │ - stream out   │  │
│ │ - streaming msgs │   │                │   └────────┬───────┘  │
│ └──────────────────┘   │ DELETE close   │            │           │
│                        │                │   ┌────────┴───────┐  │
│                        │                │   │ FileStore      │  │
│                        │                │   │ Local | S3     │  │
│                        │                │   └────────┬───────┘  │
└────────────────────────┘                └────────────┼──────────┘
                                                       │
                                                       │ uploadFile / exec
                                                       ▼
                                          ┌──────────────────────────┐
                                          │ Daytona sandbox          │
                                          │  /workspace/             │
                                          │   ├── CLAUDE.md (gen'd)  │
                                          │   └── jobs/<exp>/...     │
                                          │  ~/.claude/projects/...  │
                                          │  $ claude --print ...    │
                                          └──────────────────────────┘
```

### Approach choice: sandbox-orchestrated, not backend-orchestrated

The agent loop runs inside the sandbox via the real `claude` CLI, not as a backend-hosted Claude Agent SDK loop. This was chosen for:

- **Behavior parity.** The agent uses the actual CC tool surface (Read, Glob, Grep, Bash, Edit) on real local files, not proxied tool calls back to the backend.
- **Less code to maintain.** No in-house agent loop or tool implementations.
- **Tool-call latency.** File reads stay local to the sandbox instead of round-tripping over Daytona's API per call.

Trade-off accepted: conversation state lives in the sandbox's `~/.claude/projects/...`, so it's tied to sandbox lifetime. Ephemeral persistence makes that a non-issue for v1.

### Approach choice: full upload up-front, no FUSE / S3-mount

`ExperimentFileStore.populate(sandbox, experiment_id)` walks the source (local `jobs/` or S3) and uploads every file via `sandbox.fs.upload_file()` before the chat opens. Considered and rejected:

- **FUSE-mount S3 inside the sandbox.** Faster and cheaper at scale, but introduces AWS creds plumbing into the sandbox and bifurcates dev vs. prod ("how does it work locally?").
- **Manifest + lazy fetch tool (custom MCP).** Saves the upload cost, but the agent loses native `ls`/`Glob`/`Grep` across the tree — it can only see what it explicitly fetches. Less Claude-Code-y feel.

Full upload is uniform across dev and prod, requires no creds in the sandbox, and is fast enough for typical experiment sizes. Large-experiment optimization is deferred.

## Components

### Frontend (`frontend/src/`)

#### `components/experiments/cc-chat-modal.tsx`

A shadcn `Dialog`-based chat modal. Internally:

- **State:** `phase: "creating" | "ready" | "thinking" | "idle" | "closed" | "error"`, `messages: ChatMessage[]`, `sessionId?: string`.
- **On mount:** `POST /api/experiments/{id}/cc-session` → store `session_id`, set phase to `ready`.
- **On send:** open an `EventSource` to `POST /api/experiments/{id}/cc-session/{sessionId}/messages` with body `{ content }`. Parse stream-json events, append to transcript, set phase to `thinking` then `idle` on `event: done`.
- **On unmount / route change / `beforeunload`:** call `DELETE /api/experiments/{id}/cc-session/{sessionId}` via `navigator.sendBeacon` (fire-and-forget, survives page-unload).
- **Rendering:** reuse existing markdown / tool-call renderers from the trajectory display in `experiments/[experiment]/experiment-client.tsx`.

#### Trigger

A new "Chat with logs" button in the experiment page header (`experiments/[experiment]/experiment-client.tsx`). Visible to any user who can already see the experiment.

### Backend (`backend/api/`)

#### `routers/cc_chat.py`

Three endpoints under `/api/experiments/{experiment}/cc-session`:

- `POST /` — create session. Returns `{ session_id }`.
- `POST /{session_id}/messages` — send a turn. Body `{ content: str }`. Response is `text/event-stream`; events are stream-json envelopes from `claude` plus a final `event: done`.
- `DELETE /{session_id}` — tear down. Idempotent; 204 whether or not the session existed.

All three apply existing experiment-access auth before doing any sandbox work.

#### `services/cc_chat/orchestrator.py`

`CCChatOrchestrator` owns the session lifecycle.

```python
class CCChatOrchestrator:
    async def start(self, experiment_id: str, user: User) -> str:
        # 1. Create Daytona sandbox with autoStopInterval=30 and ANTHROPIC_API_KEY in envVars
        # 2. file_store.populate(sandbox, experiment_id)
        # 3. Upload generated CLAUDE.md to /workspace/CLAUDE.md
        # 4. sandbox.process.create_session("cc")
        # 5. sessions[session_id] = SessionState(...)
        # On any failure after step 1: daytona.delete(sandbox), then raise.
        ...

    async def send(self, session_id: str, content: str) -> AsyncIterator[bytes]:
        # 1. state = sessions[session_id]; raise SessionNotFound if missing
        # 2. resume = ["--resume", state.claude_session_id] if state.claude_session_id else []
        # 3. cmd = ["claude", "--print", "--output-format=stream-json",
        #          *resume, "--", content]
        # 4. cmd_id = sandbox.process.execute_session_command("cc", cmd, run_async=True)
        # 5. async for chunk in get_session_command_logs_async(...):
        #      for line in chunk.splitlines():
        #          event = json.loads(line)
        #          if event.get("type") == "system" and event.get("subtype") == "init":
        #              state.claude_session_id = event["session_id"]
        #          yield sse_format(line)
        # 6. yield sse_done()
        ...

    async def close(self, session_id: str) -> None:
        state = self.sessions.pop(session_id, None)
        if state:
            await self.daytona.delete(state.sandbox)
```

#### `services/cc_chat/file_store.py`

```python
class ExperimentFileStore(Protocol):
    async def populate(self, sandbox: Sandbox, experiment_id: str) -> None: ...
```

Two implementations:

- `LocalFileStore` (dev): walks `<repo_root>/jobs/<experiment_id>/`, uploads each file with the same relative path under `/workspace/jobs/<experiment_id>/`.
- `S3FileStore` (prod): lists the existing S3 prefix used by `oddish.queue` / `oddish.db.storage` for the experiment, downloads each object, uploads to the sandbox at the same relative path.

Wired by `backend/api/app.py` based on environment (`ODDISH_FILE_STORE=local` vs `s3`).

#### `services/cc_chat/claude_md.py`

`render_claude_md(experiment) -> str` produces the curated CLAUDE.md template. Template includes:

- Directory layout description (`jobs/<exp>/<trial>/{agent,verifier,result.json,trial.log,config.json}`).
- "Start with `result.json` and `config.json`; only read full trial logs when drilling in."
- "Use `Glob` and `Grep` to find things; do not `cat` every file."
- Brief dictionary of the artifact files (what `ctrf.json` is, what `trajectory.json` is, etc.).
- The experiment's own metadata at the top (id, dataset, agent(s), model(s), status, trial counts).

#### `services/cc_chat/sessions.py`

In-memory session registry:

```python
@dataclass
class SessionState:
    session_id: str
    sandbox: Sandbox
    daytona_session_id: str             # always "cc"
    claude_session_id: str | None       # None until first turn captures it
    experiment_id: str
    user_id: str
    created_at: datetime
    last_activity: datetime

sessions: dict[str, SessionState] = {}
```

A background `asyncio` task in the orchestrator runs every 5 minutes and reaps any session whose `last_activity` is older than 30 minutes (defense in depth — primary cleanup is `DELETE`).

### Inside the sandbox

- **`/workspace/CLAUDE.md`** — generated, uploaded at session start.
- **`/workspace/jobs/<experiment_id>/...`** — full uploaded artifact tree, as it exists in the source.
- **Env:** `ANTHROPIC_API_KEY` is set at sandbox creation via `daytona.create({ envVars: ... })`.
- **`claude` CLI** — installed in the base image (or via a Daytona snapshot/image with `@anthropic-ai/claude-code` preinstalled). v1 implementation step picks the cheaper of: a custom snapshot, or `npm install -g @anthropic-ai/claude-code` on first session and rely on Daytona's snapshot mechanism for subsequent fast starts.

### Data model

No Postgres tables. All session state is in-memory in the backend process. **Implication:** if the backend ever runs multiple replicas, sessions need either sticky routing on `session_id` or a shared store. Documented limitation; v2 problem.

## Data flow

### Open chat

1. User clicks "Chat with logs" on the experiment page.
2. Frontend → `POST /api/experiments/{id}/cc-session`.
3. Backend:
   - Auth: verify user has access to experiment `{id}`.
   - `daytona.create({ envVars: { ANTHROPIC_API_KEY }, autoStopInterval: 30 })`.
   - `file_store.populate(sandbox, experiment_id)`.
   - Generate CLAUDE.md → `sandbox.fs.upload_file(...)` to `/workspace/CLAUDE.md`.
   - `sandbox.process.create_session("cc")`.
   - `sessions[session_id] = SessionState(...)`.
4. Backend → `200 { session_id }`.
5. Frontend renders chat as `ready`.

### Send a message

1. User types, hits send.
2. Frontend opens an SSE connection: `POST /api/experiments/{id}/cc-session/{sid}/messages`, body `{ content }`.
3. Backend:
   - Look up `state = sessions[sid]`; 404 if missing.
   - Build `claude --print --output-format=stream-json [--resume <claude_sid>] -- "<content>"`.
   - `execute_session_command("cc", cmd, run_async=True)` → `cmd_id`.
   - `async for chunk in get_session_command_logs_async("cc", cmd_id):` parse each line as JSON; if it's the `system/init` event, capture `session_id` into `state.claude_session_id`; forward the line as an SSE `event: message` payload.
4. When the command exits, send `event: done` and close the SSE.
5. Frontend appends events to the transcript as they arrive (rendering tool calls, text deltas, tool results).

### Close

1. Frontend → `DELETE /api/experiments/{id}/cc-session/{sid}` via `navigator.sendBeacon` (no body; fires reliably on page unload).
2. Backend: `sessions.pop(sid, None)`; if it existed, `await daytona.delete(state.sandbox)`. Sandbox + `~/.claude/projects/*` + uploaded files are gone.

### Idle sweep

- Background `asyncio` task in the backend, every 5 minutes:
  - For each `(sid, state)` in `sessions`: if `now - state.last_activity > 30min`, pop and `daytona.delete`.
- Daytona's own `autoStopInterval` is the second backstop in case the backend crashes before cleaning up.

## Error handling

| Failure | Where | Behavior |
|---|---|---|
| Daytona `create()` fails or times out | session start | 502 to frontend; modal shows "Couldn't start a sandbox — try again". No partial state inserted. |
| File upload fails mid-populate | session start | Abort, `daytona.delete(sandbox)`, 502. Don't ship a half-populated session. |
| User has no access to experiment | session start | 403, before touching Daytona. |
| `session_id` not found (e.g. backend restarted) | send / close | 404. Frontend treats as "session expired" and offers a reopen button. |
| `claude` CLI exits non-zero (rate limit, model error, OOM) | mid-turn | Exit + stderr arrive via the log stream. Forward stderr as an `event: error` SSE event with the message; transcript renders an inline error bubble. Session stays alive — user can retry. |
| Daytona session/exec call fails mid-stream | mid-turn | Send `event: error` with "sandbox lost connection", close SSE. Mark session as `broken` in memory; further messages return 410 Gone. Frontend prompts "start a new chat". |
| User closes tab without firing close beacon | client | Idle sweep + Daytona's `autoStopInterval` clean it up. Both backstops exist for this reason. |
| Two browser tabs, same experiment, both chats open | client | Each gets its own `session_id` + sandbox. No cross-talk. Cost: 2× sandboxes; acceptable. |
| Experiment has zero files (in-flight / empty) | populate | Skip upload, still write `CLAUDE.md` noting "no trial data yet". Don't fail. |
| Massive experiment (thousands of files, GBs) | populate | v1: just slow. Status pill stays on `creating`. We log upload duration so we can see when it's biting. |
| Agent goes off the rails / runs forever | mid-turn | `claude` CLI's own turn budget bounds this. No extra timeout in v1. |
| Concurrent message on the same session | send | Frontend disables input while a stream is active. If a message lands mid-stream, backend 409s. |
| `claude_session_id` capture fails (init event shape changes) | mid-turn (turn 2+) | Caught at deploy time by smoke test. Runtime fallback: pass full transcript via stdin (`--input-format=stream-json` accepts a message history). v2 hardening. |

## Testing

### Backend unit tests (`backend/tests/cc_chat/`)

- `test_local_file_store.py` — `LocalFileStore` walks `jobs/<exp>/` correctly: skips dotfiles, preserves relative paths, handles empty trees. Uses a tiny experiment fixture from the existing `jobs/` checkin.
- `test_claude_md.py` — `render_claude_md(experiment)` produces the expected output. Snapshot test.
- `test_orchestrator.py` — orchestrator state transitions with a mocked Daytona client (in-memory fake): start → send → close. Verifies `claude_session_id` is captured from a synthetic `system/init` event on the first turn and re-passed on subsequent turns.

### Backend integration test (one, marked slow)

- `test_orchestrator_e2e.py` — uses real `DAYTONA_API_KEY` + `ANTHROPIC_API_KEY` from env, creates a sandbox against a tiny fixture experiment, sends one message ("list the trials"), asserts the response references the trials. `pytest.mark.skipif` unless both env vars are present. Manually runnable; not in CI by default.

### Frontend (`frontend/src/components/experiments/__tests__/`)

- One Playwright/RTL test of `cc-chat-modal.tsx` against a mocked SSE stream: submits a message, sees streamed events render, sees the close call fire on unmount. Reuses oddish's existing Playwright setup.

### Smoke test (pre-deploy)

- `backend/scripts/smoke_cc_chat.py` runs the full e2e path and prints pass/fail. Guards against `claude` CLI changing the stream-json envelope (especially the `system/init` `session_id` field). Run before each deploy.

## Open follow-ups (out of scope for v1)

- Conversation persistence across modal-close (snapshot `~/.claude/projects/<id>.jsonl` to S3 or Postgres on each turn).
- Multi-replica session routing.
- BYO Anthropic API key per user, with cost attribution.
- Lazy file population for very large experiments (FUSE-mount S3, or a custom MCP fetch tool).
- Concurrent in-flight messages on a session.
- Backend-crash recovery for in-progress streams.
