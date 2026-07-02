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

**P1 — CLI read.** `oddish ls --json` exercises the auth header over the wire and
`GET /tasks/browse`. Assert the seeded task id appears in the response's `items`
list.

**P2 — CLI submit.** `oddish run --task <id> --agent nop --n-trials 1
--background --json` exits 0. Then assert directly in the DB: one `trials` row
for the task, and one `QUEUED` `worker_jobs` row (`subject_table='trials'`)
pointing at that trial.

**P3 — CLI task detail.** `oddish status <task_id> --json` hits `GET
/tasks/{id}` and prints a `TaskStatusResponse`; assert its `trials` list has the
queued trial.

**P4 — browser task menu (the dashboard `/tasks/[task_id]` page) — plan-only,
but tractable.** Start with what the wall actually is: `frontend/src/
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
unprotected page that loads Clerk first. On development instances,
`+clerk_test` emails verify with the universal OTP `424242`, so the test user
needs no real mailbox. The honest remaining costs — why this is a Level-2
follow-up rather than part of the minimal suite: Playwright and
`@clerk/testing` are new installs into a frontend with zero test tooling (pnpm
v10); you need a dev-instance (`pk_test`/`sk_test`) Clerk user with a
`+clerk_test` email that belongs to an org; and the backend only returns data
if a seeded `OrganizationModel` row matches that org — the JWT template
`oddish` carries `org_id`/`org_role` (`SELF_HOSTING.md`). Until then, the
interim cheap browser smoke (**B0**): `/datasets` renders its empty state from
the unauthenticated `/api/public/experiments` proxy with only
`NEXT_PUBLIC_API_URL` set (a public route in the middleware matcher), and `/`
is pure static.

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
- **No CI runs pytest today.** None of the workflows in `.github/workflows/`
  invoke pytest. An e2e CI job would be a *new* workflow with a `services:
  postgres` block — named here as future work, not built.

## Deliberately out (and when you'd add it back)

- **Upload path** — add it when you stand up MinIO or a real S3 and want to
  prove archive → init → PUT → complete over the wire.
- **Real worker execution** — add it when you have Docker + provider keys +
  budget and want to watch a trial reach a terminal state.
- **Modal dispatch** — add it when you're testing the hosted deploy, not the
  vanilla server.
- **Authenticated browser flows (P4)** — tractable via `@clerk/testing` +
  Playwright (see P4); add it when you're ready to install both, create the
  dev-instance test user, and seed its matching org row.

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
uv run pytest tests/e2e/ -v      # -> 4 skipped
```

## Checklist

- [ ] P0: `GET /public/experiments` -> 200 `[]` unauthenticated
- [ ] P1: `oddish ls --json` lists the seeded task id
- [ ] P2: `oddish run --task … --agent nop --background --json` -> exit 0; DB has
      1 trial + 1 QUEUED worker_job
- [ ] P3: `oddish status <task_id> --json` shows the queued trial
- [ ] Suite skips cleanly with `ODDISH_E2E` unset
- [ ] Seeded rows are cleaned up (no `org_e2e_%` / `task_e2e_%` left behind)
- [ ] B0 (optional): `/datasets` renders its empty state unauthenticated
- [ ] P4: needs Playwright + `@clerk/testing` + dev-instance test user + seeded
      org row
