# Oddish Repository Guide

This file is the technical guide for the entire monorepo. End-user CLI docs live in `DOCS.md`.

The repo has three main packages:

- `oddish/` — the core Python CLI, FastAPI server, queueing layer, and worker runtime
- `backend/` — the hosted cloud layer built on top of `oddish`; adds multi-tenant auth, Modal deployment, and product-specific endpoints
- `frontend/` — the Next.js App Router dashboard and public pages

Python `3.13` is required for `oddish` and `backend`. Node.js `20+` and `pnpm` are required for `frontend`.

## Maintenance Notes

- Keep `DOCS.md` focused on end-user CLI workflows; keep `oddish/README.md` as a short package quick start.
- Put `oddish` implementation details, architecture notes, and local development guidance here.
- If you change the CLI surface in `oddish/src/oddish/cli/`, update `DOCS.md`,
  the command list in `oddish/README.md`, and the packaged agent skill under
  `oddish/src/oddish/assets/skills/oddish/` (served by `oddish skill`).
- If you change API contracts, queue behavior, or storage layout, update this file.
- If you change `backend/` auth, deployment, or worker orchestration, update this file.
- If you change `frontend/` routing, API proxy structure, or auth behavior, update this file.
- Preserve the package boundary: `oddish/` must remain self-hostable for the
  CLI and standalone server; hosted product concerns (auth, org membership,
  Modal app wiring, managed worker spawning, GitHub/webhook integrations, and
  cloud-only policy) belong in `backend/`.

## Repository Layout

```text
oddish/                         # Core Python package (CLI, server, workers, DB)
├── src/oddish/
│   ├── analyze/                # QA prompts and analysis helpers
│   ├── cli/                    # oddish run/upload/ls/status/cancel/pull/collect/...
│   ├── core/                   # shared endpoint/service logic (reused by backend/)
│   ├── server/                 # standalone FastAPI app (python -m oddish.server)
│   ├── db/                     # models, connection helpers, storage, soft delete
│   ├── dispatch/               # shared dispatch-cycle planning (Modal + self-host)
│   ├── integrations/           # GitHub and external integrations
│   ├── mcp/                    # doc-store MCP server (oddish-docstore-mcp)
│   ├── runtime/                # runtime result/log helpers
│   ├── worker/                 # local trial runner and probe staging helpers
│   ├── workers/                # worker_jobs runtime, handlers, cleanup
│   ├── config.py               # settings + model/queue-key canonicalization
│   ├── queue.py                # task/trial enqueue + worker_jobs enqueue helpers
│   ├── schemas.py
│   └── (shared modules: experiment.py, model_pricing.py, observability.py,
│        registry_auth.py, task_timeouts.py, timing.py, backfill_queue_keys.py)
├── alembic/                    # Core DB migrations
├── env.example
└── pyproject.toml

backend/                        # Hosted cloud layer (Modal deployment)
├── api/
│   ├── app.py                  # FastAPI app factory and lifespan wiring
│   ├── schemas.py              # Pydantic models for org/auth/share responses
│   ├── services/               # hosted service helpers (Slack unfurls, shared query helpers)
│   └── routers/                # tasks, trials, dashboard, documents, tags, skills,
│                               # admin, orgs, api_keys, imports, load, webhooks
├── auth/                       # header parsing (auth/__init__.py), API key + Clerk JWT
│                               # verification (auth/verification.py), provisioning, types
├── worker/                     # Modal dispatcher and single-job worker orchestration
├── deploy.py                   # Modal app entrypoint
├── modal_app.py                # Modal image, volumes, shared runtime, env knobs; default Harbor provider is Daytona
├── endpoints.py                # Modal ASGI app function with concurrency/volume wiring
├── serve.py                    # Railway/uvicorn entrypoint for non-Modal deployment
├── cloud_policy.py             # Hosted-only environment policy
├── carl.py / carl_agent.py     # Existing Slack app mention dispatch + read-only agent
├── models.py                   # Cloud auth models (orgs/users/api keys)
├── dashboard_cache.py          # cached dashboard aggregation (+ attribution/backfill)
├── idempotency_store.py        # DB-backed idempotency for task submission
├── alembic/                    # Cloud migrations (auth + cloud table extensions)
└── pyproject.toml

frontend/                       # Next.js App Router dashboard
├── src/
│   ├── app/
│   │   ├── page.tsx            # Public landing page / signed-in redirect
│   │   ├── (app)/              # Authenticated shell: dashboard, tasks, experiments,
│   │   │                       # qa, skills, documents, usage, settings, admin
│   │   ├── share/[token]/      # Public experiment page
│   │   ├── datasets/           # Public dataset pages
│   │   ├── api/                # Backend proxy route handlers
│   │   └── providers.tsx       # Shared SWR config
│   ├── components/             # Dashboard, detail panels, charts, nav, UI primitives
│   ├── lib/                    # API helpers, backend config, shared types, utilities
│   └── middleware.ts           # Clerk route protection
└── package.json
```

## System Architecture

```text
Browser / oddish CLI
        |
        v
Next.js route handlers (frontend/src/app/api/*)
        |
        v
FastAPI server — oddish standalone (python -m oddish.server)
           or backend cloud layer (Modal / Railway)
        |
        v
Postgres
  - worker_jobs       # unified queue (TRIAL / TASK_EXPAND / TAG_PROJECT / …)
  - trials / tasks    # domain state + live UI columns
  - trial_events      # short-lived live transcript pages for running trials
  - queue_slots       # per-queue-key concurrency leases
  - model_concurrency_overrides # admin-set limits over deploy configuration
        |
        v
Workers (auto-started by API, or standalone via python -m oddish.workers.queue.worker)
        |
        v
Harbor task execution → logs/results/artifacts (S3)
```

High-level flow:

1. Upload a task bundle directly to S3 via a presigned PUT URL.
2. Submit a sweep of agent/model trials for that task; each trial is
   enqueued as a `worker_jobs` row in the same transaction as its domain
   row. Set `max_trial_attempts` on a sweep submission or sweep config to
   override the total attempt budget for newly-created trials. Re-submitting
   the same task-version/experiment sweep reconciles to the requested count:
   live non-failed trials are retained, while failed slots get fresh trial
   rows and the old attempts point to those replacements through
   `superseded_by_trial_id`. This preserves retry history without leaving the
   failed attempts in normal UI/API trial sets.
   Hosted sweep identity is resolved once into `SweepAttribution` before the
   core call. New tasks and experiments receive their creator, API-key, owner,
   display-owner, and link provenance in their constructors; returning an
   existing experiment never claims or rewrites that provenance. Trial imports,
   collections, and combined experiments follow the same create-only owner rule.
3. Workers claim one `worker_jobs` row at a time, dispatch to the registered
   handler for its kind, write heartbeats, and exit.
   Harbor's `RetryConfig` owns exception include/exclude policy and backoff for
   both retry scopes. `Trial` first retries typed installer transport failures
   and transient provider `ApiError` failures in the active sandbox, up to
   `max_in_place_retries`; resumable agents continue their session, while other
   agents rerun the original instruction in the existing working tree. A final
   failure crosses the result boundary once through `ExceptionInfo`, including
   optional HTTP status, request ID, session ID, and retry-after metadata.
   Harbor's `TrialQueue` still owns whole-trial retries, and Oddish
   `worker_jobs` owns durable fresh-sandbox retries across worker processes.
4. Trajectory analysis is **task-scoped** and runs as a trial: when every
   agent trial of a task is terminal, one QA trial (`trials.kind = 'qa'`)
   is created on the same task. Its agent classifies
   every live trial, writes per-trial trajectory summaries, and synthesizes
   the task verdict into one artifact (`qa_result.json`); on settlement an
   importer writes `trials.analysis`, `trials.trajectory_summary`, and
   `tasks.verdict`. The verdict is only requested above an evidence bar
   (≥5 QA-eligible trials from ≥3 agents, `MIN_VERDICT_TRIALS` /
   `MIN_VERDICT_AGENTS` in `oddish.workers.analysis_trials`); below it the
   QA trial still classifies trials and the task completes without a
   verdict. A sweep of `T` tasks × `N` trials therefore creates `T`
   QA trials, not `T × (N + 1)`. The pre-trial audit is an `audit`-kind trial
   created once per task version at sweep time.
   `POST /qa-evals` is the lower-level historical prompt-replay primitive. It
   creates one output experiment and one `qa_eval` trial per exact source
   solver trial. Terminal failed sources remain replayable when no trajectory
   was recorded; the QA brief then uses the result, verifier, exception, and
   authoritative trial facts without inventing agent actions. Each new trial
   stores its source-trial id and prompt hash,
   reuses the normal QA brief and `qa_result.json`, and writes the candidate
   analysis only to the new trial. Hosted creation resolves the authenticated
   caller as the payer, admits the validated replay count once, and stamps that
   payer on every new `qa_eval` trial. When
   `ODDISH_QUOTA_COUNTS_ANALYSIS_AND_COMPUTE` is on,
   queued analysis trials reserve quota through the same inflight predicates as
   solver trials; automatic QA, audit, and summarize trials remain org-level
   spend with a null payer. Callers retain the returned trial ids and read
   results through the existing single-trial endpoint.
   Non-'agent' kinds are excluded from solver cost, leaderboard, facet, and
   public surfaces (see `oddish.filters.trial_predicates.EligibleTrialScope`);
   their separate cost and optional quota basis comes from `analysis_spend`.
5. While a trial runs, a worker-side tailer (`oddish.workers.harbor.live_tail`,
   on by default via `live_tail_enabled` / `live_tail_interval_sec`) polls the
   agent's log file inside the sandbox for supported agents (claude-code,
   codex, cursor-cli, grok-build, tbh, mini-swe-agent), folds token usage, checkpoints live
   tokens/cost onto the trial row (`UPDATE … WHERE finished_at IS NULL`, so
   inflight quota reservations only tighten), and appends transcript events to
   `trial_events` (PK `(trial_id, attempt, seq)`, capped at 5000 events).
   `GET /trials/{id}/live` serves them with an `(attempt, after_seq)` cursor to
   `oddish logs [--follow]` and the dashboard Live tab. Events are purged when
   the trial goes terminal (S3 stays the permanent record); a 24h TTL sweep in
   the cleanup pass reaps rows leaked by hard-killed workers. A RETRYING trial
   clears `finished_at` and keeps its cost monotonic so it still counts as
   inflight for quotas and `/live`. Claude assistant deltas and tool blocks carry
   a hashed `turn_id` in their event payload so clients can distinguish streamed
   suffixes from a new no-tool assistant turn without exposing provider message
   identifiers. Claude message payloads also carry a `block_index` and
   `text_mode` (`append` or `replace`) so clients can assemble corrected text
   snapshots without concatenating stale content.
   Each persisted cost checkpoint re-evaluates enforced quotas. Reaching a
   payer's rolling-24h cap cancels every quota-counted nonterminal trial billed
   to that payer; reaching the org's monthly cap cancels every quota-counted
   nonterminal trial in the org. Final result settlement performs the same
   check for agents without live usage. Cancellation retires queued, running,
   blocked, and retrying worker jobs in the database before terminating remote
   handles; a task is failed only when no other live trial remains. If quota
   cancellation interrupts a replacement QA pass, the last successful verdict
   is restored through `cancel_verdict`; a terminal QA failure instead clears
   that preserved payload through `fail_verdict`. All task verdict-column
   mutations go through `oddish.core.verdict_state`: a published payload may
   coexist with QUEUED/RUNNING while its replacement is active, but it must
   return to SUCCESS if that pass is abandoned. The
   `ck_tasks_published_verdict_status` database constraint rejects a published
   payload with a missing or FAILED status.
6. Trial completion persists queryable execution metrics on the trial row:
   input/cache/output tokens, total trajectory steps, native runtime cost when
   reported, phase timing, trajectory availability, arbitrary verifier
   `metrics.json`, and a compact `_verifier` summary when the verifier emits a
   Common Test Report Format `verifier/ctrf.json`. The full CTRF report stays in
   S3; only counts, the tool name, and the report's trial-relative artifact path
   are stored in `trials.result`. Use the CLI or dashboard to watch progress and
   pull logs/artifacts back locally.
   It also derives trajectory elapsed time and tool usage directly from ATIF
   steps into `trials.trajectory_duration_seconds`, `trials.total_tool_calls`,
   and `trials.tool_counts`. Task and experiment filters combine model and
   trajectory metric constraints against the same eligible trial. Their
   `any` mode requires one passing trial; `all` requires at least one eligible
   trial and rejects the row when any eligible trial fails the constraints.
   The canonical cross-surface contract is `oddish.filters.TrialMetricFilter`;
   CLI and API adapters must parse/serialize through it. SQL surfaces must use
   `oddish.filters.trial_predicates.build_trial_metric_predicate` with an
   injected `EligibleTrialScope` rather than reimplementing Any/All logic.

Agent capability analysis (the successful-vs-failing cohort comparison) has
been removed: its endpoints, cohort blocks, and UI pane are gone, and nothing
enqueues or handles `ANALYZER` jobs any more (the enum value survives only so
historical rows stay readable). The output schema (`AgentCapabilitiesOutput` and sub-models) is
preserved in `oddish.analyze.models`, and
`oddish/src/oddish/analyze/prompts/agent_capabilities.txt` is kept, so the
feature can return as a `'capabilities'` analysis trial.
Shared trial drawers paint terminal trials from the slim row already owned by
the task or experiment page while the authoritative `GET /trials/{id}` resource
loads. Trial controls prefetch that resource on pointer or keyboard intent, and
the drawer owns its SWR cache entry and passes the authoritative trial to the QA
card, so one trial must never produce parallel detail requests. Drawers open on
Summary and fetch trajectory and summary resources independently only after
explicit user or URL intent. Collapsed trajectory steps must not mount their
message,
reasoning, tool, or observation bodies; those potentially large bodies mount
only while the step is expanded. Trajectory summaries are written onto
`trials.trajectory_summary` by the task's QA trial import or by a
`summarize`-kind trial's import; the read path never generates. The public
summary route stays a plain column read. The authenticated routes split reads
from paid mutation: `GET /trials/{id}/trajectory/summary` returns the resource,
while
`POST /trials/{id}/trajectory/summary` (TASKS scope, member-created keys
refused, matching an analysis rerun) creates or adopts the current summarize
trial. Both authenticated methods return `{summary, refresh}` so the published
summary and its replacement lifecycle cannot hide each other. They answer 200
whenever `summary` is present, including while `refresh` is active or failed;
without a published summary, an active refresh answers 202 and a failed refresh
answers 409. `refresh` carries `status`, `job_id`, and either
`retry_after_ms` or failure `detail`; 404 means neither publication nor refresh
exists. The frontend's one summary hook owns POST, the cache transition, and
SWR polling.

A QA/audit/summarize trial's **own** summary is deterministic, never an LLM call:
settlement (`handle_analysis_trial_settled`) counts one from the run's tool
calls. `oddish.analyze.trajectory_tool_calls` owns the external ATIF tool-call
name and string-argument spellings used by activity, provenance, and delegation
scans. `oddish.analyze.analysis_activity` applies analysis-shaped labels such as
`fetching_trial_data` / `writing_result`, one component per contiguous
same-label run and stores it through the same enrichment as graded-trial
summaries. These payloads carry `generator: "analysis-activity"` and the
explicit `taxonomy_version = "analysis-activity:v1"`; any semantic change to
the ordered activity rules must increment that version. Summary prose names
only actions present in the trajectory, so a failed partial run does not claim
an unobserved oddish-query fetch or `/logs` artifact write. The same settlement
scans the QA trajectory's tool-call
arguments for each graded trial id and stamps the matching step ids onto the
graded trial's `analysis._graded_at_steps`, which the drawer's "graded by"
link uses as a `#step-` anchor into the QA run. Both writes are best-effort
telemetry: neither may block or fail the artifact import. The Activity card
degrades rather than hides when a trial has steps but no stored summary — a
single gray ungrouped band with real totals — once the summary fetch settles.

The `summarize` trial kind is the LLM path for one trial's summary. At worker
pickup, `materialize_summarize_brief` reads the target's trajectory,
instruction, and verifier output directly through `oddish.core.trial_io`, then
removes empty steps, images, oversized text, embedded subagent trajectories,
and as much of the middle as needed to keep the serialized trajectory below
400,000 characters. The worker stages that bounded prompt instead of giving
the sandbox an oddish-query CLI, Oddish API credential, or internet access.
Harbor loads `SingleLLMAgent` through `AgentConfig.import_path`; the agent uses
Harbor's `LiteLLM` once, writes `summary_result.json`
(`{target_trial_id, trajectory_summary}`), and emits an ATIF trajectory with
one LLM step plus one deterministic artifact-write step. Harbor's normal trial
result therefore remains the source for status, timing, tokens, cost, logs,
verification, retries, and S3 artifacts. The result is validated in-sandbox
and at import by the shared checker, and its importer overwrites only the
target's `trials.trajectory_summary` — no verdict, task, or analysis state.
Only `kind = 'agent'` trials with `has_trajectory` can be summarize targets;
QA and audit remain tool-using claude-code trials, while QA, audit, and
summarize runs keep their deterministic own summaries. The target's nullable
`trials.trajectory_summary_refresh_trial_id` is the durable identity of the
summarize trial responsible for its next published summary; `harbor_config`'s
`target_trial_id` remains an artifact-validation boundary, not job discovery.
Creation locks Task then target Trial, sets the pointer in the same transaction
as the summarize Trial and WorkerJob, and adopts a live or successfully settled
pointed trial. `reserve_next_trial_index` takes the Task lock itself, so two
different targets on one task cannot allocate the same `{task_id}-{N}` id.
Import locks the target and writes only when its pointer still equals the
summarize trial id; it writes `trajectory_summary` and clears the pointer in one
transaction, so a delayed older importer cannot replace a newer result. The
cleanup sweep selects at most 200 non-null pointers whose summarize trial is
SUCCESS and retries those imports after the cleanup transaction commits. A
worker that dies between trial settlement and import therefore leaves durable,
bounded recovery work instead of a permanently stale summary. In an S3-backed
run, a QA/audit/summarize trial cannot settle SUCCESS until its Harbor artifact
directory uploads successfully and the freshly uploaded attempt's root
`result.json` selects an existing child containing the required analysis result.
If the outcome reports a trajectory, that same selected child must also contain
`agent/trajectory.json`. An upload or layout-validation failure uses the trial's
normal retry budget. Storage list/download errors during import propagate so
cleanup retries them. A successfully settled summarize trial whose stored
artifact is absent or
violates the pinned contract becomes FAILED while the target pointer remains,
so GET reports 409 and the next POST replaces it instead of adopting a SUCCESS
trial that can never publish.

`trials.kind` is the canonical analysis-run discriminator. Worker preparation,
analysis overlays, artifact filenames, query access, upload prefixes, summarize
materialization, and Harbor's task-validation exception derive from that column.
`harbor_config.mode` is reserved for the existing operator-probe value `probe`;
analysis trials must not duplicate `qa`, `qa_eval`, `audit`, or `summarize` there.
QA-eval source identity lives only in
`harbor_config.analysis_payload.trial_ids`, which contains exactly one id for a
QA-eval run. The shared `oddish.core.analysis_payload` parser enforces that
cardinality for both bound-key authorization and artifact import; malformed
QA-eval payloads authorize no source trial and fail import with the same error.

Trajectory summaries use schema v6. Each taxonomy-valued `components` entry
contains its `step_ids`, summary, and deterministic `tool_count` and
`duration_ms` metadata. Step count is the length of `step_ids`; the other
analytics are computed from the immutable
trajectory after LLM parsing (not generated by the model). Component duration is
the sum of each included step's elapsed time since the preceding trajectory
step; the first step and steps without two usable timestamps contribute zero.
The frontend derives the same values for older summaries that lack the fields.
Every summary consumer and warmup path must compare the stored
`schema_version` with the packaged schema version; truthiness of
`trials.trajectory_summary` is not a freshness check.

QA analyzer prompts are **not** stored in the database. They ship as packaged
files under `oddish/src/oddish/analyze/`: `prompts/pre_trial_qa.txt` drives the
source audit, `classify_prompt.txt` drives the per-trial log classifier,
`verdict_prompt.txt` drives verdict synthesis, and
`prompts/trajectory_summary.txt` drives schema-v6 trajectory summaries; the
summary template must retain the `{{taxonomy}}` placeholder, rendered by the
QA-trial brief builder (`oddish.workers.analysis_trials`). Editing a prompt is
a code change that ships with a deploy.

Each automatic QA brief snapshots authoritative Trial facts (id, status,
reward, trajectory availability, and agent), current-version nop/oracle
baseline results, and the source-audit status/findings into its pinned analysis
payload. The QA agent fetches complete result, verifier, and trajectory
resources for each solver Trial; it writes judgments only. Import restores
`trial_name` and `reward` from the graded Trial row, and a source-audit
`must_fix` finding or failed deterministic baseline rejects an otherwise
accepted verdict.

### Worker job kinds

`WorkerJobKind` (in `oddish.db.models`):

- **Active**: `TRIAL` (Harbor trial execution — including `qa`, `qa_eval`,
  `audit`, and `summarize` kind trials), `TASK_EXPAND` (sweep expansion),
  `TAG_PROJECT` (tag recompute).
- **Legacy, enum-only**: `QA`, `VERDICT`, `ANALYSIS`, `QA_REVIEW`,
  `ANALYZER`, `ANALYZER_BLOCK`. QA/audit/analyzer work runs as trials now;
  no handler claims these kinds (workers claim only registered kinds), and
  `retirejobs01` cancelled any still-queued rows. The members stay so the
  native `worker_job_kind` Postgres type keeps the values historical rows
  reference. Nothing enqueues any of them anymore.

## Package Boundaries

`oddish` owns the execution core and shared queue/runtime primitives:

- core models and migrations, including `worker_jobs` and `queue_slots`
- unified claim/dispatch SQL, one `run_single_worker_job` runner, and a
  handler registry (`TrialJobHandler`, `TaskExpandJobHandler`,
  `TagProjectJobHandler`)
- analysis trials (`oddish.workers.analysis_trials`): brief builders,
  settlement importers, and the audit/QA pipeline edges. Workers execute no
  LLM calls of their own (the one exception is the probe transcript
  summarizer in `oddish/worker/probe_analysis.py`); every analysis agent
  runs as a trial on the analysis model's queue key
- the verdict state machine (`oddish.core.verdict_state`), the only writer
  for `tasks.verdict*` lifecycle columns, which preserves the last published
  result until a replacement QA pass succeeds or terminally fails
- shared queue-slot leasing, per-queue-key concurrency limits, and
  per-user fairness on `TRIAL` claims
- database-backed admin concurrency overrides; these take precedence over
  `ODDISH_MODEL_CONCURRENCY_OVERRIDES` and are read by both the dispatcher plan
  and each worker's slot acquisition. This is the supported way to change a
  per-model limit at runtime. The self-tuning advisory controller
  (`ODDISH_DYNAMIC_MODEL_CONCURRENCY` + `concurrency_controller.py`) is
  **deprecated** in favor of it: leave the flag OFF; enabling it logs a
  deprecation warning and the path may be removed
- stale-heartbeat reaping, RETRYING → QUEUED mirror-back, and pipeline
  stage reconciliation in one cleanup sweep
- soft-delete semantics on domain rows via the `deleted_at` column and
  a session-level filter (`oddish.db.soft_delete`)

The analyzer-block machinery (`oddish/src/oddish/blocks/`, the backend
`api/services/blocks/` tree, `summarize_trajectory`) is deleted; the
`analyzer_blocks` table is dropped, with each trial's newest stored summary
backfilled onto `trials.trajectory_summary` first. Trial-level trajectory
analysis and the task verdict are the QA trial's job (above); there is no
separate report machinery.

`oddish` must not import from `backend/`, `backend.auth`, `backend.models`,
`cloud_policy`, `idempotency_store`, Clerk, or Modal app/deployment modules.
Keep optional provider/runtime SDK imports lazy behind core abstractions so a
CLI/self-host install can run without hosted deployment dependencies. If shared
behavior is needed by both products, put the host-agnostic primitive under
`oddish/src/oddish/core`, `oddish/src/oddish/workers`, or another neutral
`oddish` module, then wrap it from `backend/`.

`backend` wraps `oddish` with the hosted-only layer: Clerk/API key auth,
org-scoped APIs, Modal worker spawning and runtime patching, cloud environment
policy, GitHub notification hooks, and public sharing / product endpoints.

`frontend` provides the user-facing layer: the authenticated dashboard,
Clerk-based auth and org management, and Next.js route handlers that proxy
requests to the backend.

The hosted `/admin` dashboard is tenant-scoped even though its core diagnostic
helpers also serve the global self-hosted/operator view. Ordinary hosted queue
status, queue health, worker, orphan, cost, per-user cost, and task-expansion
handlers must pass `auth.org_id`; never accept an organization selector from
the client. A user cost drilldown returns 404 when the requested user belongs
to another org. Deployment-wide diagnostics or mutations (global queue
status/health and slot topology, model concurrency, shared-channel Slack
alert settings, and the global cost-exclusion lists) additionally require the
active org to match
`ODDISH_OPERATOR_ORG_ID`, which fails closed when unset; the frontend discovers
that capability through `GET /admin/operator-access` and hides those controls
for other orgs. `GET /admin/concurrency` reports the deploy, database override,
deprecated-controller advisory, and actual effective limit for one canonical
queue key; `PUT /admin/concurrency` sets or clears the database override.

Admin cost exclusions (`oddish/core/cost_exclusions.py`) name spend that was
never really paid for, along three axes: a **model** (`cost_excluded_models`,
stored and matched by provider-independent model family against `trials.model`,
global and retroactive) and an **experiment**
(`cost_excluded_experiments`, matched against `trials.experiment_id` so a
collection cannot launder gathered trials' cost), or a provider key
(`cost_excluded_llm_keys`, matched through the one-way `trials.llm_key_hash`
stamped before execution, including BYOK overlays). All three fold into
`first_party_spend_filter` and the quota inflight predicates, so excluded
spend leaves the cost dashboards and stops counting against caps together.
It is dropped from accounting but **not** hidden: experiment, task, and trial
surfaces still render the money and label it, via `excluded_cost_usd` on the
experiment rollup and `cost_exclusion_reason` on `TrialResponse`. Keep the SQL
predicates and the `CostExclusions` Python twin in step — a surface that
labels spend differently from the way accounting drops it is worse than one
that says nothing. Callers that do not pass an exclusions snapshot report
`cost_exclusion_reason=None`, which means "unresolved", not "real".

The authenticated org-scoped cost leaderboard is served by `GET /leaderboard` in
`backend/api/routers/dashboard.py`. It shares the admin cost dashboard's
settled first-party spend basis and must stay in sync with its per-user rows:
every spend bucket except Unattributed ranks, including GitHub-identity buckets
with no registered user (shown by their submitted `@handle`). A registered
person's display name falls back name → `@github_username` → email local part,
so an account with no GitHub link still appears; the full email address must
never be exposed. The response deliberately exposes only a person's spend rank,
display name, and cost. Every query is restricted to the active auth
organization. The frontend `/leaderboard` page and dashboard top-five strip
must not add org, email, model, experiment, trial, or internal-id fields to
that contract. Each row also carries its spend rank so the rare row with no
safe display label (e.g. a payer outside the auth org) drops without
renumbering everyone else.

Dashboard experiment free text resolves every parsed bare token against active
members in the authenticated organization before the experiment page query
runs. `backend/dashboard_attribution.py` may search `users.name` and
`users.github_username`, then passes only the token-to-stable-user-id mapping
into `oddish.core.dashboard`; the self-hostable core must not import the hosted
`UserModel`. Each token's owner and latest-runner alternatives belong in the
page predicate before ordering and pagination so AND, OR, exclusion, and quoted
phrase semantics stay intact. The mapping is part of the experiments cache key.
`GET /people/search` is the ordinary READ-scope typeahead endpoint: it is
active-user and organization scoped, returns only `id`, `display_name`, and
`github_username`, and must never search or serialize email addresses.

The admin `GET /admin/costs` response includes analysis spend time series both
by model (`series_qa_by_model`) and by analyzer job kind
(`series_by_analysis_type`). The Cost breakdown chart exposes the latter as the
`Analyzer` stack; analyzer spend does not belong on the people leaderboard.

QA agree/disagree submissions live in the append-only core `feedback` table
(`FeedbackModel`) and use the hosted backend's authenticated
`POST /experiments/{id}/feedback` route. Every row requires a `trial_id`, a
`target` (`qa_verdict` or `qa_action_item`), the verdict classification or
action-item id as `target_key`, and an `agree` or `disagree` vote. Creation
validates organization scope and experiment membership through the shared
`trial_in_experiment` predicate. The dashboard waits for persistence, reports
errors, and permits one successful submission per mounted control. There are
no public, read, update, triage, snapshot, or notification paths.

### Task Identity

`GET /tasks/{task_id}/open` is the bounded first-paint contract for the task
page. It resolves one org-scoped task plus the requested/default version before
running aggregate work. Top-level task status always uses the default version
from `tasks.current_version_id`; selected-version counters, direct version tags,
experiments, and exact agent/model summaries use the requested version. Its
experiment list is derived from that version's live, non-probe, non-superseded,
non-combine trial population, matching `/detail`. Pre-trial audit metadata stays
on `/detail` and is not serialized with the bounded version summary. The
response also carries compact QA verdict presentation/control fields and one
`active_qa_trial` lightweight ref for the task's live, non-superseded QA run.
That ref is task-scoped rather than selected-version-scoped. Every lightweight
trial ref carries `kind` and `has_trajectory`, so a drawer can choose its
initial tab while the authoritative trial-detail request loads; the
selected-version trial preview remains capped at 20 rows. The handler uses at
most three SQL statements, stays below the
50 KB response budget, and must not select trial `result`, `analysis`,
`error_message`, jobs, or ORM relationships. `GET /tasks/{task_id}/detail`
remains the compatibility bundle for CLI and drawer consumers during the soak;
do not point the task route back at it.

`tasks.name` is the human-readable lookup key within an org. Live task names
must stay unique and indexed (`idx_tasks_unique_org_name`) so an upload of the
same task name resolves to the existing task and creates a new `task_versions`
row instead of creating a different task. Renaming a task is allowed, but any
rename path must preserve the live `(org_id, name)` uniqueness invariant and
must not split the task's version history.

`TaskStatusResponse.current_version` / `current_version_id` always report the
task's selected default (`tasks.current_version_id`), including on experiment
pages. Experiment endpoints may scope their trials and aggregate counts to an
experiment-relevant historical version so old or gathered runs remain visible,
but that trial-selection pivot must not replace the reported task default. They
report the pivot separately as `trial_version` / `trial_version_id`, including
on lightweight task shells that omit trial rows.

`tasks.current_version_id` is the user-selectable default, not necessarily the
numerically latest version. In an experiment view, `trial_version_id` uses that
default when the experiment has a non-superseded, non-probe trial for it;
otherwise it falls back to the highest version represented by such trials. The
`task-shells` and `slim-tasks` endpoints must apply the same trial-version rule
so progressive loading cannot change the files/counts pivot or mix one
version's trials with another's artifacts.

`overwrite_current_version` replaces the archive and metadata for
`tasks.current_version_id` without changing its ID or version number. Uploads
land at a unique staging key, copy to an immutable
`tasks/<id>/v<N>-revisions/<token>/` source, and become visible only when the
version row atomically switches `task_s3_key`. Expanded-file readers accept a
manifest only when its `archive_key` matches that selected source, so failed
cleanup cannot expose the prior expansion. The replacement clears derived-file
bookkeeping and pre-trial audit state before re-enqueuing expansion. Existing
trials pinned to that version resolve to the replacement content.

Sweep appends resolve their own version through `resolve_append_version_id`
(`oddish/core/endpoints/sweep.py`). A submission whose `content_hash` is `None`
uploaded no task directory, so it pins new trials -- and scopes its
failed-trial reconciliation -- to the target experiment's effective version
rather than `tasks.current_version_id`. This keeps a top-up on the version the
experiment grid already displays instead of pivoting the whole row onto a
default that an unrelated run advanced. Only an experiment the submission names
explicitly counts: an append that falls back to the task's implicit primary
experiment keeps the task default, so probes and task-page top-ups never run
against older content. Submissions that carry content or name an experiment
with no trials for the task also keep the task default. `create_task` is
unaffected: a fresh task always runs its own upload.

The resolved version is threaded on to `maybe_enqueue_auto_probe` through
`_finalize_sweep`, so an auto-probe inspects the same content its sweep's
trials ran. A probe left on `tasks.current_version_id` while the trials sit on
an older pin would inspect content no trial used, be filtered out of the
experiment grid, and mark the wrong version probed -- leaving the version that
actually ran unprobed. Callers that pin nothing keep the task's current
version. The pre-trial audit trial enqueued inside `append_trials_to_task`
follows the same pin for the same reason.

`use_default_version` on `TaskSweepSubmission` (`--use-default-version`, or
`use_default_version: true` in a sweep config) is the opt-out for deliberately
moving an experiment onto the task's current content. It resolves per task, so
one flag covers a sweep whose tasks sit on different versions. It pins to the
task default rather than the numerically highest version, so the appended
trials are the ones the grid pivots to and stay visible.

`GET /experiments/{experiment_id}/cost-totals` reports both cost and token
usage across every trial owned by the experiment, including older versions,
superseded retries, probes, and soft-deleted trials. Its `billed_*` cost and
token fields are the billed-user subset used by the frontend's New spend tile.

### Task Browser Summary

The default `GET /tasks/browse` path selects and paginates tasks before card
enrichment. Ordering and exact card counters come from the selected
`tasks.current_version_id` row in `task_version_browse_summaries`; there is no
fallback scan over organization trial history when a summary row is missing.
The visible cards then fetch at most 24 current-version trials per task through
a lateral query. `latest_trials_truncated` tells the frontend that the preview
is shorter than the exact `total_trials`.

Summary scope matches normal task cards: exclude probes, superseded attempts,
soft-deleted trials, and `combine:` copies. Any mutation that changes that
population or its metrics must call
`refresh_task_browse_summaries` inside the same transaction. This includes
trial create/import, start/reset, completion, cancellation, retry/supersede,
scoped deletion, and default-version selection. Advanced aggregate filters,
comparisons, and non-default aggregate sorts intentionally retain their
on-demand trial aggregation path.

Refreshes serialize per version with sorted transaction-scoped PostgreSQL
advisory locks; do not replace those locks with `FOR UPDATE` on
`task_versions`, because concurrent trial inserts already hold foreign-key
`KEY SHARE` locks and lock upgrades can deadlock.

---

## `oddish/` — Core Package

### Install Extras

The base `pip install oddish` is CLI-only (light deps). Use extras for server and worker use cases:

```bash
pip install oddish            # CLI only — typer, httpx, pydantic, harbor
pip install oddish[server]    # + FastAPI, SQLAlchemy, asyncpg, alembic, aioboto3
pip install oddish[worker]    # + server + LLM provider SDKs
pip install oddish[all]       # everything including dev tools
```

### Entry Points

- CLI: `oddish` → `oddish.cli:app`
- API server: `python -m oddish.server` (requires `oddish[server]`)
- Standalone worker: `python -m oddish.workers.queue.worker` (requires `oddish[worker]`)
- DB helper CLI: `python -m oddish.db` (requires `oddish[server]`)
- Doc-store MCP server: `oddish-docstore-mcp` (see `oddish/src/oddish/mcp/README.md`)
- Queue key backfill (one-off ops tool): `python -m oddish.backfill_queue_keys`

### Soft Delete

Every model that mixes in `TimestampedMixin` has a `deleted_at` column, but
only classes registered through `oddish.db.soft_delete.register_soft_delete_models`
participate in the session-level auto-filter:

| Package | Soft-deletable models |
|---------|------------------------|
| `oddish.db.models` | `ExperimentModel`, `TaskModel`, `TrialModel`, `TagModel`, `TagAssignmentModel`, `TagExclusionModel`, `TagGrantModel`, `SavedTagFilterModel`, `SkillModel`, `DocumentModel` |
| `backend.models` | `OrganizationModel`, `UserModel`, `APIKeyModel` |

Behavior:

- ORM `SELECT` / `UPDATE` / `DELETE` issued through a session pick up
  `WHERE deleted_at IS NULL` automatically, including eager-loaded
  relationships and aliased subqueries.
- The deletion helpers in `oddish.core.endpoints.deletion` (`delete_task_core`,
  `delete_experiment_core`, `delete_trial_core`) tombstone rows via
  `UPDATE ... SET deleted_at = NOW()` and cancel any matching `worker_jobs`
  rows. They return an empty `s3_prefixes` list so caller S3 cleanup is a
  no-op — S3 data is preserved for restore.
- `unlink_task_from_experiment_core` (same module) is the *scoped* sibling:
  it tombstones only the `task_experiments` join row for one
  `(task_id, experiment_id)` pair plus that experiment's trials for the task,
  and **never** the task row — so a *shared* task can be pulled out of one
  experiment without disturbing the others. It also fires the
  membership-removed tag hook so inherited EXPERIMENT tags drop.
- The `task_experiments` join table also carries `deleted_at`. Because it is a
  SQLAlchemy `Table`, not a registered model, live membership queries and
  relationship joins must explicitly include `task_experiments.deleted_at IS NULL`.
- Raw `text()` SQL doesn't run through the ORM listener; the dispatcher claim
  path (`worker_job_single_job.py`), cleanup sweep, and admin diagnostics each
  add `deleted_at IS NULL` inline.
- The `(org_id, name)` uniqueness on `tasks` is a **partial** unique index
  (`WHERE deleted_at IS NULL`) so a deleted task's name slot is reusable.
- To read or rewrite tombstoned rows, opt out per statement:
  `session.execute(stmt.execution_options(include_deleted=True))`.

### Worker Runtime (`oddish.workers.queue`)

| File | Purpose |
|------|---------|
| `worker_job_dispatcher.py` | `discover_active_worker_job_queue_keys`, `get_worker_job_org_queue_counts`, `build_spawn_plan` (org-first fair-share, with within-org round-robin across queue_keys) |
| `worker_job_single_job.py` | `_CLAIM_WORKER_JOB_SQL`, `run_single_worker_job`, `heartbeat_worker_job` |
| `trial_handler.py` | TRIAL execution body |
| `task_expand_handler.py` / `tag_project_handler.py` | TASK_EXPAND and TAG_PROJECT job bodies |
| `cleanup.py` | Zombie reaper, stale-heartbeat sweep, stage safety nets, **per-slot** orphaned-slot release (see invariants below) |
| `slots.py` | `queue_slots` lease acquire/release (`locked_by` / `locked_until` / `locked_at`) |
| `queue_manager.py` | Per-queue-key concurrency bookkeeping, `run_polling_worker` |
| `worker.py` | Standalone poll loop (`python -m oddish.workers.queue.worker`) |

Auxiliary modules (`concurrency_controller.py` (deprecated — see admin
overrides), `db_helpers.py`, `job_tokens.py`, `runtime_status.py`, `shared.py`,
`trial_failures.py`) support these.

Handler registration lives in `oddish.workers.jobs` (`registry.py`,
`handlers.py`). Both the standalone worker and the backend call
`ensure_builtin_handlers_registered()` at startup.

### Local Development

You need a running Postgres instance. Start one however you prefer (e.g.
`docker run -d --name oddish-db -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish -p 5432:5432 postgres:16-alpine`),
then:

```bash
cd oddish
cp env.example .env
uv sync --extra server
uv run python -m oddish.db setup
uv run python -m oddish.server
```

That gives you the API on `http://localhost:8000` with background workers
started by the API process. Point the CLI at it with
`export ODDISH_API_URL="http://localhost:8000"`. For the hosted Oddish API
instead, keep the default API URL and set `ODDISH_API_KEY="ok_..."`.

`python -m oddish.server` auto-starts workers by default. For separate worker
processes (scaling or debugging): `uv run python -m oddish.workers.queue.worker`.

### Database Commands

```bash
uv run python -m oddish.db init    # run Alembic migrations
uv run python -m oddish.db setup   # alias for init
uv run python -m oddish.db reset   # drop and recreate all tables
uv run python -m oddish.db purge   # delete data, preserve migration state
```

### API Server Flags

```bash
uv run python -m oddish.server --host 0.0.0.0 --port 9000
uv run python -m oddish.server --n-concurrent '{"openai/gpt-5.2": 8, "anthropic/claude-sonnet-4-5": 8}'
```

### HTTP Endpoints (core standalone server)

Routes registered in `oddish/src/oddish/server/__init__.py`. The hosted backend
exposes a superset (org-scoped, plus documents/tags/skills/orgs/api-keys/admin
extensions) — see `backend/README.md`.

| Area | Endpoints |
|------|-----------|
| Health / dashboard | `GET /health`, `GET /dashboard` |
| Task upload | `POST /tasks/upload/init` (returns presigned PUT URL), `POST /tasks/upload/complete` |
| Trial import | `POST /trials/import/init`, `POST /trials/import/complete` |
| Sweeps | `POST /tasks/sweep`, `POST /tasks/sweep/batch` |
| Tasks | `GET /tasks`, `GET /tasks/browse`, `GET /tasks/browse/experiment-options` (typeahead for the experiment filter; `facets.experiments` is deprecated/always empty; the other facet lists are served from the `trial_facets` vocabulary — write-through on trial creation plus a periodic rebuild sweep, see `oddish/src/oddish/core/trial_facets.py`), `GET /tasks/{task_id}`, `GET /tasks/{task_id}/open`, `GET /tasks/{task_id}/detail`, `GET /tasks/{task_id}/versions[/{version}]`, `PUT /tasks/{task_id}/versions/{version}/default`, `POST /tasks/cancel` (optional `experiment_id` scopes the cancel to that experiment's trials so shared tasks keep running elsewhere) |
| Task QA | `POST /tasks/{task_id}/qa/retry`, `POST /tasks/{task_id}/qa/cancel`, `POST /tasks/{task_id}/qa/backfill` |
| Experiments | `POST /experiments/combine`, `PATCH /experiments/{experiment_id}` |
| Trials | `GET /tasks/{task_id}/trials/{index}`, `POST /trials/{trial_id}/retry` (optional `registry_auth` body), `GET /trials/{trial_id}/live` ((attempt, seq)-cursor live transcript), `GET /trials/{trial_id}/logs[/structured]`, `GET /trials/{trial_id}/trajectory`, `GET /trials/{trial_id}/result` |
| Files | `GET /tasks/{task_id}/files[/{path}]` (`inline=false` omits listing bodies; `presign=false` omits URLs; `max_bytes=N` caps archive-backed file reads), `GET /trials/{trial_id}/files[/{path}]`, `GET /trials/{trial_id}/debug-files` |
| Admin diagnostics | `GET /admin/slots`, `GET /admin/queue-status`, `GET /admin/orphaned-state`, `GET /admin/queue-health` |
| Public sharing | `/public/experiments...` router from `oddish.core.sharing.public` |

The core server has **no DELETE routes**. Deletion endpoints
(`DELETE /experiments/{id}`, `DELETE /experiments/{id}/tasks/{task_id}`,
`DELETE /trials/{trial_id}`) exist only on the hosted backend (admin-gated) and
call the shared `oddish.core.endpoints.deletion` helpers.

Public share links use 256-bit `public_token` values and are access-by-link, not
enumerable. The unauthenticated `/public/experiments` list intentionally returns
no share tokens. Public task/trial/live/file routes must stay scoped under
`/public/experiments/{public_token}/...` and verify membership in that shared
experiment; do not reintroduce `/public/tasks/{task_id}` or
`/public/trials/{trial_id}` ID-only access. Unpublishing an experiment clears
`public_token`, so republishing mints a fresh link and old URLs stay revoked.
Capability evidence links on a share page must remain inside `/share/{token}`;
they select the shared task and trial, open the trajectory tab, and retain the
cited step anchor. They must never point signed-out readers at authenticated
`/tasks/...` routes.

### Configuration and model routing

Settings are loaded from `oddish/.env`; see `oddish/env.example`,
`backend/.env.example`, and `frontend/env.example` for the complete env surface.
Keep these routing rules in sync with `oddish/src/oddish/config.py` and
`oddish/src/oddish/workers/harbor/runner.py`:

- EC2 is an explicit, opt-in Harbor backend: `ODDISH_EC2_ENABLED=true` registers
  it and permits hosted `environment=ec2`, but capability ordering keeps Daytona
  as the CPU default. V1 launches one ephemeral CPU instance per trial and uses
  public-IP, key-only SSH. It does not support accelerators, attach/retain mode,
  private networking, Spot, or AWS infrastructure provisioning.
- An EC2 deployment must provide an existing Ubuntu-compatible AMI, subnet,
  security group, EC2 key pair/private key, region, and instance type. The
  security group must allow TCP/22 from the Modal worker network path. Keep
  `ODDISH_EC2_SSH_PRIVATE_KEY` in the dedicated worker secret, materialize it
  mode `0600`, and never bake it into an image or attach it to API, dispatcher,
  or reconciler functions.
- EC2 control credentials must be least privilege: workers need
  `sts:GetCallerIdentity` plus launch, describe, image lookup, tagging, and
  termination actions; reconciliation needs `sts:GetCallerIdentity`, describe,
  and tag-scoped termination. Store them under the namespaced
  `ODDISH_EC2_AWS_*` settings; workers materialize a mode-`0600` AWS profile and
  scrub the raw values before starting Harbor. API cancellation delegates to a
  dedicated Modal teardown function, so API and dispatcher containers receive
  neither EC2 control nor SSH secrets. An optional platform-owned
  `ODDISH_EC2_INSTANCE_PROFILE` may be attached; it is visible to tenant code,
  so keep it task-scoped and grant the control identity `iam:PassRole` only for
  that role. Oddish always requires IMDSv2 so cloud-init can retrieve the EC2
  launch key: the response hop limit is one without an instance profile and two
  when a profile is explicitly exposed to Docker containers.
- Oddish does not create the VPC, subnet, security group, AMI, key pair, or IAM
  policy. Every instance and root volume must carry protected Oddish ownership,
  deployment, task/trial, worker-job, worker-attempt, sandbox-run, unguessable
  launch-token, and Harbor-session tags. A durable `sandbox_runs` row is created
  before launch; Harbor's `environment-provisioned` event binds the structured
  handle before SSH/bootstrap. The locked Harbor exposes that event natively;
  ephemeral pins that predate it are bridged by wrapping
  `EC2Environment._launch_instance` and emitting the same identity immediately
  after launch. A pin whose EC2 environment does not expose the required launch
  seam fails before `Job.run()` rather than launching untracked provider state.
  Normal teardown, cancellation, stale-heartbeat cleanup, and reconciliation
  terminate only after the full ledger/tag tuple agrees.
- EC2 orphan reconciliation snapshots deployment-tagged instances before the
  shared cleanup transaction, evaluates worker liveness using the database clock,
  and terminates only after the transaction commits. It preserves live linked
  jobs and conservatively preserves unlinked trial startup for 30 minutes, then
  reaps terminal and stale owners with an exact ledger match; missing or
  mismatched ledgers are ownership refusals, never destructive guesses. The
  protected 14-hour hard maximum age overrides worker liveness only for exactly
  owned instances. `ODDISH_EC2_MAX_CONCURRENT_INSTANCES` is enforced globally
  with heartbeat-renewed `sandbox_capacity_leases`, independent of model/variant
  queue slots. The dispatcher budgets against live EC2 leases before spawning,
  while each worker still acquires the lease atomically before claiming a job.
  A successful inventory snapshot also closes `PROVISIONING` / `TERMINATING`
  ledger rows that have no provider identity, no running owner, no matching
  inventory tags, and are older than the 30-minute launch-race grace. Capacity
  cleanup reruns after that transaction commits so those rows cannot reserve
  slots forever; an inventory failure never authorizes this finalization.
  Inventory and termination failures stay visible in logs/metrics while the rest
  of queue cleanup continues.
- Claude Code currently prefers the direct Anthropic API whenever
  `ANTHROPIC_API_KEY` is available because
  `ODDISH_CLAUDE_CODE_FORCE_DIRECT_API` defaults to `1`. Set the flag to `0`
  to restore the Modal image's Bedrock route (`CLAUDE_CODE_USE_BEDROCK=1`).
  Bedrock model aliases must normalize to an invokable inference profile
  (`global.` / `us.` / ARN) via `to_bedrock_model_id`. The separate
  `anthropic-hdo/<model>` prefix always uses `ANTHROPIC_HDO_API_KEY` and blanks
  Bedrock routing for that trial.
- OpenAI-family jobs default to Azure OpenAI. Use
  `ODDISH_OPENAI_PROVIDER=openai` plus `OPENAI_API_KEY` only when intentionally
  routing to public OpenAI.
- z.ai, MiniMax, Moonshot/Kimi, Fireworks, xAI, Meta, and Anthropic HDO each
  have explicit canonical provider prefixes and queue keys: `zai/`, `minimax/`,
  `moonshot/`, `fireworks/`, `xai/`, `meta/`, and `anthropic-hdo/`. Add or
  change provider aliases in `config.py`, then update env injection in the
  Harbor runner and the network allowlist notes.
- Gemini model ids use the `gemini/<id>` prefix. `_build_agent_config` hands
  each agent the spelling its LLM client expects (litellm agents in
  `_LITELLM_MODEL_ID_AGENTS`, Vercel AI SDK agents in
  `_AI_SDK_MODEL_ID_AGENTS`); add a new agent to the set matching its client.
- Kubernetes task charts that enforce their own runtime egress boundary can opt
  into Oddish's model-route bridge with a chart-root
  `.oddish-agent-egress-hosts` marker containing exactly
  `agentEgressProxy.runtimeAllowedHosts`. Because EC2/k3s cannot perform
  Harbor's dynamic phase-policy switch, the task must retain a public
  environment baseline, omit formal agent/step network policies, and declare
  its stable task-owned proxy hosts in
  `metadata.oddish_agent_egress_allowed_hosts`. Before Harbor instantiates the
  environment, Oddish merges that task policy with the selected model and
  agent runtime hosts and explicit `extra_allowed_hosts` entries, then writes
  the resolved normalized policy as a semicolon-delimited value to
  `environment.kwargs.helm_values.agentEgressProxy.runtimeAllowedHosts`. That
  Helm key is reserved for Oddish: the chart must treat it as the authoritative
  runtime allowlist for its deny-by-default proxy, using chart defaults only
  when no hosted override is supplied. This chart contract accepts exact DNS
  hostnames only; wildcard, IP, and CIDR policies fail closed because the task
  proxy materializes concrete DNS and TLS/SNI routes. Charts without the
  declaration are untouched; Compose and single-container egress behavior
  remains on its existing paths.
- Provider secrets are referenced by env var name (`AWS_BEARER_TOKEN_BEDROCK`,
  `ANTHROPIC_HDO_API_KEY`, `ZAI_API_KEY`, `MINIMAX_API_KEY`, `MOONSHOT_API_KEY`,
  `FIREWORKS_API_KEY`, `XAI_API_KEY`, `META_API_KEY`) and must not be persisted
  on trial rows.
- `grok-build` (xAI) writes a Grok CLI config whose `[model.*]` blocks pin an
  `api_backend`. Upstream Harbor hardcodes `responses` (`POST /v1/responses`),
  but not every xAI model is served there — some (e.g. newer/unreleased models)
  live only on Chat Completions and answer a Responses request with a 404
  `The model <id> does not exist or your team does not have access to it`.
  `OddishGrokBuild` accepts an `api_backend` kwarg
  (`chat_completions` | `responses` | `messages`); pass
  `--agent-kwarg api_backend=chat_completions` to route such a model. When
  unset, the upstream `responses` default is preserved.
- `grok-build` trajectories come from the CLI's on-disk **session store**, not
  its headless stdout. `grok -p --output-format json|streaming-json` only emits
  the assistant's `text`/`thought` — no tool calls and no token usage — so
  `OddishGrokBuild` copies `$GROK_HOME/sessions/.../<id>/` into
  `/logs/agent/grok-session` after the run and converts `updates.jsonl`
  (ACP `tool_call` / `tool_call_update` / `agent_message_chunk`) plus
  `events.jsonl` usage into the ATIF trajectory + token `FinalMetrics`
  (`grok_build_session.py`). If the session store is missing it falls back to
  the text-only stdout trajectory. Do not "fix" trajectories by parsing stdout —
  the tool calls are only in the session store.
- The grok **live** transcript is the one reader that does parse stdout
  (`GrokBuildFold` in `live_tail.py` tails `/logs/agent/grok-build.json`): the
  session store is copied into the trial logs only after the run, so it cannot
  feed a live view. That panel is therefore text and reasoning only, with no
  tool calls and no running token/cost counters; it is not the trajectory and
  must not be used to build one.

Storage defaults:

- S3-compatible storage is **required**. Clients PUT task bundles directly
  to a presigned URL returned by `/tasks/upload/init` and then call
  `/tasks/upload/complete`.
- uploaded task bundles: normally `tasks/<task_id>/v<N>/.oddish-task.tar.gz`;
  in-place replacements use immutable
  `tasks/<task_id>/v<N>-revisions/<token>/.oddish-task.tar.gz` sources selected
  by `task_versions.task_s3_key` (legacy unversioned bundles remain readable)
- Harbor job outputs: `/tmp/harbor-jobs`
- Modal workers also check `/mnt/oddish-tasks` before falling back to the S3 download path

EC2 canary procedure:

1. In a non-production AWS account, create the Ubuntu-compatible AMI, subnet,
   public-IP route, SSH security group, key pair, and least-privilege worker IAM
   credentials. Enable the backend with the `ODDISH_EC2_*` settings documented
   in `backend/.env.example`.
2. Submit a small CPU-only task with `oddish run <task> --env ec2 --background`.
   Confirm the trial records provider `ec2` and an external instance handle, and
   confirm the instance and root volume have the protected Oddish tags.
3. Verify SSH/bootstrap, Docker Compose execution, result/artifact collection,
   and terminal instance state. Confirm the instance has the configured IAM
   profile (or none), and that metadata is IMDSv2-only with response hop limit
   one without a profile or two with a profile.
4. Start a longer canary, cancel it with `oddish cancel <trial-or-task-id>`, and
   confirm the tagged instance terminates exactly once.
5. In the non-production deployment only, deliberately interrupt a worker after
   launch. Confirm stale-heartbeat/orphan reconciliation preserves it during the
   grace window and terminates it afterward. Also verify the hard maximum-age
   path. Review logs/metrics for the candidate, ownership decision, and terminate
   result before enabling production traffic.

### Using as a Library

```python
from oddish.config import settings
from oddish.db import (
    TaskModel,
    TrialModel,
    WorkerJobModel,
    WorkerJobKind,
    WorkerJobStatus,
    get_session,
    init_db,
)
from oddish.queue import create_task
from oddish.schemas import HarborConfig, TaskSubmission, TaskSweepSubmission, TrialSpec
from oddish.workers import run_polling_worker
```

---

## Repo-wide Gotchas

### Never expose probes in public/share views

Probes are an **experimental, internal-only** feature. They must never appear in
any public, unauthenticated surface — the `/share/[token]` experiment view, the
`/datasets/[token]` view, or any `/public/*` API response. Both public views are
fed by the same endpoints in `oddish/src/oddish/core/sharing/public.py`, so the
filtering lives at the **data layer** (don't return `is_probe` trials), not just
the UI:

- `get_public_task_for_experiment` (`sharing/helpers.py`) strips `is_probe` trials from the
  loaded task, covering `get_public_task_status`.
- `list_public_experiment_tasks` excludes `is_probe` when filtering each task's
  trials.
- `list_public_task_trials` always passes `probe=False` (never honors a
  caller-supplied probe filter publicly).

When adding a new public/share endpoint or surfacing a new trial/task field
publicly, exclude probes the same way. Filter at the query/data layer — UI
guards alone are not enough, since the trials still ship to the browser.

### `list_tasks_core` `load_only` and MissingGreenlet

`list_tasks_core` (`oddish/src/oddish/core/endpoints/tasks_query.py`) powers
every `/tasks` route, including the experiment page. Its **compact path**
(`compact_trials=True`) restricts the trial/task/experiment selectin loads with
`load_only(...)`, which makes *only* the enumerated columns eager and defers
everything else. Under async SQLAlchemy, reading a deferred column in a
response builder fires a lazy-load outside the request greenlet and 500s with
`sqlalchemy.exc.MissingGreenlet`.

So: whenever you surface a **new `TrialModel` / `TaskModel` / `ExperimentModel`
column in the FE** (i.e. read it in `build_trial_response`,
`build_compact_trial_response`, or `_build_task_status_response` in
`core/helpers.py`), you **must also add that column to the matching `load_only`
set** in `list_tasks_core`. The full (non-compact) builder has no `load_only`,
so it won't catch the omission — the failure only shows up on the compact
experiment page. Builder unit tests can't catch it either (in-memory models
have all attrs set); the bug lives in the query options, not the builder.

### Dashboard pipeline stats use reserved queue keys

`get_queue_stats` / `get_queue_stats_by_org` (`oddish/src/oddish/queue.py`)
bucket trial counts by each trial's own `queue_key`, and the
trajectory-analysis / verdict pipeline counts under the **reserved**
`analysis` / `verdict` buckets (`ANALYSIS_PIPELINE_QUEUE_KEY` /
`VERDICT_PIPELINE_QUEUE_KEY` in `oddish/src/oddish/config.py`). Never key
pipeline counts off the analysis/verdict *model*'s queue key: that folds
pipeline state into a real model's bucket — an incident rendered 4k+ trials
mid-classification as "running workers" under one model's queue while that
model's actual trials were routed into the "analyses" pipeline. These are
presentation buckets only; QA/audit/analyzer trials queue under
`get_qa_queue_key()` (the analysis model's concurrency bucket) and are
excluded from the per-queue trial scans by `kind = 'agent'`.

Related invariant: a QA trial that dies retries like any trial; a terminal QA
trial whose import never landed is re-imported by the VERDICT_PENDING healer
in the cleanup sweep, which also creates a fresh QA trial when none exists.
Appending trials to a task cancels its in-flight QA trial (stamped with the
cancelled harbor_stage) so a stale import can't overwrite the new set's
verdict; the importer additionally refuses to store a verdict while any live
agent trial is non-terminal.

---

## `backend/` — Hosted Cloud Layer

### Authentication Model

The backend accepts auth from `Authorization`, `X-Clerk-Authorization`, or
`X-Authorization` (parsed in `backend/auth/__init__.py:get_auth_context`;
token verification lives in `backend/auth/verification.py`).

- **API keys** (`ok_...`): stored hashed (SHA-256) in `api_keys`; scopes are `full`, `tasks`, `read`
- **Clerk JWTs**: validated against Clerk JWKS; org context extracted from token claims

There are exactly two org roles: `admin` (manage users/settings) and `member`
(run evals, view results). New users default to `member`.

Auth flow: read token → if `ok_` prefix validate API key → otherwise validate Clerk JWT and resolve org/user → return `AuthContext`.

Short-lived internal READ keys may set `api_keys.bound_analysis_trial_id`. The
binding stores only the requesting analysis trial id; every request derives its
allowlist from that Trial row. QA and QA-eval keys may GET only Trial resources
whose ids appear in `harbor_config.analysis_payload.trial_ids`; a QA-eval
payload must contain exactly one non-empty source id or the key authorizes
nothing. Audit keys may
GET only `/tasks/{task_id}/files` resources for the analysis trial's exact
`task_version_id`, and the request must carry that pinned version number.
Summarize trials receive no query key. Bound keys fail closed on other routes
and on every non-GET request. Ordinary operator-probe keys remain unbound and
retain the existing organization-wide READ policy. Do not copy source ids or
task ids onto API-key rows and do not add analysis-only mirror endpoints.

API key creation is user-auth only (API-key auth is rejected so one key cannot
mint another) and is self-service for every org — any `admin` or `member` user
may create keys for their own org (`can_create_api_keys` /
`require_api_key_creator`). Admins may mint `full`, `tasks`, or `read` keys;
members may mint only `tasks` or `read` keys. Member-created `tasks` keys can
run task/trial workflows and read files, and can cancel in-flight runs, but are
blocked from broader org mutations such
as tagging, collections, documents, skills, and GitHub webhook updates. The
creator role is stamped on the API key at mint time so later role changes or
deleted creator rows do not broaden a member-created key.

If a Clerk JWT arrives without `org_id`, the backend tries to resolve a single existing org membership, or provisions a personal org.

### Worker Architecture

Dispatcher + reconciler + single-job pattern, backed by the unified
`worker_jobs` table. **Dispatch and reconciliation are deliberately separate
scheduled functions** so a slow or deadlocking reconciliation sweep can never
block worker spawning (previously they shared one function under a tight 60s
timeout; a sweep that timed out spawned zero workers that cycle, and a SIGKILL
mid-sweep left orphaned `idle in transaction` locks that deadlocked the next
sweep):

1. `poll_queue()` runs on a `POLL_INTERVAL_SECONDS` (180s) Modal schedule under
   `DISPATCHER_TIMEOUT_SECONDS` (120s). It only discovers active queue keys
   (`discover_active_worker_job_queue_keys`) and launches up to
   `MAX_WORKERS_PER_POLL` single-job containers via the org-first fair-share
   `build_spawn_plan`. It runs no cleanup. `MAX_WORKERS_PER_POLL` is the
   dominant throughput ceiling: long agent trials hold a `queue_slots` lease
   for their full duration, so steady-state running workers ≈
   `spawns_per_poll × trial_duration / poll_interval`. It must stay high enough
   to fill the per-model concurrency limits; the per-queue-key slot caps and
   `WORKER_MAX_CONTAINERS` remain the real bounds.
2. `reconcile_queue_state()` runs on its own `CLEANUP_INTERVAL_SECONDS` (240s)
   schedule under a generous `CLEANUP_TIMEOUT_SECONDS` (600s) so it is never
   SIGKILLed mid-transaction. Each phase is wrapped best-effort: stale
   `queue_slots` lease cleanup, `cleanup_orphaned_queue_state` (zombie-txn reap
   + stale-heartbeat sweep + stage safety nets + **per-slot** orphaned slot
   release — see invariants below), and the experiments owner backfill
   (`dashboard_owner_backfill`, which keeps the dashboard Mine filter on its
   indexed fast path). The display-hygiene clear of terminal-trial claim
   metadata (`clear_terminal_trial_runtime_refs`) runs after the main
   transaction commits, in batched `FOR UPDATE SKIP LOCKED` transactions, so it
   can neither deadlock against live workers nor roll back the sweep.
3. `process_single_job(queue_key)` acquires a `queue_slots` lease (stamping
   `locked_by = <worker_id>`, `locked_at = NOW()`, `locked_until = NOW() +
   WORKER_TIMEOUT + 30s`) and calls `run_single_worker_job` →
   `drain_worker_jobs`, which atomically claims one or more `worker_jobs` rows
   (stamping `current_worker_id`), dispatches to the registered handler for the
   row's kind, writes heartbeats on both `worker_jobs.heartbeat_at` and the
   mirrored domain column, records the outcome (`SUCCESS` / `RETRYING` /
   `FAILED` / `CANCELLED`), runs the post-success hook when applicable,
   releases the slot in its `finally`, and exits.
4. `send_slack_expense_notifications()` runs every five minutes in production
   when the webhook or the bot token is configured. It deterministically
   alerts for experiments at $1,000 and each additional $1,000 of spend, and
   for any recent trial over $200 -- the old "must exceed 2x the same-task/model
   peer average, with at least one peer" filter (`trial_average_multiplier`)
   is gone, so the $200 floor is unconditional. A trial over $1,000 produces
   two alerts: the owner's DM, plus a separate in-channel escalation
   (`trial-escalation:{id}`) mentioning the owner and the admin-editable
   always-ping list (see below). Both
   carry the ":rotating_light: *Very expensive trial*" heading in place of
   the usual ":warning: *Expensive trial*". Milestones are driven by *new*
   spend: spend that finished within the 2h watch window. Milestones already
   covered by the pre-window baseline (`total - recent`) are claimed and
   completed silently so first observing pre-existing spend never dumps
   historical alerts. Failed loud deliveries retain per-channel retry
   markers; primary and retry completion is atomic. Indeterminate loud claims
   are not repeated because the external channels do not offer an idempotency
   key, while interrupted silent claims are completed without sending.
   The in-channel escalation -- the $1,000 floor a trial must clear to post to
   the shared channel, plus the always-ping list -- is admin-editable at runtime
   from the Costs tab of `/admin`, backed by the single `slack_alert_settings`
   row (`PUT /admin/slack-alert-settings`, `require_admin`). The constants in
   `slack_alert_settings.py` are the defaults that stand when no row exists, and
   DELETE restores them. `load_alerts` reads the row once per run in a session
   of its own -- a missing table (deploy-before-migrate) falls back to the
   defaults rather than aborting the run's transaction. The escalation threshold
   is deliberately absent from the alert key: a key that embedded it would mint
   fresh dedup rows on each retune and re-alert the whole window. The per-user
   DM cutoffs -- the $1,000 milestone/repeat and the $200 trial floor -- are
   deploy-time constants (`DEFAULT_*_USD` in `user_alert_prefs.py`) that each
   person inherits until they override them in their own notification settings;
   they are not admin-editable. The 0.5 experiment-failed ratio stays a module
   constant in `slack_notifications.py` because it governs failure DMs, not
   spend. The five `ODDISH_SLACK_*` threshold env vars
   (`ODDISH_SLACK_EXPENSIVE_EXPERIMENT_USD`, `ODDISH_SLACK_EXPERIMENT_REPEAT_USD`,
   `ODDISH_SLACK_EXPENSIVE_TRIAL_USD`, `ODDISH_SLACK_TRIAL_AVERAGE_MULTIPLIER`,
   `ODDISH_SLACK_EXPERIMENT_FAILED_RATIO`) remain gone. It uses the shared
   settled-cost basis and contains no agent/LLM path. It is on by default for
   the production app and off by default on preview apps; a preview opts in
   by setting `ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS=true` and providing
   either `SLACK_EXPENSE_WEBHOOK_URL` or `SLACK_ALERT_BOT_TOKEN`, optionally
   through a preview-only named secret selected by
   `ODDISH_SLACK_EXPENSE_SECRET_NAME`. The email delivery channel
   (`RESEND_API_KEY`, `ODDISH_EXPENSE_EMAIL_FROM`, `send_owner_emails`,
   `_post_email`) has been deleted entirely.
   Cost alerts -- experiment milestones and expensive trials -- DM their
   experiment's owner; the email channel is gone. The only cost alert that
   still reaches the webhook is the over-$1,000 trial escalation, which
   carries an `<@...>` mention-line prefix resolved from the relevant emails.
   `send_alerts(webhook_url, alerts, *, bot_token=None)` claims each alert
   before resolving its mentions, so already-delivered alerts cost zero Slack
   lookups; a mention-lookup failure never sinks the underlying alert, it
   just posts without the prefix. The DM-only kinds (`dm_only=True`,
   delivered solely by `send_owner_dms`, never posted to the webhook) are
   experiment milestones, expensive trials, experiment-failed, trial-failed,
   and qa-failed. Trial-failed fires for any trial with
   `status == FAILED`, or `status == SUCCESS` with `result->>'harbor_exception'`
   set (a crashed agent still gets its verifier run, so the row lands as
   SUCCESS with an exception marker rather than FAILED); SKIPPED trials never
   match either arm, and soft-deleted, superseded (retried), and
   user-cancelled (`harbor_stage == 'cancelled'`) trials are additionally
   excluded via the existing `current_trial` predicate, gated on
   `finished_at >= recent_cutoff` (the same 2h window). Qa-failed fires for
   `verdict_status == SUCCESS` with `verdict->>'is_good' == 'false'`, or
   `verdict_status == FAILED` with a `verdict_error` other than the
   user-cancellation message `"Cancelled by user"` (cancellation also stamps
   FAILED and is not a QA failure), gated on
   `verdict_finished_at >= recent_cutoff`; its recipient is resolved through
   `TaskModel.created_by_user_id -> UserModel.email`. Both dedup on
   `alert.key` = `"trial-failed:{bucket}"` / `"qa-failed:{bucket}"` where
   `bucket` is the task version id (falling back to the task id on
   unversioned trials); `build_alerts` also collapses duplicate keys produced
   within a single run. The DM claim key is `"dm:{alert.key}:{recipient}"`,
   so each person is DMed at most once per task version, ever.

Handler registration happens at container load via
`ensure_builtin_handlers_registered()`. `_POST_SUCCESS_HOOKS` in
`worker/functions.py` contains only `notify_github_trial` for successful
`TRIAL` worker jobs. QA GitHub notifications use a separate import hook:
`register_qa_imported_hook(notify_github_qa)` refreshes the whole PR comment
(per-trial classifications plus the task verdict) after a QA trial's artifact
is imported. There is no `notify_github_analysis` hook or active task-level
`QA` worker-job handler.

### Trial Storage Layout

Trial artifacts live under ``tasks/<task_id>/trials/<trial_id>/``. Every upload
uses an immutable retry prefix: ordinary agent and operator-probe attempts use
``attempt-<attempt>/``; QA, QA-eval, audit, and summarize attempts use
``analysis-<kind>/attempt-<attempt>/``. Harbor's randomly named trial directory
lives below that attempt prefix. ``trials.trial_s3_key`` stores the exact
attempt prefix returned by the uploader, so later retries never replace the
manifest or leave the row pointing at a mixed set of attempt directories.

The attempt root's Harbor ``result.json`` is the artifact manifest. The shared
trial-artifact resolver extracts ``trial_results[].trial_name``, sanitizes it
with the same storage-key encoding used during upload, and selects exactly one
``<trial_s3_key>/<trial_name>/`` directory. Trajectory, task instruction,
verifier output, agent-file, and structured/free-form log readers all use that
selected directory. If the
manifest is malformed or an exact artifact is absent, a reader returns no
artifact; it never substitutes a sibling retry directory. Deterministic
candidate/list fallback exists only for imported and historical layouts without
a root manifest. When ``trials.trial_s3_key`` is null, the canonical trial root
is eligible for that historical fallback only if it contains no ``attempt-N``
or ``analysis-*/attempt-N`` namespace; once immutable attempts exist, the
missing pointer makes every sibling non-authoritative and artifact reads fail
closed. The file LISTING and file CONTENT endpoints both root at
``trials.trial_s3_key`` when set, so listed relative paths round-trip without
doubling an analysis or attempt segment. Analysis-result readers locate their
one result artifact by filename suffix within that authoritative attempt prefix.

### Worker Runtime Invariants & Pitfalls

Load-bearing properties, several learned from incidents. Changing them naively
silently breaks throughput or correctness — read before touching
`worker/functions.py`, `slots.py`, `cleanup.py`, or the dispatcher.

1. **Workers hold NO DB connection during the Harbor run.** A trial runs for
   minutes to ~12h but only touches the DB for a few ms (claim, 30s heartbeats,
   outcome), so workers use `NullPool` (`Settings.db_use_null_pool`) + per-op
   `asyncpg` connections. ⚠️ Never introduce a pooled/long-lived connection or
   open session spanning the run: it pins one idle connection per running trial
   and exhausts the Supavisor/PgBouncer cap. (The API keeps a warm `QueuePool`
   only because it's short-lived — that reasoning doesn't transfer to workers.)
   Quota pause signals come from live cost checkpoints. The owning worker calls
   Harbor `Job.pause()` / `Job.resume()` without holding a database session. A
   paused trial's `trials.status` becomes `PAUSED`, while its owning
   `worker_jobs` row stays `RUNNING`; it retains its queue slot and keeps
   heartbeating. Running and paused jobs periodically open a short session so
   they react to spend reported by sibling trials and to quota changes.

2. **`queue_slots` is the real concurrency gate.** Per-queue-key concurrency is
   enforced by leasing a `queue_slots` row (`acquire_queue_slot`, `FOR UPDATE
   SKIP LOCKED`), not by spawn count. The dispatcher budgets on `worker_jobs`
   RUNNING (`limit - running`) while the worker gates on a free slot — if those
   counters drift, the dispatcher over-spawns workers that exit immediately
   (watch for `metric=queue_lock_contention` floods).

3. **Slot leases can outlive their worker — reclaim per-slot.** The lease
   (`locked_until`) is `WORKER_TIMEOUT_SECONDS + 30` (~12h); a SIGKILLed /
   preempted worker never runs its `finally` release. `cleanup_orphaned_queue_state`
   frees a slot whenever its `locked_by` has no `RUNNING` `worker_jobs` row on
   `current_worker_id` (with a `locked_at` grace, `ORPHANED_SLOT_GRACE_MINUTES`
   = 2, for the acquire→claim gap). ⚠️ Never gate this per-queue_key (e.g.
   "release only if zero jobs RUNNING on the key") — that was the original bug:
   one live job pinned every leaked lease for ~12h and starved the queue. The
   link is always `queue_slots.locked_by == worker_jobs.current_worker_id`.
   The limit used for both spawn planning and slot acquisition comes from
   `model_concurrency_overrides` when an admin override exists, otherwise from
   the deploy-time `ODDISH_MODEL_CONCURRENCY_OVERRIDES` / default settings.
   Dynamic advice never exceeds an admin override, and an override-read failure
   fails closed at zero rather than risking reopening a disabled queue.

4. **One model ⇒ one queue_key.** Limits key off the full `queue_key`; the same
   model under two keys gets the *sum* of both buckets against one provider quota
   (→ 429s, split dashboards, starvation). Canonicalize at enqueue in
   `oddish.config` (`normalize_trial_model` / `get_queue_key_for_trial` /
   `normalize_queue_key`): nop/oracle + variants collapse to the single
   `nop_oracle` id (`is_nop_oracle_agent`); z.ai / MiniMax / Moonshot / xAI map
   to `<provider>/<id>`. ⚠️ Known gap: Gemini isn't canonicalized — a bare
   `gemini-…` becomes `google/…` while `gemini/…` stays `gemini/…`, splitting one
   model across two buckets.

5. **No provider-level concurrency cap.** Each Bedrock/Gemini model id is its own
   bucket, but they share one AWS/Google account quota — the sum of per-model
   limits can exceed account RPM/TPM with no global throttle (a source of 429s).

6. **Stale-heartbeat reap can double-run a trial.** If heartbeats stall for
   `STALE_HEARTBEAT_MINUTES` (15, e.g. a pooler blip), the reaper flips the live
   trial to `RETRYING` and another worker may run it concurrently — no fencing
   token. The window is a deliberate trade-off (raised from 10 after an incident);
   shrink with care.

7. **A stable-variant harbor pin is REWRITTEN at claim time.** A trial's
   `harbor_config.resolved_sha`/`source` (and the indexed `trials.harbor_sha`
   projection) are stamped at submission, but the deployment is the unit of
   harbor identity for a stable variant (`variant_id == "gke"`): a trial
   queued across a pin bump executes whatever the deployment now ships. The
   claim path (`_refresh_stable_variant_pin`, trial_handler) rewrites the
   recorded pin to what the claiming runtime EXECUTES -- read from the
   imported harbor's PEP 610 installation metadata, so a Modal variant
   image stamps its blessed pin, the default image stamps the locked
   default, and self-host or local workers stamp whatever is installed --
   logging one supersession warning. Consequence for readers: pin filters and
   audit queries over `harbor_sha` see the EXECUTING revision for stable
   variants (the `gke` blessed pin, or the locked default pin for every
   non-registered variant), never a stale submission-time value. `ephemeral`
   exact-pin trials keep their submission pin verbatim -- they run it
   out-of-process against the recorded source/SHA -- and the projection is
   reconciled at claim for every harbor-running trial, healing retry/
   combine/import copies persisted without it. Local (self-host) mode
   routes through the same refresh and needs no override: the metadata of
   the harbor it imports is what it executes.

### Local Development

```bash
cd backend
uv sync
uv run modal serve deploy.py
```

### Configuration (backend)

```bash
cp backend/.env.example backend/.env
```

Minimum required: `ODDISH_DATABASE_URL` and `CLERK_DOMAIN`. Add
`CLERK_SECRET_KEY` for Clerk-backed org management and `CLERK_WEBHOOK_SECRET`
for webhook ingestion. Common optional settings include `CORS_ALLOWED_ORIGINS`,
`CLERK_ISSUER`, `CLERK_JWT_AUDIENCE`, the `ODDISH_S3_*` set, provider keys
(`AZURE_OPENAI_*`, `GEMINI_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`, …),
`GITHUB_TOKEN`, and `ODDISH_DASHBOARD_URL`. See `backend/.env.example` for the
full surface and `backend/README.md` for details.

Slack link unfurls are a lean hosted-only integration configured through
`ODDISH_SLACK_UNFURL_*`. One manually installed Slack workspace is bound to one
Oddish org; the Slack app needs `links:read` and `links:write`, subscribes to
`link_shared`, and sends signed events to `POST /webhooks/slack/events`.
Optional team and channel allowlists provide defense in depth. This integration
is separate from the scheduled expense-notification webhook.

Hosted API containers keep a conservative warm SQLAlchemy pool by default so
Modal bursts do not overrun shared Postgres poolers. The engine still disables
prepared statement caching so it remains compatible with transaction-mode
poolers such as Supavisor / PgBouncer.

Modal runtime knobs (scaling, schedules, CPU/memory, concurrency) are read
directly by `backend/modal_app.py` from `ODDISH_MODAL_*` /
`ODDISH_DEFAULT_MODEL_CONCURRENCY` / `ODDISH_MODEL_CONCURRENCY_OVERRIDES` /
`ODDISH_ENABLE_SLACK_EXPENSE_NOTIFICATIONS` / `MODAL_APP_NAME` /
`MODAL_SECRET_ENVIRONMENT` env vars. `modal_app.py` is the
source of truth for the full list and defaults (e.g.
`ODDISH_MODAL_MAX_WORKERS_PER_POLL=256`,
`ODDISH_MODAL_WORKER_MAX_CONTAINERS=2688`).

### GKE Placement Contract

The pinned Harbor (harbor-gke `6ec8e946`+) requires explicit placement for
every GKE TPU trial — there is no default provisioning mode, no table-derived
zone pool, and `accelerator_region_prefixes` is inert. A trial missing any
required field fails at environment construction with an error naming the
field and the served zones; it does not sit in a scheduling wait.

Required fields and where they come from:

- **`provisioning_mode`** (`on-demand` | `spot` | `flex-start`) — from the
  task's `[environment.kwargs]` or a per-submission
  `--environment-kwarg provisioning_mode=...`. A deployment MAY force a
  fleet-wide mode by setting `ODDISH_GKE_PROVISIONING_MODE`; the backend
  ships it only when configured (unset means "not configured", and each
  task/submission states its own). Deployment-shipped kwargs override the
  task's in Harbor's merge — a deployment that forces a mode overrides every
  task's choice, which is why previews deliberately leave it unset.
- **`[environment.tpu] zones`** — from the task only. Validated against the
  mode's served-zone table, scoped to the region. A single-zone pool is
  pinned through the node selector; multi-zone pools use affinity.
- **`region`** — from deployment coordinates (`ODDISH_GKE_REGION`) or a
  per-submission kwarg override.

Migration: GKE tasks written before this contract (no mode, no zones) stop
scheduling and fail with the requiredness error until updated.

### Preview Branch Preserved Rows

Each preview branch database holds a schema named `preview_preserved` with one
table, `rows`. It keeps the API keys that a person creates from that preview
dashboard, and the `organizations` and `users` rows those keys need.

**Why it exists.** A preview rebuild runs `DROP SCHEMA public CASCADE` and
restores a schema-only snapshot of production, which holds no rows. Without
this stash, every rebuild removes the keys.

**Why it sits outside `public`.** The rebuild drops `public` and nothing else,
so the stash survives. It also holds the rows in the database rather than in
the workflow process: the preview workflow uses `cancel-in-progress`, so a
second push can stop a run between the drop and the write-back.

**Lifecycle.** `bootstrap_preview_db.py` writes to the stash before the drop
and reads from it after `upgrade head`. The stash is not cleared after a
successful write-back, so a cancelled run stays recoverable. Rows are jsonb,
and `jsonb_populate_record` maps each one onto the current row type.

**Do not copy it between environments.** An API key is a credential for one
environment only. The seed must never sample `api_keys` from production or
from another branch; `_NEVER_SAMPLED_TABLES` in `backend/preview_seed.py`
enforces that, and `_assert_no_forbidden_tables` fails the seed if a later
change breaks the rule. Cloning a preview branch, or copying this schema
between branches, would put one environment's credentials in another.

**Do not delete it while diagnosing a branch.** It looks unused, and removing
it destroys the keys of everyone using that preview. A branch that is deleted
and made again loses the stash with the rest of the database; that is expected,
and a new key is then required.

### Database Migrations

Two migration stacks are required:

```bash
# Core tables (run in oddish/)
uv run alembic upgrade head

# Cloud tables/extensions (run in backend/)
uv run alembic upgrade head
```

In hosted environments both stacks run in that order *before* the code deploy,
because the backend can hard-require new schema on its hot paths.
`.github/workflows/staging-deploy.yml` sequences migrations then the Modal
deploy; `modal-deploy.yml` (production) additionally orders the Vercel frontend
after the backend, so a new frontend never reaches an old backend.

### Key Files

| Path | Purpose |
|------|---------|
| `deploy.py` | Modal app entrypoint |
| `modal_app.py` | Modal image, volumes, shared runtime, env knobs |
| `endpoints.py` | Modal ASGI app function |
| `serve.py` | Railway/uvicorn entrypoint |
| `slack_notifications.py` | Deterministic scheduled experiment/trial expense alerts |
| `cloud_policy.py` | Hosted-only environment policy |
| `api/app.py` | FastAPI app factory |
| `api/routers/tasks.py` | Task upload, browse, sweep, sharing, retries, deletion |
| `api/routers/trials.py` | Trial logs, result, trajectory, retries, deletion |
| `api/routers/dashboard.py` | Cached aggregate dashboard endpoint |
| `api/routers/admin.py` | Auth wrapper over `oddish.core.admin` (slots, queue status, orphaned state, worker_jobs) |
| `api/routers/slack.py` | Signed Slack Events API endpoint for link unfurls |
| `api/services/slack_unfurls.py` | Task/experiment summary queries and Slack block construction |
| `auth/__init__.py` | Header parsing, `get_auth_context`, permission dependencies |
| `auth/verification.py` | API key + Clerk JWT verification |
| `worker/functions.py` | Modal dispatcher (`poll_queue`), reconciler (`reconcile_queue_state`), and kind-agnostic single-job runner |
| `worker/runtime.py` | Modal runtime patching and storage setup |
| `worker/github.py` | GitHub notification hooks used as post-success actions |

Every hosted HTTP response carries a fixed backend `Server-Timing` phase set:
`auth_verify`, `auth_cache`, `auth_total`, `db_checkout`, `db_sql`,
`external_http`, `db_commit`, `handler_db`, `handler_total`, and
`backend_total`. Missing work is represented as zero rather than omitting the
phase, so cold, warm, and concurrent traces are comparable. The
`backend.request.phases` span records per-request SQL counts and transmitted
response-body bytes. `backend_total` and `handler_total` stop at response start
because they ship in the response headers; the trace-only
`backend_complete.duration_ms` observation ends after the final ASGI body chunk
and includes streaming time, but not response background tasks. Production
entrypoints must use `create_asgi_app()` so timing wraps FastAPI's complete
middleware stack, including unhandled-error and capacity responses. Hosted and
core code must use `RequestTimedAsyncClient` for outbound HTTPX calls so the
request-wide `external_http` phase cannot depend on route-local wrappers. Never
attach response bodies, request payloads, credentials, or SQL parameter values.

---

## `frontend/` — Next.js Dashboard

The frontend is a Next.js 16 / React 19 App Router app. Browser code calls
`src/app/api/*` route handlers, which forward to the backend from
`NEXT_PUBLIC_API_URL` and preserve auth. Public routes are `/`, `/share/*`,
`/datasets/*`, `/api/public/*`, `/sign-in`, `/sign-up`, `/api/client-traces`,
and — deliberately, for link-unfurl bots — `/experiments/*`; everything else
is Clerk-protected.

Authenticated proxy routes forward incoming `traceparent`, `tracestate`, and
`baggage` headers to the backend and join the backend's `Server-Timing` value
onto the Next response on success, upstream error, and streamed passthrough
responses. Keep this behavior in `frontend/src/lib/proxy-headers.ts`; the
generic JSON proxy requires its incoming request, and bespoke hot routes must
use the same helpers instead of replacing an existing timing value.

The trial drawer surfaces verifier test counts only as a small passed/total
row in the Summary tab (shown on public share views too); trials without test
counts show no row. Persisted `_verifier` CTRF counts are the sole source.
Historical trials without that summary show no count; opening a trial must not
list or read its artifacts to reconstruct one.

On an experiment page, removing a task always calls the scoped
`DELETE /experiments/{experiment_id}/tasks/{task_id}` proxy. It unlinks that
experiment membership and its scoped trials without deleting the task, even
when it was the task's final experiment membership. Whole-task deletion remains
a separate explicit action outside the experiment-scoped table.

See `frontend/README.md` for route groups, scripts, env vars, and deployment
commands. See `SELF_HOSTING.md` for full-stack local development and production
deployment.

---

## Troubleshooting

### API does not start

```bash
uv run python -m oddish.db setup
curl http://localhost:8000/health
```

### Pulling from a remote API fails

- Verify `ODDISH_API_URL` and `ODDISH_API_KEY`.
- Try `oddish status` first to confirm auth and connectivity.

### Frontend "Failed to fetch" or disconnected backend

```bash
curl ${NEXT_PUBLIC_API_URL:-http://localhost:8000}/openapi.json
```

### Clerk auth issues

- Verify Clerk keys in `frontend/.env.local`.
- If org-scoped backend access fails, confirm `CLERK_JWT_TEMPLATE` is set and includes `org_id`.
- If using production Clerk keys locally, use `frontend/run-prod-clerk-local.sh`.
