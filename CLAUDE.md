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

Never directly commit or push to `main` or `staging`. Check out a feature
branch, commit there, push that branch, and open a PR for review — PRs target
`staging` (the default branch). `main` is release-only: it advances solely via
fast-forward promotion by a maintainer with push access to `main`, who runs
the `Promotion Preflight` workflow (it verifies the promotion PR, the staging
deploy, and the fast-forward condition, then prints the push command) and
executes that push themselves; never merge, squash, or push to `main` directly.
An organization member with `write`, `maintain`, or `admin` access can instead
comment `/promote` on the promotion pull request; the workflow runs the same
checks and, when the promote token is set, does the push. Bare `/promote`
promotes the sha pinned in the pull request body (the template's
`promotion-target` marker), so commits that reach `staging` after the
promotion pull request was written do not ride along; `/promote <sha>`
overrides the pin, and a body without one promotes the staging tip.

**Never complete a promotion pull request with the merge button.** The button
squashes, which puts a new commit on `main` and breaks the fast-forward
model. Any agent that opens a promotion pull request must start its body
with this block, marker comment included:

<!-- promote-warning -->
> [!CAUTION]
> **DO NOT USE THE MERGE BUTTON ON THIS PULL REQUEST.**
> **THE BUTTON CREATES A NEW COMMIT AND BREAKS THE RELEASE MODEL.**
> **COMMENT `/promote` TO COMPLETE THE PROMOTION.**

The marker comment is what marks the body as warned. The `Promotion warning`
workflow looks for it, and adds the same block to a promotion pull request
that opens without it. Agents use the template at
`.github/PULL_REQUEST_TEMPLATE/promotion.md` as the body skeleton for every
promotion pull request; humans get it with
`?quick_pull=1&template=promotion.md` on the compare URL.

## Hotfixes

Branch the fix from `main`, not from `staging`. `main` is always an ancestor of
`staging`, so a fix based on it carries no unreleased work and still
fast-forwards cleanly:

```bash
git fetch origin main && git checkout -b fix/<name> origin/main
```

Open it as a normal PR into `staging`, get an expedited review, squash-merge,
then promote immediately. This is the standard path — use it whenever the
pipeline is fast enough for the incident.

Break-glass (landing a fix on `main` directly) is only for two cases: the
pipeline is too slow for the incident, or `staging` holds work that cannot
ship. It breaks the fast-forward invariant on purpose, so it needs an incident
ticket, a second person's approval, and an immediate repair afterwards —
fast-forward `staging` up to `main`, or rebuild `staging` on the new `main` if
it carries unpromoted commits. Never cherry-pick the fix into `staging`: the
copy gets a different commit id, so the branches stay diverged.

Not every change has to be releasable to merge. Land unfinished work behind a
flag that is off by default (as `ODDISH_GKE_ENABLED` and
`ODDISH_PRE_TRIAL_ENABLED` do), or promote only part of `staging` by giving
the promotion workflow the commit to stop at (the `target_sha` input on
Promotion Preflight, `/promote <sha>` on the promotion pull request, or the
`promotion-target` pin in its body).

## Useful pointers

- **Run backend locally:** `cd backend && uv run modal serve deploy.py`. See `backend/README.md` for required env vars.
- **Run frontend locally:** `cd frontend && pnpm dev`. See `frontend/README.md`.
- **Tests:** `pytest` from `oddish/` or `backend/`. Frontend has no test suite wired up yet.
- **Self-hosting:** see `SELF_HOSTING.md` for Modal, Clerk, migrations, and local HTTPS.
