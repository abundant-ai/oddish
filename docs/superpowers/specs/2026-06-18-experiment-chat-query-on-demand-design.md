# Experiment-scope chat → query-on-demand

**Date:** 2026-06-18
**Status:** Approved (design)
**Branch:** `fix/experiment-chat-no-task-cap`

## Problem

The experiment-level cc_chat mounts an experiment's **entire** artifact tree into the
sandbox via `collect_experiment_files` (`backend/api/services/cc_chat/experiment_files.py`).
That collector enforced a global 50 MB byte budget and `break`'d out of the trial loop the
moment the running total crossed the cap (~trial #51), so every later trial contributed
**zero files**. An experiment with 232 tasks surfaced only ~51 to the chat.

Removing the cap (the first cut on this branch) fixes the count but keeps the underlying
design flaw: provisioning cost scales with experiment size. A large experiment means a slow,
memory-heavy upload of artifacts the agent may never read.

## Goal

Stop mounting. Give the experiment chat the **`oddish-query` CLI** — the query-on-demand
mechanism the `global` scope already uses — so the agent fetches trial data on demand,
driven by the user's question. Provisioning cost becomes flat regardless of experiment size,
and there is no task-count ceiling.

This **supersedes** the byte-cap removal already made on this branch; the experiment mount
path is removed entirely.

## Non-goals

- **No MCP server.** The repo has no MCP plumbing wired into cc_chat sandboxes. The
  `oddish-query` CLI is the proven, lower-surface path and is what we extend. (A real MCP
  server remains a possible future direction, explicitly out of scope here.)
- **No persisted derived-metric columns** (e.g. `num_steps`). Per-trial step counts are
  computed on demand by the CLI. Cheap experiment-wide aggregates over step counts are
  deferred; see "Known limitation" below.
- No changes to `global`, `task`, or `task_probes` scope behavior.

## Background: how `global` scope already works

`global`-scope chat mounts no files. Instead `orchestrator._provision_sandbox`:

1. Mints a short-lived `READ`-scope internal API key (45-min TTL), stored on
   `ChatSession.query_api_key_id`, revoked in `close()`.
2. Injects `extra_env = {"ODDISH_API_KEY": <raw_key>, "ODDISH_API_BASE_URL": <public base>}`.
3. Uploads `oddish/src/oddish/cc_chat_query_cli.py` into the sandbox as `./oddish-query`
   (`_upload_query_cli`).
4. Renders a CLAUDE.md (`render_global_claude_md`) that instructs the agent to use
   `./oddish-query` via Bash, shallow-first, with truncation discipline.

All of (1)–(4) are currently gated on `scope_kind == "global"`. This design extends those
same gates to `"experiment"`.

## Architecture

### 1. New backend endpoint

`GET /experiments/{experiment_id}/trials` — `require_scope(READ)`, org-filtered.

Returns the experiment's non-superseded trials (same predicate
`collect_experiment_files` used: `TrialModel.experiment_id == experiment_id`,
`superseded_by_trial_id IS NULL`, org filter). Each row carries only **cheap, already-stored
columns** so listing and aggregates over them stay one call:

```
trial_id, task_name, status, reward, is_probe,
input_tokens, output_tokens, cost_usd, phase_timing,
has_trajectory, started_at, finished_at
```

Lives in `backend/api/routers/trials.py` (or `experiments.py` if one exists), reusing the
existing trial-response helpers. No new DB columns.

Per-trial reads already exist and are reused unchanged:
`GET /trials/{id}/result`, `/trajectory`, `/logs`, `/files`, `/files/{path}`.

### 2. CLI extensions — `oddish/src/oddish/cc_chat_query_cli.py`

Add subcommands (same projection + `MAX_BYTES` budgeting discipline as existing commands):

| Command | Endpoint | Notes |
|---|---|---|
| `experiments trials <exp_id>` | `GET /experiments/{id}/trials` | Projected rows; `_has_more`/`_truncated` markers as today |
| `trials result <id>` | `GET /trials/{id}/result` | Budgeted to `MAX_BYTES` |
| `trials trajectory <id>` | `GET /trials/{id}/trajectory` | Head/tail-truncated like `logs` |
| `trials trajectory <id> --summary` | `GET /trials/{id}/trajectory` | **Computes client-side** `{num_steps: len(steps), num_tool_calls, final_metrics}` — tiny exact output |
| `trials files <id> [--prefix P] [--recursive]` | `GET /trials/{id}/files` | S3 listing |
| `trials file <id> <path>` | `GET /trials/{id}/files/{path}` | Fetch one file, budgeted |

The existing `trials logs <id> [--trajectory]` command stays unchanged so `global`-scope's
CLAUDE.md contract is untouched. `--summary` is the path for exact step counts: the CLI
already downloads the full trajectory JSON, so it computes `len(steps)` before any truncation.

`--summary` derivation: parse the trajectory dict; `num_steps = len(steps)` where `steps` is
the top-level list; `num_tool_calls = sum(len(step.get("tool_calls") or []) ...)`;
pass through `final_metrics` if present. Tolerate missing/malformed fields (return 0 / null,
never raise).

### 3. Orchestrator wiring — `backend/api/services/cc_chat/orchestrator.py`

- Extend the `upload_query_cli` gate and the READ-key-minting / `extra_env` injection from
  `scope_kind == "global"` to `scope_kind in ("global", "experiment")`.
- `_resolve_scope_inputs` experiment branch: stop calling `collect_experiment_files`; return
  `files = []`. The agent lists trials via the CLI, so provisioning no longer needs a DB
  session, `blob_store`, or `trial_ids`. Drop the `if self._blob is None: raise` guard for
  experiment scope.
- `render_experiment_claude_md` is called with just `experiment_id` (no `trial_ids`).

### 4. CLAUDE.md — `backend/api/services/cc_chat/claude_md.py`

Rewrite `render_experiment_claude_md` to mirror `render_global_claude_md`:

- State the `experiment_id` and that the agent is scoped to this experiment (read-only).
- Describe the workflow: `./oddish-query experiments trials <exp_id>` to list, then drill into
  a specific trial's `result` / `trajectory` (`--summary` for step counts) / `logs` / `files`
  **as the question requires** — fetch only what's needed.
- Note `is_probe` appears in the listing so probe trials are distinguishable.
- Shallow-first discipline + truncation-marker handling, as `global` documents.

Signature drops `trial_ids`; template no longer enumerates trials (the agent queries them).

### 5. Cleanup

- Delete `backend/api/services/cc_chat/experiment_files.py` (`collect_experiment_files`) and
  `backend/tests/cc_chat/test_experiment_files.py` — both become dead.
- This removes the byte-cap-removal edits made earlier on this branch (now moot).

## Data flow (after)

```
User opens experiment chat
  └─ orchestrator: mint READ key → inject ODDISH_API_KEY/BASE_URL
                   → upload ./oddish-query → render query-style CLAUDE.md
                   → mount NO files
  └─ agent (in sandbox), per user question:
       ./oddish-query experiments trials <exp_id>        # the list
       ./oddish-query trials result <trial_id>           # "why did X fail?"
       ./oddish-query trials trajectory <id> --summary   # "how many steps?"
       ./oddish-query trials files <id> --recursive       # browse artifacts
       ./oddish-query trials file <id> <path>            # read one file
  └─ close(): revoke READ key
```

## Known limitation (accepted)

Experiment-wide aggregates over **derived** trajectory metrics (e.g. "average step count
across all 232 trials") require the agent to fetch each trial's trajectory and sum
`--summary` results — N calls, and large downloads for big experiments. This re-touches the
scaling concern, but only for the narrow class of derived-metric aggregates, and only when a
user explicitly asks for one. Aggregates over the **cheap** columns in the listing (reward,
tokens, cost, durations, status, `has_trajectory`) are one call. If derived-metric aggregates
become common, a follow-up can persist a `num_steps` column populated at trial finalization
(the extraction code already reads the trajectory there) — out of scope for this change.

## Testing

- **Backend** (`backend/tests/...`): `GET /experiments/{id}/trials` — org scoping (other
  org's trials excluded), superseded-trial filter, `is_probe` surfaced, expected fields
  present, empty/unknown experiment yields `[]`.
- **CLI** (mirror existing `cc_chat_query_cli` tests): `experiments trials` projection +
  budget markers; `trials result` budgeting; `trials trajectory --summary` computes
  `num_steps` from a fake trajectory and tolerates missing fields; `trials files` / `file`.
- **Orchestrator**: experiment scope uploads the CLI, mints **and revokes** the READ key,
  injects `ODDISH_API_KEY`/`ODDISH_API_BASE_URL`, mounts **zero** files, and renders the
  query-style CLAUDE.md (no `trial_ids`, no `blob_store` required).

## Files touched

| File | Change |
|---|---|
| `backend/api/routers/trials.py` (or `experiments.py`) | + `GET /experiments/{id}/trials` |
| `oddish/src/oddish/cc_chat_query_cli.py` | + `experiments trials`, `trials result/trajectory[--summary]/files/file` |
| `backend/api/services/cc_chat/orchestrator.py` | extend CLI-upload + key-mint gates to `experiment`; drop experiment mount |
| `backend/api/services/cc_chat/claude_md.py` | rewrite `render_experiment_claude_md` (query workflow; drop `trial_ids`) |
| `backend/api/services/cc_chat/experiment_files.py` | **delete** |
| `backend/tests/cc_chat/test_experiment_files.py` | **delete** |
| backend + CLI tests | new endpoint, CLI subcommands, orchestrator experiment-scope tests |
