# Oddish Chat — Recovery Substrate + Global & Task Scopes

**Status:** Approved design (2026-06-14, rev 2)
**Branch (oddish):** `chat/experiment-trial-chat`
**Primary implementation repo:** `agent-sandbox-service` (chat domain). Oddish is the thin client (entry points + cutover).

> **Rev 2 supersedes rev 1.** Rev 1 proposed an in-process Anthropic SDK tool-use loop inside oddish. That was wrong: the chat engine already exists as **Claude Code running in Daytona sandboxes**, orchestrated by `agent-sandbox-service` (`services/agent_runtimes/claude_code.py` runs `claude --print --output-format=stream-json --resume <id>`). This rev builds on that.

## What we're taking from atelier

**Not** its engine (Codex-in-Firecracker). We already have the equivalent (Claude-Code-in-Daytona). We're taking atelier's **state management + chat recovery**: an append-only event log persisted *per chunk* to Postgres, durable in-flight turn tracking with a one-running-turn invariant, and replay-then-resume on reconnect. This is the backbone; the two chat scopes are built on top of it.

## Current state (agent-sandbox-service)

- `ChatSession` (Postgres, `models.py:92`): durable **metadata** only — `id, org_id, user_id, scope_kind, scope_id, sandbox_id, daytona_session_id, claude_session_id, status, error`. `scope_kind`/`scope_id` are already generic.
- Engine: long-lived Daytona sandbox per session; `claude` installed once; `claude --resume <claude_session_id>` per message; scoped `CLAUDE.md` (`render_experiment_claude_md`, `render_task_probes_claude_md`).
- Routes: `POST /chat-sessions`, `GET /chat-sessions/{id}`, `POST /chat-sessions/{id}/messages` (SSE), `DELETE /chat-sessions/{id}`, skills export.
- **Transcript: `SessionTranscriptBuffer` — in-memory dict, flushed to MinIO once at close, "Lost on service crash by design."** No event-log table, no per-turn record, no replay endpoint.

## The gap (vs. atelier)

| Concern | atelier | agent-sandbox-service today |
|---|---|---|
| Transcript durability | per-chunk append to Postgres `sandbox_events` | in-memory, flush-at-close only |
| In-flight turn tracking | `agent_turns` + one-running-turn unique index | none |
| Mid-turn refresh | replay log + tail live stream | nothing to replay from |
| Service crash mid-session | transcript intact in DB | transcript lost; session marked `broken` |

The cold archive (MinIO flush at close) stays. We add the **hot path** (durable per-event log) that makes recovery actually work.

---

## Backbone: recovery substrate

### 1. Durable append-only event log
New table **`chat_session_events`**: `id`, `session_id` (FK → `chat_sessions`), `seq` (monotonic per session), `event` (jsonb — one stream-json event verbatim), `created_at`. Unique `(session_id, seq)`; index `(session_id, seq asc)`.

- `SessionTranscriptBuffer.append(...)` is replaced/backed by a write to this table **as each event is emitted** during `send_message`'s stream, before it is yielded to the client. The transcript becomes an ordered replay of this log (atelier's model).
- The in-memory buffer may remain as a write-through cache, but it is no longer the source of truth.
- MinIO flush at close still happens (cold archive), now sourced from the log.

### 2. Durable turn record
New table **`chat_turns`**: `id`, `session_id` (FK), `seq` (turn ordinal), `user_message` (text), `status` (`running|done|failed|canceled`), `started_at`, `ended_at`, `error`. **Partial unique index** `WHERE status='running'` on `session_id` → at most one running turn per session (atelier's `agent_turns` concurrency guard). A turn row is opened when a user message starts streaming and closed (`done`/`failed`) when the stream ends.

On service restart, `restart_sweep` marks any `running` turn `failed` (its sandbox stream is gone) but **keeps the event log** — so the transcript is intact and the session can accept a new message.

### 3. Replay + resume on reconnect
New route **`GET /chat-sessions/{id}/events?since=<seq>`** → returns events with `seq > since` (full transcript when `since` omitted). Client flow:
1. On load, `GET /chat-sessions/{id}` (metadata + whether a turn is `running`) and `GET …/events` to rebuild the transcript.
2. If a turn is `running`, re-attach to the live `messages` SSE stream (or a dedicated tail) and continue from the last `seq` seen.
3. `claude --resume <claude_session_id>` already preserves *Claude's* context; the event log preserves *UI/transcript* continuity — the half currently lost.

---

## Scope 1: Global cross-task query chat (net new)

A `scope_kind = "global"` session (scope_id = org). Entry point: the oddish **tasks page**. Purpose: "find tasks with xyz characteristics" across all tasks/experiments/trials.

- **Priming:** new `render_global_claude_md(org_id)` describing how to query and the available query tool.
- **Querying:** Claude Code in the sandbox calls a **new oddish query endpoint** (e.g. `POST /tasks/query`) via an injected CLI/tool in the sandbox (atelier's "credentialed CLI in the sandbox" pattern), wrapping existing `oddish.core` browse/aggregate logic. Supports structured filters (status, agent, model, reward range, tags, experiment, dates) and trial-outcome aggregations. Auth/org-scoping enforced server-side in oddish.
- Semantic search over task *bundle content* is a fast-follow (separate decision: keyword-grep vs. embeddings).

## Scope 2: Task-level trial-log chat, version-aware (extends existing)

A `scope_kind = "task"` session (scope_id = task_id). Entry point: the oddish **task detail page**. Purpose: chat with the trial logs for this task.

- **Version defaulting:** primes with the task's **`current_version`** trials by default; the `CLAUDE.md` + an injected tool let the user pull in **past version runs** on request (`list_task_versions`, `list_trials(task_id, version)`). This is the new "default latest, see past versions" behavior on top of the existing task-probes scope.
- **Logs:** reuses the existing `OddishClient.list_experiment_trial_files` / trial-file fetch path so Claude Code reads structured trial logs/trajectory from S3 in-sandbox.
- The existing `experiment`-scoped chat remains; `task` scope is the version-aware addition the user asked for.

---

## Oddish cutover (entry points)

Oddish becomes a thin client of agent-sandbox-service's chat API (Plan 3 direction).
- **Frontend:** a shared `chat-panel.tsx` (SSE reader + replay-on-mount using the events endpoint). Mounted at two entry points — tasks page (`scope=global`) and task detail page (`scope=task`, `scope_id=task_id`).
- **Proxy routes:** `frontend/src/app/api/chat/...` → agent-sandbox-service, auth header injected (existing proxy pattern).
- **New oddish backend endpoint:** `POST /tasks/query` for the global scope's query tool (wraps `oddish.core` browse/aggregate; org-scoped via existing `require_auth`).

## Data model summary (new, in agent-sandbox-service Alembic)
- `chat_session_events` (durable transcript log)
- `chat_turns` (durable turn record, one-running-turn partial-unique index)
- `chat_sessions`: add `global` and `task` to the `scope_kind` set (generic column, no schema change beyond allowed values).

## Streaming protocol
SSE stream-json events (existing). Each event is **persisted to `chat_session_events` before being yielded**. Heartbeat to hold the connection. Replay via `GET …/events?since=<seq>`; resume by tailing from the last seen `seq`.

## Error handling
- Service crash mid-turn → `restart_sweep` marks the `running` turn `failed`, event log preserved, session reusable.
- Sandbox/stream error → emit an error event (also persisted), close the turn `failed`.
- Query tool failure (global scope) → structured error back to Claude Code in-sandbox, not a 500.
- Org-scoping enforced in every oddish endpoint the sandbox calls.

## Testing
agent-sandbox-service (`tests/services/chat/`, `tests/routers/test_chat.py`):
- Event log: per-event persistence ordering; replay `since=<seq>` correctness.
- Turn record: one-running-turn invariant; restart sweep marks running→failed and preserves log.
- Reconnect: rebuild transcript from log + resume a live turn.
- `render_global_claude_md` / `render task version-aware` prompt builders.
- Global query tool dispatch (mock oddish query endpoint): filters, aggregation, org-scoping, error mapping.

oddish: `POST /tasks/query` unit tests (filters/aggregation/org-scoping); proxy-route smoke. (No frontend test suite in oddish.)

## Phasing
1. **Backbone** — `chat_session_events`, `chat_turns`, per-event persistence, replay endpoint, restart sweep. (Hardens existing scopes immediately.)
2. **Scope 2 (task, version-aware)** + oddish task-detail entry point.
3. **Scope 1 (global)** — `render_global_claude_md`, oddish `POST /tasks/query` + sandbox query tool, tasks-page entry point.
4. **Fast-follow:** semantic task-content search for global scope.

## Open risks
- agent-sandbox-service replica count: if >1, durable turn ownership may need a lease (atelier's `compute_leases`) so reconnect routes to the worker holding the sandbox. Single-replica → not needed yet; flagged.
- Per-event DB writes add write load; batch within a turn if it shows up in practice (log is still per-event-ordered).
- `claude --resume` session continuity vs. our turn log must stay consistent if a turn fails mid-stream (resume from last completed turn).
