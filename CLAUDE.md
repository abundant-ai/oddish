# Claude Code Guide — Oddish

The canonical engineering guide for this repo is **`AGENTS.md`** at the repo root. Read it first; it covers the three packages (`oddish/` CLI+server, `backend/` hosted cloud layer, `frontend/` Next.js dashboard), required toolchains, and maintenance rules. End-user CLI docs are in `DOCS.md`; self-hosting is in `SELF_HOSTING.md`.

## Git workflow — NEVER commit or push to `main`

**Never directly commit or push to `main`.** Always check out a new branch first
(`git checkout -b <type>/<short-desc>`), commit there, push that branch, and open
a PR for review. This applies to every change, no matter how small — even a
one-line fix. If you find yourself on `main` with staged or unstaged changes,
create a branch and move the work onto it before committing.

## What this project is

Oddish runs evals on [Harbor](https://github.com/laude-institute/harbor) tasks in the cloud: provider-aware queuing, real-time monitoring, Postgres-backed state, S3 log storage. End users replace `harbor run` with `oddish run`. The hosted layer (`backend/` + `frontend/`) is deployed on Modal and surfaces a dashboard at oddish.app.

The three packages require Python `3.12+` (`oddish/`, `backend/`) and Node.js `20+` with `pnpm` (`frontend/`).

## Common commands

### `oddish/` — core CLI, server, workers (run from `oddish/`)

```bash
uv sync --extra server                  # install with server deps (use [all] for everything)
uv run python -m oddish.db setup        # create tables (needs a running Postgres)
uv run python -m oddish.server          # API on :8000, auto-starts background workers
uv run python -m oddish.workers.queue.worker   # standalone worker process (optional)
uv run pytest                           # run tests
uv run ruff check . && uv run ruff format .    # lint + format

# Database helper CLI (python -m oddish.db)
uv run python -m oddish.db init|setup|reset|purge   # migrate / setup / drop+recreate / wipe data
uv run alembic upgrade head             # core DB migrations
```

Point the CLI at a local server with `export ODDISH_API_URL="http://localhost:8000"`,
or at the hosted API with `export ODDISH_API_KEY="ok_..."`.

### `backend/` — hosted cloud layer (run from `backend/`)

```bash
uv sync
uv run modal serve deploy.py            # local Modal serve of the backend
uv run pytest                           # run tests
uv run alembic upgrade head             # cloud-only tables/extensions migrations
```

> ⚠️ Do **not** deploy to Modal prod with a `backend/.env` present — the secret-count
> mismatch crash-loops every container. Deploy via `uv run modal` with no `.env`.

### `frontend/` — Next.js dashboard (run from `frontend/`)

```bash
pnpm install
pnpm dev                                # dev server on :3000
pnpm build                              # production build
pnpm lint                               # ESLint
pnpm format                             # Prettier
pnpm dead-code                          # knip unused-code scan
```

No frontend test suite is wired up yet.

## Useful pointers

- **Local dev setup:** each package has `env.example`/`.env.example` to copy and a `### Local Development` section in `AGENTS.md` with the full recipe (Postgres, Clerk keys, etc.).
- **Local job fixtures:** `jobs/` at the repo root contains a sample experiment tree mirroring the production S3 layout — useful when developing features that read trial artifacts.
- **Design docs and plans:** `docs/superpowers/specs/` and `docs/superpowers/plans/`.
- **Two migration stacks:** core tables migrate from `oddish/`, cloud tables/extensions from `backend/` — run `alembic upgrade head` in both.
- **Changelog:** `CHANGELOG.md` tracks user-facing changes.
