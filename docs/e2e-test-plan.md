# End-to-end test plan

This is a deliberately small end-to-end suite: a handful of cheap, high-signal
paths that exercise the real wire — a real server on a real port, the real
`oddish` CLI as a subprocess, real Postgres. Everything expensive (S3 uploads,
worker execution, Modal, authenticated browser flows) is deferred on purpose,
and each deferral says what it would cost to bring back. The whole suite runs
in ~15s against the dev container.

The runnable scaffold lives in `backend/tests/e2e/`. Read this doc first; it
explains *why* each choice is the cheapest one that still proves something.

---

## Q1 — We already have ASGITransport tests. Why aren't those end-to-end?

Look at `backend/tests/test_collections_route.py` or the `client` fixture in
`backend/tests/test_github_linkage_gate.py`: both build `create_app()` and drive
it through `httpx.ASGITransport(app=app)`. That is a fast, honest integration
test — but it is not end-to-end. Sit with what it skips:

- **No TCP socket.** ASGITransport calls the app object in-process. Nothing is
  ever serialized over a wire, so a bug in how uvicorn frames requests, or how
  the CLI's `httpx.Client` talks to a real port, is invisible.
- **No real uvicorn lifespan.** Startup/shutdown events, the import-time engine
  + asyncpg pool, the process-level env — none of it runs the way production
  runs it.
- **No subprocess CLI.** The tests call route handlers directly. They never
  prove that `oddish run` reads `ODDISH_API_URL`, builds the right payload, and
  surfaces the response.
- **No serialized auth over the wire.** The header is a Python dict handed to a
  transport, not a `Authorization: Bearer …` string parsed off a socket.

So "end-to-end" here means exactly the three things ASGITransport cannot give
you: a real server process, the real CLI subprocess, and a real database
between them.

## Q2 — What is the smallest real stack?

The server. `backend/serve.py` is the vanilla-uvicorn entrypoint: it imports
`create_app()` from `backend/api/app.py` and binds to `PORT` (default 8000). No
Modal, no `modal serve`. We launch it as a subprocess on a free port.

Postgres. The documented dev container is enough:

```
docker run -d --name oddish-db \
  -e POSTGRES_USER=oddish -e POSTGRES_PASSWORD=oddish -e POSTGRES_DB=oddish \
  -p 5432:5432 postgres:16-alpine
```

S3. The five `ODDISH_S3_*` env vars can be dummies. `StorageClient` is lazily
initialized (`oddish/src/oddish/db/storage.py`), so the app boots fine without a
reachable bucket. The only code that touches S3 is the upload path — and Q4
explains why we never take it.

Clerk. `CLERK_DOMAIN` is never read on the API-key auth branch
(`backend/auth/__init__.py`, `backend/auth/verification.py`): an `ok_`-prefixed
key is hashed and matched against `api_keys` with no JWKS fetch. We still pass
`CLERK_DOMAIN=dummy` so nothing at import time complains.

## Q3 — How do you authenticate without Clerk?

You mint an API key in-process. `oddish.core.api_keys.create_api_key(org_id,
name)` returns `(APIKeyModel, raw_key)`; you persist the model with a session and
hand the raw key to the CLI as `ODDISH_API_KEY`. The default scope is `FULL`,
which satisfies both the `READ` scope that `/tasks/browse` and `GET /tasks/{id}`
require and the `TASKS` scope the sweep route requires
(`backend/api/routers/tasks.py`).

Why seed the key in the DB instead of calling `POST /api-keys`? Because that
route is locked to Abundant-org Clerk users — there is no HTTP-only bootstrap.
Seeding through the database is the intended pattern; `backend/tests/
test_github_linkage_gate.py` does exactly this with its `_seed_key` /
`_seed_tasks_key` helpers.

## Q4 — What makes a submitted run cost nothing? (the core question)

A naive `oddish run ./task` would upload an archive to S3, validate a model,
poll for results, and eventually spend money executing a trial. We want the
submission path with none of that. Take it apart, one flag at a time:

- **`--task <seeded_id>` skips the upload phase entirely.** In `oddish/src/
  oddish/cli/run.py`, the `--task` branch sets `submit_targets` directly from the
  id and never calls the upload helpers. The sweep runs in *append* mode
  (`oddish/src/oddish/core/endpoints/sweep.py` checks the existing task), which
  never calls `resolve_task_storage`. No archive, no S3.
- **`--agent nop` skips model validation.** `oddish/src/oddish/schemas.py`
  requires a model for every agent *except* nop/oracle, so we can omit `--model`
  entirely. A real worker would route this to the cheap `nop_oracle` queue.
- **`--background` skips the watch loop.** Without it the CLI polls every ~2s for
  trial progress; with it, submit-and-return. (`--json` implies `--background`,
  so passing both is belt-and-suspenders.)
- **Omitting `--github-id` skips both linkage checks.** No `--github-id` means
  the CLI never runs its preflight `GET /github/linkage`, and the server's 403
  gate is a no-op when `github_id` is absent from the payload.
- **No worker process means the trial never runs.** With nothing claiming the
  queue, the `worker_jobs` row sits in `QUEUED` forever. The reconciler only
  reaps `RUNNING` rows with stale heartbeats.

So ask yourself: what is the terminal state of this trial? There isn't one —
and that is fine. The assertion is about *submission*, not execution: a trial
row exists and a `QUEUED` worker job points at it.

---

## The paths

**P0 — liveness.** There is no `/health` endpoint. So what is the cheapest
readiness probe? `GET /public/experiments`
(`oddish/src/oddish/core/sharing/public.py`) needs no auth and returns 200. Note
the shape: it returns a bare JSON list `[]`, not `{"items": []}`. That is the
readiness signal the server subprocess is polled against.

**P2 — CLI submit.** `oddish run --task <id> --agent nop --n-trials 1
--background --json` exits 0. Then assert directly in the DB: one `trials` row
for the task, and one `QUEUED` `worker_jobs` row (`subject_table='trials'`)
pointing at that trial.

**P4 — browser task menu (the dashboard `/tasks/[task_id]` page) — scaffolded,
unverified against a real Clerk instance.** The runnable skeleton now lives in
`frontend/e2e/` (`global-setup.ts`, `tasks-view.spec.ts`) plus
`frontend/playwright.config.ts` at the package root, `@playwright/test` +
`@clerk/testing` are installed, and the spec skips itself until its env gate is
satisfied — so a credential-less `pnpm exec playwright test` reports it SKIPPED
and exits 0. What it has *not* done is run green against a live Clerk dev
instance; nobody in this environment has the secrets or a test user, so treat
"it typechecks (`pnpm run typecheck:e2e`) and skips" as the current bar, not
"it passes."
Start with what the wall actually is: `frontend/src/
middleware.ts` calls `auth.protect()` on every non-public route, and the
`(app)` layout adds a client-side `RedirectToSignIn`. So what does that
middleware actually check — and what does Clerk give you to satisfy it without
a browser typing passwords? It checks for a valid Clerk session, and Clerk
ships a first-class way to mint one in tests. `@clerk/testing`
(`pnpm add -D @clerk/testing`) provides Playwright helpers: `clerkSetup()` in
global setup mints a Testing Token via the Backend API that bypasses bot
detection with no frontend code change, and

```ts
await clerk.signIn({ page, emailAddress: "qa+clerk_test@example.com" });
await page.goto(`/tasks/${taskId}`);
```

signs in programmatically — a server-side token that bypasses all verification
including MFA, no UI flow. It needs `CLERK_SECRET_KEY` set and one visit to an
unprotected page that loads Clerk first — which is exactly what
`e2e/tasks-view.spec.ts` does (`setupClerkTestingToken` → `goto("/")` →
`clerk.signIn` → `goto("/tasks")` → click the first task → assert the detail
page's stable "Agents" heading is visible). On development instances,
`+clerk_test` emails verify with the universal OTP `424242`, so the test user
needs no real mailbox.

So what's actually left, given both the scaffold *and* the CI wiring now exist?
Not code. `.github/workflows/e2e-dashboard.yml` boots the whole stack in one
runner — the postgres service, `serve.py`, and `pnpm dev` — seeds the org +
task through `backend/tests/e2e/seed_dashboard.py`, and runs this spec against
it. So the two prerequisites an earlier draft called out by hand — "a dev stack
already running at `E2E_BASE_URL`" and "a seeded `OrganizationModel` row" — are
now handled by CI; the seed even hands its task id to `E2E_TASK_ID` so the spec
jumps straight to the detail page.

What remains is two human inputs: five repo secrets, and a one-time Clerk
dev-instance setup (a test user with a `+clerk_test` email in an org, plus an
`oddish` JWT template carrying `org_id`/`org_role` claims — see
`SELF_HOSTING.md`). The five secrets:

- `E2E_CLERK_EMAIL` — the `+clerk_test` user's address.
- `E2E_CLERK_ORG_ID` — that user's Clerk org id; the seed writes it as *both*
  `OrganizationModel.id` and `clerk_org_id` (see the provisioning note below).
- `CLERK_SECRET_KEY` — the dev-instance `sk_test`; `clerkSetup()` mints the
  Testing Token from it and the spec's env gate keys off it.
- `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` — the matching `pk_test` the dev server
  boots Clerk with.
- `CLERK_DOMAIN` — the backend's Clerk domain (unused on the auth path this
  test exercises, but the gate requires it so the boot never runs half-configured).

On org auto-provisioning: it does *not* happen for an unknown org. A Clerk JWT
whose `org_id` has no local row resolves through `get_org_from_clerk_id`
(`backend/auth/provisioning.py`, keyed on `clerk_org_id`), which returns `None`
and the request is rejected — nothing is minted. That is why the seed is
explicit *and* why it sets `clerk_org_id`, not just `id`, to `E2E_CLERK_ORG_ID`.
The task row is needed regardless so the detail page has data — but an empty
task (zero trials) still renders the "Agents" heading (it shows "No trials for
this version yet."), so no trial row is seeded.

Absent the secrets the CI job is a *designed* no-op: a first gate step flips
`have_clerk=false` and every later step skips, so forks and unconfigured repos
stay green. And it is still **unverified against a real Clerk instance** until
those secrets exist — treat "typechecks (`pnpm run typecheck:e2e`) and skips" as
today's bar, "green in e2e-dashboard.yml" as the bar once the dev instance is
wired. `E2E_BASE_URL`/`E2E_TASK_ID` remain the knobs for a manual local run.

Why no `webServer`? Because Playwright starts it *before* it evaluates any
`test.skip`, so a credential-less run — the whole point of the skip path — would
still try to spin up `pnpm dev`. Omitting it keeps the skip run instant and
side-effect-free; the cost is that a real run assumes you've started the stack
yourself. Until all of the above exists, the interim cheap browser smoke
(**B0**): `/datasets` renders its empty state from the unauthenticated
`/api/public/experiments` proxy with only `NEXT_PUBLIC_API_URL` set (a public
route in the middleware matcher), and `/` is pure static.

### What we deliberately did *not* test here (and why)

An earlier draft carried two more CLI paths — a read (`oddish ls --json` sees
the seeded task via `GET /tasks/browse`) and a task-detail readback (`oddish
status <id> --json` reads the queued trial via `GET /tasks/{id}`). They're gone.
So ask the sharp question: if they're cheap and they pass, why cut them?

Because they don't earn their keep. Both are thin CLI wrappers over endpoints
the in-process `ASGITransport` suite already covers (`backend/tests/`), so what
they add on top of P0 + P2 is *only* the wire-and-subprocess plumbing — and P0
already proves the socket boots and P2 already drives the CLI subprocess through
a real submit. A green `ls`/`status` past that point re-covers query logic
that's tested faster elsewhere; its marginal signal is ~zero. So the rule we're
applying: an end-to-end test justifies its cost only by the *layer it uniquely
exercises*, not by the endpoint it happens to hit. P1 and P3 fail that test, so
they're the first candidates to cut the moment they cause maintenance friction —
which is now.

## Gotchas

- **Schema comes from `create_all`, not alembic.** We build the schema with
  `Base.metadata.create_all` over the combined backend + oddish metadata
  (`import models` registers the cloud tables on the shared `Base`), mirroring
  `.github/scripts/preview/bootstrap_preview_db.py::_rebuild_schema`. The
  cross-stack alembic chain has a duplicate-revision cycle locally, so `alembic
  upgrade head` is not a local option.
- **The partial unique index is alembic-only.** `create_all` does not build
  `uq_worker_jobs_tag_project_active`, but the sweep's `ON CONFLICT` path needs
  it. Replicate the DDL after `create_all`; `backend/tests/test_sweep_batch.py`
  has the exact `CREATE UNIQUE INDEX … WHERE kind = 'TAG_PROJECT' …` statement.
- **api_keys has no DB-level FK — and needs no shim.** `backend/models.py`
  declares the `api_keys` relationships (`OrganizationModel.api_keys`,
  `UserModel.api_keys`) as `viewonly` with explicit `primaryjoin`/`foreign()`
  precisely because `api_keys.org_id` carries no database FK; nothing extra is
  needed for a `create_all`-built schema to work.
- **Never point this at a shared DB.** Seeding is additive and cleaned up, but
  the plan assumes a throwaway container — and the conftest enforces it: a
  localhost-only guard on the `ODDISH_DATABASE_URL` host fails loudly before
  any DDL runs.
- **The skip gate lives in the test module, not the conftest.** A `pytestmark`
  in `conftest.py` does not gate the tests that import it; the skip must be
  declared in `test_cli_smoke.py` so its fixtures never run when the switches are
  off.
- **This is the only pytest job in CI.** No other workflow in
  `.github/workflows/` invokes pytest, so `.github/workflows/e2e-cli.yml` is the
  first. It runs the trimmed suite on PRs and pushes to main against a `services:
  postgres` container. One subtlety worth internalizing: the job runs *directly*
  on `ubuntu-latest`, not inside a `container:` image, because a job container
  would reach the service under the hostname `postgres` — and the conftest's
  localhost guard would (correctly) refuse it. Mapping the service port to the
  host keeps the DB at `127.0.0.1:5432` so the guard passes without being
  weakened.

## Deliberately out (and when you'd add it back)

- **Upload path** — add it when you stand up MinIO or a real S3 and want to
  prove archive → init → PUT → complete over the wire.
- **Real worker execution** — add it when you have Docker + provider keys +
  budget and want to watch a trial reach a terminal state.
- **Modal dispatch** — add it when you're testing the hosted deploy, not the
  vanilla server.
- **Authenticated browser flows (P4)** — scaffolded via `@clerk/testing` +
  Playwright in `frontend/e2e/` (see P4), but skipped until it's given a
  dev-instance test user, a matching seeded org row, and the CLERK secrets.
  It's unverified against a real Clerk instance until then.

## What should we test next?

A recon of the existing test suites — a couple hundred files across
`oddish/tests/` and `backend/tests/` — turns up a surprise: the "obvious" scary
areas are already well covered. Queue-key canonicalization has 30+ cases
(`test_config_queue_keys.py`), sweep idempotency has 11 real-DB tests
(`test_sweep_idempotency.py`), dispatcher fair-share has 20
(`test_worker_job_dispatcher.py`), and share-token revocation plus probe
filtering in public views are both exercised
(`test_collection_export_and_public.py`). So the productive question is not
"what looks untested?" — it's "where does the existing suite only *pretend* to
cover?" Look for the monkeypatch seams: places where a test stubs out the very
code whose behavior is the risk. Ranked:

1. **The stale-heartbeat flip** (highest risk). The cleanup SQL in
   `oddish/src/oddish/workers/queue/cleanup.py` that selects RUNNING
   `worker_jobs` with `heartbeat_at` older than `STALE_HEARTBEAT_MINUTES` (15)
   and flips them to RETRYING has zero coverage. We test the mirror-back SQL
   shape (`test_worker_jobs_runner.py` — monkeypatched) and the heartbeat
   *writer* (`test_heartbeat_diagnostics.py`) — but never the detector. Why does
   that matter more than most gaps? Both failure directions are incidents: too
   eager double-runs a live trial (the incident that forced 10→15 min); too lazy
   strands crashed trials forever. Shape: seed a RUNNING job with a backdated
   `heartbeat_at` → run the sweep → assert the flip; seed a fresh one → assert
   untouched. Real Postgres, no server needed.
2. **Orphaned-slot reclaim.** The per-slot `queue_slots.locked_by ==
   worker_jobs.current_worker_id` reclaim in `cleanup.py` (gated by
   `ORPHANED_SLOT_GRACE_MINUTES` = 2) is the fix for the 12h queue-starvation
   incident — and no test runs its real SQL; `test_assigned_queue_worker.py`
   monkeypatches acquire/release away. A refactor could silently reintroduce the
   per-queue-key variant AGENTS.md warns about, with green tests. Shape: seed a
   leaked lease with no matching RUNNING job → assert released; seed a live
   pairing → assert kept.
3. **Task re-upload identity.** AGENTS.md calls "same name → same task row +
   new `task_versions` row" load-bearing, yet nothing exercises upload/complete
   against a pre-existing name. It's also the natural next e2e increment: add a
   MinIO service container to `e2e-cli.yml` and this suite can cover the real
   presigned-PUT upload path P2 deliberately skips (see "Deliberately out").
4. **Route-level scope enforcement.** The `require_scope` primitive
   (member-created `tasks` keys blocked from org mutations) is well unit-tested
   (`test_api_key_permissions.py`, 11 tests) — but nothing verifies routes
   actually *call* it. A new mutation endpoint that forgets the dependency ships
   a privilege escalation with green tests. Shape: one parametrized
   ASGITransport test iterating every mutation route with a member-created
   `tasks` key, asserting 403.
5. **Tier-3, briefer.** A soft-delete negative test: prove a plain ORM select
   cannot see a tombstoned row without `include_deleted`, and that the
   dispatcher claim path's hand-written `deleted_at IS NULL` guard holds. And
   once P4's Playwright runs for real, a public share-page browser test
   asserting a seeded probe trial never reaches the DOM — defense-in-depth over
   the data-layer filtering tests that already exist.

And the anti-goal, so this list doesn't invite padding: do not add tests to the
already-thorough areas at the top. Duplicated coverage is maintenance without
protection — the same rule that cut P1 and P3.

## Running it

Gating: the suite skips unless **both** `ODDISH_E2E=1` and
`ODDISH_DATABASE_URL` are set — the same env-gating spirit as
`oddish/tests/perf/conftest.py`.

```
# from backend/, with the dev container up on :5432
ODDISH_E2E=1 \
ODDISH_DATABASE_URL=postgresql+asyncpg://oddish:oddish@localhost:5432/oddish \
uv run pytest tests/e2e/ -v

# confirm it skips cleanly with the switches off
uv run pytest tests/e2e/ -v      # -> 2 skipped
```

In CI, `.github/workflows/e2e-cli.yml` runs the same command on PRs and pushes
to main against a `postgres:16-alpine` service container reachable at
`127.0.0.1:5432`.

The P4 scaffold lives under `frontend/`. Without CLERK env it skips:

```
# from frontend/
pnpm install
pnpm exec playwright install chromium
pnpm exec playwright test      # -> 1 skipped (no CLERK env)
```

With the dev stack running and the P4 env vars set (see the P4 section), the
same command signs in and asserts the task-detail page renders.

## Checklist

- [ ] P0: `GET /public/experiments` -> 200 `[]` unauthenticated
- [ ] P2: `oddish run --task … --agent nop --background --json` -> exit 0; DB has
      1 trial + 1 QUEUED worker_job
- [ ] Suite skips cleanly with `ODDISH_E2E` unset
- [ ] Seeded rows are cleaned up (no `org_e2e_%` / `task_e2e_%` left behind)
- [ ] B0 (optional): `/datasets` renders its empty state unauthenticated
- [x] P4: Playwright + `@clerk/testing` scaffolded in `frontend/e2e/`; skips
      cleanly without CLERK env
- [ ] P4 (unverified): run green against a dev-instance test user + seeded org
      row (needs all five repo secrets: `E2E_CLERK_EMAIL`, `E2E_CLERK_ORG_ID`,
      `CLERK_SECRET_KEY`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `CLERK_DOMAIN` —
      the `e2e-dashboard.yml` gate requires all of them)
