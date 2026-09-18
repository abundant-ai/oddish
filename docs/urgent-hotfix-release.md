# Urgent hotfix release procedure

Validated rehearsal for shipping a production fix faster than a normal
feature → staging soak → promote cycle. Builds on the Hotfixes section of
[`CONTRIBUTING.md`](../CONTRIBUTING.md) and the contributing guide from
[#1700](https://github.com/abundant-ai/oddish/pull/1700).

This document does **not** change the release model: `main` still advances
only by fast-forward from a commit that is already on `staging`, and
Staging Deploy must be green on that commit before promote. It records which
gates are required, which waits are optional, how long each gate recently
took, and how to roll back.

## When to use which path

| Situation | Path |
|---|---|
| Production is broken; staging is shippable | **Standard urgent path** below |
| Staging Deploy / preview pipeline is too slow for the incident | **Break-glass** in `CONTRIBUTING.md` (land on `main` with dual approval, then repair staging) |
| Staging holds unpromotable work | **Break-glass**, or promote a pinned earlier sha that is still green |

## Standard urgent path (preferred)

1. Branch from `main`, not from `staging`:
   ```bash
   git fetch origin main && git checkout -b fix/<name> origin/main
   ```
2. Open a normal feature PR into `staging`. Expedite review. Squash-merge.
3. Wait only until **Staging Deploy** is green on the squash commit. Do not
   wait for an extra soak on staging.oddish.app unless the fix needs a
   manual smoke that Staging Deploy does not cover.
4. Promote **that sha**, not whatever tip staging has drifted to:
   - Prefer a standing staging→main promotion PR with
     `<!-- promotion-target: <hotfix_sha> -->`, then comment `/promote`.
   - Or run **Promotion Preflight** with `target_sha=<hotfix_sha>` and
     execute the printed push (ruleset-bypass maintainer).
   - `/promote <hotfix_sha>` overrides a stale pin.
5. Confirm **Production Deploy** starts for that sha and finishes green.

Keep a standing promotion PR open so step 4 does not spend incident time on
compare-URL boilerplate. There is no open promotion PR when staging and
main are identical after a promote; recreate it as soon as staging moves
again, or immediately after the hotfix squash-merge.

## Required checks

| Check | Required? | Notes |
|---|---|---|
| Feature PR: `Require working preview` | Yes (standard path) | Staging ruleset; skip only via break-glass |
| Feature PR: review + resolved threads | Yes | Expedite; do not skip the second person on break-glass |
| `Staging Deploy` success on the promote target | Yes | Enforced by `verify_promotion_target.sh` |
| Promotion Preflight / local verify | Recommended | Same script the workflows call |
| Open staging→main promotion PR | Yes for `/promote` and Promotion Preflight | Maintainer push can skip the PR after a local verify |
| Production Deploy success | Yes to call the release done | Migrations → Modal backend → Vercel frontend |

## Avoidable delays (do not wait for these)

- Staging soak after Staging Deploy is already green.
- Promoting the staging tip when later unrelated commits have landed — pin
  the hotfix sha instead.
- Creating the promotion PR only after the squash-merge (keep one standing,
  or open it in parallel with review).
- Full feature-PR turnaround norms (3-day draft window, non-expedited review).
- Waiting to clean unpromotable staging work when break-glass is already
  justified.

## Rollback

Preferred: ship a forward fix with the same urgent path.

Emergency: restore the previous production tip on `main` (non-fast-forward;
ruleset-bypass push access required):

```bash
git fetch origin main
# PREVIOUS_MAIN_SHA = origin/main immediately before the bad promote
git push --force-with-lease origin <PREVIOUS_MAIN_SHA>:refs/heads/main
```

Then:

1. Confirm Production Deploy runs for the restored sha.
2. Leave the bad commit on staging until a forward fix or a staging rebuild.
3. If break-glass left `main` and `staging` diverged, run **Sync Preflight**
   and apply its repair commands. Never cherry-pick the fix into staging.

## Dry-run rehearsal

From the repository root (read-only; never pushes):

```bash
.github/scripts/promote/rehearse_urgent_hotfix.sh
# or pin a sha:
.github/scripts/promote/rehearse_urgent_hotfix.sh <staging_commit_sha>
```

## Rehearsal record — 2026-09-18

Dry-run only against `abundant-ai/oddish`. No promotion and no push to
`main` or `staging`.

| Item | Value |
|---|---|
| Date (UTC) | 2026-09-18 |
| `origin/main` | `9929b5645d4317317e00a445e1654157070c5825` |
| `origin/staging` | `4de241c9605fd174f28cd41a2d49ed89a5c2f00f` |
| Relationship | staging 1 commit ahead; main is an ancestor (healthy) |
| Target verified | `4de241c9605fd174f28cd41a2d49ed89a5c2f00f` |
| Staging Deploy on target | success (run `35292232515`, ~3.0 min) |
| Local `verify_promotion_target.sh` | passed in ~1.1 s |
| Standing promotion PR | **none open** (recorded avoidable delay) |
| Rollback tip verified | previous good `main` = `9929b564…` (commands printed, not executed) |

Recent gate durations sampled the same day:

| Gate | Observed |
|---|---|
| Staging Deploy (3 green runs) | 2.3–3.0 min |
| Production Deploy (2 green runs) | 3.7–3.9 min |
| Promotion Preflight (2 green runs) | 14–17 s |
| PR preview (completed samples) | ~4–7 min |

**Release-time conclusion:** once the hotfix squash-merge exists, the
mandatory automated wait is Staging Deploy (~3 min) plus Production Deploy
(~4 min). Promotion verification is seconds. The largest avoidable
post-merge delay is an absent promotion PR or promoting the wrong tip;
the largest pre-merge delay remains `Require working preview` (~4–8 min)
unless break-glass applies.

Re-run the dry-run script after any change to
`.github/scripts/promote/verify_promotion_target.sh` or the promote
workflows, and paste a fresh table into a follow-up commit or PR.
