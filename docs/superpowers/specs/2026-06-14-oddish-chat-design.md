# Oddish Chat — Query Experiments/Trials & Chat with Trial Logs

**Status:** Approved design (2026-06-14)
**Branch:** `chat/experiment-trial-chat`
**Inspiration:** atelier's chat (event-log message store, NDJSON streaming, scoped-vs-general priming) — *not* its Codex/Firecracker execution model.

## Problem

Two distinct conversational needs against oddish data:

1. **Global query chat** (tasks page): "find tasks that have xyz characteristics" — ask natural-language questions across all tasks/experiments/trials.
2. **Scoped log chat** (task/experiment detail page): chat with the trial logs associated with a task/experiment, defaulting to the latest version while able to reach past version runs.

## Decision: one engine, two primers (two entry points)

We build **one** chat engine with **two configurations** (a `scope`), not two independent chats and not a single chat that carries context between scopes. The two jobs need genuinely different tools — global is *structured search over Postgres*; scoped is *on-demand retrieval over large S3 log blobs* — so context-carryover plumbing is unnecessary for v1. The only things that differ per entry point are **which tools are offered** and the **system prompt**.

### Approaches considered

| Approach | Verdict |
|---|---|
| Copy atelier literally (Codex CLI in Firecracker sandbox, tools as injected CLIs) | Rejected — massive infra for "query Postgres + read S3 blobs"; oddish has no sandbox layer. |
| Text-to-SQL (model writes SQL) | Rejected — unsafe/brittle; bypasses the tested `oddish.core.*` access layer and org-scoping/auth. |
| **In-process Anthropic tool-use wrapping `oddish.core.*`** | **Chosen** — reuses the existing Anthropic client + model routing, inherits auth/org-scoping, deterministic tools, scales via on-demand retrieval. |

## Engine

An in-process **manual agentic loop** (Anthropic SDK, pinned `anthropic==0.76.0`):

1. Load the conversation's prior messages from the DB.
2. Build the **system prompt** from `scope` (general vs. primed with this task's id / `current_version` / compact trial summary).
3. Run the tool-use loop with the **async streaming** variant:
   - `async with client.messages.stream(model, max_tokens, system, tools, messages) as stream:` — forward text deltas to the client as they arrive.
   - `final = await stream.get_final_message()`.
   - While `final.stop_reason == "tool_use"`: execute each `tool_use` block via the dispatch table, append the assistant message (`final.content`) and a user message of `tool_result` blocks (`{"type":"tool_result","tool_use_id":..,"content":..,"is_error":?}`), and stream again.
   - Stop on `stop_reason == "end_turn"`.
4. Persist each completed turn to the DB and stream assistant text + tool-activity events to the browser.

**SDK constraints (pinned 0.76.0):** use raw-JSON tool schemas (`{"name","description","input_schema"}`) and the manual loop. Do **not** use `@beta_tool`/`client.beta.messages.tool_runner` or `messages.parse` — not assumed present in this pin. Reuse the client-selection + model-routing pattern from `oddish/src/oddish/core/digest.py` (`AsyncAnthropic` vs `AsyncAnthropicBedrock` via `config.py`), extended to support z.ai routing already in `config.py`.

**Model:** new `CHAT_MODEL` config in `oddish/src/oddish/config.py` (alongside `ANALYSIS_MODEL`). Default `claude-opus-4-8` for the direct API; ops sets the corresponding Bedrock inference-profile / z.ai id in deployment via the existing routing helpers. `max_tokens` ~16000. Adaptive thinking (`thinking={"type":"adaptive"}`) enabled only when the routed model is a Claude 4.6+ model; left off for z.ai/GLM routing to avoid 400s. Final decision deferred to implementation per deployed routing.

## Tools

Tool functions wrap existing `oddish.core.*` helpers and run under the **same `require_auth` + org filter** as the existing routers, so chat can never read another org's data. A failing tool returns a structured `tool_result` with `is_error: true` (not an HTTP 500) so the model can recover or report.

### Global scope (tasks page)
- `search_tasks(status?, agent?, model?, provider?, tags?, experiment?, reward_min?, reward_max?, date_range?, limit?)` → wraps `tasks/browse` core logic.
- `get_task_detail(task_id)` → versions + trial totals (`TaskDetailResponse`).
- `aggregate_trials(group_by, filters)` → trial-outcome stats (e.g. "tasks with verifier pass rate < 30%").
- `search_verdicts(query)` → over `task.verdict` / `trial.analysis` JSONB text.

### Task/Experiment scope (detail page)
- `list_trials(task_id, version?)` → defaults to `current_version`; pass a `version` to reach past runs. Filters `superseded_by_trial_id IS NULL` by default.
- `get_trial_logs(trial_id)` → `read_trial_logs_structured` (categorized agent/verifier). **Truncates** at a documented cap and tells the model it truncated.
- `get_trial_trajectory(trial_id)` → ATIF steps via `read_trial_trajectory` (gated by `has_trajectory`).
- `list_task_versions(task_id)` → so the model can answer "what past versions exist" and switch.

The task-scope system prompt is seeded with: task id, name, `current_version`, and a compact latest-version trial summary (`id / version / reward / status`) so the model answers immediately and only fetches full logs on demand. Experiment scope seeds the experiment id + its tasks; `list_trials`/`get_trial_logs` operate per task within it.

### Fast-follow (not in v1)
- `search_task_content(query)` → semantic/keyword search over task **bundle** text in S3. Needs a keyword-grep-on-demand vs. embeddings-index decision; specced separately so it does not gate v1.

## Data model (persist from day one)

Two new tables in the **backend (cloud) Alembic chain** (`backend/alembic/versions/`) + `backend/models.py`, since they reference `org_id`/`user` from the cloud layer. Follows atelier's append-and-replay pattern.

- **`chat_conversations`**: `id`, `org_id`, `user_id`, `scope` (`global|task|experiment`), `scope_ref_id` (nullable task/experiment id), `title`, `created_at`, `last_activity_at`.
- **`chat_messages`**: `id`, `conversation_id` (FK), `role` (`user|assistant|tool`), `content` (JSONB — text + tool_use/tool_result blocks, replayed verbatim), `created_at`. Index `(conversation_id, created_at asc)`.

A page reload replays the transcript from `chat_messages`.

## Components / files

**Backend** (fills the empty `backend/api/services/cc_chat/` + `backend/tests/cc_chat/` placeholders):
- `backend/api/services/cc_chat/engine.py` — the streaming tool-use loop + turn persistence.
- `backend/api/services/cc_chat/tools.py` — tool JSON schemas + dispatch → `oddish.core.*`.
- `backend/api/services/cc_chat/prompts.py` — global vs. scoped system prompts.
- `backend/api/routers/chat.py` — routes (below), registered in `backend/api/app.py`.
- `backend/models.py` + new `backend/alembic/versions/<rev>_add_chat_tables.py`.
- `oddish/src/oddish/config.py` — add `CHAT_MODEL`.

**API routes** (mirror existing auth/proxy conventions):
- `POST /chat/conversations` — create (body: `scope`, `scope_ref_id?`).
- `GET /chat/conversations` / `GET /chat/conversations/{id}` — list / fetch with messages.
- `POST /chat/conversations/{id}/messages/stream` — send a user message, stream the assistant turn as NDJSON.

**Frontend** (Next.js App Router, SWR, existing `/api/*` proxy pattern):
- `frontend/src/components/chat/chat-panel.tsx` — shared streaming chat UI; NDJSON reader modeled on atelier's `streamCodex` (`response.body.getReader()` + `TextDecoder`, split on `\n`).
- Entry points: a button/drawer on `frontend/src/app/(app)/tasks/page.tsx` (global), and on `tasks/[task_id]` + `experiments/[experiment]` (scoped, passing `scope_ref_id`).
- `frontend/src/app/api/chat/...` thin proxy routes → FastAPI backend (auth header injection via `@/lib/backend-config`).

## Streaming protocol

NDJSON lines (atelier shape): `{type:"started", conversation_id, turn_id}`, `{type:"text", delta}`, `{type:"tool", name, status}` (activity chips), `{type:"done"}`, `{type:"error", message}`. Heartbeat every ~10s to hold the connection during long tool loops. Each completed turn is persisted before `done` so a refresh replays from the DB.

## Error handling

- Tool failure → structured `tool_result` with `is_error: true`; model recovers or reports. Tool args validated before execution.
- Auth/org-scoping enforced inside every tool (same filter as existing routers).
- Unbounded logs → `get_trial_logs` truncates at a documented cap and signals truncation to the model.
- Stream errors emit `{type:"error"}`; partial turns are not persisted as if complete.

## Testing

Unit tests in `backend/tests/cc_chat/`:
- Tool dispatch (mock `oddish.core.*`): arg validation, org-scoping, error→`is_error` mapping, log truncation.
- Prompt builders: global vs. scoped seeding (task id / `current_version` / trial summary).
- Message replay/persistence: turn round-trips through `chat_messages` and rebuilds the transcript.
- Engine loop: mocked Anthropic stream driving one tool round-trip to `end_turn`.

(No e2e/browser tests — repo has no frontend test suite.)

## Phasing

- **v1:** everything above except `search_task_content`.
- **Fast-follow:** `search_task_content` (semantic task-content search) — separate spec; decide keyword-grep vs. embeddings.

## Open risks

- Exact `thinking`/model behavior depends on the deployed routing target (Claude direct vs. Bedrock vs. z.ai/GLM) — resolved at implementation against the configured `CHAT_MODEL`.
- Log truncation cap is a tunable; start conservative and adjust from real transcript sizes.
