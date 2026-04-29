# Claude Code Guide — Oddish

The canonical engineering guide for this repo is **`AGENTS.md`** at the repo root. Read it first; it covers the three packages (`oddish/` CLI+server, `backend/` hosted cloud layer, `frontend/` Next.js dashboard), required toolchains, and maintenance rules. End-user CLI docs are in `DOCS.md`.

## What this project is

Oddish runs evals on [Harbor](https://github.com/laude-institute/harbor) tasks in the cloud: provider-aware queuing, real-time monitoring, Postgres-backed state, S3 log storage. End users replace `harbor run` with `oddish run`. The hosted layer (`backend/` + `frontend/`) is deployed on Modal and surfaces a dashboard at oddish.app.

## Useful pointers

- **Run backend locally:** `cd backend && uvicorn api.app:create_app --factory --reload`. See `backend/README.md` for required env vars.
- **Run frontend locally:** `cd frontend && pnpm dev`. See `frontend/README.md`.
- **Tests:** `pytest` from `oddish/` or `backend/`. Frontend has no test suite wired up yet.
- **Local job fixtures:** `jobs/` at the repo root contains a sample experiment tree mirroring the production S3 layout — useful when developing features that read trial artifacts.
- **Design docs and plans:** `docs/superpowers/specs/` and `docs/superpowers/plans/`.
