# Changelog

All notable changes to Oddish are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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
