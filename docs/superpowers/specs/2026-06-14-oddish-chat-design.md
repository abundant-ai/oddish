# Oddish Chat — Recovery Substrate + Global & Task Scopes

**Status:** Approved design (2026-06-14, rev 3)
**Branch:** `chat/experiment-trial-chat`
**Implementation repo:** `oddish` only (`backend/api/services/cc_chat/`). No separate service.

> **Revision history.**
> - **Rev 1 (wrong):** in-process Anthropic SDK tool-use loop inside oddish.
> - **Rev 2 (wrong home):** build on `agent-sandbox-service` (Claude-Code-in-Daytona) with oddish as a thin proxy client.
> - **Rev 3 (this):** Claude-Code-in-Daytona, but the **entire chat domain lives inside oddish's backend**. We are *un-doing* the earlier extraction (oddish commit `2ed6cda`, "replaced by agent-sandbox-service proxy"): the orchestrator comes home to `backend/api/services/cc_chat/`. The most mature implementation to port from is `agent-sandbox-service/src/agent_sandbox/services/chat/`.

## Engine (now in oddish)

Claude Code running in a per-session **Daytona** sandbox, orchestrated directly by oddish's backend:
- `claude` installed once at session start; `claude --print --output-format=stream-json --resume <claude_session_id>` per message; scoped `CLAUDE.md` dropped into the sandbox.
- **No new top-level deps:** `oddish/pyproject.toml` already pins `daytona>=0.165.0` and `anthropic==0.76.0`.
- Backend deployment target is **Modal** (ephemeral containers) — see Concurrency below; this is why the recovery substrate is load-bearing, not optional.

## What we're taking from atelier

**Not** its engine (Codex-in-Firecracker) — we have the Claude-Code-in-Daytona equivalent. We're taking atelier's **state management + chat recovery**: an append-only event log persisted *per event* to Postgres, durable in-flight turn tracking with a one-running-turn invariant, and replay-then-resume on reconnect. This is the backbone; the two chat scopes sit on top.

## Starting point to port

The chat domain already exists, most-evolved, in `agent-sandbox-service/src/agent_sandbox/services/chat/` (orchestrator start/send/close, `sessions.py`, `claude_md.py` scopes, idle reaper, restart sweep) + `routers/chat.py` + `ChatSession` model. Port these into oddish `backend/api/services/cc_chat/`, then add the recovery substrate + scopes below. The current `SessionTranscriptBuffer` there is **in-memory, flushed to object storage once at close, "lost on service crash by design"** — that is exactly what the substrate replaces.

## The gap (vs. atelier)

| Concern | atelier | ported chat domain as-is |
|---|---|---|
| Transcript durability | per-event append to Postgres `sandbox_events` | in-memory, flush-at-close only |
| In-flight turn tracking | `agent_turns` + one-running-turn unique index | none |
| Mid-turn refresh | replay log + tail live stream | nothing to replay from |
| Container/crash mid-session | transcript intact in DB | transcript lost |

The cold archive (S3 flush at close) stays. We add the **hot path** (durable per-event log) that makes recovery work — and on Modal's ephemeral containers, in-memory buffering can't be the source of truth at all.

---

## Backbone: recovery substrate (oddish backend)

All tables in oddish's **cloud Alembic chain** (`backend/alembic/versions/`), alongside the ported `chat_sessions`.

### 1. Durable append-only event log
**`chat_session_events`**: `id`, `session_id` (FK → `chat_sessions`), `seq` (monotonic per session), `event` (jsonb — one stream-json event verbatim), `created_at`. Unique `(session_id, seq)`; index `(session_id, seq asc)`.
- During the `messages` SSE stream, each event is **written here before being yielded** to the client. The transcript becomes an ordered replay of this log.
- The in-memory buffer may stay as a write-through cache but is no longer the source of truth.

### 2. Durable turn record
**`chat_turns`**: `id`, `session_id` (FK), `seq` (turn ordinal), `user_message`, `status` (`running|done|failed|canceled`), `started_at`, `ended_at`, `error`. **Partial unique index** `WHERE status='running'` on `session_id` → at most one running turn per session. Opened when a user message starts streaming, closed when the stream ends.
- Restart/eviction sweep marks any `running` turn `failed` (its sandbox stream is gone) but **keeps the event log**, so the transcript stays intact and the session can accept a new message.

### 3. Replay + resume on reconnect
**`GET /chat-sessions/{id}/events?since=<seq>`** → events with `seq > since` (full transcript when omitted). Client flow:
1. On load: `GET /chat-sessions/{id}` (metadata + is a turn `running`?) and `GET …/events` to rebuild the transcript.
2. If a turn is `running`, re-attach to the `messages` SSE stream and continue from the last `seq` seen.
3. `claude --resume <claude_session_id>` preserves *Claude's* context; the event log preserves *UI/transcript* continuity.

### Retention
After a successful close-time S3 flush, **prune the session's `chat_session_events` rows** (and closed `chat_turns`). The Postgres log holds only *active + recently-closed* sessions; S3 is the durable long-term archive. PG = ephemeral hot working set, S3 = cold storage — so the table never grows unbounded despite per-event writes.

---

## Scope 1: Global cross-task query chat (net new)

`scope_kind = "global"` (scope_id = org). Entry point: oddish **tasks page**. Purpose: "find tasks with xyz characteristics" across all tasks/experiments/trials.
- **Priming:** new `render_global_claude_md(org_id)`.
- **Querying:** Claude Code in the sandbox calls a **new oddish endpoint `POST /tasks/query`** via an injected in-sandbox CLI/tool (atelier's credentialed-CLI pattern), wrapping existing `oddish.core` browse/aggregate logic — structured filters (status, agent, model, reward range, tags, experiment, dates) + trial-outcome aggregations. Org-scoping enforced server-side via `require_auth`.
- Semantic search over task *bundle content* is a fast-follow (keyword-grep vs. embeddings — separate decision).

## Scope 2: Task-level trial-log chat, version-aware (extends ported task scope)

`scope_kind = "task"` (scope_id = task_id). Entry point: oddish **task detail page**. Purpose: chat with this task's trial logs.
- **Version defaulting:** primes with the task's **`current_version`** trials by default; `CLAUDE.md` + an injected tool let the user pull in **past version runs** on request (`list_task_versions`, `list_trials(task_id, version)`). This is the "default latest, see past versions" behavior.
- **Logs:** reuse the ported trial-file fetch path so Claude Code reads structured trial logs/trajectory from S3 in-sandbox.
- **Reconcile with ported `task-probes` scope:** decide during implementation whether `task` replaces or coexists with the existing probe-scoped chat. The existing `experiment` scope is retained.

---

## Entry points (oddish frontend → oddish backend)

- Shared `frontend/src/components/chat/chat-panel.tsx`: SSE reader + replay-on-mount via the events endpoint.
- Two mounts: tasks page (`scope=global`) and task detail page (`scope=task`, `scope_id=task_id`).
- `frontend/src/app/api/chat/...` proxy routes → **oddish's own backend** chat router (auth header injection via existing pattern). No external service.

## Files

**Backend** (`backend/`):
- `api/services/cc_chat/orchestrator.py`, `sessions.py`, `claude_md.py`, `claude_code_runtime.py`, `daytona_client.py` — ported from agent-sandbox-service, plus event-log/turn persistence.
- `api/services/cc_chat/events.py` — append/replay over `chat_session_events`.
- `api/routers/chat.py` — start / get / messages (SSE) / events replay / close, registered in `backend/api/app.py`.
- `backend/models.py` + `backend/alembic/versions/*` — `chat_sessions`, `chat_session_events`, `chat_turns`.
- `api/routers/tasks.py` — add `POST /tasks/query` (global scope tool).

**Frontend:** `components/chat/chat-panel.tsx`, two entry-point mounts, `app/api/chat/*` proxy routes.

## Streaming protocol
SSE stream-json events; each event **persisted to `chat_session_events` before being yielded**. Heartbeat to hold the connection. Replay via `GET …/events?since=<seq>`; resume by tailing from the last seen `seq`.

## Error handling
- Container crash / eviction mid-turn → sweep marks the `running` turn `failed`, event log preserved, session reusable.
- Sandbox/stream error → emit an error event (also persisted), close the turn `failed`.
- Global query tool failure → structured error back to Claude Code in-sandbox, not a 500.
- Org-scoping enforced in every oddish endpoint the sandbox calls.

## Testing
`backend/tests/cc_chat/`:
- Event log: per-event persistence ordering; replay `since=<seq>`; prune-after-flush.
- Turn record: one-running-turn invariant; sweep marks running→failed and preserves log.
- Reconnect: rebuild transcript from log + resume a live turn.
- Prompt builders: `render_global_claude_md`, task version-aware rendering.
- Global query tool dispatch (mock `oddish.core`): filters, aggregation, org-scoping, error mapping.
- `POST /tasks/query` route unit tests.

(Frontend has no test suite wired up.)

## Phasing
1. **Port + backbone** — bring `cc_chat` home from agent-sandbox-service; add `chat_session_events`, `chat_turns`, per-event persistence, replay endpoint, sweep, prune-after-flush. Hardens existing scopes immediately.
2. **Scope 2 (task, version-aware)** + task-detail entry point.
3. **Scope 1 (global)** — `render_global_claude_md`, `POST /tasks/query` + in-sandbox query tool, tasks-page entry point.
4. **Fast-follow:** semantic task-content search for global scope.

## Open risks / concurrency
- **Modal container lifecycle:** long-lived SSE streams + Daytona sandbox ownership on ephemeral Modal containers need care. The DB-backed substrate is what makes reconnect work across containers (any container replays from PG). But a *live* turn's SSE is served by one container holding the sandbox connection — if that container is evicted mid-turn, the turn is marked `failed` and the user re-sends (or we resume via `claude --resume` from the last completed turn). Whether to add atelier-style **sandbox ownership/lease + request routing** (so a reconnect re-attaches to a still-live turn on another container) is the main open question; defer until single-container behavior is proven.
- Per-event DB writes: small and prunable (see Retention); batch within a turn only if measured load warrants — log stays per-event-ordered either way.
- `task` vs. `task-probes` scope reconciliation (above).
