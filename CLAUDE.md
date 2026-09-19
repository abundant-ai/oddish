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

Read `CONTRIBUTING.md` first; it is the process guide. Summary:

| Stage | Base branch | Completes by |
|---|---|---|
| Feature PR | `staging` | Merge button, squash only |
| Staging deploy | automatic on every push to `staging` | Nothing to do |
| Promotion PR (`staging` -> `main`) | `main` | `/promote` comment. Never the merge button |

If your branch is not literally `staging`, you are opening a feature PR.

Never directly commit or push to `main` or `staging`. Check out a feature
branch, commit there, push that branch, and open a PR into `staging` (the
default branch). Write every PR body with the `write-pr` skill; the default
template in `.github/PULL_REQUEST_TEMPLATE.md` has the same sections. Keep
all descriptions under 300 words and each included PR description in a
promotion under 50 words. Start TL;DR with one summary sentence, then
app/test/docs-other line counts and net, followed by What changed and Tests.
Keep application code under 500 added lines per PR and include a screenshot
or preview link for anything a user can see. Explain material compatibility
effects in What changed (see the "Installed clients are always behind the
server" gotcha in `AGENTS.md`). Do not add co-author trailers or attribution
lines to commits or PR bodies.

`main` is release-only: it advances solely via fast-forward promotion, either
by a maintainer running the `Promotion Preflight` workflow and executing the
push command it prints, or by an organization member with `write`,
`maintain`, or `admin` access commenting `/promote` on the promotion pull
request. Bare `/promote` promotes the sha pinned in the pull request body
(the template's `promotion-target` marker), so commits that reach `staging`
after the promotion pull request was written do not ride along; `/promote
<sha>` overrides the pin, and a body without one promotes the staging tip.

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
`.github/PULL_REQUEST_TEMPLATE/release-promotion.md` as the body skeleton for
every promotion pull request; humans get it with
`?quick_pull=1&template=release-promotion.md` on the compare URL.

## Hotfixes

Branch the fix from `main`, open it as a normal PR into `staging`, squash,
then promote immediately (pin the hotfix sha; skip extra staging soak).
`CONTRIBUTING.md` has the full procedure and break-glass rules;
`docs/urgent-hotfix-release.md` has the timed checklist, rollback steps, and
dry-run rehearsal. Never cherry-pick a fix into `staging`.

## Useful pointers

- **Run backend locally:** `cd backend && uv run modal serve deploy.py`. See `backend/README.md` for required env vars.
- **Run frontend locally:** `cd frontend && pnpm dev`. See `frontend/README.md`.
- **Tests:** run `pytest` from `oddish/` or `backend/`. In `frontend/`, use
  `pnpm test` for unit tests, `pnpm test:e2e` for Playwright, and
  `pnpm typecheck:e2e` for the end-to-end TypeScript project.
- **Self-hosting:** see `SELF_HOSTING.md` for Modal, Clerk, migrations, and local HTTPS.
