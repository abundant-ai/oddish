# Claude Code Guide — Oddish

The canonical engineering guide for this repo is **`AGENTS.md`** at the repo
root. Read it first; it covers the three packages (`oddish/` CLI+server,
`backend/` hosted cloud layer, `frontend/` Next.js dashboard), package
boundaries, worker-runtime invariants, and the repo-wide gotchas (probe
visibility in public views, `list_tasks_core` `load_only`). End-user CLI docs
are in `DOCS.md`.

## What this project is

Oddish runs evals on [Harbor](https://github.com/laude-institute/harbor) tasks
in the cloud: provider-aware queuing, real-time monitoring, Postgres-backed
state, S3 log storage. End users replace `harbor run` with `oddish run`. The
hosted layer (`backend/` + `frontend/`) is deployed on Modal and surfaces a
dashboard at oddish.app.

## Git workflow

Never directly commit or push to `main`. Check out a feature branch, commit
there, push that branch, and open a PR for review.

## Useful pointers

- **Run backend locally:** `cd backend && uv run modal serve deploy.py`. See `backend/README.md` for required env vars.
- **Run frontend locally:** `cd frontend && pnpm dev`. See `frontend/README.md`.
- **Tests:** `pytest` from `oddish/` or `backend/`. Frontend has no test suite wired up yet.
- **Self-hosting:** see `SELF_HOSTING.md` for Modal, Clerk, migrations, and local HTTPS.
