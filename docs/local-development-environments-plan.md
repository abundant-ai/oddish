# Local Development Environments and Representative Data Plan

## Status

- Implementation branch: `feat/isolated-local-dev-cells`
- Base: `staging` at `70cff19d`
- Scope: local full-stack environments, representative development data,
  deterministic product scenarios, shared local/CI browser testing, and a
  narrower remote-preview role
- Phase 1 implementation is in progress: the launcher, data services, migrations,
  backend readiness, rendered-frontend failure detection, and owned cleanup
  have been exercised. Valid-auth startup and concurrent two-worktree
  acceptance remain. Representative data and scenarios remain later phases.

## Outcome

An engineer or coding agent should be able to create a feature worktree and run
one command:

```bash
git worktree add .worktrees/capabilities \
  -b kate/capabilities origin/staging
cd .worktrees/capabilities
./scripts/dev up --data synthetic --scenario capabilities-stale
```

The command must produce an isolated, populated Oddish stack with a stable app
URL, deterministic identities, useful logs, and a Playwright-ready scenario.
Normal development must not depend on a Modal deploy, a Vercel deploy, a full
staging database copy, or real LLM calls.

The target feedback loop is:

```text
feature worktree
  -> isolated local stack
  -> representative base data
  -> deterministic scenario overlay
  -> Playwright trace/screenshots
  -> ordinary CI
  -> one final remote preview when review needs it
  -> merge queue
  -> staging
```

## Why This Work Is Needed

The current repository has the necessary pieces, but they live in separate
paths:

- `.github/workflows/e2e-dashboard.yml` starts Postgres, seeds one task, starts
  the backend and frontend, signs in through a Clerk development instance, and
  runs Playwright.
- `backend/tests/e2e/seed_dashboard.py` creates a minimal dashboard fixture.
- `backend/preview_seed.py` selects a referentially complete production subset
  for remote PR previews.
- `.github/scripts/preview/bootstrap_preview_db.py` snapshots production-shaped
  schema and migration pointers before exercising new migrations.
- `.github/scripts/staging/refresh_staging_db.py` mirrors all production data to
  staging, including multi-gigabyte table spooling and runtime quiescence.
- `deploy/docker/docker-compose.yml` runs the self-hosted API, Postgres, and
  worker topology, but is not a feature-worktree development launcher.

The latest capability-analysis work exposed the cost of this fragmentation.
PRs #1239, #1240, #1242, #1248, #1251, and #1252 changed queued generation,
stale results, evidence links, eligibility, missing summaries, public sharing,
and experiment scoping across several layers. PR #1249 then removed the large
experiment radar. Each slice had focused validation, but there was no cheap,
named, full-stack state matrix available before remote preview deployment.

## Product and Engineering Principles

1. **A worktree owns one complete dev cell.** Its database, object store, ports,
   process IDs, logs, browser origin, and generated identifiers move together;
   the current branch is metadata, not resource identity.
2. **Database size is not coverage.** The default data set deliberately covers
   product states and realistic cardinalities instead of copying every staging
   row.
3. **Synthetic data is the default.** It is deterministic, safe, fast, and
   available without production or staging credentials.
4. **Sampled data crosses a trust boundary.** It is selected and sanitized in a
   controlled CI job, validated, and then published as a private immutable
   artifact. Developer laptops do not query production to build it.
5. **Full clones stay remote.** A full-size provider-side scratch branch is an
   explicit, short-lived escape hatch for migration or query-plan work, not the
   normal local loop.
6. **Scenarios are overlays, not alternate applications.** They manipulate the
   real database/object-store state and exercise the ordinary product UI and
   API.
7. **Fail loudly.** A missing sampled snapshot, invalid manifest, migration
   mismatch, occupied port, or missing Clerk credential is an error. The
   launcher must not silently choose another path or fall back to a different
   data source.
8. **One source of truth.** Runtime state lives in one per-worktree state file;
   scenario IDs live in the scenario result; React derives presentation from
   API data rather than mirroring it in local state.

## User-Facing Commands

The root entrypoint is one executable, `scripts/dev`. It owns dev-cell
lifecycle and invokes database-specific operations through the backend Python
environment.

```bash
# Validate required binaries, versions, credentials, and ports.
./scripts/dev doctor

# Start the standard synthetic environment.
./scripts/dev up

# Choose the base data and an optional deterministic overlay.
./scripts/dev up --data synthetic --scenario capabilities-stale
./scripts/dev up --data sampled --scenario public-share-capabilities
./scripts/dev up --data synthetic-large

# Inspect or operate the current worktree's cell.
./scripts/dev status
./scripts/dev logs backend
./scripts/dev logs frontend
./scripts/dev open
./scripts/dev test capabilities-stale

# Reset data without rebuilding dependencies or changing ports.
./scripts/dev data reset
./scripts/dev scenario apply capabilities-stale
./scripts/dev scenario advance summary-ready

# Stop only this worktree's processes and containers.
./scripts/dev down

# Fetch an approved sanitized snapshot.
./scripts/dev data pull-sample

# Rare, privileged, remote full-fidelity path.
./scripts/dev data remote-clone create --ttl 24h
./scripts/dev data remote-clone destroy
```

Commands always target the current worktree. There is no `--repo` argument and
no search through candidate directories.

## Dev Cell Ownership

### Stable identity

`scripts/dev` derives a cell ID from the absolute worktree path and records the
current branch as metadata, for example:

```text
worktree: /Users/kyle/Desktop/oddish/.worktrees/capabilities
branch:   kate/capabilities
cell:     capabilities-a81f
```

The derived cell ID names the Compose project, volumes, logs, unique
`<cell>.localhost` browser origin, and state directory. The state file stores
resolved ports so they remain stable across branch switches and restarts:

```text
.oddish/dev/state.json
.oddish/dev/lifecycle.lock
.oddish/dev/logs/backend.log
.oddish/dev/logs/frontend.log
```

`.oddish/` is already ignored.

### Isolated resources

Each cell owns:

- one Compose project;
- one Postgres volume and database;
- one MinIO volume and bucket;
- one backend process and port;
- one frontend process and port;
- one browser origin, with cookies isolated from every other cell;
- one process-state file and log directory.

The launcher selects ports deterministically from the cell hash, verifies that
the selected ports are available, and records them. If a port is occupied by a
different process, startup fails with the exact conflict. It does not probe a
list of alternative ports.

### Configuration boundary

The launcher reads developer-only secrets from one documented location:

```text
~/.config/oddish/dev.env
```

`ODDISH_DEV_ENV_FILE` may explicitly replace that path. The launcher does not
probe `.env`, `.env.local`, parent directories, or other candidate files.

Required Clerk development-instance values are:

```dotenv
E2E_CLERK_EMAIL=
E2E_CLERK_ORG_ID=
CLERK_SECRET_KEY=
NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY=
CLERK_DOMAIN=
```

There is no local authentication bypass. The seeded organization is bound to
`E2E_CLERK_ORG_ID`, matching the existing authenticated dashboard E2E path.

## Direct Implementation Shape

Initial files:

```text
scripts/dev                              # executable Python lifecycle owner
deploy/dev/docker-compose.yml            # Postgres + MinIO only
backend/devdata/__main__.py               # data command boundary
backend/devdata/synthetic.py              # deterministic base generator
backend/devdata/sample.py                 # external sample + sanitization boundary
backend/devdata/scenarios.py              # named overlays and transitions
backend/devdata/validation.py             # export/restore safety gates
backend/tests/devdata/                     # focused data-contract tests
frontend/e2e/scenarios/                    # browser journeys against real UI
.github/workflows/dev-dataset.yml          # controlled sampled snapshot producer
```

These modules own different invariants. Do not split them into managers,
providers, adapters, factories, repositories, or one-file-per-scenario wrappers
until a concrete second implementation requires that ownership.

`scripts/dev` directly runs Docker, Alembic, the backend, the frontend, and
Playwright. It may use small functions for lifecycle/resource-cleanup rules, but
must not build a wrapper chain around `subprocess.run`.

Validate unknown data only at boundaries:

- environment file input;
- CLI arguments;
- snapshot manifest and checksum;
- rows arriving from staging;
- process output used to discover readiness or IDs.

After those boundaries, use typed internal values without repeated parsing.

## Base Data Tier 1: Synthetic

Synthetic data is the default and must require no network access after package
installation.

### Profiles

| Profile | Organizations | Users | Experiments | Tasks | Trials | Purpose |
|---|---:|---:|---:|---:|---:|---|
| `synthetic-small` | 1 | 5 | 4 | 40 | 300 | quick backend work |
| `synthetic` | 3 | 25 | 20 | 500 | 7,500 | normal full-stack work |
| `synthetic-large` | 8 | 150 | 100 | 10,000 | 100,000 | pagination and query work |

The exact counts are source constants in `synthetic.py`, not repeated in a
manifest and a React view. Tests assert required ranges and state coverage.

### Required state coverage

The standard profile includes:

- task statuses: completed, failed, queued/running representations where the
  product supports them, and soft-deleted rows;
- task versions: current default, newer non-default, and historical versions;
- experiment membership across current and historical versions;
- trial statuses: success, failure, skipped, queued, running, retrying,
  cancelled, superseded, probe, and soft-deleted;
- agents and models across the supported provider prefixes;
- populated and missing verifier rewards/metrics;
- live cost checkpoints and settled token/cost totals;
- QA verdict states: absent, queued, running with a preserved published result,
  success, cancelled replacement, and terminal failure;
- trajectory summaries with current schema, older schema, and missing summary;
- capability analyses: missing, queued, running, stale-while-rebuilding,
  successful, failed, and experiment-scoped;
- public and private experiments with valid membership boundaries;
- direct task/version/experiment tags and exclusions;
- skills, documents, API attribution, quotas, cost series, and leaderboard
  identities;
- enough rows for pagination, virtualization, filtering, and `any`/`all`
  metric predicates.

All active work is inert by default:

- API keys are absent;
- workers are disabled;
- queued/running rows are display fixtures only;
- no external LLM or sandbox call can start;
- notification integrations are disabled.

### Artifact coverage

MinIO receives deterministic, synthetic objects for selected tasks/trials:

- task bundles and listings;
- normal and structured logs;
- ATIF trajectories;
- result and verifier artifacts;
- trajectory summaries;
- analyzer outputs and evidence targets.

Do not copy production or staging S3 objects into local development.

### Determinism

The generator accepts one integer seed. IDs, timestamps, values, and object
keys are stable for the same schema, generator version, profile, and seed.
Timestamps are anchored to a fixed epoch and distributed relative to it; tests
must not depend on wall-clock time.

## Base Data Tier 2: Sanitized Sample

The sanitized sample provides real distributions and strange-but-valid graph
shapes without copying the enormous staging database.

### Selection

Reuse the graph-selection lessons from `backend/preview_seed.py`:

- recent experiments;
- deterministic pseudo-random experiments;
- per-owner anchors;
- bounded extra tasks;
- bounded trials per experiment;
- referenced task versions, users, organizations, tags, assignments, skills,
  documents, and terminal jobs;
- explicit handling of known FK back-edges.

The local exporter must not import `sample_prod_subset` unchanged because that
function uses `SELECT *` and preserves user/organization values for a controlled
preview environment. Extract shared graph-selection rules only when doing so
creates one real source of truth; otherwise keep the local sampler separate and
explicit.

### Generation boundary

Only `.github/workflows/dev-dataset.yml` may generate the distributed sampled
snapshot. It runs on a schedule and by manual dispatch with a read-only staging
credential. Developer laptops download the finished artifact; they do not read
staging directly.

Generation steps:

1. Create an empty temporary Postgres database.
2. Restore current staging-shaped schema and Alembic pointers.
3. Select a referentially complete bounded graph from staging.
4. Sanitize every selected row before insertion into the temporary database.
5. Generate synthetic MinIO-compatible artifacts; do not fetch staging S3.
6. Apply both Alembic stacks to repository head.
7. Run data-safety, FK, model-schema, and state-coverage validation.
8. Export a compressed Postgres custom-format dump.
9. Publish the dump and signed manifest to private object storage.

### Sanitization contract

The sampler uses explicit column allowlists. `SELECT *` is forbidden.

It must:

- deterministically remap user, organization, Clerk, GitHub, and Slack
  identities;
- replace names and emails with `@example.test` identities;
- remove API keys, hashes, credentials, webhook secrets, and integration
  tokens;
- regenerate public share tokens;
- clear worker ownership, queue slots, remote handles, and idempotency claims;
- neutralize active jobs and disable notifications;
- replace task instructions, document bodies, prompts, logs, and arbitrary text
  that may contain customer or secret material;
- allowlist safe JSON fields instead of recursively guessing which values are
  secrets;
- preserve status, cardinality, timestamps, cost/token distributions, model
  identifiers, and relationships needed to reproduce product behavior.

Opaque source IDs are deterministically HMAC-remapped before export so the
snapshot cannot be joined back to staging by possession of a raw ID.

### Required validation

Publication fails unless all checks pass:

```text
api_keys = 0
nonterminal executable worker_jobs = 0
queue slot leases = 0
remote handles = 0
notification destinations = 0
non-example.test emails = 0
known secret-pattern matches = 0
broken foreign keys = 0
model/schema mismatches = 0
required product-state categories missing = 0
snapshot size above configured budget = 0
```

The snapshot manifest contains:

```json
{
  "format_version": 1,
  "source": "staging-sanitized",
  "generated_at": "2026-08-13T00:00:00Z",
  "oddish_alembic_head": "...",
  "backend_alembic_head": "...",
  "model_fingerprint": "...",
  "sanitizer_version": 1,
  "selection_seed": "weekly-2026-33",
  "row_counts": {},
  "sha256": "..."
}
```

`pull-sample` verifies the checksum and manifest before touching Postgres. A
schema mismatch fails with a command to fetch/build a compatible snapshot; it
does not silently migrate an unknown snapshot or fall back to synthetic data.

## Base Data Tier 3: Remote Full Clone

A full clone is technically possible but is not copied to each laptop. The
existing staging mirror can spool tens of gigabytes, preserves operationally
sensitive shapes, and takes long enough to defeat the local loop.

For the rare case requiring full cardinality, `remote-clone create` should use
an isolated provider-side scratch branch when the database provider supports
the required branch semantics. Otherwise this tier remains unavailable until
a safe server-side snapshot/restore path exists; the CLI must not fake it by
performing a local full dump.

Creation must:

- require explicit privileged credentials;
- name the branch with owner, worktree cell, and expiry;
- apply a maximum 24-hour TTL by default;
- purge API keys and idempotency claims;
- quiesce jobs, slots, workers, notifications, and remote handles;
- rotate or remove public tokens;
- start the local backend with workers disabled;
- record the remote branch ID in the cell state for deterministic cleanup.

This tier is for migration rehearsal, real-cardinality query plans, and bugs
that cannot be reproduced with sampled or synthetic data. It is not used by
Playwright CI.

## Snapshot and Reset Performance

Dataset cache keys derive from:

```text
oddish Alembic head
backend Alembic head
model fingerprint
generator or sanitizer version
profile
seed
```

Cached artifacts live outside worktrees:

```text
~/.cache/oddish/dev-data/<cache-key>.dump
~/.cache/oddish/dev-data/<cache-key>.manifest.json
```

The first synthetic build runs migrations, generates data, validates it, and
writes a custom-format dump. Later cells restore it with parallel `pg_restore`.

Within each cell, Postgres keeps:

```text
oddish_base    # immutable restored/generated base
oddish_dev     # disposable database used by the app
```

`data reset` disconnects clients, drops `oddish_dev`, recreates it from
`oddish_base`, and reapplies the selected scenario. It never rebuilds package
dependencies or chooses a new port.

## Scenario Overlays

Scenarios are named data mutations over either synthetic or sampled bases.
They return a typed result containing stable IDs and the starting path:

```json
{
  "scenario": "capabilities-stale",
  "task_id": "scenario-capabilities-task",
  "trial_id": "scenario-capabilities-trial",
  "experiment_id": "scenario-capabilities-experiment",
  "entry_path": "/tasks/scenario-capabilities-task"
}
```

Initial scenarios:

1. `capabilities-missing-summary`
   - completed, fetchable trajectory;
   - no trajectory summary;
   - API returns pending generation state;
   - a deterministic transition publishes the summary.
2. `capabilities-stale`
   - successful published capability payload;
   - replacement analysis is running;
   - stale payload remains visible;
   - transition replaces it successfully.
3. `public-share-capabilities`
   - overlapping tasks across two experiments;
   - one public token;
   - experiment-scoped analysis/cache rows;
   - evidence links and model chips resolve only inside the shared experiment.
4. `task-version-pivot`
   - current default differs from the newest version;
   - an experiment contains historical trials;
   - shell, detail, files, and trial pivots remain consistent.
5. `large-experiment-navigation`
   - enough tasks and trials to exercise pagination, virtualized lists, drawer
     navigation, and request-count assertions.

Transitions are explicit CLI actions:

```bash
./scripts/dev scenario advance summary-ready
./scripts/dev scenario advance analyzer-failed
```

They update the real local database/object store. Playwright does not intercept
API responses to simulate backend behavior, and no real analyzer/LLM runs.

Do not introduce a scenario plugin framework. A single explicit registry in
`scenarios.py` is sufficient until multiple independently owned scenario
packages actually exist.

## Frontend and React Constraints

No React dev-tools dashboard is part of the initial implementation. The CLI,
cell state file, scenario result, and Playwright report already provide the
needed control and observability. A new UI would duplicate state and add a
second control path.

Production React components must not gain dev-only props, effects, context, or
environment branches. Scenarios create backend states and exercise the same
components users see.

When frontend changes are needed for the scenarios or for genuine product
fixes, review them with these constraints:

- derive labels, counts, filtered lists, and statuses from props/API state
  during render;
- do not mirror API values into component state unless the component owns an
  intentional editable draft;
- represent async lifecycle with one status value rather than contradictory
  booleans;
- use effects only for synchronization with external systems such as polling,
  timers, subscriptions, or browser APIs;
- abort or invalidate stale async work;
- keep event-driven behavior in handlers;
- use refs only for DOM/process handles and non-render state;
- use stable domain IDs for list keys;
- do not add single-use wrapper components or hooks that merely rename one
  expression/hook;
- do not add context to avoid shallow prop passing;
- do not add memoization unless measured cost or identity-sensitive consumers
  justify it.

Playwright should assert product behavior and visible state, not React
implementation details.

## Local and CI Browser Testing

`./scripts/dev test <scenario>`:

1. confirms the matching cell and scenario are active;
2. reads the scenario result and base URL from cell state;
3. uses the existing Clerk development-instance sign-in flow;
4. runs the tagged Playwright journey;
5. retains trace, screenshot, frontend log, and backend log on failure;
6. prints direct artifact paths.

The dashboard E2E workflow should call the same public commands:

```yaml
- run: ./scripts/dev up --ci --data synthetic-small --scenario dashboard
- run: ./scripts/dev test dashboard
- if: always()
  run: ./scripts/dev down
```

CI may still provision caches and secrets, but it must not duplicate stack
startup logic in YAML. A missing Clerk configuration must be reported as a
visible skip/non-required result or a failure for repositories expected to have
the secrets; it must not appear as a successful exercised E2E run.

## Remote Preview Reform

Local scenarios become the default feedback mechanism. Remote previews remain
useful for deployed-network, Modal, Vercel, and provider-integration behavior.

Split the current preview workflow into two concurrency domains:

### Stateful preview data

```yaml
concurrency:
  group: pr-preview-state-${{ github.event.pull_request.number }}
  cancel-in-progress: false
```

This path creates/repairs the preview database, runs migrations, and performs
an explicit data refresh. Trigger it when a preview is first requested, when
migrations change, or through manual dispatch.

### Replaceable app deployment

```yaml
concurrency:
  group: pr-preview-app-${{ github.event.pull_request.number }}
  cancel-in-progress: true
```

This path deploys the latest Modal backend and Vercel frontend against the
existing PR database. A newer commit cancels an obsolete app deployment without
interrupting stateful database work.

Draft PRs run local-equivalent CI without provisioning a remote preview. A
`needs-preview`/ready-for-review signal creates the preview. The merge queue
rebases once and runs one final preview smoke against the merge candidate.

## Implementation Sequence

### PR 1: Dev-cell launcher

Changes:

- add `scripts/dev`;
- add Postgres/MinIO development Compose file;
- implement cell ID, port ownership, state, logs, readiness, cleanup, and
  `doctor`;
- start the existing backend and frontend with workers/integrations disabled;
- document the one required credential file.

Exit criteria:

- two worktrees run concurrently without shared ports, volumes, PIDs, logs, or
  browser state;
- `down` cannot stop another cell;
- startup failure names the exact missing dependency/credential/resource;
- warm startup reaches a usable app in under 90 seconds on a normal developer
  machine.

### PR 2: Synthetic data and cached resets

Changes:

- implement the three synthetic profiles;
- seed Postgres and synthetic MinIO artifacts;
- add model/FK/state validation;
- add dump caching and per-cell template database reset;
- replace the one-task dashboard seed with the standard data command.

Exit criteria:

- required state coverage is asserted in tests;
- the same seed/profile produces stable IDs and counts;
- `data reset` completes in under 15 seconds for the standard profile;
- no code path can execute external work from fixture jobs.

### PR 3: Scenario overlays and shared Playwright path

Changes:

- implement the initial five scenarios and transitions;
- add tagged Playwright journeys;
- make local and CI tests call the same dev commands;
- retain traces/screenshots/logs with direct paths.

Exit criteria:

- each scenario starts and passes locally from a clean worktree;
- capability/share state transitions require no network or real model;
- ordinary product components contain no dev-only control paths;
- a coding agent can reproduce a failure with one command.

### PR 4: Sanitized sampled snapshot

Changes:

- add bounded staging graph selection with explicit columns;
- implement deterministic identity/text sanitization;
- add publication safety checks and manifest signing/checksums;
- add scheduled/manual dataset workflow;
- implement `pull-sample` and compatible restore.

Exit criteria:

- security/privacy review approves the allowlists and transformations;
- secret scanning and all validation gates pass before publication;
- sampled snapshot stays within the configured size/time budget;
- a developer without staging credentials can download and restore the approved
  artifact using existing repository authentication.

### PR 5: Preview concurrency split

Changes:

- separate stateful preview data from replaceable app deployment;
- preserve component-aware deployment planning;
- add explicit preview/data-refresh triggers;
- make app deployment cancel superseded commits;
- make the final merge-candidate preview the required remote acceptance step.

Exit criteria:

- a new PR commit never waits for an obsolete frontend/backend deployment;
- cancelling app deployment cannot interrupt database mutation;
- staging movement alone does not rebuild a PR database;
- latest deployed SHA and data-refresh status are visible in the PR summary.

### PR 6: Remote full-clone escape hatch, only if justified

Do this only after sampled data is in use and a documented class of bugs still
requires full cardinality.

Exit criteria:

- provider-side branch semantics are verified;
- TTL cleanup is automated and observable;
- all quiescence/privacy controls run before returning a connection string;
- no full staging dump is written to a developer laptop.

## Test Strategy

### Unit and contract tests

- deterministic cell ID and port resolution;
- state-file parsing at the disk boundary;
- process ownership and cleanup refusal for mismatched cells;
- seed determinism and profile state coverage;
- HMAC identity mapping and column allowlists;
- sanitizer rejection of unsafe values;
- snapshot manifest/checksum compatibility;
- scenario application, idempotence, transitions, and cleanup;
- source and destination FK closure.

### Integration tests

- blank database -> both migration stacks -> synthetic seed -> validation;
- dump -> restore -> row/state equivalence;
- template reset -> scenario overlay -> backend reads;
- MinIO object listing/read paths;
- workers disabled despite display fixtures in active-looking states;
- sampled export against a synthetic stand-in source database.

### Browser tests

- authenticated dashboard and task navigation;
- capability missing/stale/success/failure transitions;
- public experiment scoping and evidence links;
- current-version versus experiment-trial-version behavior;
- pagination/network shape on the large-navigation scenario.

### Operational tests

- concurrent cells in two linked worktrees;
- stale PID and interrupted-start recovery;
- occupied port refusal;
- checksum/schema mismatch refusal;
- `down` and CI cleanup after failed startup/test;
- remote preview cancellation during app deploy while database work continues.

## Anti-Indirection Review Gate

Before each implementation PR merges, inspect the diff for:

- one-line/single-use wrappers around subprocess, Docker, database, or scenario
  calls;
- `Manager`, `Factory`, `Provider`, or `Adapter` types without lifecycle,
  selection, or boundary ownership;
- candidate path/port/env probing instead of explicit selection;
- negative booleans that make lifecycle conditions difficult to read;
- fallback from sampled to synthetic data;
- internal `unknown` parsing after a validated boundary;
- repeated safety assertions that belong at the data/export boundary;
- stored counts/status booleans derivable from the manifest or process state;
- try/catch blocks that only rethrow or hide startup failure;
- tests preserving implementation wrappers rather than product behavior.

The preferred rewrite is deletion or inlining. Keep an abstraction only when it
owns a real invariant such as resource lifecycle, cleanup, sanitization,
external input validation, or scenario product semantics.

## Success Metrics

- warm `dev up`: less than 90 seconds;
- standard `data reset`: less than 15 seconds;
- named Playwright scenario: less than 2 minutes;
- two or more simultaneous worktree cells without collisions;
- zero real LLM/provider calls from synthetic and sampled profiles;
- zero unsafe sampled snapshots published;
- ordinary PR pushes receive useful local-equivalent CI without waiting for a
  remote preview;
- superseded remote app deployments stop instead of queueing;
- fewer follow-up fix/revert PRs caused by missed cross-layer UI states.

## Explicit Non-Goals

- cloning the full staging database into every worktree;
- copying production/staging S3 artifacts locally;
- adding a local auth bypass;
- implementing a general-purpose fixture/plugin framework;
- adding a React dev-tools dashboard in the first iteration;
- making local workers execute real trials or analyzer jobs by default;
- replacing targeted unit/integration tests with browser tests;
- making remote previews disappear entirely.

## Final Definition of Done

The work is complete when a new contributor or coding agent can start from a
fresh feature worktree, run one documented command, sign into a convincingly
populated isolated Oddish instance, reproduce a named cross-layer state, run a
deterministic browser journey with useful failure artifacts, and tear down the
cell without affecting any other checkout. Remote preview deployment should be
a deliberate final acceptance tool, not the primary development loop.
