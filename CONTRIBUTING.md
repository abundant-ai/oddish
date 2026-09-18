# Contributing to Oddish

This file is the process guide: how a change gets from a branch to
production, what a pull request must contain, and which documents a change
obligates you to update. Architecture and gotchas live in `AGENTS.md`.
End-user CLI documentation lives in `DOCS.md`.

## The three stages

A change passes through three stages. Two of them are pull requests that
look identical in the GitHub interface but complete in opposite ways.

| Stage | Base branch | What CI does | How it completes |
|---|---|---|---|
| Feature PR | `staging` | `pr-preview` deploys a preview app and preview database for the PR on every push and posts the links as a comment. | Press the merge button. Squash is the only allowed method. The `Require working preview` check must be green and every review thread resolved. |
| Staging deploy | none | `staging-deploy` runs on every push to `staging` (a squash merge counts) and deploys staging.oddish.app. | Automatic. Nobody does anything. |
| Promotion PR | `main`, head `staging` | `Promotion warning` adds the caution block to the body; `promote-comment` verifies the pinned commit and fast-forwards `main`. | Comment `/promote` on the PR. Never press the merge button. |

If your branch is not literally `staging`, you are opening a feature PR.

`main` is release-only and is always an ancestor of `staging`: it advances
solely by fast-forward. The merge button on a promotion PR creates a squash
commit on `main`, which breaks that relationship and the release model with
it. Open promotion PRs with the `release-promotion.md` template
(`?quick_pull=1&template=release-promotion.md` on the compare URL), fill in
the `promotion-target` marker with the staging commit you validated
(`git rev-parse origin/staging`), and complete the PR by commenting
`/promote`. Bare `/promote` promotes the pinned commit, so commits that reach
`staging` afterwards do not ride along; `/promote <sha>` overrides the pin. A
maintainer with push access to `main` can instead run the `Promotion
Preflight` workflow and execute the push command it prints.

Never commit or push to `main` or `staging` directly.

## Hotfixes

Branch the fix from `main`, not from `staging`. `main` is always an ancestor
of `staging`, so a fix based on it carries no unreleased work and still
fast-forwards cleanly:

```bash
git fetch origin main && git checkout -b fix/<name> origin/main
```

Open it as a normal feature PR into `staging`, get an expedited review,
squash-merge, then promote immediately. This is the standard path; use it
whenever the pipeline is fast enough for the incident. Pin the hotfix sha
on the staging→main promotion PR (`promotion-target` or `/promote <sha>`)
so later staging commits do not ride along, and do not wait for an extra
staging soak after Staging Deploy is green.

For the timed checklist, required checks, rollback commands, and the dry-run
rehearsal (`rehearse_urgent_hotfix.sh`), see
[`docs/urgent-hotfix-release.md`](docs/urgent-hotfix-release.md).

Break-glass (landing a fix on `main` directly) is only for two cases: the
pipeline is too slow for the incident, or `staging` holds work that cannot
ship. It breaks the fast-forward invariant on purpose, so it needs an
incident ticket, a second person's approval, and an immediate repair
afterwards: fast-forward `staging` up to `main`, or rebuild `staging` on the
new `main` if it carries unpromoted commits. Never cherry-pick the fix into
`staging`; the copy gets a different commit id, so the branches stay
diverged.

Not every change has to be releasable to merge. Land unfinished work behind
a flag that is off by default (as `ODDISH_GKE_ENABLED` does), or promote only
part of `staging` by giving the promotion workflow the commit to stop at.

## Pull request hygiene

- A pull request is merged or closed within 3 days of leaving draft. If it
  cannot land in that window, split it, or convert it back to draft with a
  comment saying what blocks it and who owns the blocker.
- Application code changes add at most 500 lines. Tests, documentation,
  migrations, lockfiles, and generated files do not count toward the limit.
  Larger work lands as a stack of pull requests, each one reviewable on its
  own; the body of each names the one before it.
- A pull request that changes anything a user can see includes a screenshot,
  or the preview link with the page and state named. Reviewers should not
  have to reconstruct the UI from the diff.
- Every pull request body follows the format in
  `.claude/skills/write-pr/SKILL.md`. Agents run the `write-pr` skill; humans
  fill in the default template, which has the same sections. The body
  explains the existing behavior and its consequence before naming
  implementation files, and reports what was actually tested.
- Review threads are resolved by the author after addressing them, with a
  reply saying what changed. The `staging` ruleset blocks merge while any
  thread is open.
- Do not reformat lines you did not change. The pre-commit prettier and the
  project's pinned prettier disagree, and committed files mix both styles.
- Do not add co-author trailers or attribution lines to commits or pull
  request bodies unless the person named asked for it.

## Compatibility with installed clients

The `oddish` CLI is installed from Homebrew and upgrades only when the user
runs `brew upgrade abundant-ai/tap/oddish`. The live server therefore
always serves clients one or more releases old. Before changing anything
under `oddish/` that a client reads (response fields, status vocabularies,
CLI options, queue payloads, storage keys), find what depends on it: search
`oddish/src/oddish/cli/`, the packaged skill references under
`oddish/src/oddish/assets/skills/oddish/`, `backend/`, and `frontend/` for
readers of the thing you are changing, and list them in the pull request
body under `Compatibility`.

The previously released CLI version must keep working against the new
server. Add fields rather than renaming them, keep old values accepted for
at least one release, and never change the meaning of an existing value. If
compatibility cannot be kept, the pull request body says so, names the
first client version that breaks, and the change ships with a release that
tells older clients to upgrade instead of failing their tasks. The rule
exists because a server change that dropped support for an older request
shape once failed every task submitted from an older CLI until users
noticed and upgraded.

The same check applies inside the repository: `backend/` and `frontend/`
read `oddish/` contracts, and other open pull requests may depend on a
helper you are about to remove. `AGENTS.md` describes the pruning
procedure.

## Documents to update

| You changed | Update |
|---|---|
| CLI commands or options in `oddish/src/oddish/cli/` | `DOCS.md`, the command list in `oddish/README.md`, and the packaged skill under `oddish/src/oddish/assets/skills/oddish/` (served by `oddish skill`) |
| API contracts, queue behavior, or storage layout | `AGENTS.md` |
| `backend/` auth, deployment, or worker orchestration | `AGENTS.md` and `backend/README.md` |
| `frontend/` routing, API proxy structure, or auth behavior | `AGENTS.md` and `frontend/README.md` |
| Self-hosting steps, environment variables, or migrations | `SELF_HOSTING.md` |
| Manually invoked diagnostics or repair commands | `docs/operations-tools.md` |
| The release process or pull request rules | this file and `CLAUDE.md` |

Preserve the package boundary: `oddish/` must remain self-hostable for the
CLI and standalone server; hosted product concerns (auth, organization
membership, Modal app wiring, managed worker spawning, GitHub and webhook
integrations, cloud-only policy) belong in `backend/`.

## Local setup and tests

- Core package: `cd oddish && uv sync --extra server`, then `uv run pytest`.
  Database-backed tests need a running Postgres and `ODDISH_DATABASE_URL`;
  see the Local Development section of `AGENTS.md`.
- Backend: `cd backend && uv sync && uv run pytest`. Run
  `uv run modal serve deploy.py` to serve it locally; `backend/README.md`
  lists the required environment variables.
- Frontend: `cd frontend && pnpm install && pnpm dev`. `pnpm test` runs unit
  tests, `pnpm test:e2e` runs Playwright, `pnpm typecheck:e2e` checks the
  end-to-end TypeScript project. See `frontend/README.md`.
- Migrations: run `uv run alembic upgrade head` in both `oddish/` and
  `backend/`. A migration parented on a `down_revision` that is no longer
  staging's head fails the `migration-head-guard` check; re-point it.
- Self-hosting: `SELF_HOSTING.md`.
