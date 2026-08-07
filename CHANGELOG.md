# Changelog

All notable changes to Oddish are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [2026-08-07]

### Changed

- Every analysis job now runs as a trial. QA (classification + trajectory
  summaries + verdict) is one `kind='qa'` trial per task, created when the
  task's agent trials settle; the pre-trial audit is a `kind='audit'` trial
  created once per task version at sweep time; analyzer reports run as
  `analyzer_map` / `analyzer_reduce` trials on a generated host task. Each
  writes one JSON artifact that a settlement importer parses into the same
  columns as before (`trials.analysis`, `trials.trajectory_summary`,
  `tasks.verdict`, `task_versions.pre_trial`, the `analyzers` row), so the
  dashboards, GitHub comments, and alerts read unchanged data.
- Non-'agent' trial kinds are excluded from quotas, the leaderboard, Slack
  expense alerts, browse facets, dashboard queue scans, and every public
  share surface. Analysis spend still lands on trial rows and stays visible
  in admin cost views.

### Removed

- The block framework (`oddish/blocks/`), the worker-local classifier and
  verdict synthesis (with its provider-fallback machinery), the pre-trial
  synth, the trajectory-summary generator, the Daytona sandbox runtime in
  `backend/api/services/sandbox/`, and the QA/ANALYZER/ANALYSIS worker-job
  handlers. Workers no longer execute LLM calls (the probe transcript
  summarizer is the one remaining direct API call). The QA orphan-recovery
  subsystem is replaced by trial retries plus the VERDICT_PENDING healer.
  `GET /trials/{id}/trajectory/summary` now serves the stored summary only.
- Settings `ODDISH_VERDICT_MODEL`, `ODDISH_VERDICT_FALLBACK_MODEL`,
  `ODDISH_PRE_TRIAL_MODEL`, `ODDISH_PRE_TRIAL_TIMEOUT`,
  `ODDISH_AGENT_DAYTONA_SNAPSHOT`, `ODDISH_ANALYZER_SANDBOX_ENABLED`, and
  `ODDISH_POST_TRIAL_SANDBOX_ENABLED`. The `analyzer_blocks` and
  `analysis_costs` tables stop receiving writes and remain as history.

---

## [2026-08-07]

### Removed

- The cc_chat dashboard chat feature is gone end to end: the `/chat-sessions`
  backend router and orchestrator, the chat drawer/button UI and its
  `/api/chat-sessions` proxies in the frontend, the `ChatSession` /
  `ChatSessionEvent` / `ChatTurn` models, and the chat tables themselves
  (dropped by backend migration `dropchat001`; `api_keys.is_internal` stays —
  internal key minting also serves probe credentials and the sandbox
  analyzer). The chat-only settings `ODDISH_CC_CHAT_DAYTONA_SNAPSHOT` and
  `ODDISH_PUBLIC_API_BASE_URL` are removed with it. ⚠️ Deployments that set
  only `ODDISH_CC_CHAT_DAYTONA_SNAPSHOT` must now set
  `ODDISH_AGENT_DAYTONA_SNAPSHOT` (same snapshot name) or analyzer sandboxes
  fall back to installing claude-code + harbor at provision time.
- The shared sandbox infrastructure the chat feature grew — Daytona client,
  provisioner, Claude Code runtime, stream renderer — survives because the
  hosted analyzer runs on it; it moved from `backend/api/services/cc_chat/`
  to `backend/api/services/sandbox/`, and the analyzer cohort modules
  (`analyzer_block_runner`, `analyzer_parse`, `analyzer_prompt`) moved to
  `backend/api/services/blocks/analyzer/`.

---

## [2026-08-05]

### Added

- `opencode` trials can now run on closed-internet tasks. Stock opencode self-installs (nvm/Node/`opencode-ai`) during agent SETUP, which runs under the ENVIRONMENT baseline network policy — the agent-phase allowlist (`extra_allowed_hosts`, runtime-host merges) only applies around `agent.run()`, so no agent-phase declaration can save a self-installing agent: the trial died at DNS during setup (`curl: (6) Could not resolve host: raw.githubusercontent.com`) before the model was ever reached. The fix mirrors the existing claude-code installer arm in `run_harbor_trial_async`: `-a opencode` now merges `OPENCODE_INSTALL_HOSTS` plus the model transport host (via `outbound_hosts_for_model`, which resolves `openrouter/tencent/hy3` → `openrouter.ai`) into `env_config.extra_allowed_hosts`, which harbor folds into the environment baseline so the allowlist spans install *and* run. On legacy closed tasks (`[environment] allow_internet=false` → no-network baseline for every phase, e.g. the GDM SWE-Marathon samples) this is the only channel that works at all; on modern swe-marathon-shaped tasks (public setup → restricted agent) harbor ignores baseline extras on the public baseline and the agent phase keeps its model-host-only allowlist, so no install hosts leak into agent run there. `_build_agent_config` still routes `-a opencode` through the `OddishOpenCode` wrapper. Note: `required_outbound_domains` — the hook two earlier revisions of this change relied on, and which several wrapper docstrings describe as "Harbor builds the Modal egress allowlist from this hook" — has **no consumer** in oddish or harbor; it is kept declarative-only for interface parity (both failed approaches were validated end-to-end on the PR preview backend before landing on this one).

---

## [2026-07-27]

### Changed

- Enforced quotas now cancel every quota-counted nonterminal trial as soon as live or settled spend reaches the payer's rolling 24-hour cap or the organization's monthly cap. User caps stop that payer's trials; org caps stop all trials in the org, including queued and retrying work, and remote workers are terminated after the cancellation transaction commits.

---

## [2026-07-20]

### Changed

- The task-level QA worker job now leases concurrency from the **analysis model's** queue key (`get_qa_queue_key()` returns `normalize_queue_key(analysis_model)`, currently `anthropic/claude-sonnet-5`) instead of the verdict model's. The bulk of a QA job's LLM work is the per-trial classification pass on the analysis model; keying the lease off the verdict model capped QA throughput at the verdict bucket's default (48) while the analysis bucket sat idle. ANALYZER jobs share the QA queue key and move with it (#802).
- Raise the baked `anthropic/claude-sonnet-5` queue-key concurrency override in the Modal deploy from 128 to 256, giving the relocated QA jobs and the analysis model's trials more headroom; operators can still override the whole JSON via the env var / `oddish-prod` secret (#802).

---

## [2026-07-18]

### Changed

- The shared analysis model (`ODDISH_ANALYSIS_MODEL` — trajectory graph, trajectory summary, trial classifier, probe analysis) now defaults to Claude Sonnet 5 as the plain Anthropic-style id `claude-sonnet-5`, replacing the Bedrock inference-profile id `global.anthropic.claude-sonnet-4-6`. Plain Claude ids route analysis calls to the direct Anthropic API, and the analysis queue key changes accordingly to `anthropic/claude-sonnet-5` (#794).
- Bake a per-model `ODDISH_MODEL_CONCURRENCY_OVERRIDES` default into the Modal deploy that raises the `anthropic/claude-sonnet-5` queue-key concurrency lease to 128 (up from the 48 default), giving the relocated analysis model the same headroom its predecessor queue key had; operators can still override the whole JSON via the env var / `oddish-prod` secret (#795).
- Bake a per-model `ODDISH_MODEL_CONCURRENCY_OVERRIDES` default into the Modal deploy that raises the `global.anthropic.claude-sonnet-4-6` queue-key concurrency lease to 128 (up from the 48 default) — the queue key every Sonnet 4.6 trial id spelling normalizes to; operators can still override the whole JSON via the env var / `oddish-prod` secret (#796).

### Fixed

- Dashboard queue stats no longer fold the trajectory-analysis and verdict pipeline counts into the analysis/verdict *model*'s queue bucket. They now live under reserved `analysis` / `verdict` queue keys, so trials awaiting or undergoing classification can no longer masquerade as that model's queued/running trial workers (an incident showed 4k+ phantom "running" rows under one model's queue while the model's real trials were misrouted into the "analyses" pipeline). The reserved buckets report the QA job bucket's concurrency instead of a meaningless per-model default.
- A QA job that dies or is cancelled mid-classification no longer strands trials in a non-terminal `analysis_status`. The stale-heartbeat reap now resets the dead job's task trials inline (RETRYING → `QUEUED`, exhausted → `FAILED`), the append-supersede cancel requeues in-flight rows, and a new `_reset_orphaned_trial_analysis` cleanup phase heals any remaining orphans: never-classifiable rows (superseded / skipped / gate-skipped / bulk-imported trials, soft-deleted tasks, or terminal tasks with no active QA job) are finalized `FAILED`, while rows a future QA attempt will re-classify are moved back to `QUEUED`. Previously these accumulated forever as phantom in-flight analyses. Orphan-finalized rows carry an `Analysis orphaned:` sentinel prefix on `analysis_error`, and resurrecting a task by appending trials reopens them so the fresh QA pass classifies them instead of inheriting a permanent verdict gap. Every reset selects its trial rows `FOR UPDATE SKIP LOCKED` so the sweep can never deadlock against the trials-then-task lock order the cancel path takes.

---

## [2026-07-16]

### Fixed

- A grok trial killed by an xAI rate limit (`You've hit your team's API rate limit`) is no longer thrown away mid-run: the resume loop that already rescues idle-timeout deaths now also resumes rate-limited ones, sleeping first with a doubling backoff (60s, 120s, 240s) before each replay. The case was previously excluded on purpose, since an immediate `grok -c` re-hits the same wall — the throttle is on the account, not on one replica — but xAI's limits are refilling token buckets, so a resume that waits often lands, and a limit that never clears just fails as it did before. Idle timeouts still resume with no delay. Observed on a trial that spent 19 minutes and 437k tokens, announced its next step, and died to the limit; the truncated trajectory was then graded as a model failure rather than an infra one (#758).

---

## [2026-07-15]

### Added

- Analyzers: a new cross-experiment trajectory-analysis feature that gathers finished trials from one or more experiments and synthesizes four evidence-backed narrative sections (bad failures, good failures, universal capabilities, headroom) via a Haiku agent-team map/reduce pipeline, with inline `[trajectory](...)` deep links, new `analyzers` REST endpoints, an `oddish analyzer create` CLI, and a dashboard Analyzers tab with list/create/detail pages (#706). Analyzer pipeline logs are now prefixed with the driving job's kind (e.g. `[ANALYZER]`) for easier attribution in mixed worker logs (#710), and the reduce-stage prompt that produced an analyzer's sections is now persisted on the row for debugging/reproducibility, though not exposed via the API (#711).
- Task pages can now promote any stored task version to be the default: a new `PUT /tasks/{task_id}/versions/{version}/default` endpoint updates the task's current-version pointer (and legacy storage mirrors), and the task page gained a "Make default" action with an optimistic update and inline error handling (#713).
- Owner-directed expense alerts (expensive experiment/trial) and a new failed-experiment alert (fires when a finished experiment has no active trials and at least half — configurable — of its current trials are FAILED) can now be DMed to the owner on Slack via `SLACK_ALERT_BOT_TOKEN`, matched to their Slack account by account email, alongside the existing webhook and email channels (#703).
- Oddish task, experiment, and public-share links posted in a configured Slack workspace now unfurl with outcome glyphs, run details, and a compact task-by-agent result matrix for smaller experiments, via a new signed `POST /webhooks/slack/events` endpoint bound to one workspace/org (#700).
- Trial drawer gained an adaptive Verifier Results card: test-based tasks report Common Test Report Format (CTRF) passed/failed/skipped/pending counts, benchmark-style tasks show scalar metrics, and other tasks fall back to the reward score; historical trials without a persisted summary lazily discover and parse their `verifier/ctrf.json` artifact (#699).

### Changed

- Markdown-rendered hyperlinks (analyzer reports, probe summaries, and other markdown content) now render in a theme-aware blue instead of a hard-to-recognize brown/off-white, so they read as clickable links (#712).
- The trial Live tab now shares its step, tool-call, and observation rendering with the Trajectory tab, grouping streamed events into collapsible per-turn steps where the newest step auto-expands as the previous one collapses; Claude live-tail events now carry a `turn_id`/`block_index`/`text_mode` so streamed text deltas merge into one step instead of duplicating (#675).

### Fixed

- Analyzer reports no longer wait behind the QA backlog they share a queue with: `ANALYZER` and `QA` jobs both land on the QA queue key, and the claim orders by `priority DESC, running_count ASC, created_at ASC` — but every enqueue site left `priority` at 0, so the first two keys tied and claims fell through to pure FIFO, stranding an analyzer behind whatever QA burst a sweep had just produced (one report waited ~59 minutes to start). Analyzer jobs now enqueue at `priority=1`, so a draining worker picks them up ahead of that backlog (#744).
- Non-Meta `mini-swe-agent` trials (e.g. Claude, GPT models) no longer crash deterministically on their first model call with `ModuleNotFoundError: orjson` — the `litellm[proxy]` reinstall that previously only applied to Meta-model trials now applies to all mini-swe-agent trials via a shared `OddishMiniSweAgent` base class (#714).
- Experiment cost rollups now attribute spend only to a trial's home experiment (`trials.experiment_id`), so collection/rollup views that render trials gathered from other experiments no longer double-count that spend on both the collection and the trial's owning experiment (#702).
- Pricing lookups now walk a specificity-ordered, case-insensitive chain of model-id candidates (exact id, path suffixes, provider vocabulary aliases, spelling variants, then generic provider prefixes) instead of one-off guesses, resolving previously-unpriced production model ids while preserving provider-specific rates; token-bearing trials that still settle to an unpriced `NULL` cost now emit a structured `trial_cost_unpriced` warning to logs and Logfire (#660).

---

## [2026-07-07]

### Added

- `oddish preflight <path>` checks local tasks for integrity problems before
  they cost a trial: solution/tests baked into the agent image, repo fetches or
  `.git` directories that expose branch history, unjustified open internet,
  patch-file solutions, and brittle source-scanning anti-cheat. `--json` emits
  findings for CI.
- `oddish run` now runs preflight before upload. `--force` submits anyway and
  still prints the findings. `oddish upload` is gated the same way (same
  `--force` override) — closing a two-step bypass where uploading a leaky task
  directly, then running it by ID, skipped preflight entirely.

### Changed

- `oddish run` aborts when **any** resolved task fails preflight. Previously a
  broken task in a multi-task run was reported and silently skipped while the
  rest proceeded. Use `--force` for the old behaviour.
- API key creation is now self-service for every organization, gated on the caller's role in their current org instead of membership in the hardcoded Abundant org. `can_create_api_keys` no longer checks an org-slug/Clerk-org allowlist — any `admin` or `member` (Clerk-JWT auth only) may create keys for their own org, admins minting `full`/`tasks`/`read` and members minting `tasks`/`read`. API-key auth still cannot mint keys, and listing/revoking all org keys stays admin-only. Removed `API_KEY_CREATOR_ORG_SLUGS` / `API_KEY_CREATOR_CLERK_ORG_IDS` and refreshed the stale `@abundant.ai`/Abundant-org wording in the settings UI, endpoint errors, and docs (#617).

### Fixed

- Admin cost dashboard "Cost by user" rows now link to the per-user drilldown whenever the row resolves to a **real oddish user** (billed user or submitting credential), even when some or all of its trials are unbilled — previously a row was only clickable when *every* trial was billed, so real users with pre-billing spend (e.g. created before quota billing stamping shipped) or offboarded/unlinked spend were shown as non-clickable "unbilled". A new `CostUserBreakdown.has_unbilled_spend` field drives an "unbilled" chip: on a linkable (registered-user) row the tooltip explains the drilldown counts billed spend only, so its total may be less than the row total; GitHub-handle-only / Unattributed rows that are not registered users stay non-clickable with the existing "not a registered user" wording.

---

## [2026-07-06]

### Added

- Org-wide aggregate **calendar-month (UTC)** spend cap, layered on top of the per-user rolling-24h cap. Admission now sums every payer's settled org spend (including unattributed NULL-billed spend) plus the org's in-flight reservation and blocks when it reaches the effective org limit (override row `org_quotas` ?? `ODDISH_DEFAULT_ORG_MONTHLY_QUOTA_USD` ?? none); over-cap submissions get HTTP 402 under `enforce` and log `reason=org_over_budget` under `shadow`. Advisory-lock order is org → payer → row locks (ENFORCE-only, org lock only when a cap is configured). Admins set/clear the cap via `PUT /quotas/org` and see month-to-date org usage on `GET /quotas`; any member reads the org budget snapshot plus an adaptive daily-goal via the new `GET /quotas/org`. Ships inert (no default, no override rows). oddish core reads `org_quotas` via raw `text()` SQL to preserve the oddish→backend package boundary; per-user rolling-24h behavior is unchanged.

### Changed

- Per-user quotas now use a rolling 24-hour window instead of a UTC-midnight reset. `quota_window_start()` replaces `start_of_today_utc()` in admission and quota usage reads, while `ODDISH_DEFAULT_DAILY_QUOTA_USD` keeps its existing name and value.

---

## [2026-07-02]

### Added

- Trial-collection experiments: `oddish experiment create --name "..." <trial_id...>` and `POST /experiments/collections` (TASKS scope) gather existing trials into a new read-only `is_collection` experiment via `create_trial_collection_core`, without moving trials out of their home experiment. Membership is additive through a new `experiment_trials` join table (plus `task_experiments` for parent tasks); read paths — dashboard aggregates, task listing/effective-version resolution (including older task versions), export, and public/share views — treat membership as home `experiment_id` OR a gathered row via shared `experiment_membership` helpers, with probes still excluded from every public surface. New runs and sweep-appends targeting a collection are rejected. (First landed in #536, reverted, relanded in #552, reverted again after a prod migration deadlock, and relanded unchanged in #556 with a deadlock-safe `exp_trials_join_001` migration — each DDL step now runs in its own `autocommit_block`, FKs are added `NOT VALID` then `VALIDATE CONSTRAINT`, and a `lock_timeout` guards against lock-order conflicts with hot-table DML.) (#536, #552, #556)
- Task browser overhaul: `browse_tasks_core` / `GET /tasks/browse` accept a large set of new filters — task metadata (status, priority, verdict, has-link, run-analysis/probe, created-date presets/custom range, experiments), trial-level `EXISTS` filters (agent, model, agent·model pair, provider, environment, trial status, origin, analysis classification, has-error, has-trajectory, attempts, tokens/steps/reward ranges), on-the-fly aggregate filters/sort (avg score, total tokens, trial counts, pass/partial/fail/harness buckets, run time), agent/model "Compare A vs B" and "top performer" comparisons, and an `or_groups` DNF combinator for "match any of" condition groups. A new `GET /tasks/browse/facets` endpoint supplies sidebar option values. The frontend tasks page moves from client-side SWR to URL-driven, server-rendered filtering with a sticky filter sidebar (draft-then-Apply for heavy fields), Suspense/skeleton loading, link-based pagination, and whole-URL (v2) saved filters. (#540)
- Migration Head Guard CI workflow: a new `.github/workflows/migration-head-guard.yml` runs `alembic heads` on the PR's merge ref for both the `oddish` and `backend` Alembic trees and fails if either would have more than one head after merging into main, catching stale `down_revision` forks (the kind that caused a prior `experiments.is_collection` prod incident) before they reach main. (#549)

### Changed

- Harbor artifact extraction is now centralized: a new `oddish.core.harbor_artifacts` module holds shared trajectory-metrics, timing, token, reward, and error extraction from Harbor `TrialResult`s; both the live worker outcome path (`workers/harbor/outcome.py`) and CLI/server trial import (`cli/api.py`) reuse it instead of duplicating logic. Sauron trial-directory discovery now uses Harbor's `JobScanner` (with legacy fallback preserved), zip-import task-name inference prefers Harbor config/result model parsing over nested legacy JSON discovery, and several edge cases were fixed along the way (worker reward propagation, multi-trial token precedence, invalid trajectory cost handling, non-numeric verifier reward handling). (#557)
- Task version content hashing (`compute_task_content_hash`) now hashes only execution-relevant task contents: it uses Harbor's `Packager.collect_files` publishable-file selection (respecting default ignores like `__pycache__`) and semantically parses `task.toml` via `TaskConfig`, so descriptive `[metadata]`/`[task]` edits no longer create a new Oddish task version — only changes to runtime fields (verifier, agent, environment, steps, etc.) or other files do. (#546)
- Dispatch planning is now shared across hosts: a new `build_dispatch_plan` / `DispatchPlan` in `oddish.dispatch.cycle` centralizes queue discovery, counts, held-slot accounting, concurrency limits, and spawn-plan calculation. Modal's `poll_queue` (`backend/worker/functions.py`) and the standalone self-hosted `run_polling_worker()` (now routed through `run_dispatch_loop` with a new `InProcessDispatcher`) both consume the shared planner, while assigned single-queue Docker/Kubernetes workers are unchanged. The shared dispatch loop also now catches and retries on transient failures instead of propagating them. (#545, #547)
- Backend router decomposition: hosted task-submission identity resolution, GitHub attribution, experiment-owner stamping, and auto-publish logic are extracted out of the large `backend/api/routers/tasks.py` into a new `task_submission.py` module; Claude Code's Anthropic-compatible environment setup in `workers/harbor/agent_config.py` is now shared across the OpenRouter, Fireworks, z.ai, MiniMax, and Moonshot routes instead of being duplicated per provider. (#554)
- Trial `environment` is now surfaced directly in trial responses: `TrialResponse.environment` (including the compact experiment-page response) exposes `trials.environment`, added to the compact eager-load `load_only` set in `list_tasks_core`; the trial detail drawer's sandbox badge and rerun command now prefer this field over worker-job metadata, so the badge renders instantly instead of waiting on `jobs` data (worker-job metadata remains the fallback for legacy rows). (#531)
- Locked `harbor` git dependency (`rishidesai/harbor@main`) advances from `aeadaf4b` to `2ae61e86`, with `HARBOR_DEFAULT_SHA` and both `uv.lock` files updated in lockstep. (#543)
- Probe detail panel: the trial ID on the probe run detail view now renders as a labeled "Trial: <id>" line (matching the existing "Preset:" styling) instead of small unlabeled muted text; the panel also no longer shows the red trial-error block when `error_message` contains "exit 137" (SIGKILL/OOM-style termination). (#532, #534)

### Fixed

- Off-Modal dispatch no longer over-spawns workers: `run_dispatch_cycle` now sizes its spawn plan against `max(running, held)` per `queue_key`, using a new `count_held_queue_slots` helper, instead of `worker_jobs` RUNNING counts alone. An event-triggered cycle re-firing before newly-spawned workers show as RUNNING previously over-planned, causing wasted Docker/Kubernetes container churn as the extras lost the `queue_slots` acquire race and exited; Modal's production `poll_queue` is unaffected since it doesn't pass held counts. (#539)
- An empty Alembic merge migration (`merge_mca01_wjtoken_heads`) unified two Alembic heads left by concurrently-merged migrations, which had blocked `alembic upgrade head` with "Multiple head revisions" and stalled the production Supabase DB migration deploy. (#538)
- Grok Build trials no longer fail deterministically on large instructions: `OddishGrokBuild.run()` now uploads the rendered task instruction into the sandbox as a file (`environment.upload_file`, `chmod 0644`) and reads it back via `grok -p "$(cat ...)"` instead of inlining it into the exec argv (embedded up to three times across CLI fallbacks); large instructions previously produced exec commands exceeding Modal's 65536-byte ARG_MAX limit and failed at agent start. (#535)

### Removed

- The hosted per-user probe auto-opt-in default is removed: the `users.run_probe_default` column and its submission-time hook are dropped, so `run_probe` is now only ever true when explicitly requested on a submission — no per-user server-side default can silently enable probe trials. (#553)

### Security

- The unauthenticated public-experiments list endpoint (`list_public_experiments`) no longer queries or returns any experiment rows — it always returns an empty list — so share tokens can no longer be discovered by enumeration; direct `/public/experiments/{public_token}` lookups for a token a caller already has continue to work unchanged. (#558)

---

## [2026-07-01]

### Added

- Grok Build trials are now converted into full ATIF trajectories: streamed `grok-build.json` events are parsed into reasoning/tool-call/tool-result/message steps with final metrics (tokens, cost) via a new agent wrapper, and the trajectory endpoint falls back to synthesizing ATIF from `agent/grok-build.json` for older trials that predate the conversion (#530)

### Fixed

- Completed Grok Build trials are now marked as having a fetchable trajectory even when the DB's `has_trajectory` flag is stale, so the dashboard no longer skips trajectory loading for them (#530)

---

## [2026-06-30]

### Added

- `oddish-query` CLI gains five probe-only commands: `solution cat`, `solution fetch`, `verifier source`, `harbor src`, and `verify run`; probe-only assets are now staged to a root-owned hidden directory (`/opt/oddish-probe`, via `ODDISH_PROBE_STAGE_DIR`) off the agent's browsable tree so the agent can't passively stumble on verifier/solution content; every command output is wrapped in a `PROBE-ONLY` boundary banner (carried in a `note` field for `verify run`'s JSON) so the boundary travels with the data through subagents; both local and cloud runners updated for identical container layout (#504)
- CI guard (`oddish/scripts/load_only_guard.py`) that statically diffs columns **read** on the compact `/tasks` response-builder path against columns declared in `load_only(...)` sets in `list_tasks_core`, failing the build on any gap; prevents the `MissingGreenlet` class of 500 from shipping silently; triggered on PRs touching `oddish/src/oddish/**` (#495)

### Changed

- Probe details now open in a sliding `ResizableDrawer` panel from both the experiment trials matrix and the task probe-history table instead of navigating to a full page; a new shared `ProbeDetailPanel` component handles self-fetching, SWR polling, on-demand artifact loading, prev/next navigation across a task's probes, and agent-process keyword filtering; the standalone `/tasks/[id]/probe/[trial_id]` URL continues to render a full page via `ProbeDetailPanel` in `contentOnly` mode so deep links are preserved (#505, #508)
- Task Analysis card on the task page now shows the **latest run of each distinct probe type** as a separate labeled section (probe-type header with `agent · model` sub-line) instead of collapsing all probe runs for a version to a single newest trial; polling continues while any per-type latest run is still in-flight (#512)
- Reverted experiment grid to use `GET /tasks?include_trials=true` instead of the short-lived `slim-tasks` endpoint; removes the dedicated `GET /experiments/{id}/slim-tasks` backend route, `GET /trials/{trial_id}` single-trial detail route, and associated Next.js proxies; also reverts the Usage page Cost tab, `CostingPanel` SSR component, cost CSV export, and the `series_top_n` admin costs parameter (#507)
- Bake a per-model `ODDISH_MODEL_CONCURRENCY_OVERRIDES` default into the Modal deploy that raises the `xai/redacted-model` queue-key concurrency lease to 128 (up from the 48 default), giving Harbor's grok-build arm more throughput; operators can still override the whole JSON via the env var / `oddish-prod` secret

### Security

- Probe trials are no longer returned by any public unauthenticated endpoint; `get_public_task` strips `is_probe` trials before returning the task, `list_public_experiment_tasks` excludes probes when scoping each task's trials (and now filters unconditionally regardless of experiment-id resolution), and `list_public_task_trials` always passes `probe=False` with the public `probe` query parameter removed — probe data never reaches the browser regardless of UI guards (#513)

---

## [2026-06-29]

### Added

- Probe instructions now include a **REFERENCE SOLUTION** section when the golden/oracle solution is staged, telling the probe it may copy or adapt it into `/app` as a baseline before pursuing the operator directive; omitted when no `solution/` directory was staged (#500)
- Probe instructions always include a **SUBAGENTS** section encouraging the probe to fan out parallel Task-tool subagents for independent investigation threads, noting the one-level nesting limit so all parallel work is dispatched directly (#500)
- Probe run page now shows a **Preset** line (between the run header and Summary section) displaying `harbor_config.probe_name`, falling back to the agent name for older or preset-less runs (#502)

### Changed

- Probe launch form "Instructions" field now populated from the selected skill's **SKILL.md body** (frontmatter stripped) instead of the legacy `operator_prompt`; skills without a SKILL.md file fall back to `operator_prompt` (#501)
- Probe launch form fields renamed: "Extra instructions" → **"Instructions"**, "Result focus (optional)" → **"Output JSON / Result Focus"** (#501)
- `extractSkillMdBody` extracted from `skills-client.tsx` into a new shared `frontend/src/lib/skill-md.ts` module, reused by both the skills editor and the probe form (#501)
- `CLAUDE_CODE_SUBAGENT_MODEL` is now pinned to the main agent's normalized model id for probe claude-code trials on both cloud (`agent_config.py`) and local (`local_runner.py`) paths, ensuring Task-tool subagents have an explicit model on the direct Anthropic API path where Harbor's `run()` does not set it; a pre-set value is never overridden (#500)

### Fixed

- Probe summaries using a `result_focus` JSON Schema with `oneOf` (e.g. from Pydantic v2 discriminated unions) no longer fail with `BadRequestError: Schema type 'oneOf' is not supported`; `normalize_findings_schema` now rewrites `oneOf` → `anyOf` at analysis time since the two are equivalent for constrained generation, transparently unblocking already-saved skills without operator action (#499)

---

## [2026-06-28]

### Added

- Batch probe-analysis backfill Modal script (`backend/scripts/backfill_analysis.py`) that accepts comma-separated task names, finds eligible probe trials with S3 artifacts whose analysis isn't `SUCCESS`, resets their analysis state, and re-runs the analyzer; dry-run by default, `--execute` to write; reports per-name match counts and trials skipped for having no S3 artifacts (#493)
- LLM-powered `result_focus` repair (`core/result_focus_repair.py`): malformed-but-JSON-intended `result_focus` values (trailing commas, single quotes, code fences) are coerced into valid JSON via a cheap Haiku pass before driving probe analysis or being stored on a skill; falls back gracefully on any failure, leaving the original value unchanged (#492)

### Changed

- Experiment owner and PR link are now stamped set-once on the experiment itself (new `experiments.owner` / `experiments.link` columns, backfilled from each experiment's earliest linked task) from the creating run's submitter; re-runs of shared tasks no longer overwrite the original experiment's provenance; dashboard and experiment detail view prefer experiment-level fields with task-derived fallback for un-backfilled rows; `taskPrUrl` now prefers `link` over `github_meta.pr_url` (#358)
- Probe directive fields (operator prompt, evaluation metric, result focus) removed from the skill upload/edit form as they duplicate configuration handled elsewhere; the form now only shows upload folder, name, description, SKILL.md body, and additional files (#497)

### Fixed

- Probe summaries using a structured-output `result_focus` schema no longer crash with `TypeError: unexpected keyword argument 'output_config'`; the field is now forwarded via the Anthropic SDK's `extra_body` escape hatch, compatible with the pinned `anthropic==0.76.0` (#493)

---

## [2026-06-27]

### Fixed

- Probe result cells in the experiment trials table now navigate to the probe's dedicated run page (`/tasks/{id}/probe/{trial_id}`) instead of opening the trial drawer; non-probe cells are unchanged (#490)

---

## [2026-06-26]

### Added

- Admin cost breakdown dashboard tab (`/admin` → Costs) with per-window totals (24h/7d/30d/all-time), cost-over-time chart stacked by model/user/agent dimensions, and ranked tables by user, model, and experiment; `GET /api/admin/costs` backend endpoint aggregates globally using native `cost_usd` when present and per-model token estimates otherwise; cost split between native and estimated spend is surfaced per entry (#452)
- Run Probe tab in the QA tab bar (`/qa/run`) with a full-page task search that replaces the old "+ New probe run" dialog on the Probe Runs page; typing filters tasks, clicking one navigates to `/tasks/{id}/probe` to configure and launch; default landing on `/qa` still shows Probe Runs (#478)

### Changed

- Probe presets and skills unified into a single Skills feature: `SkillModel` gains optional `operator_prompt`, `result_focus`, and `evaluation_metric` directive columns; probe form now selects a Skill (not a preset); skills mount **only** when explicitly selected at launch via `skill_ids` rather than auto-mounting into every probe; `probe_presets` table, router, and schemas fully removed; `/qa/presets` redirects to `/qa/skills`; existing presets migrated into skills (ids preserved); 13 built-in directive and bundle seed skills seeded on fresh databases; auto-probe default repointed to the `cheat-detector` seed skill (#477)
- Probe trials now render as a sorted-last "Probe" agent group inside the normal trials grid on both the task detail page and the experiment matrix, scoped to the task's effective version; the separate Probe tab is removed from the experiment view; backend batch-loads effective-version probe trials and merges them into each task's trials while keeping aggregate counts (total/completed/reward) probe-free (#471)
- Probe launch buttons (task detail header, experiment trials table icon, and experiment "New probe") now navigate directly to `/tasks/{id}/probe` instead of opening an inline modal; the "Submit a probe run" CTA interstitial on the probe page is removed so the form renders immediately (#474)
- Preview database schema bootstrap reverted to model-based creation (`Base.metadata.create_all` + `alembic stamp head`) instead of running the full Alembic migration chain on rebuild; migration fingerprint removed from schema trust marker (#469)

### Fixed

- Production incident: every `GET /tasks` (experiments page) 500-ing and all worker jobs failing with `InvalidRequestError: One or more mappers failed to initialize` — `OrganizationModel.api_keys` and `UserModel.api_keys` lost their join condition after #466 dropped DB-level FKs; fixed by adding explicit `primaryjoin` with `foreign()` annotation and `viewonly=True` on both relationships (#468)
- Trials with verification disabled (`verifier.disable: true`) that complete with `reward=None` no longer consume all retry attempts before failing; the worker now terminates them as `SUCCESS` on the first attempt; the UI shows a new `scoreless` status (slate "SCORELESS" badge, minus-circle icon) instead of treating them as perpetually pending (#462)

---

## [2026-06-25]

### Added

- `oddish backfill-analysis` CLI command to (re)run trial analysis (LLM trajectory classification + task verdict) for an experiment, a task, or a single trial; `POST /tasks/{task_id}/qa/backfill` backend endpoint on both cloud and local server with `force`, `enable_analysis`, and `trial_ids` options; `rerun_task_qa_core` refactored to delegate to the new `backfill_task_analysis_core` primitive (#456)
- "Open task page" link button in the experiment trials table for direct navigation from the experiment view to a task's dedicated page; hidden on read-only share view since `/tasks/[id]` requires authentication (#442)

### Changed

- Chat button across all scopes (global `/tasks` header, per-task detail, per-experiment header) switched from outline to solid blue fill (`bg-blue-600`) for improved visibility (#465)
- Preview database schema rebuilt by running `alembic upgrade head` for both stacks in order instead of `Base.metadata.create_all` + `alembic stamp head`; eliminates missing DB objects (e.g. `queue_runtime_status`, `tag_projection_sweep_state`, partial unique indexes) that only exist as raw DDL in migrations and are invisible to the ORM graph; production DROP SCHEMA guard re-introduced (#460)
- Preview schema trust marker now folds in a migration fingerprint (SHA256 of both stacks' Alembic head revisions) in addition to the model-graph fingerprint, so a cached schema is also invalidated when a migration is added without any ORM model change (#460, #440)
- Core package layout reorganized into focused subpackages: `oddish.core.tags` (service, projection, enqueue, filter_ast, naming, permissions, policies, profanity, ownership_transfer, saved_filters), `oddish.core.sharing` (public, helpers, documents), `oddish.core.ingest` (trial_imports, zip_imports, extraction), `oddish.core.probe` (auto_probe, presets); workers reorganized into `oddish.workers.agents` (claude_code, codex) and `oddish.workers.harbor` (runner, ephemeral, agent_config, outcome, storage, patches, modal_debug) (#438)

### Fixed

- `api_keys` cross-stack foreign keys dropped from `org_id` and `created_by_user_id` columns; new migration `apk01dropfk` drops constraints `IF EXISTS` so production converges; fixes `NoReferencedTableError` that prevented the oddish Alembic chain from bootstrapping independently (e.g. in the schema-parity CI job) (#466)
- Probe and offline trials now get `network_mode = "public"` (and `allowed_hosts` cleared) instead of the legacy `allow_internet = true` flag in `enable_local_internet`; fixes silent no-op on Harbor tasks that set `network_mode`/`allowed_hosts` explicitly, which caused claude-code installs to SYN-timeout (~127s, curl exit 28) and the oddish-query CLI to lose egress; applies to both local Docker and cloud Modal probe paths (#464)
- Chat session provisioning (`POST /chat-sessions`) no longer fails with "could not start chat" when the harbor pip install errors; harbor install is now best-effort (logs a warning and continues) since chat reads trial data through the oddish-query CLI, not the harbor package; claude-code install remains fatal (#458)
- `uq_worker_jobs_tag_project_active` partial unique index declared on `WorkerJobModel.__table_args__`; prevents silent omission from model-built schemas where `ON CONFLICT … DO NOTHING` index inference for TAG_PROJECT job coalescing had nothing to infer against; no new migration (index already exists in the DB via `aa00ta01core`) (#454)
- Preview bootstrap `_rebuild_schema` now calls `_assert_preview_branch` before `DROP SCHEMA`, refusing to proceed when `ODDISH_DATABASE_URL` resolves to production (matched via `SUPABASE_PROJECT_REF` or `PREVIEW_SAMPLE_SOURCE_DB_URL`) (#450)
- Cloud auth migrations `a1b2c3d4e5f6` and `r4s5t6u7v8w9` made replay-safe against schemas built from the current model graph: `supabase_user_id` index creation guarded on column existence; `userrole` enum rebuild guarded on `owner` still being an enum value; fixes `Supabase DB Migrations` workflow failing with `column "supabase_user_id" does not exist` (#443)
- Daytona dependency floor raised from `>=0.165.0` to `>=0.185.0`; fixes Daytona trials failing with a misleading `MissingExtraError` caused by Harbor importing `GpuType` from the daytona SDK, which was only added in version 0.185.0 (#439)

---

## [2026-06-24]

### Added

- Configurable per-run Harbor source via `--harbor <spec>` flag (or `ODDISH_HARBOR` env / `oddish.toml` `[harbor]` manifest): resolves the spec to a concrete commit SHA at submit time, stamps `trials.harbor_sha` and `worker_jobs.harbor_variant_id`, and executes the run on that exact Harbor version; blessed variants use digest-pinned worker images while arbitrary refs run in an ephemeral out-of-process engine (`uv run --no-project --with harbor@<sha>`); dispatcher routes on `(queue_key, harbor_variant_id)` so variants are isolated but share per-queue-key provider caps; Harbor commit shown in the trial detail drawer (#413)
- Trajectory viewer keyword search bar: filters steps by message text, reasoning, tool call names/arguments, and observations; step count shows "N of M steps" while a filter is active; clicking a hidden step in the timing bar clears the filter to reveal it (#425)
- `trials.total_steps` column (nullable integer) persists the total agent trajectory step count; extracted from `trajectory.json` at trial completion on cloud workers, the local runner, and CLI imports; surfaced in trial API responses and model usage aggregation (#396)

### Changed

- PR preview pipeline: backend stop step folded into prepare-database; Vercel and backend deploys decoupled via a deterministic Modal URL formula, enabling parallel execution; `preview_api_url` single-sourced as a workflow job output; `stop_previous_preview_backend.sh` removed (#430)
- Preview database bootstrap now rebuilds the `public` schema from the combined oddish+backend model graph (`Base.metadata.create_all`) when the schema is untrusted (no `oddish-preview:schema-built-from-base` namespace comment); trusted branches continue to run `alembic upgrade head` only; re-seeds when a rebuild drops data (#429, #430)
- Preview database schema is now upgraded to head on any backend deploy, not only when migration files change, so code-only PRs on reused branches cannot run against a stale schema; seeding remains gated to new branches and explicit migration runs (#424, #430)
- Preview seed sample sizes drastically reduced (random experiments 4000→8, trials per experiment 100→50, skills/documents/presets 200→10) to cut seed time; code-only pushes no longer trigger re-seeding of an already-populated branch; prepare-preview-database workflow job capped at 12 minutes (#419)

### Fixed

- Experiment page 500-ing with `MissingGreenlet` on the compact trials path: `harbor_sha` was not included in the `load_only` set in `list_tasks_core`, causing a deferred-column lazy-load outside the async greenlet; added alongside its sibling `harbor_config`; CLAUDE.md updated to document the trap for future columns (#433)
- Task cancellation database errors (e.g. deadlocks) now return HTTP 503 with a clear user message instead of propagating as an opaque 500 (`Internal Server Error`); full diagnostic detail (sqlstate, failing SQL, traceback) logged server-side; the Next.js cancel proxy now guards `JSON.parse` so a plain-text error body is forwarded correctly instead of surfacing a misleading parse exception (#421)
- New preview database branches no longer replay the full Alembic migration history against an already-at-head schema: the bootstrap script reads each stack's revision from the parent database and stamps the branch to that revision before running `upgrade head`, so only this PR's new migrations run (#417)

---

## [2026-06-23]

### Added

- Per-run container-registry credentials: any way a run is triggered (the `oddish run` CLI's `--registry-login` flag / `ODDISH_DOCKERHUB_*` env, the experiments-repo and harbor-forge CI workflows, a direct `POST /tasks/sweep` `registry_auth` field, or a trial retry request) can now supply its own Docker login so the trial sandbox's inner Docker-in-Docker daemon authenticates compose image pulls — fixing multi-service tasks failing at setup with Docker Hub `toomanyrequests`. The credential is per-user (never a shared Modal/oddish secret), Fernet-encrypted as it crosses the queue on `worker_jobs.payload` (never written to `trials.harbor_config`), passed to `docker login` before `compose build`/`up` via the Harbor DinD shim, then logged out on teardown. The encrypted ciphertext stays on the transient `worker_jobs` row while automatic retries remain possible and is scrubbed from the payload once the row reaches a terminal state.
- `GET /experiments/{id}/trials` read endpoint returns all non-superseded trials for an experiment, gated by READ scope with org-scoped access control; trial rows now include an `is_probe` flag (#414)
- Probe agent receives short-lived read-only API credentials and the `oddish-query` CLI on launch, enabling it to pull trial trajectories and logs on demand; a credentials-mint failure now fails the trial with a stored error rather than silently proceeding without access (#414)

### Changed

- `oddish-query` CLI ported from Python to a dependency-free Node.js script so it works in all claude-code environments where Node is guaranteed but `python3` is not; old Python CLI removed (#414)
- `APIKeyModel`, `APIKeyScope`, and key helpers relocated from `backend/models.py` into `oddish.db.models` and `oddish.core.api_keys` so workers can mint credentials; `backend/models.py` re-exports both for backwards compatibility (#414)
- cc_chat scope CLAUDE.md templates rewritten around the `oddish-query` CLI (`experiments trials`, `tasks trials`, `trials logs`); per-scope artifact file mounting (`experiment_files.py`, `task_files.py`, `file_loader.py`) removed in favour of query-on-demand access (#414)
- Probe trials are now forced to `allow_internet=true` to give the `oddish-query` CLI egress to the Oddish API (#414)
- Experiment probe-launch button in the trials table is now always visible instead of appearing only on row hover; `group/task-row` hover marker removed (#400)
- `backend/uv.lock` synced to record the missing `jsonschema` dependency edges under the `oddish` editable package, fixing dirty lockfile state on every `uv` invocation and `uv sync --frozen` compatibility in CI (#415)

### Fixed

- Codex trials on `openai/*` models (e.g. `openai/gpt-5.5`) no longer time out with zero tokens: `AzureCompatibleCodex` now implements `required_outbound_domains` to declare the configured Azure OpenAI endpoint host and per-trial `OPENAI_BASE_URL` alongside the OpenAI defaults, so Harbor's Modal egress firewall allowlists the Azure host and requests reach the model (#416)

---

## [2026-06-21]

### Changed

- Automated daily changelog updated with entries for 2026-06-20 changes (#409)

---

## [2026-06-20]

### Added
- `POST /tasks/sweep/batch` endpoint on cloud and standalone servers for submitting N task-sweeps in one request with per-item partial-success; each item runs inside its own savepoint so a failure in one item neither aborts the batch nor rolls back siblings; returns HTTP 207 Multi-Status when at least one item fails; CLI prefers the batch path and falls back to per-task only on 404/405 (#406)
- Adaptive AIMD in-flight limiter for `oddish run` task submission: replaces fixed one-at-a-time concurrency with an additive-increase/multiplicative-decrease controller clamped to [4, 16]; backs off on 429/5xx, request timeouts, slow pool checkouts, and EWMA latency overshoot; S3 presigned-PUT step uses a separate smaller bound ([1, 6]) to avoid polluting the API backpressure signal; configurable via `--submit-concurrency` flag or `ODDISH_TASK_UPLOAD_CONCURRENCY` env (#404)
- Submission idempotency for `POST /tasks/sweep`: CLI stamps a stable `Idempotency-Key` (canonical digest of experiment + task_id + sweep spec) on every sweep call; server deduplicates retried submissions and replays the stored response from a new `submission_idempotency` table (24h TTL, unique-insert-wins savepoint); same key + different body → 409 Conflict; prevents duplicate trials on transient 5xx retries (#399)
- Opt-in task-submission timing harness in `oddish/tests/perf/` measuring throughput (tasks/min), per-call latency (p50/p95/max per phase), 5xx count, and client-vs-server split; skipped in CI unless `ODDISH_PERF` and `ODDISH_API_URL` are both set (#391)

### Changed
- Batch sweep submission now chunks payloads client-side into groups of at most `ODDISH_SWEEP_BATCH_MAX_TASKS` (default 10, tunable via env) to stay under Modal's per-request ceiling; first-chunk 404/405 still falls back to per-task; later-chunk failures surface as an error to prevent double-submission after earlier chunks already committed (#407)
- Task tarballing in `upload_task` is now deferred until the server returns a presigned upload URL; dedup hits (content-hash match on `/tasks/upload/init`) skip archiving entirely, saving CPU on re-runs where the task content hasn't changed (#390)
- Sweep re-runs reconcile to exactly k trials per task in the target experiment: unchanged tasks (same version) count existing live trials and only add the shortfall; changed tasks (new version) still get a full k; the `--add` opt-out flag is removed — reconcile-to-N is now unconditional (#386)

### Fixed
- `/tasks/sweep` server-side DB round-trips cut: deduplicated browse-projection recompute in `create_task` (was running twice — once on incomplete pre-version state, once after trials); per-row `session.add` loops replaced with a single `INSERT … SELECT unnest(…) WITH ORDINALITY` statement for both trials and worker_jobs, keeping the statement shape constant under Supavisor transaction pooling (#397)
- Upload `init` and `complete` calls now retry on 429/500/502/503/504 with capped exponential backoff, full jitter, `Retry-After` header support, and a token-bucket retry budget (≤10% of requests); transient blips no longer abort a submission entirely (#397)
- `probe_presets_001` Alembic migration now adds `ratio_unit` and `ratio_verb` columns idempotently before the bulk seed insert, fixing `alembic upgrade head` failures on fresh databases where `000_initial_schema` creates `probe_presets` from current models that no longer carry those columns (#401)

---

## [2026-06-19]

### Added
- Experiment-level probes: submit a probe whose agent can read artifacts from all trials in an experiment (per-task-balanced, up to 30 trials); "New probe" button added to the experiment Probe tab in both empty and populated states (#372)
- Probe launch button in the experiment trials table (hover icon next to version badge) and task detail header (labeled "Launch probe"), both opening the probe submit form in a dialog (#385)
- DinD Docker daemon failure diagnostics: when `dockerd` fails to start in a Harbor DinD worker, its logs (tail 200), process state, memory, and disk are captured from the still-alive VM and folded into `exception.txt` to aid root-cause analysis (#361)

### Changed
- All claude-code trials now route to the direct Anthropic API instead of Bedrock (Bedrock credentials were unavailable); trial classifier also switched off Bedrock to the direct Anthropic API (#374, #377)
- claude-code model id now matches the transport: when Bedrock env signals are absent, `_build_agent_config` emits a plain Anthropic API model id (e.g. `claude-sonnet-4-6`) instead of a Bedrock inference-profile id (`global.anthropic.claude-sonnet-4-6`), preventing HTTP 400 "Operation not allowed" errors (#359)
- Probe result-focus redesigned: `result_focus` is now a JSON-schema structure with enforced `ResultFocusFindings` fields; action items lead instead of free-form text; ratio metric removed; full JSON summary accessible via toggle (#370, #378)
- Probe result display overhauled: cheating badges, "investigation steps," and "task is gameable" tally removed from the summary panel; summary row and latest-probe card now show action-items count (`no action items` / `N action items · M must-fix`) instead of cheat verdicts; `reward 0.0` fallback replaced with "awaiting analyzer" (#365, #379)
- Probe transcripts no longer clipped before summarization; probe analyzer upgraded to a larger model (#364)
- Auto-probe now defaults to the Task Construction Auditor preset (#376)
- Subscription auth route (`sub/<model>` prefix, OAuth token / Codex auth.json path) and the `claude-opus-4-8` sub-bucket concurrency bump reverted; related config symbols, tests, and BYOC credential infrastructure removed (#371)
- Modal worker container cap raised 768 → 2688; connection-budget comment updated to document the 2882/3000 worst-case client-connection estimate (#387)
- nop/oracle queue concurrency default raised 32 → 256 in both core config and the Modal deployment (#380)
- Harbor runner split into focused submodules (`harbor_agent_config.py`, `harbor_modal_debug.py`, `harbor_outcome.py`, `harbor_storage.py`); `harbor_runner.py` retained as an orchestration facade for backward-compat imports (#383)
- Project repositioned as "batch execution and continuous QA for Harbor-compatible RL environments" in the landing page hero copy, README tagline, site metadata, and `pyproject.toml` keywords (#388)
- API key creation permission check extended to recognize the Abundant production Clerk org ID (`org_39ufkEqie8rLlVhoK4YMm4IMx0L`) in addition to org slugs (#382)

### Fixed
- Cancel deadlocks with concurrent worker progress: `cancel_tasks_runs` now locks trial rows first then parent task rows (matching the worker domain-write order), and uses a `WITH … FOR UPDATE` CTE to lock matching `worker_jobs` rows before cancelling them (#393, #395)
- Unresolved Git LFS pointer files are now detected before archiving and uploading a task directory; `oddish run` fails fast with the affected relative paths and a `git lfs pull` remediation hint (#394)
- Probe summary crash on token-cap-truncated analyzer JSON (#375)
- Probe build error: second result-focus panel now rendered via `ResultFocusFindings` (#373)
- Experiment agent and model icons: agent-harness logos shown in experiment table column headers; model logos used for model rows and pass@k legend; `/` separator replaces `@` in model-scoped experiment labels (#381)

---

## [2026-06-18]

### Changed
- Official provider logo assets: Gemini, Kimi, and MiniMax now use official SVG assets (`google-gemini.svg`, `kimi-k-only.svg`, `minimax-vertical.svg`) instead of third-party icon library glyphs; a shared `ProviderLogoImage` component consolidates the rendering (#355)
- Harbor pin updated to `beabbb7a` picking up the agent-tools image update that bakes `ripgrep` into closed-internet Codex trials so runtime installs are skipped when `codex` and `rg` are already prebaked (#357)
- `sub/claude-opus-4-8` deploy-time concurrency raised 4 → 8 in `modal-deploy.yml`, roughly halving wall-clock for the claude-code eval arm running against the OAuth-multiplexed Claude Max subscription (#352)
- Task and trial ID columns widened: `tasks.id` VARCHAR(64→128), `task_versions.id` VARCHAR(128→160), `trials.id` VARCHAR(128→160), and all FK references widened correspondingly; Alembic migration `long_task_ids_001` handles the resize under `ACCESS EXCLUSIVE` lock with FK rebuild (#354)
- Org role model simplified to `admin` / `member`; the legacy `owner` role is removed (existing owners promoted to `admin` via migration `r4s5t6u7v8w9`) and API key creation is now gated to admins with an `@abundant.ai` email in the Abundant org; new `GET /api-keys/permissions` endpoint reports whether the current user may create keys (#170)
- CI preview pipeline now computes component diffs via the GitHub compare API instead of `dorny/paths-filter`, which ignored its `base` input on `pull_request` events and always diffed the whole PR, forcing unnecessary ~15-min Supabase DB seed runs on frontend-only pushes (#346)

### Fixed
- Experiment-scope chat sessions now mount the jobs artifact tree inside the Daytona sandbox; previously the `experiment` branch of `_resolve_scope_inputs` left `files` empty so the sandbox only received `CLAUDE.md` and the agent reported "(no trial data available yet)"; new `collect_experiment_files` mirrors `collect_task_version_files` and uploads artifacts at `jobs/{experiment_id}/{trial_id}/…` as the CLAUDE.md template promises (#347)

---

## [2026-06-17]

### Added
- Fireworks routing: GLM / MiniMax / Kimi (and other open models) can run on the stock `claude-code` agent via Fireworks' single Anthropic-compatible endpoint. Opt in with an explicit `fireworks/` (or `fw/`) prefix (e.g. `fireworks/glm-5.2`, `fireworks/minimax-m3`, `fireworks/kimi-k2.7-code`), which gets its own `fireworks/<id>` provider/queue bucket and authenticates with `${FIREWORKS_API_KEY}`; bare `glm/minimax/kimi` ids keep their existing direct-provider routes. Default claude-code settings (no forced thinking/effort)
- Global chat scope (`global`) with an `oddish-query` read-only CLI injected into the sandbox; the agent can search, inspect, and drill into trial logs across all org tasks; a short-lived internal API key is minted per-session (45-min TTL) for credential isolation; global-scope Chat button added to the tasks page (#332)

### Changed

- Chat sandbox provisioning now supports an optional pre-baked Daytona snapshot (`ODDISH_CC_CHAT_DAYTONA_SNAPSHOT`): `ClaudeCodeRuntime.install()` skips tools already present and installs any missing tools concurrently instead of sequentially, reducing provisioning from ~1 min to a few seconds when a snapshot is configured (#340)
- API container concurrency reduced from 8 → 3 and max containers raised 24 → 64 (peak throughput unchanged at 192); experiment-scoped `GET /tasks` now loads only the requested experiment's non-probe trials in SQL instead of fetching every trial for each task and filtering in Python; `GET /tasks` limit parameter capped at 2000 (#337)

### Fixed

- Chat session creation (`POST /chat-sessions`) returned 500 in prod because the Daytona region only permits ephemeral sandboxes; `create_sandbox` now passes `ephemeral=True` (#333, #339)
- Chat messages returned `session_not_found` when the API routed the request to a different autoscaled container than the one that provisioned the session; sandbox handles are now reconnected from the DB-persisted `sandbox_id` so any container can serve any session (#334, #339)
- First chat message showed nothing for ~10 seconds during sandbox provisioning and the composer stayed live, allowing a second send to race in; user bubble is now echoed and composer locked before `ensureSession()` runs (#335)
- Per-task Chat button showed "no trial data available yet" because it used the stub `task_probes` scope; button now uses the `task` scope with the full trial tree staged per version; probe trials are marked `(probe)` in the agent's `CLAUDE.md` with a "Regular runs vs probe runs" explanation (#336)
- `GET /chat-sessions` (chat history list) took 19–43 s under prod DB load; a composite index on `(org_id, scope_kind, scope_id, last_activity desc)` now serves the filter and sort directly, and turn counts are batched into one grouped query instead of a correlated subquery per row (#338)
- Claude Code ran headless (`--print`) without a permission flag, so every tool call (Bash, Read, etc.) blocked on an approval gate nothing could answer; `--permission-mode bypassPermissions` is now passed on both the chat (`stream_chat`) and probe (`run_once`) launch paths (#341)
- After ~30 min idle, Daytona auto-stops and deletes the ephemeral sandbox; the next message raised `session_not_found`; `send()` now transparently self-heals by calling `resume()` to re-provision the sandbox and restore from the per-turn archive (#342)
- Container startup sweep (`sweep_orphan_chat_sessions`) ran on every API container start and marked all active chats broken — since the API autoscales across many containers with no session affinity, any new container (autoscale-up or deploy rollout) was killing every live chat globally; startup sweep removed from lifespan; recovery is now lazy (reconnect by `sandbox_id`, self-heal via `resume()`, Daytona idle auto-stop as backstop) (#343)

---

## [2026-06-16]

### Added

- Claude Code chat sessions (Phase 1): durable `chat_session_events` append-only log and `chat_turns` table (one-running-turn-per-session enforced by a partial unique index) with a full orchestration engine — Daytona sandbox provisioner, Claude Code runtime, idle reaper, and restart sweep that marks orphaned running turns `failed` while preserving the event log; API routes `POST /chat-sessions`, `GET /chat-sessions/{id}`, SSE `POST /chat-sessions/{id}/messages`, events-replay `GET /chat-sessions/{id}/events?since=<seq>`, and `DELETE /chat-sessions/{id}`; sessions survive page refresh and backend container restarts (#306)
- Chat `task` scope (Phase 2a): chat sessions scoped to a task download trial log files from S3 and upload them into the Daytona sandbox as `jobs/v{version}/{trial_id}/…` (byte-capped at 50 MB); a version-aware `CLAUDE.md` highlights the current version as the default focus and de-emphasizes past versions (#307)
- Task detail page now shows a "Probe runs" card for the selected version: the latest probe run's agent/preset name, run status, cheat/blocked/neutral result, and prioritized action items from the analyzer; auto-polls while the probe is in-flight and links to the full probe result page (#310)
- Admin `GET /queue-health` endpoint and dashboard overview card exposing throughput, per-queue-key capacity fill, and persisted dispatcher/reconciler heartbeats so operators can self-diagnose "queued but not running" without querying psql or Modal logs; backed by a new `queue_runtime_status` table written at the end of each dispatcher/reconciler cycle (#312)

### Changed

- Probe submit page now shows a prominent "Submit a probe run" button that expands to reveal the agent picker and form on click (previously the form rendered inline on agent selection); probe history list sorted newest-first; task ID on the probe run detail page rendered as a clickable link to the experiment page (#313)
- Modal function CPU/memory resource floors now configurable via env vars (`ODDISH_MODAL_API_CPU`/`MEMORY_MB`, `ODDISH_MODAL_WORKER_CPU`/`MEMORY_MB`, `ODDISH_MODAL_DISPATCHER_CPU`/`MEMORY_MB`, `ODDISH_MODAL_RECONCILER_CPU`/`MEMORY_MB`); API defaults to 2 CPU / 4 GiB (was unconstrained fractional-core), reducing latency spikes under concurrent load; `WORKER_MAX_CONTAINERS` raised 320 → 448 (#312)
- Probe submit form converted to shadcn `Button`, `Input`, and `Select` components; unused frontend exports flagged by knip removed; pre-commit hooks (ruff, black, mypy, prettier) pass cleanly across the full repo; dead code removed: `TrialClassifier.classify_trials` batch method and `AUTO_PROBE_INSTRUCTIONS` constant (#311)

### Fixed

- Worker dispatcher no longer starved by a slow or deadlocking reconciliation sweep: `reconcile_queue_state` now runs as its own dedicated Modal scheduled function (240s interval, 600s timeout) instead of inline inside `poll_queue`; a SIGKILL mid-sweep previously left orphaned `idle in transaction` locks that deadlocked the next sweep cycle and spawned zero workers; `poll_queue` now only discovers queue keys and spawns workers, with `MAX_WORKERS_PER_POLL` raised 64 → 128 (#309)

---

## [2026-06-15]

### Added

- Copy button in the task file viewer header copies the raw file content to clipboard with a 2-second check-icon confirmation; resets on file switch and cleans up its timeout on unmount (#299)

### Changed

- Trajectory analysis is now a single task-level `QA` worker job instead of one classification job per trial plus a separate verdict job: when every trial of a `run_analysis` task finishes, one `QA` job classifies all live trials (unchanged taxonomy/evidence/reasoning, still written to `trials.analysis`) and then synthesizes the task verdict (`tasks.verdict`), so a sweep of `T` tasks × `N` trials enqueues `T` jobs instead of `T × (N + 1)`. The whole surface uses one "QA" concept: worker-job kind `QA` (migration `qa01`/`qa02` adds it and repoints old `VERDICT` rows; `ANALYSIS`/`VERDICT` remain only as legacy enum values for historical/in-flight rows), one `run_task_qa_job` handler, one set of endpoints (`POST /tasks/{id}/qa/retry`, `POST /tasks/{id}/qa/cancel`), one CLI surface (`oddish run --retry --qa`, `oddish cancel --qa`), and one dashboard control (Run QA / Cancel QA). The per-trial analysis and separate verdict retry/cancel endpoints, CLI flags (`--analysis`/`--verdict`), and UI buttons were removed (#315) (#315)
- Probe agent container now receives the full staged task directory via a Harbor `AGENT_START` hook, with all probe-only material (`tests/`, `solution/`, `harbor_src/`, `related_trials/`, `AGENT_BRIEF.md`) staged under `/probe-harness/` instead of `/app`, keeping the real agent's workspace pristine; probe instruction reframes the task spec as a "REAL AGENT BRIEF" with an auto-generated visibility map, eliminating false-positive vulnerability reports for files the real agent cannot access (#300, #301)
- Probe analyzer prompt gains a SCOPE section instructing it not to emit recommendations premised on probe-only paths (under `/probe-harness/`) being agent-reachable, and to preserve the probe agent's own hedges rather than upgrading them to `must_fix` (#301)
- Harbor bumped to `07a576944` picking up MiniMax M3 and Kimi K2.7 long-run hardening: streaming/timeout env vars (`API_TIMEOUT_MS=3.6M`, idle stream timeout, eager flush, max output tokens), Claude Code pinned to `2.1.167` (fixes MiniMax exit-137 mid-stream stalls), and plan-mode tools (`EnterPlanMode`, `ExitPlanMode`, `AskUserQuestion`) disabled for Kimi variants (fixes K2.7 plan-mode no-op bail) (#303)
- QA Probe Runs listing (`/qa/runs`) now aggregates in SQL using window functions — one row per task — rather than fetching all probe trial rows and folding them in Python; backed by a new partial index on `(org_id, task_id, created_at DESC) WHERE is_probe`, making load time scale with tasks-per-org rather than total probe trial count (#286)

---

## [2026-06-14]

### Added

- Advanced free-text grammar for the task browser search box: space-separated terms AND together in any order, `"quoted text"` matches as a contiguous phrase, a leading `-` (or uppercase `NOT`) excludes a term, and uppercase `OR` makes either side of a group match; a `?` icon inside the input opens a syntax cheatsheet tooltip; `parse_search_query` lives in `oddish/core/helpers.py` so the dashboard, standalone server, and cloud API all share one grammar; LIKE metacharacters remain escaped as literals (preserving #285 semantics); needles capped at 16 (#295)
- "Delete tag for everyone…" action in the tag chip editor: an inline confirm panel shows the tag's current name (refreshed after 409 races) and its direct-assignment count before the destructive click; sends `cascade=true` to flip ACTIVE assignments; `onDeleted` drops the chip locally without a redundant unassign call; `DELETE` passthrough added to the `/api/tags/[tag_id]` Next.js proxy route (#293)

### Changed

- Bake per-model `ODDISH_MODEL_CONCURRENCY_OVERRIDES` defaults into the Modal deploy that raise the `global.anthropic.claude-haiku-4-5-20251001-v1:0` (also the analysis-model queue key) and `openai/gpt-5.4-mini` queue-key concurrency leases to 128 (up from the 48 default); trajectory analysis gets more headroom; operators can still override the whole JSON via the env var / `oddish-prod` secret (#297)

### Fixed

- `DELETE /tags/{id}` backend route now accepts a `?cascade=` query parameter so callers can consent to flipping ACTIVE assignments to REMOVED; previously the flag was unreachable from HTTP and tag deletion always failed for any still-assigned tag (#293)

---

## [2026-06-13]

### Added

- Task page now lists all affiliated experiments as linked chips (dot-separated) instead of just the primary one; public share view only exposes public experiment names to prevent private experiment name leakage, and `GET /public/tasks/{id}?include_trials=false` no longer 500s for tasks in multiple public experiments (#288)
- Tag chips on dashboard experiment rows and `tag:` / `-tag:` / `OR` / `NOT` filter syntax in the experiments search box, matching the existing `/tasks` grammar; tag chips hydrated in a single batch query per page with graceful degradation on failure (#291)
- Admin "Tag Policy" tab now fully functional: numeric limits, who-can-create and profanity-mode toggles, comma-list editors for reserved prefixes and allow/deny lists, and a 403 → "Admins only" error state (#291)
- Probe summary now includes a prioritized "Action items" block with `must_fix` / `should_fix` / `optional` recommendations, color-coded and sorted by severity; an empty probe returns a "No fixes needed — task held up to probing" confirmation; legacy probe rows without the field render nothing (#284)
- Probe trials now upload the staged task directory (including `related_trials/`, `harbor_src/`, `tests/`, `solution/`) into the agent container at start time via a Harbor `AGENT_START` hook, so the agent can actually access the reward-hack surface the probe instruction references (#282)
- Skill create/edit form gains an "Upload folder" button that reads a `SKILL.md` plus supporting files and auto-fills the form; strips dotfiles and binary files; shows a "skipped N files" notice for anything dropped (#281)
- Saved-filter bookmark menu beside the tasks search bar: lists org-shared and private saved filters, applies one as `tag:` search text, and saves the current query under a name with Private/Org visibility; filters persist stable tag IDs so they survive renames and merges; deletes are optimistic with SWR rollback on failure (#280)

### Changed

- Dashboard "Avg score" column and experiment page KPI tile now use a task-weighted average (mean over tasks of per-task mean reward) with nop/oracle baselines excluded everywhere; backend computes and returns this as a new `avg_score` field; a loading spinner is shown on the KPI tile while trial pages are still streaming in; both surfaces explain the calculation on hover (#292)
- Tag mutations (assign, unassign, delete, archive, merge, set-visibility) now invalidate the dashboard cache, so tag chip and filter changes appear immediately without waiting for the 30-second TTL (#291)

### Fixed

- Probe runs no longer pollute the task browser: trial counts, reward stats, experiment chips, and `last_run_at` (which drives page ordering) now all exclude `is_probe` trials, consistent with probes having their own tab (#285)
- LIKE wildcards in task browser and document search are now escaped as literals: searching `_` no longer matches every task, and `%` and `\` behave as plain characters (#285)
- Browse page query performance improved: the org-wide trial aggregate now computes only the `max(activity)` needed for ordering; per-task counters are fetched as a separate targeted query over the visible page only, reducing latency ~40% at prod volume (#285)
- `GET /tasks` no longer intermittently 500s with `MissingGreenlet`; `TrialModel.is_probe` added to the compact-trials `load_only` allowlist so it is loaded eagerly on the async path (#283)
- Probe summarizer no longer reports "received no output" for skills: user-turn text blocks (the mechanism claude-code uses to deliver skill bodies) are now captured as `injected_context` so the summarizer sees the full skill content (#289)

---

## [2026-06-12]

### Added

- QA tab consolidating probe runs, presets, skills, and documents under a new `/qa` route group; includes a Probe Runs list (one row per task with probe activity, ordered most-recent-first), a full Presets CRUD management page, and a new org-wide `GET /probes` backend endpoint; old `/skills` and `/documents` routes redirect transparently to their new `/qa/*` homes (#266)
- Probe run summary accessible from the experiment task-file drawer sidebar: a "Latest probe run" entry below the file tree opens a compact card with status, agent/model, headline, metric chips, and cheating verdict; links to the full probe detail page; hidden on public share view; auto-polls while the probe is still running (#265)

### Changed

- Auto-probe is now opt-in and off by default; `maybe_enqueue_auto_probe` previously fired unconditionally on every sweep; now gated on a new `run_probe: bool` field on `TaskSubmission`, `TaskSweepSubmission`, and `TaskStatusResponse`, a `run_probe` DB column with migration `run_probe_001`, and a `--run-probe` CLI flag on `oddish run`; append-mode submissions flip the flag on first opt-in, mirroring the existing `run_analysis` pattern (#271)
- Org switcher moved from Settings > Workspace into the top nav bar so the active workspace is always visible and switchable; settings page retains a read-only current-workspace card with updated copy; SWR cache is flushed on org change to prevent stale data from the previous workspace leaking through (#274)
- Nav "QA" label renamed to "Agents" (route, active-state checks, and `QA Verdict`/`QA Review` strings elsewhere are unchanged) (#270)
- Probe run detail page and experiment task-drawer probe card now share a single `ProbeRunSummary` component, bringing the drawer card to full parity with the detail page; the card previously omitted `key_actions` and `tool_insights` sections (#269)
- Preview environments now seeded with a pseudo-random, deterministic subset of real production data (rows drawn by `md5(id || PR_NUMBER)`) instead of curated fixtures; reviewers authenticate with their real org credentials in preview; in-flight tasks/trials are normalized to `FAILED` on import to prevent the preview's safety nets from enqueuing real analysis/verdict jobs; convergence tracked via a private `_preview_seed_state` table; per-row savepoints prevent a single constraint collision from aborting the run (#264)

### Fixed

- Probe trials no longer appear in the experiment trial matrix; filtered server-side in the `experiment_id` branch of `list_tasks` before version resolution, preventing probe runs from cluttering the main task grid and preventing a probe-only version from skewing the effective version display; public `/public/tasks/{task_id}/trials` endpoint gains an optional `?probe=` filter param (`true`/`false`/omit) (#276)
- `?task=` deep links on the experiment page now fall back to matching by task name when no exact task ID is found, fixing hand-written links such as `?task=ghsa-rpfr-x88x-xwcw` that previously opened nothing (#275)
- Task version badges (`v{n}`) in the public experiment table are now hidden for unauthenticated viewers; `oddish run` reproduction command in the trial drawer hidden on public share pages; timing row in the trial drawer now shows the viewer's local timezone abbreviation (e.g. `PDT`) (#277)
- GLM/z.ai provider icon corrected from the ChatGLM mammoth glyph to the ZAI "Z" logo (#277)
- Footer "by Abundant AI" link updated from `abundantdata.com` to `abundant.ai` (#277)

---

## [2026-06-11]

### Added

- Auto-enqueue one probe trial per task version on sweep submit using round-robin model rotation; new `GET /experiments/{experiment_id}/probes` endpoint lists the latest probe trial per task; experiment page fetches and displays the default probe preset (#248)
- Dashboard experiment owner filter (Org / Mine / admin member picker) via `experiments_author` parameter on `/dashboard`; defaults the Recent Experiments table to the signed-in user's experiments; legacy rows matched via GitHub username and email for accurate attribution (#214)
- Self-healing experiments owner backfill runs on every queue-poll tick, converging `owner_user_id` so the dashboard Mine filter stays on its indexed fast path; adaptive filter uses a pure indexed `owner_user_id` seek once all experiments in the org are attributed, with an `__unattributed__` sentinel for unowned rows (#255)
- `ShareNav` component for public experiment share pages with Oddish logo, "by Abundant AI" branding, and theme toggle; replaces the full app `Nav` on share pages (#260)
- Cursor provider icon support: `cursor` and `cursor-cli` agents and the `composer-*` model family now resolve to the Cursor icon in the queue key icon display (#260)
- Claude Fable 5 mapped to Bedrock global cross-region inference profile (`global.anthropic.claude-fable-5`); note this is a Covered Model requiring AWS account data retention mode set to `provider_data_share` (#254)

### Changed

- Pass@k graph and leaderboard default to visible on public experiment share pages; authenticated views are unchanged (#256)
- Tasks/Probe tab toggle hidden on public share view — probing requires an authenticated session (#260)
- OpenRouter models (Gemini 2.5 Flash, DeepSeek Chat) removed from probe model rotation; only `claude-haiku-4-5` remains (#253)

### Fixed

- Pass@k calculation now uses per-agent per-task attempt count instead of the global maximum, fixing incorrect curves when agents in the same experiment have different trial counts (e.g. oracle with 1 trial shown as `33%/67%/100%` instead of its observed flat rate) (#258)
- Dashboard Mine filter no longer degenerates into a near-full-table scan; an adaptive indexed owner seek is used once `owner_user_id` is backfilled; attribution profiles are served from cache instantly with background refresh; frontend keeps existing rows visible during polling revalidation instead of blanking to "Loading…" (#255)
- GitHub PR branch link (experiment PR chip) hidden on public share view to avoid exposing internal repository context to anonymous viewers (#261)
- `skills_001` and `documents_001` Alembic migrations made idempotent for data-less preview branches, fixing "relation already exists" errors that caused PR preview deploys to fail at `alembic upgrade head` (#251)

---

## [2026-06-10]

### Added

- Skills library and agent doc store: org-scoped skills with CRUD, a frontend Skills page, probe sandbox injection (`.claude/skills/` via overlay), and Harbor `AgentConfig.skills` delivery; a document library with text/markdown/PDF/CSV ingestion, LLM digest generation (summary + tags), and keyword+tag search; `oddish-docstore` MCP server exposes `search`, `get`, and `inspect` tools; Skills and Documents added to the app nav (#217)
- `is_probe` boolean column on `trials` with migration and backfill from `harbor_config->>'mode'`; `?probe=true/false` filter on `GET /tasks/{id}/trials`; probe history table now filters server-side (`?probe=true`) instead of client-side; inline probe summary generated in the cloud trial handler after each probe trial completes (#231)
- Probe skill and MCP tool usage captured as structured signal in probe artifacts: `_classify_tool_use` tags every transcript `tool_use` entry as `skill`, `mcp`, or `builtin`; `_summarize_tool_usage` produces a deterministic `tool_usage` roll-up (skill slugs and MCP server/tool pairs, ordered by first appearance) surfaced under `extract_probe_artifacts` (#244)
- Probe summary "Tools & skills used" section: when an agent invoked skills or MCP servers, `run_probe_analyzer` appends per-tool `tool_insights` entries (name, kind, one-sentence note grounded in the transcript); rendered as a labeled list with `Skill`/`MCP` chips on the probe result page (#245)

### Changed

- PR preview databases switched from prod-clone (`--with-data`) to data-less Supabase branches populated by a deterministic seed (`backend/preview_seed.py`): reflection-driven, idempotent and convergent (upsert + full-PK reconcile), spanning both Alembic stacks without ORM imports; `seed-gate` CI job validates schema/seed drift on every PR; preview branch readiness deadline cut from 20 min to 5 min (#243)
- Harbor dependency bumped to 0.13.1 (from 0.8.0), adding the `glm-claude-code` agent (z.ai base URL, `ZAI_API_KEY` auth, recommended streaming env, Claude Code version pin 2.1.167), closed-internet IPv4 fixes for `api.z.ai`, and harbor-framework v0.13.1 (#247)
- `Settings` now auto-loads `.env.local` layered over `.env` for local backend development; later file wins on duplicate keys, exported env vars still outrank both (#230)

### Fixed

- Probe result page now shows agent transcript and verifier output for cloud trials; cloud runs do not inline `_artifacts` into `trial.result`, so a new `GET /trials/{id}/probe-artifacts` endpoint and BFF proxy download artifacts on demand from object storage and cache them for finished trials (#227)
- Cloud probe summary no longer fails with auth errors (`PermissionDeniedError` / `TypeError`); the probe analyzer now always runs on the direct Anthropic API (`ANTHROPIC_API_KEY`) instead of Bedrock; Bedrock inference-profile model IDs are normalized to their plain API form via new `to_anthropic_api_model_id()` helper (#236)
- Cloud probe summary `NameError` fixed: `extract_probe_artifacts` and `run_probe_analyzer` imports were missing from `trial_handler.py`, causing every cloud probe run's inline summary to fail with `NameError: name 'extract_probe_artifacts' is not defined` (#232)
- Experiment page no longer returns 500 `MissingGreenlet` errors for tasks with trials; `TrialModel.harbor_config` and `TrialModel.is_probe` added to the compact-trials `load_only` allowlist so deferred JSONB columns are not lazy-loaded outside the async greenlet (#228, #235)
- Preview branches stuck in `RESTORE_FAILED`, `INIT_FAILED`, or `PAUSE_FAILED` states are now detected immediately and torn down for recreation instead of polling until the 20-minute deadline; deadline timeouts also trigger delete-and-recreate; retry budget raised to 3; seed reclaims `clerk_org_id` from pre-existing rows on reused branches to avoid duplicate-key errors (#224)

---

## [2026-06-09]

### Added

- GLM / z.ai routing for the `claude-code` harness: GLM models (`zai/glm-x-preview[1m]`, bare `glm-...`, or `z-ai/`/`z.ai/` prefixes) canonicalize to a `zai/<id>` id so they get their own `zai` provider and `zai/<id>` queue bucket instead of inheriting claude-code's fixed Bedrock provider/queue (keeping GLM trials from contending with Bedrock traffic for concurrency slots); `harbor_runner` injects the z.ai Anthropic-skin env (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN=${ZAI_API_KEY}`, model + size aliases, z.ai's recommended long-context settings) and blanks the ambient Bedrock/Anthropic credentials so the z.ai route wins
- `oddish probe` CLI command with full cloud probe agent support: probe presets stored in Postgres with CRUD backend endpoints and UI on task pages; probe trials bypass strict `task.toml` timeout validation and use a capped 30-minute agent timeout; local and cloud runners share the same probe implementation (#218)

### Changed

- Experiment trials table on the experiment page now defaults to A→Z task name sort instead of insertion order; the sort header toggle still cycles through all options (#220)

### Fixed

- Probe UI no longer shows "Failed to fetch" CORS errors; browser fetches for probe presets and sweep submissions moved from direct cross-origin backend calls to the same-origin Next.js BFF proxy (`/api/probe-presets`, `/api/tasks/sweep`), removing CORS as a failure point; reverts the hardcoded prod-origin CORS allowlist from #221 since it is no longer needed (#222)
- Probe trials on tasks whose `task.toml` omits agent timeout settings no longer hard-fail validation; a shared `PROBE_AGENT_TIMEOUT_SEC` constant (default 30 min, overridable via `ODDISH_PROBE_AGENT_TIMEOUT_SEC`) is applied by both local and cloud runners (#225)
- PR lineage badges now render consistently across dashboard, experiment page, task detail header, and task browser cards; fixed blank badge on the experiment page caused by `TaskModel.link` missing from the `compact_trials` `load_only` set; fixed missing badge on task cards when the PR URL lived only in `github_meta.pr_url`; added shared `taskPrUrl(link, github_meta)` resolver in `lib/utils.ts`; dashboard PR column now falls back to the `link` column when `github_meta` is absent (#197, #223)
- Fresh-database `alembic upgrade head` no longer fails with duplicate column or foreign-key errors; three incremental migrations (`add_column last_activity_at`, `fk_tasks_current_version_id`, `fk_trials_experiment_id`) are now idempotent; a merge migration joins the two divergent heads into a single chain (#215)
- Resolved Alembic double-head in the oddish migration chain caused by `probe_presets_001` and `dispatch_log` branching from the same migration tip; `probe_presets_001` re-parented onto `c1d2e3f4a5b6` to restore linear history (#219)

### Removed

- Repository dispatch emitter (`github/dispatch.py`) and `experiment_dispatch_log` table dropped; the `repository_dispatch` event path requires `Contents:write` permission that the production token lacks (#211)

---

## [2026-06-08]

### Added

- Fire a `repository_dispatch` webhook to consumer repos when all tasks in an experiment reach a terminal state; an `experiment_dispatch_log` table provides idempotent single-fire semantics; dispatch target and event type are read from `github_meta.dispatch` on task tags; gated by `GITHUB_DISPATCH_ALLOWED_REPOS` allowlist and authenticated via `GITHUB_DISPATCH_TOKEN` (falls back to `GITHUB_TOKEN`) (#206)

### Changed

- Bake a per-model `ODDISH_MODEL_CONCURRENCY_OVERRIDES` default into the Modal deploy that raises the `google/gemini-3.5-flash` queue-key concurrency lease to 128 (up from the 48 default); operators can still override the whole JSON via the env var / `oddish-prod` secret (#213)
- Raise `ODDISH_DEFAULT_MODEL_CONCURRENCY` fallback from 32 to 48, increasing per-model queue-key concurrency in the Modal runtime without changing the per-poll spawn cap (#208)
- Experiment trials table tooltip for truncated task names now shows the full task name instead of the generic "View task files" label, with responsive max-width and word-break styling for long names (#207)

---

## [2026-06-07]

### Changed

- Automated daily changelog updated with entries for 2026-06-06 changes (#202)

---

## [2026-06-06]

### Added

- Trial detail panel now shows a "Sandbox" button linking to the Daytona dashboard when a trial has an associated Daytona worker job; `provider` and `external_id` fields exposed in the worker job API response to enable this (#190)

### Fixed

- Codex workers running against Azure OpenAI endpoints no longer fail with 302 errors from the websocket Responses route; a new `AzureCompatibleCodex` runner disables the `unified_exec` websocket transport and injects an HTTP-only OpenAI-compatible provider config; trajectory is recovered from stdout JSONL as a fallback when the Codex session file is sparse (#193)
- `enqueue_analysis_worker_job` now skips enqueueing analysis for trials with no stored result (neither S3 key nor local path), immediately marking analysis `FAILED` instead of burning all 6 retries on a doomed job; a staleness-gated cleanup backstop finalizes `ANALYZING` tasks with no live trials and cancels their dangling queued `ANALYSIS` worker jobs (#196)
- Stuck-`ANALYZING` cleanup pass rescoped to correctly target tasks whose live trials have `analysis_status = NULL` (analysis was never enqueued) rather than tasks with no live trials at all; NULL analysis statuses are now marked terminal so `maybe_start_verdict_stage` can advance the task to `VERDICT_PENDING` instead of leaving it indefinitely blocked; tasks with no live trials are still finalized `FAILED` (#200)
- Worker containers now drain short-job queues by claiming and running multiple jobs back-to-back on their held slot until the queue empties or a wall-clock budget (`ODDISH_MODAL_WORKER_BATCH_BUDGET_SECONDS`, default 300s) expires; lifts utilization for analysis (~54s), verdict (~9s), and nop-oracle (~46s) queues toward 100% without changing global spawn rates or concurrency limits; long agent trials exceed the budget on the first job and continue to run one-per-container (#201)

---

## [2026-06-05]

### Added

- `oddish cancel` gains `--analysis` and `--verdict` flags to cancel active analysis or verdict jobs independently without stopping unrelated trials; new API endpoints `POST /tasks/{task_id}/analysis/cancel`, `POST /tasks/{task_id}/verdict/cancel`, and `POST /trials/{trial_id}/analysis/cancel`; dashboard adds per-trial, bulk-selection, and task-detail cancellation controls for both stages (#189)
- `oddish run --link <url>` attaches a source URL (PR, issue, or CI run) to a task at submission time; auto-derived from `--github-meta` `pr_url` when `--link` is omitted; displayed in the task detail page header; re-runs update the link when a new value is provided and leave it unchanged when none is given (#178)

### Fixed

- Daytona sandbox creation no longer fails with "Only ephemeral sandboxes are permitted in this region"; a new `daytona_ephemeral` setting (default `True`) causes harbor trials to request ephemeral sandboxes, matching the Daytona region's configuration; harbor pin bumped to include matching ephemeral sandbox support (#188)
- GitHub PR comment now auto-updates as trials, analyses, and verdicts complete; the previous implementation nested two DB sessions inside `notify_trial_update`, `notify_analysis_update`, and `notify_verdict_update`, deadlocking the worker's size-1 connection pool before the GitHub write was reached (#187)
- `alembic upgrade head` no longer fails with "Multiple head revisions are present"; a merge migration (`74a0eab3e564`) joins the divergent `provider/external_id` and `task_link` heads into a single unified head (#186)

---

## [2026-06-04]

### Fixed

- `oddish run` no longer crashes when a task's `task.toml` omits the `gpus` field; `_task_config_requests_gpu` now treats an absent `gpus` as 0 GPUs instead of raising `TypeError: '>' not supported between 'NoneType' and 'int'` (#181)
- Claude Code workers using an OpenRouter model now receive the correct Anthropic-skin environment (`ANTHROPIC_BASE_URL` pointing to the OpenRouter endpoint, `ANTHROPIC_AUTH_TOKEN` set to `${OPENROUTER_API_KEY}`); conflicting Bedrock and direct-Anthropic ambient credentials are blanked so the OpenRouter route takes effect (#175)

---

## [2026-06-03]

### Added

- OpenAI-family workers now route through Azure OpenAI by default; `ODDISH_OPENAI_PROVIDER` (default `azure`) selects the transport, with `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_API_VERSION`, and `ODDISH_AZURE_OPENAI_DEPLOYMENTS` for per-model deployment mapping; set `ODDISH_OPENAI_PROVIDER=openai` to use the public OpenAI API instead
- Daytona sandboxes now auto-stop (30 min) and auto-delete (60 min) as a backstop for sandboxes that escape explicit teardown; `worker_jobs` gains `provider`/`external_id` columns and cancel/orphan-reap paths now terminate the underlying sandbox by ID, preventing idle sandbox accumulation

### Changed

- `to_bedrock_model_id()` now passes through any model ID with an explicit non-Anthropic provider prefix (e.g. `openrouter/anthropic/claude-opus-4.8`) unchanged so it runs through that provider rather than being rewritten to a Bedrock inference-profile ID; `claude-code` agent in harbor_runner updated to reflect the same pass-through semantics
- Oddish GitHub PR comment now includes a "Performance: X/Y trials passed (Z%)" summary line and Status + Reward columns in the experiment trajectory table, surfacing actual agent scores alongside the existing classification status

### Security

- API key creation now requires the requesting user to be an admin of the Abundant organization; the API keys settings section is hidden in the UI for non-admins

---

## [2026-06-02]

### Changed

- `claude-opus-4-8` added to the Bedrock model ID mapping table, resolving to `global.anthropic.claude-opus-4-8`; `claude-opus-4-7` moved to the legacy section of the table; regression tests added for all resolution forms (bare, `anthropic/`-prefixed, dotted foundation-model id) (#169)

---

## [2026-06-01]

### Changed

- Automated daily changelog updated with entries for 2026-05-31 changes (#166)

---

## [2026-05-31]

### Changed

- Automated daily changelog updated with entries for 2026-05-30 changes (#165)

---

## [2026-05-30]

### Added

- `oddish run --retry` re-runs existing work for a trial, task, or experiment; re-queues failed trials by default, or re-runs analysis/verdict stages with `--analysis` / `--verdict`; `-y/--yes` skips confirmation (#163)
- `oddish publish` / `oddish unpublish` commands toggle public read-only sharing for an experiment from the CLI and return the shareable URL; previously only possible at submit time via `run --publish` or in the web UI (#163)
- `--json` machine-readable output added to `oddish status`, `cancel`, `delete`, and `pull` via a shared `print_json` helper; `--json` implies non-interactive mode and takes a single snapshot (no live watch) (#163)
- `oddish combine` CLI command merges two or more experiments into a new result experiment, copying finished trials and their artifacts; supports `--name`, `--copy-artifacts/--no-copy-artifacts`, and `--json` flags (#162)

---

## [2026-05-29]

### Added

- `oddish run --retry` re-runs existing work for a trial, task, or experiment id (positional, `--task`, or `--experiment`): re-queues failed trials by default, or re-runs analysis/verdict with `--analysis` / `--verdict`; `-y` skips confirmation
- `oddish publish` / `oddish unpublish` commands toggle public read-only sharing for an experiment from the CLI and return the shareable URL
- `--json` machine-readable output added to `oddish status`, `cancel`, `delete`, and `pull` (previously only on `run` / `upload` / `ls`)
- `oddish combine` CLI command to merge two or more experiments into a new result experiment; copies finished trials with artifacts from source experiments and supports `--name`, `--copy-artifacts/--no-copy-artifacts`, and `--json` flags (#162)
- `POST /experiments/combine` API endpoint that creates a new result experiment by merging task memberships and finished trials (with S3 artifacts) from two or more source experiments; in-flight trials are skipped and counted in the response; append-only so requires only `tasks` scope (#157)

### Changed

- Analysis and verdict UI (trial analysis dots, legend section, analysis card, verdict badge, and run analysis/verdict actions) is now hidden in the public share view (`/share/[token]`) via a new `showAnalysis` prop on `ExperimentDetailView`; authenticated views are unchanged (#159)

### Fixed

- Trial retry no longer returns a 500 error; the new trial row is now flushed before the old trial's `superseded_by_trial_id` self-referential FK is set, preventing a Postgres FK violation (#155)

---

## [2026-05-28]

### Changed

- Harbor dependency updated to a fork commit that corrects Google API CIDR ranges for proper network access in restricted environments
- Automated daily changelog updated with entries for 2026-05-27 changes (#153)

---

## [2026-05-27]

### Changed

- Automated daily changelog updated with entries for 2026-05-26 changes (#152)

---

## [2026-05-26]

### Fixed

- Preview banner now sticks to the top of the viewport and no longer overlaps the nav bar, task drawer, or settings sidebar; a CSS custom property `--preview-banner-h` (0px normally, 1.75rem in preview mode via `data-preview` on `<html>`) propagates the banner height to all affected components so `calc()` offsets stay in sync without hardcoded values (#146)

### Changed

- Automated daily changelog updated with entries for 2026-05-25 changes (#151)

---

## [2026-05-25]

### Changed

- Automated daily changelog updated with entries for 2026-05-24 changes (#149)

---

## [2026-05-24]

### Changed

- Automated daily changelog updated with entries for 2026-05-23 changes (#148)

---

## [2026-05-23]

### Changed

- Daytona is now the default execution environment for CPU-only hosted tasks; Modal is automatically selected when a task's `task.toml` declares GPU requirements or when `--override-gpus` is set to a positive value; `--env` help text updated to reflect the new defaults
- Harbor dependency updated to a version that runs build tools under a restricted network

---

## [2026-05-22]

### Changed

- Automated daily changelog updated with entries for 2026-05-21 changes (#142)

---

## [2026-05-21]

### Added

- Sticky PR comment automatically posted (and updated on re-pushes) with preview environment links — Vercel frontend URL, stable `pr-NNN` Vercel alias, and Modal API URL — via new `post_preview_links.py` script (#141)
- In-app preview banner rendered when `NEXT_PUBLIC_IS_PREVIEW=true`, surfacing PR context to reviewers using the preview environment (#141)

### Changed

- PR preview workflow refactored from a monolithic `modal-preview.yml` into `pr-preview.yml` backed by focused per-phase shell scripts (`prepare_preview_database.sh`, `deploy_preview_backend.sh`, `update_vercel_preview.sh`, etc.), making migration-only and backend-only preview runs possible without triggering a full component redeploy (#141)
- Deployment planning now tracks `deploy_frontend` as a separate output flag alongside `deploy_backend` and `run_migrations`; frontend-only PRs skip Supabase/Modal provisioning entirely, and non-`synchronize` events fall back to PR-wide path filters to decide which components need deploying (#141)
- Newly created Supabase preview branches now cancel in-flight cloned production work (queued/running jobs, tasks, and trials) via `cancel_cloned_preview_work.sh` to prevent spurious activity from the data clone (#141)

---

## [2026-05-20]

### Changed

- `ODDISH_MODAL_MAX_WORKERS_PER_POLL` default raised from 48 to 64, allowing the dispatcher to ramp queued work faster when per-queue slot capacity is available; env override behavior unchanged (#138)

---

## [2026-05-19]

### Added

- `max_trial_attempts` top-level field for sweep YAML/JSON configs and `TaskSubmission`/`TaskSweepSubmission` API schemas, plus `--max-trial-attempts` CLI flag on `oddish run`, to control the total Oddish worker attempt budget per trial including the initial run; old `max_attempts` config key is now rejected with a clear error (#134)
- `ODDISH_MODAL_POLL_INTERVAL_SECONDS` env var to configure the Modal queue dispatcher poll cadence; preserves the existing 180-second default when unset (#133)
- Baseline-specific context injected into the trial analysis classifier prompt for `oracle` and `nop`/`noop` agents: oracle trials are no longer penalized for reading reference solutions, and NoOp runs are evaluated as baseline checks; normal agents receive no extra context (#132)
- `--environment-kwarg` / `--harbor-environment-kwarg` CLI flag and top-level `harbor.environment.kwargs` block in sweep YAML for passing arbitrary Harbor environment kwargs (primary use case: `agent_tools_image` for Modal closed-internet runs); CLI values override config-file values on collision (#131)
- "Any error" row filter in the experiment trials table to show only tasks where at least one agent hit a harness or infrastructure error, complementing the existing "Any failed" / "All failed" filters (#130)
- `DELETE /experiments/{experiment_id}` admin API endpoint for soft-deleting an experiment and its scoped trials; artifacts are preserved in S3 (#128)

### Changed

- `nop` and `oracle` sweep config entries no longer require a `model_name` field (#131)
- Experiment table toolbar UI polished: filter buttons styled with updated tokens and tighter layout, toolbar reorganized into a responsive flex row (#130)

### Fixed

- Experiment detail page creation timestamp now uses the canonical `ExperimentModel.created_at` value (surfaced via new `experiment_created_at` field on task-status responses) instead of inferring creation time from the earliest task in the experiment (#129)
- Experiment-to-task membership rows in `task_experiments` are now tombstoned (`deleted_at` set) instead of hard-deleted when an experiment or scoped task is removed; DB migration `k2l3m4n5o6p7` adds the column with partial indexes on live rows; dashboard cache is invalidated after experiment and trial deletions (#128)

---

## [2026-05-18]

### Added

- Dashboard status filter now includes a "Retrying trials" option; retrying trial counts shown as `(nR)` in amber in the Trials column (#125)
- Dedicated `nop_oracle` queue for `nop` and `oracle` trial agents with a separate `ODDISH_NOP_ORACLE_CONCURRENCY` setting (default 32; 48 in Modal), preventing these lightweight trials from competing with model-provider queues; DB migration moves existing non-terminal nop/oracle jobs to the new queue key (#121)
- Bounded exponential backoff for trial retries: 30 s base delay, up to 30 min cap, with ±25% jitter; rate-limit errors (429, quota exceeded, throttled, etc.) start at a 5 min base; retry delay persisted to `worker_jobs.available_after` and mirrored to `trials.next_retry_at` (#122)

### Fixed

- Modal image build failures (`Image build for im-... failed`) now permanently fail the trial instead of requeueing, preventing repeated retry burns on deterministic Dockerfile errors; user-cancelled trial state is preserved when a build failure and a user cancel race (#124)
- Retry API proxy routes (trial retry, trial analysis retry, task analysis retry, task verdict retry) now surface the real upstream error when the backend returns non-JSON plain text, instead of a misleading JSON parse exception; shared `backend-response.ts` helper introduced for safe response reading (#126)

---

## [2026-05-17]

### Fixed

- `oddish status` and `oddish status --watch` now show a `Detail` column with per-trial status context — `cancelled by user`, the active Harbor stage while running, or the terminal error message on failure — replacing the old `Stage` column that only populated during `running` state (#112)
- CLI task discovery no longer calls the removed `TaskPaths.is_valid` API; `is_task_dir` and `get_task_paths_from_local` now validate candidate directories by constructing `Task(path)`, matching the path already used by `validate_tasks` and preventing submit failures on newer Harbor builds that dropped the compatibility helper (#119)

---

## [2026-05-16]

### Added

- Copy-to-clipboard button beside task names in the experiment trials table: a copy icon appears on hover/focus and shows a brief check-mark confirmation after copying, without opening the task files panel (#114)

### Changed

- Drawer panels (`TaskFilesPanel`, `TrialDetailPanel`, `ArtifactsViewer`, `TrajectoryViewer`) in experiment and task detail views are now lazy-loaded via Next.js `dynamic()` imports, shrinking the initial page bundle (#113)
- Browser Logfire/OpenTelemetry tracing is now deferred behind a conditional dynamic import in `instrumentation-client.ts`, keeping it off the critical bundle when disabled or unconfigured (#113)
- Browser observability spans now export directly to Logfire's OTLP endpoint using a `NEXT_PUBLIC_LOGFIRE_TOKEN` write-only token, replacing the backend proxy route (`/logfire-proxy/*`) that consumed Modal container concurrency slots; `LogfireProxyCORSMiddleware` and `mount_browser_proxy()` removed from the backend (#111)
- Preview branch provisioning switched back to Supabase's native `--with-data` clone; the custom `restore_prod_data.sh` `pg_dump | pg_restore` script is removed (#111)

---

## [2026-05-15]

### Added

- Task detail page (`/tasks/[task_id]`) with KPI bar showing total cost, trial count, average score, and last run time; version switcher for per-version breakdown; per-agent stacked cards with trial-status chips that open existing task/trial drawers; new `GET /tasks/{task_id}/detail` endpoint bundles task, trials, per-version summaries, and cost rollups in one round-trip (#103)
- Trajectory JSON export button on the trajectory viewer side-pane; downloads the loaded trajectory payload as `trajectory-<trialId>.json` client-side without additional API calls (#92)

### Changed

- Claude Code now routes through AWS Bedrock by default in the Modal deployment: `CLAUDE_CODE_USE_BEDROCK=1` baked into the Modal image; new `to_bedrock_model_id` normalizer in `oddish/config.py` converts Anthropic-style and bare Claude model ids to invokable Bedrock cross-region inference profile ids (`global.` prefix for most models, `us.` for Opus 4.1 / Opus 4 which have no global profile); trial analysis classifier strips Bedrock env vars when running against a non-Bedrock analysis model id (#108)

### Fixed

- Bedrock model id mapping table now emits `global.`/`us.` cross-region inference profile ids instead of bare `anthropic.claude-...` foundation-model ids; bare foundation-model ids are also re-resolved through the table rather than passed through, closing a gap that caused 400 "Invocation of model ID with on-demand throughput isn't supported" errors in production (#109)
- Alembic migrations now pin `search_path=public` via asyncpg `server_settings` for both oddish and backend migration chains, fixing `InvalidSchemaNameError` on freshly-created Supabase preview branches where the Supavisor session pooler hands out backends with an empty `search_path` (#103)
- Vercel preview environment now updated and redeployed whenever the Modal backend redeploys (not only on first Supabase branch creation), so previews that failed mid-flight on a prior push self-recover on the next push rather than silently serving the production API (#103)

---

## [2026-05-14]

### Added

- Pydantic Logfire full-stack observability: backend auto-instruments FastAPI, SQLAlchemy, asyncpg, and httpx; browser spans tunnel through a server-mounted `/logfire-proxy/v1/traces` route so the write token never reaches the client; `Server-Timing: traceparent` header injected by middleware to fix document-load orphan spans; worker container init and per-job cycles wrapped in explicit `worker.container_init` / `worker.poll_queue_cycle` / `worker.process_single_job` spans; PR/SHA/branch/env resource attributes attached for per-deployment filtering in Logfire (#89)
- Side-by-side task files + trial detail layout in the experiment drawer: `ResizablePanelGroup` with an adjustable 40/60 split, a toggle button, localStorage persistence (`oddish:trial-drawer-side-by-side`), auto-expand of drawer width on enable, and direct presigned-S3 artifact loading with backend-proxy fallback (#91)
- `ghcr.io/abundant-ai/oddish-ci-base` prebuilt Docker image baking Python 3.13, uv, Node 20, Vercel CLI, Supabase CLI, PostgreSQL 17 client, Modal CLI, and pre-built project venvs at `/opt/venvs/{backend,oddish}`; published to GHCR weekly and on lockfile/Dockerfile changes via `.github/workflows/ci-base-image.yml` (#101)
- `daytona>=0.165.0` added to the `oddish[worker]` extra so hosted workers can construct Harbor Daytona environments for Docker-in-Docker compose tasks (#86)
- Daily changelog CI workflow (`.github/workflows/daily-changelog.yml`) that runs nightly at 00:00 UTC, uses Claude to summarize merged PRs from commits and diffs, and opens a `changelog/YYYY-MM-DD` PR with auto-merge enabled; `CHANGELOG.md` backfilled for all PRs to date (#84)
- Vercel Speed Insights integration: `@vercel/speed-insights` dependency added and `<SpeedInsights />` component mounted in the root layout to track Core Web Vitals across all pages (#82)

### Changed

- CI workflows (`modal-preview.yml`, `modal-deploy.yml`, `supabase-db-migrations.yml`) now run inside `ghcr.io/abundant-ai/oddish-ci-base`; `UV_PROJECT_ENVIRONMENT` points at the image's pre-built venvs, making `uv sync --frozen` a near-instant no-op instead of a full dependency install on every push (#100)
- Preview branch data population switched from Supabase's `--with-data` logical-replication clone (~20 min) to a direct `pg_dump | pg_restore` stream (~5 min); empty branches are provisioned first and populated from prod only on first branch creation via `.github/scripts/preview/restore_prod_data.sh` (#96)
- `modal-preview.yml` now has a `detect-changes` job that queries the GHA API for the last successful deploy of each component and uses `dorny/paths-filter` to skip unchanged backend/migration deploys, reducing unnecessary CI runs (#88)
- Signed-in users are now redirected from `/` to `/dashboard` via `clerkMiddleware` at the edge; the dead client-side `<Show when="signed-in"><RedirectToDashboard /></Show>` wrapper in `page.tsx` removed (#97)
- Nav account dropdown and sign-in button now driven by `isLoaded && isSignedIn` from `useUser()` directly, replacing the server-only `<Show>` component wrapper that caused incorrect client-side visibility (#99)
- Observability environment label standardized to `"production"` (was `"prod"`) across `backend/observability.py`, `frontend/src/instrumentation.ts`, and `frontend/src/lib/observability.ts` (#93)
- `oddish run --env daytona` now passes through to the Modal-hosted Oddish Cloud API instead of being forced to `--env modal`; warning message updated to reflect that both `modal` and `daytona` are supported cloud environments (#86)
- Daily changelog workflow is now safe to re-run the same day: the date branch is force-pushed and an existing open PR is reused instead of failing with a non-fast-forward error (#106)
- GitHub Actions versions bumped across all workflows: `actions/checkout` v4→v5, `actions/setup-python` v5→v6, `astral-sh/setup-uv` v4→v8.1.0, `supabase/setup-cli` v1→v2.0.0 (#90)

### Fixed

- Preview database restore now drops all public-schema FK constraints via `ALTER TABLE ... DROP CONSTRAINT` before running `pg_restore`, preventing prod's stray dangling refs from rolling back entire COPY operations for `tasks`, `task_versions`, `trials`, and related tables; `--disable-triggers` was not viable because Supabase's `postgres` role lacks superuser privileges (#99)

---

## [2026-05-13]

### Fixed

- Next-trial-index allocators now include soft-deleted trials when scanning for the next available index, preventing PK collision 500s on `INSERT` after a trial at `{task_id}-{N}` is soft-deleted; `execution_options(include_deleted=True)` added to `initialize_trial_import`, `reserve_next_trial_index`, and `append_trials_to_task` (#81)

---

## [2026-05-12]

### Removed

- `oddish/environment_policy.py` module (its exports `normalize_environment`, `enforce_trial_environment`, `EnvironmentName` had no callers; hosted policy lives in `backend/cloud_policy.py`) (#80)
- Unused `trialHasActiveAnalysis` and `getActiveAnalysisCount` exports from `frontend/src/lib/job-status.ts` (#80)

### Changed

- Frontend cleanup pass: downgraded several `job-status.ts` helpers (`ACTIVE_TRIAL_STATUSES`, `ACTIVE_PIPELINE_STATUSES`, `ACTIVE_VISIBLE_JOB_STATUSES`, `isActiveTrialStatus`, `isActiveVisibleJob`, `getActiveTrialCount`) from public exports to module-private; type-only exports (`TaskStatus`, `TrialStatus`, `VisibleJobKind`, `VisibleJobStatus`) made file-local (#80)
- Settings sidebar nav and import-dialog "Clear" control rewritten to use the shadcn `Button` primitive instead of raw `<button>` elements (#80)
- Removed unused `logging` import and unused `logger` from `backend/api/routers/github_webhooks.py` (#80)

---

## [2026-05-11]

### Fixed

- Supabase database migrations workflow now syncs oddish with `--extra server` so server-specific deps (alembic, SQLAlchemy, asyncpg) are present during migration runs (#79)

---

## [2026-05-10]

### Added

- `ODDISH_SAURON_AWS_SECRET_NAME` setting on the backend Modal app, defaulting to `aws-credentials`, to control which Modal secret is layered onto worker containers for the sauron S3 mirror; setting it to empty skips loading (#68, #74)

### Changed

- Worker runtime now loads the `aws-credentials` Modal secret alongside `oddish-prod`, so `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` are populated and `SauronS3Uploader.is_enabled()` actually returns true; this completes the wiring for the sauron mirror introduced in #39, whose credential plumbing was dropped during the original squash-merge (#68, #74)
- Backend Dockerfile installs `git` so `uv sync --frozen` can fetch the harbor dependency (sourced via git URL in `[tool.uv.sources]`) when building outside Modal (e.g. Railway, generic container hosts) (#75)

### Fixed

- Rollback merge that resets `main` back to a known-good state after the task-first data model (#55) caused breakage; reverts the bulk of that change-set on `main` (#72)

---

## [2026-05-08]

### Added

- Sauron S3 mirror: when `ODDISH_SAURON_S3_BUCKET` is set, oddish workers mirror trial artifacts to a sauron-compatible S3 layout and write a `run-meta.json` manifest (schema_version 1) at the run root so sauron's existing `/{org}/{repo}/{pr}/{run}` route renders both PR-triggered (`{owner}/{repo}/pr-{N}/run-{exp}/...`) and CLI-triggered (`{ODDISH_SAURON_S3_ORG}/runs/{exp}/run-{exp}/...`) runs without sauron changes; disabled by default, best-effort with try/except on failures (#39)
- Drag-and-drop zip import UI: dashboard now has an import dialog with native drag-and-drop slots for task and trial zips, mirroring `oddish upload` (#42)
- `POST /imports/zip` and `POST /imports/zip/inspect` backend endpoints for streaming task/run zip uploads with 1 GiB per-file cap, presigned-URL task uploads, concurrent trial fan-out, and a read-only preview path; new framework-agnostic `oddish/core/zip_imports.py` reuses CLI utilities for parity (#42)
- Task-first data model (Phase 1 + Phase 3): new `JobModel` and `ExperimentCellModel` tables, `JobKind` enum (`validation`, `experiment_backfill`, `ad_hoc`), agent equivalence keying via SHA256 of `(harness | model | provider)` for trial fungibility, and trials joining experiments through `(task_version_id, agent_equivalence_key)` at read time rather than ownership; 7 alembic migrations seed cells/jobs and enforce `task_versions` immutability (#55)
- `POST /experiments`, `GET /experiments/{id}/cells`, cell CRUD, `/experiments/{id}/resolve`, `/experiments/{id}/backfill`, `/agents/known`, and `/api/jobs/*` endpoints, plus `experiment-cell-matrix.tsx`, `experiment-leaderboard.tsx`, `experiment-pass-at-k.tsx`, `trial-inspect-drawer.tsx`, `jobs-client.tsx`, and `new-experiment-client.tsx` frontend components (#55)
- `ExperimentCreateResponse` schema that extends `ResolvedExperimentResponse` with an optional `backfill` receipt field (#69)

### Changed

- `POST /experiments` now enqueues a backfill automatically and returns the resolved experiment with the new trial receipts in a single round-trip; pass `?dry_run=true` to keep the previous create-only semantics (#69)
- Frontend experiment creation flows updated to use the combined create+backfill call and drop the separate backfill request (#69)
- Gemini model routes canonicalized: `google/gemini...` and bare Gemini model inputs now normalize onto LiteLLM's `gemini/...` route in the queue/model resolution helpers (#71)

### Fixed

- Experiment-visibility regression: migration `p7e8f9a0b1c2` backfills `experiment_tasks` from `task_experiments` joined to each task's current version, and `experiment_agents` from distinct `(experiment_id, agent_equivalence_key)` pairs observed in trials (using the most recent trial's identity strings); both inserts use `ON CONFLICT DO NOTHING` so the migration is re-runnable (#66)
- Pass@k calculation now only counts completed attempts (`have_n_successful + have_n_failed`), excluding running and queued trials that produced no evidence; each task result carries its own `n` so per-task attempt counts are honored, with fallback to the agent-level `n` when absent (#66)
- Supabase migration workflow now installs the `server` extra so the `alembic` console script is present; the previous `uv sync --frozen` without `--extra server` silently failed every run (#70)

---

## [2026-05-05]

### Changed

- Drop Python 3.14 support (range tightened to `>=3.12,<3.14`) to fix dep resolution: `harbor==0.6.2` requires `litellm>=1.83.14`, which declares `Requires-Python <3.14`. `tool.mypy.python_version`, Trove classifier, `backend/Dockerfile`, and GitHub Actions `setup-python` all moved from 3.14 → 3.13; `uv.lock` relocked in both `oddish/` and `backend/` (#54)

---

## [2026-05-02]

### Added

- `--force-new-version` flag on `oddish run` (and corresponding `force_new_version` field on `TaskUploadInitRequest`) that allocates a new task version even when the local content hash matches the latest existing version, enabling callers to flip per-version-immutable flags like `run_analysis` without a content change (#59)

### Changed

- `create_task_sweep_core` now flips `task.run_analysis` from `False` to `True` when an append submission requests analysis, instead of returning a 400 "Cannot enable run_analysis when appending..." — this matches the documented intent of `--force-new-version` and unblocks full validation on tasks first registered without analysis (#60)

---

## [2026-05-01]

### Changed

- Task author now resolved backend-side from the authenticated identity (precedence: `--user` → `--github-user` → Clerk-backed `UserModel.email` → `api_key.name` → `"unknown"`) instead of `getpass.getuser()`; CLI no longer fills `task.user` from the OS username, so experiments stop showing `ubuntu` / `root` as Author (#52)
- `TaskSubmission.user` / `TaskSweepSubmission.user` are now optional on the wire; `submission.github_username` is auto-filled from the actor's `UserModel.github_username` when missing (#52)

### Fixed

- Removed the 400 guard in `create_task_sweep_core` that refused append-mode submissions for tasks in `ANALYZING` / `VERDICT_PENDING`; the existing `append_trials_to_task` path already handles the state cleanup (flips status back to `RUNNING`, clears verdict fields, cancels in-flight `VERDICT` worker jobs), so re-appending now lands cleanly and re-enters the analysis/verdict pipeline once the new trials complete (#53)

---

## [2026-04-30]

### Changed

- Bump `harbor` to `0.6.2` in the core package; regenerate `oddish` and `backend` lockfiles; realign direct pins on `litellm`, `openai`, and backend `python-dotenv` to match the new harbor dep graph; update task-status test doubles to the current `build_trial_response` shape (#48)
- Preview environment strategy: Supabase preview branches are now created with `--with-data` so they clone production data instead of starting empty, and the bootstrap script uses `ON CONFLICT DO NOTHING` for idempotent org seeding; branches are reused across pushes within a PR (#46)

### Removed

- `DELETE /tasks/{task_id}`, `DELETE /experiments/{experiment_id}`, `DELETE /trials/{trial_id}` HTTP endpoints from both `oddish/server` and `backend/api/routers` (the underlying `delete_*_core` helpers remain available for admin/CLI use through an auth-scoped surface) (#46)

---

## [2026-04-28]

### Added

- `oddish ls` CLI command that lists tasks via the existing `/tasks/browse` API and renders a Rich table with latest version, trial counts, reward summary, last run time, and linked experiments; supports `--limit` (capped at 100), `--offset`, and `--json` for scripting (#40)
- README section documenting `pip install` from a GitHub ref via `#subdirectory=oddish`, alongside the existing PyPI quick-start (#41)

---

## [2026-04-27]

### Added

- Supabase preview branch provisioning in the `modal-preview` PR workflow: Python polling step waits up to 10 minutes for Supabase to create the preview branch, runs both `oddish` and `backend` alembic chains against it, and layers a `PREVIEW_DATABASE_URL` Modal secret on top of the production secret so PR previews use isolated preview databases (#35)
- `supabase/config.toml` with project ID to enable Supabase's GitHub integration, plus `SUPABASE_ACCESS_TOKEN` / `SUPABASE_PROJECT_REF` env vars in the workflow (#35)

---

## [2026-04-26]

### Added

- "Rendered" vs "Raw" view-mode toggle on the task-files panel for text-based files, backed by a new `RawRenderer` component that displays content in a monospace `<pre>` block; URL-based renderers (image, video, audio, PDF, xlsx, docx, binary) ignore the toggle (#37)

### Changed

- File-content fetching no longer sniffs binary-vs-text — all text-based files are fetched via `response.text()`; legacy detection helpers (`isTextContent`, `shouldSniffTextContent`, `looksLikeTextBytes`, `readResponseTextContent`, `getBinaryFileMessage`) removed (#37)
- CLI docs (`DOCS.md`) gained a new "Reading data from Oddish" section with a decision table for `oddish status` vs `oddish pull`, expanded examples for `--watch`, the auto-detection fallback chain, per-trial file layout, idempotent re-pulling, and public-endpoint fallback for shared experiments (#38)

---

## [2026-04-25]

### Changed

- Experiment legend Trial-outcome chips resized to 22×18 / `rounded-[4px]` with a 10×10 SVG (was 14×14 / `rounded-[3px]` with 8×8 SVG) so legend swatches read as the same primitive as the matrix cells and the anatomy demo in the same toolbar (#36)

---

## [2026-04-24]

### Added

- `/settings` page redesigned with a sidebar layout (Account / Workspace / API keys), `Panel` / `PanelHeader` / `SectionHeading` primitives, Clerk-native `OrganizationSwitcher` instead of a hand-rolled workspace list, status-dot active-workspace indicator, and a real empty state for API keys; legacy `?tab=` URLs still accepted alongside the new `?section=` (#33)

### Changed

- Frontend `JobStatus.PENDING` is now folded into the `queued` matrix bucket: `getMatrixStatus` returns `queued` for `trial.status === "pending"`, `STATUS_FILTER_ORDER` and the URL filter type-guard no longer list `pending`, while backend-wire-aligned types and analysis/verdict in-flight checks still accept `pending` since the backend enum is unchanged (TODO comment added on `JobStatus` documenting the eventual full deprecation) (#27)
- Task detail drawer navigation simplified: removed the always-disabled left chevron and the standalone `FileText` indicator; the icon-only right chevron is now a labeled "View trials →" button; vertical progress sliver replaced with a legible "N / M" text readout between up/down chevrons (#29)
- Experiment trials table: first column now has a dedicated 240px default width so the `v1`/`v2` version badge no longer sits flush against the cell border; header cells gained `py-3` so the header row is visibly taller than data rows (#31)
- Experiment results visual refresh: 22×18 rounded matrix tiles with hover lift; thick-stroke geometric SVGs for pass/fail/partial/error/queued/running/pending replacing lucide glyphs; warm oklch color ramp (red → orange → olive → green) for partial scores; legend renamed (`Trial outcome` / `QA verdict`) with anatomy key, `Partial` chip dropped, `Harness error` renamed to `Error`; pass@k chart and leaderboard cross-highlight on agent hover, leaderboard bars switched to the shared `AGENT_COLORS` palette (#21)

### Fixed

- `/settings` dark-mode contrast: bumped `--muted-foreground` from `30 6% 62%` → `30 8% 74%`, pointed Clerk's `colorTextSecondary` at `hsl(var(--foreground) / 0.78)`, added the missing `appearance.elements` keys for active-device / profile-section / org-preview surfaces, plus a small `.dark .cl-*` block in `globals.css` for cases where Clerk's internal styles win the cascade (#34)
- `/settings` section-switch flicker: all three sections now render with CSS visibility rather than conditional mount/unmount, so Clerk's `UserProfile` / `OrganizationProfile` no longer remount on every tab click (#34)

---

## [2026-04-23]

### Added

- Experiment-level cost tracking in the summary bar: `oddish/model_pricing.py` provides per-token pricing for Anthropic (Claude 3.5/3.7/4/4.1/4.5), OpenAI (GPT-4o, GPT-4.1, GPT-5.x including codex variants, o3/o4-mini, codex-mini), and Google (Gemini 2.5/3) families with substring matching for Anthropic-API, Bedrock, and LiteLLM-style provider-prefixed names; ordered most-specific-first so `gpt-5-mini` never resolves to `gpt-5` rates (#23)
- `cost_usd` and `cost_is_estimated` fields on the trial response builders (full + compact); `ExperimentDetailView` summary bar aggregates cost across visible trials with `~` for pure estimates and trailing `*` for mixed native+estimated totals (#23)

### Changed

- Frontend major-dep upgrades landed: `@clerk/nextjs` 6.36.8 → 7.2.5 (replaced `SignedIn` / `SignedOut`, swapped `afterSignInUrl` / `afterSignUpUrl` for `signInFallbackRedirectUrl` / `signUpFallbackRedirectUrl`); `lucide-react` 0.468.0 → 1.9.0 with an inline `GithubIcon` SVG replacing the removed brand icon; `tailwindcss` 3.4.19 → 4.2.4 via the official `@tailwindcss/upgrade` codemod (rewrote `globals.css` to `@import "tailwindcss"` + `@theme {}`, swapped to `@tailwindcss/postcss`, dropped `autoprefixer`, mechanical class renames `shadow-sm` → `shadow-xs`, `outline-none` → `outline-hidden`, `flex-shrink-0` → `shrink-0`, etc., `tailwindcss-animate` wired via `@plugin`); `eslint` 9.39.2 bump deferred pending `eslint-plugin-react` peer-range update (#24)

### Fixed

- `frontend/run-prod-clerk-local.sh` now preserves `PATH` when re-execing itself via `sudo`, so the documented `cd frontend && sudo rm -rf .next && ./run-prod-clerk-local.sh` flow works on systems where `pnpm` lives on a user-managed path (e.g. nvm) (#22)

---

## [2026-04-22]

### Added

- Per-file expanded S3 layout for task files alongside the canonical tarball: new `TASK_EXPAND` `WorkerJobKind`, alembic migration `c4b5a6d7e8f9` adding nullable `expanded_at` / `expanded_manifest_key` on `task_versions`, `task_expand_handler.py` worker with semaphore-bounded per-member uploads + 30s heartbeats, `tasks_expand_archive` / `tasks_expand_max_bytes` / `tasks_expand_max_member_bytes` / `tasks_archive_cache_mb` settings, and `StorageClient.upload_bytes`; UI reads from the expanded layout by default and falls back to the archive for in-flight expansions or legacy versions (#13)
- `StorageClient` bytes+parsed-members cache per archive ETag (default 256 MB) so a listing + content click on the same version reuses one download and one tarball parse; backend returns `ETag` + `Cache-Control: private, max-age=86400, immutable` and 304s on `If-None-Match` when `version` is pinned (#13)
- Local-storage preflight on Harbor worker startup that validates free bytes, inode headroom, and a create/write/delete probe against both `harbor_jobs_dir` and the active temp root (#14)
- Temp-dir cleanup when S3 hydration fails before Oddish falls back or raises, and pruning of empty Harbor parent directories after trial artifact upload cleanup (#14)
- Clickable Task column header on the experiment trials table cycling `default → name A→Z → name Z→A` with `ArrowUpDown` / `ArrowUp` / `ArrowDown` indicators; sort layers on top of the existing search filter so virtualization and row selection pick it up unchanged (#19)
- Per-PR Modal preview webhook subdomains: `@modal.asgi_app(label=...)` label now derives from `MODAL_APP_NAME` (`"api"` for production, `"{app}-api"` for previews like `oddish-pr-19-api`) so concurrent PR previews no longer collide on `abundant-ai-preview--api.modal.run` (#20)

### Fixed

- Harbor temp-root preflight now only probes `tempfile.gettempdir()` when `harbor_config.docker_image` or `harbor_config.mcp_servers` requires task patching; previously a constrained `/tmp` rejected valid trials that never needed temp patching (#16)
- `oddish` sdist packaging: the `pyproject.toml` `include` override that restricted the sdist to `src/oddish/analyze/*.txt` is removed, so `pip install oddish` from sdist now ships the full package instead of an empty shell; regression test asserts `src/oddish/__init__.py` and `src/oddish/cli/__init__.py` are present in built sdists (#18)

---

## [2026-04-17]

### Changed

- Pass@K graph tooltip replaced with a custom recharts `content` renderer: entries sorted by pass rate descending to match the visual line order, agent labels shown with color-indicator squares, values formatted as percentages with one decimal, card styling with max-height and scrolling for many agents (#8)

---

## [2026-04-16]

### Changed

- Heavy-run preset bumped from Claude Opus 4.6 to Opus 4.7 (#12)

---

## [2026-04-09]

### Added

- Strict `/tasks/upload/init` + `/tasks/upload/complete` handshake so `oddish run` reserves task/version metadata, uploads task archives directly to S3 via presigned `PUT`, and finalizes the version without proxying bytes through the API; `oddish pull` likewise prefers presigned trial-file URLs and presigned-archive downloads (#11)

### Removed

- Legacy proxied `/tasks/upload` flow; the CLI now fails fast when direct upload is unavailable instead of silently falling back (#11)
- `ODDISH_S3_ENABLED` setting and persistent local task-storage branches — S3-compatible storage is now required for task/artifact storage; self-hosting docs updated accordingly (#11)

---

## [2026-04-07]

### Added

- Org-scoped `/tasks/browse` backend endpoint with latest-version task aggregates, experiment lists, compact latest-version trial rows, search, and pagination (#10)
- Clerk-authenticated frontend API proxy and shared task-browser response types (#10)
- `/tasks` page rendered as a card grid with latest-version trial status graphics, debounced search, SWR polling, skeleton/loading states, and a Tasks nav link (#10)

---

## [2026-04-02]

### Changed

- Experiments view replaces the manual `LOAD MORE` button (10 tasks/page) with a two-phase progressive loader: phase 1 fetches all tasks at once via `include_trials=false` so the list appears instantly; phase 2 streams trial data in 50-task batches via `include_trials=true&compact_trials=true`, progressively filling trial status icons with a subtle "Loading trials 50/200…" header indicator (#7)

---

## [2026-03-27]

### Changed

- Backend module restructure: split the monolithic `backend/worker.py` into a `worker/` package (`functions.py` for the Modal dispatcher / spawn orchestration, `runtime.py` for Modal runtime patching and storage setup, `github.py` for GitHub notification hooks around shared queue execution); extract hosted-only environment policy into `backend/cloud_policy.py` (`ALLOWED_CLOUD_ENVIRONMENTS`, `get_default_cloud_environment`, `enforce_trial_environment`); move public-API helpers into `oddish.api.public_helpers`; drop the now-unowned `queue_slots` table from `backend/models.py` and stub its migration (#5)
- No-op tweak to `.github/workflows/modal-preview.yml` to exercise the shared Modal `preview` environment plus per-PR app-naming end-to-end on a real PR (#3)

---

## [2026-02-26]

### Added

- Monorepo restructure with `oddish/` (core Python package, published to PyPI), `backend/` (Modal-hosted API + worker orchestration with multi-tenant Clerk/API-key auth, org-scoped data, and queue-key concurrency), and `frontend/` (Next.js dashboard); two-stack alembic migrations (`oddish/alembic/` for core, `backend/alembic/` for cloud auth tables); cloud auth schema including `organizations`, `users` (with Clerk + Supabase user-id columns), `api_keys` (scoped `full` / `tasks` / `read`), with FKs adding `org_id`, `created_by_user_id` onto `tasks`; pre-commit pipeline covering ruff, black, mypy, prettier, and eslint across `backend|oddish` and `frontend` paths (#1)

---
