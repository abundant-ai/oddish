# Bounded Experiment Loading Implementation Plan

## Goal

Replace the authenticated and public experiment-page read paths with one bounded
core model. Opening an experiment must fetch only the metadata, exact summary,
task shells, and visible slim trials needed for the first paint. Full trial
analysis, phase timing, results, Harbor configuration, and error bodies remain
on the existing trial-detail resources and must not enter the experiment-page
payloads.

The measured production baseline for an 80-task, 401-trial public share was a
1,287,753-byte decoded response and 3.4-6.0 seconds wall time. The `trials`
field alone accounted for 1,162,667 bytes. The replacement `/open` response
must stay under 50 KB, use at most five SQL statements, and return at most 100
task shells. Each `/trial-page` response must return at most 250 slim trials
and enforce an explicit serialized-byte limit.

## Ownership

`oddish/src/oddish/core/endpoints/experiment_open.py` owns the shared read
rules and response construction. Its `ExperimentReadScope` contains:

```python
experiment_id: str
org_id: str | None
audience: Literal["member", "public"]
model_display_names: dict[str, str]
```

The scope owns the security boundary. Member reads may include member-visible
tags, stored model names, and internal cost labels. Public reads include only
PUBLIC tags, apply experiment model aliases, and omit internal fields. Route
handlers resolve access and then call the same core functions; they do not
repeat query or redaction logic.

`frontend/src/components/experiment-page-client.tsx` owns the browser resource
lifecycle for both access modes:

```tsx
type ExperimentPageAccess =
  | { kind: "member"; experimentId: string }
  | { kind: "public"; token: string };
```

The component derives endpoint paths and capabilities during render. It owns
the `/open` SWR resource, visible `/trial-page` resources, deferred member cost,
and active-experiment revision polling. The route files only construct the
access value and provide route-shell data such as the authenticated org role.

## Delivery Steps

### 1. Core contracts and access resolution

- Add `ExperimentOpenResponse`, `ExperimentTrialPageResponse`, summary, task
  shell, slim-trial, and opaque cursor schemas.
- Resolve an authenticated experiment ID after checking organization scope.
- Resolve a public token only when `experiments.is_public` is true.
- Load experiment model-display aliases once while resolving the scope.
- Return HTTP 404 for an experiment outside the caller's organization and for
  an unpublished or unknown public token.

### 2. Explicit SQL projections

- Refactor `list_experiment_task_shells_core` and
  `list_experiment_slim_tasks` so the bounded readers reuse their effective
  version and membership rules without ORM relationship hydration.
- Select scalar task and trial columns only. Do not use
  `selectinload(TaskModel.trials)` or `selectinload(TaskModel.experiments)`.
- Apply direct and gathered experiment membership, effective task version,
  `is_probe = false`, `superseded_by_trial_id is null`, and soft-delete rules
  in SQL.
- Compute the exact summary separately from paged rows so pagination cannot
  change task/trial counts or scores.
- Use stable cursor order `(task.created_at DESC, task.id DESC)` for task pages.
  A trial page is bounded by complete task rows so the frontend never receives
  half of one task's grid row; stop before the next task when adding it would
  exceed 250 trials or the byte limit.

### 3. Route adapters

- Add authenticated `GET /experiments/{experiment_id}/open` and
  `GET /experiments/{experiment_id}/trial-page` routes in the hosted backend.
- Add unauthenticated `GET /public/experiments/{public_token}/open` and
  `GET /public/experiments/{public_token}/trial-page` routes in the shared
  public router, which is also used by the standalone server.
- Keep `task-shells`, `slim-tasks`, and the public full-task route during the
  cutover. Delete them only after both pages use the new contract.
- Record backend phase timing for access resolution, exact summary, task page,
  tag projection, response construction, and total serialization.

### 4. Proxy response forwarding

- Add one public proxy helper that never reads Clerk authentication.
- Share only the private response-forwarding function with the authenticated
  proxy helper.
- Forward trace headers upstream and preserve `Server-Timing`, cache,
  content-type/content-length where valid, and trace headers downstream.
- Pass response bodies through without parsing and reserializing JSON.
- Add `/api/experiments/[experiment]/open`,
  `/api/experiments/[experiment]/trial-page`,
  `/api/public/experiments/[token]/open`, and
  `/api/public/experiments/[token]/trial-page` route handlers.

### 5. Shared React owner and public cutover

- Add `ExperimentPageClient` with the member/public access union.
- Fetch `/open`, then enable the first `/trial-page` key.
- Merge task shells and loaded trial pages with
  `mergeExperimentTaskPages` in `useMemo`; that helper remains the owner of the
  effective-version conflict rule.
- Load the next page from an `IntersectionObserver` attached to a visible
  sentinel or from an explicit user action. The observer Effect owns cleanup.
- Derive public/read-only and member/manage capabilities from `access` and org
  role; do not copy them into state.
- Cut `/share/[token]` over and delete
  `frontend/src/lib/public-experiment-tasks.ts` after its final caller is gone.

### 6. Authenticated cutover and cost isolation

- Cut the authenticated experiment route over to the same client owner.
- Remove the Effect that advances `useSWRInfinite` until every task page is
  loaded.
- Remove the timer that refreshes every loaded page and the cost response.
- Enable the member-only cost SWR key only after the first trial page has
  rendered.
- Keep completed experiments idle. For active experiments, poll a small
  revision resource and revalidate only `/open` and currently visible trial
  pages when the revision changes.
- Do not add a durable cost projection in this change. Measure isolated
  `cost-totals` after the first-load contention is removed.

### 7. Legacy deletion and documentation

- Delete `GET /public/experiments/{token}/tasks` after all browser callers use
  `/open` and `/trial-page`; preserve task-detail and trial-detail public APIs.
- Delete authenticated `task-shells` and `slim-tasks` only after checking CLI,
  tests, and non-page consumers.
- Remove duplicate Next.js proxy implementations replaced by the public helper.
- Update `AGENTS.md` and `backend/README.md` because API contracts and frontend
  proxy structure changed.

## Verification

Backend tests must prove:

- `/open` serializes below 50,000 bytes for a fixture with at least 100 tasks
  and 500 trials.
- `/open` executes no more than five SQL statements.
- `/trial-page` returns no more than 250 trials and stays below its declared
  byte limit.
- Neither contract contains `analysis`, `phase_timing`, `result`,
  `harbor_config`, or full error text.
- Member and public routes return identical task IDs, trial IDs, and effective
  task-version IDs for the same experiment.
- Public output contains only PUBLIC tags and aliased model names; stored model
  names and cost-exclusion labels cannot be recovered from the JSON.
- Probe, superseded, deleted, off-version, and unrelated experiment trials are
  absent.
- Direct and gathered experiment trials follow the existing membership rule.

Frontend tests must prove:

- A completed public share makes one `/open` request and one first
  `/trial-page` request, with no timer-based follow-up.
- Later pages load only after the sentinel becomes visible or the user requests
  them.
- Member cost is not requested before the first trial page paints.
- Active revision changes revalidate visible data but do not fetch pages that
  were never opened.
- Public controls cannot retry, delete, publish, edit, or reveal private model
  names.
- Every new proxy preserves `Server-Timing` and trace headers.

Local verification order:

1. Targeted Python unit/API tests for the new readers and routes.
2. Python lint/type checks for changed core and router files.
3. Targeted frontend unit tests for merging, pagination, polling, and proxy
   headers.
4. Frontend TypeScript and lint checks.
5. Docker-backed API integration test with a large seeded experiment.
6. Browser test of one completed share and one active authenticated experiment,
   recording request count, decoded bytes, time to first grid, and
   `Server-Timing` headers.

## Commit Boundaries

1. `Add bounded experiment open and trial-page readers`
2. `Forward bounded experiment responses through public proxies`
3. `Share experiment page loading across member and public routes`
4. `Remove eager experiment pagination and legacy public loading`

Each commit must leave its introduced route covered by tests. The old API stays
available until the browser cutover commit, so intermediate commits remain
deployable and reversible.

## Status

- [x] Create `codex/bounded-experiment-loading` from `5f58ed64`.
- [x] Preserve the prior performance-history plan at
  `docs/performance-history-and-regression-tracking-plan.md`.
- [x] Add core schemas, scope resolution, and bounded SQL readers.
- [x] Add member/public backend route adapters and tests.
- [x] Add pass-through proxy helpers and route handlers.
- [x] Add `ExperimentPageClient` and cut public shares over.
- [x] Cut authenticated experiments over and defer cost.
- [x] Delete legacy page-loading routes and the one-use public array wrapper.
- [x] Run backend, frontend, Docker, and browser verification.
