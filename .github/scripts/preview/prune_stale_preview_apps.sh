#!/usr/bin/env bash
# Stop preview Modal apps that outlived their reason to exist.
#
# pr-preview.yml's stop-preview job tears an app down when its PR closes; this
# daily sweep catches everything that path misses: PRs closed before that job
# existed, teardown-job failures, and — the important class — apps whose
# Supabase preview branch the branch prune (which runs just before this
# script) deleted out from under them. Such an app keeps redialing the
# deleted Supabase tenant from every scheduled function forever; one of these
# zombies emitted ~726k error records into Logfire in a single week.
#
# An app is stopped when its PR is closed, its `pr-<N>` Supabase branch no
# longer exists, or that branch is past the branch prune's own MAX_AGE_DAYS
# cutoff. The age check exists for DRY_RUN fidelity: a dry-run branch prune
# deletes nothing, so the listing this script reads still contains every
# branch a real run would have deleted moments earlier — without the age
# check a dry run under-reports the stop list. An open PR with a live, fresh
# branch is never touched; a swept-but-still-open PR gets both app and branch
# back on its next push.
set -euo pipefail

: "${SUPABASE_ACCESS_TOKEN:?}"
: "${SUPABASE_PROJECT_REF:?}"
: "${GITHUB_REPOSITORY:?}"
: "${GITHUB_TOKEN:?}"
MODAL_ENVIRONMENT="${MODAL_ENVIRONMENT:-preview}"
MAX_AGE_DAYS="${MAX_AGE_DAYS:-7}"
DRY_RUN="${DRY_RUN:-false}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

case "$MAX_AGE_DAYS" in
  '' | *[!0-9]*)
    echo "MAX_AGE_DAYS must be a whole number of days, got '$MAX_AGE_DAYS'" >&2
    exit 1
    ;;
esac

cutoff=$(($(date +%s) - MAX_AGE_DAYS * 86400))

branches_json=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json)

# `description` carries the app name; only "deployed" apps run schedules
# (stopped/ephemeral rows in the listing have nothing to prune).
apps=$(modal app list --env "$MODAL_ENVIRONMENT" --json \
  | jq -r '.[] | select(.state == "deployed") | .description' \
  | { grep -E '^oddish-pr-[0-9]+$' || true; })

stopped=0
teardown_failures=0
for app in $apps; do
  pr="${app#oddish-pr-}"

  # A failed lookup (API blip) yields "unknown"; see below for how it gates.
  pr_state=$(curl -sf \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/pulls/$pr" \
    | jq -r '.state' || echo "unknown")

  # missing: no pr-N branch. stale: present but past the prune cutoff (the
  # branch prune deletes it this run — or would, under DRY_RUN). alive:
  # present and fresh. jq aborts on an unparseable created_at, failing the
  # whole script before any stop: an unreadable listing must never be read
  # as "these branches are gone".
  branch_state=$(jq -r --arg name "pr-$pr" --argjson cutoff "$cutoff" '
      def parse_supabase_time:
        sub("\\+00:00$"; "Z")
        | sub("\\.[0-9]+Z$"; "Z")
        | fromdateiso8601;

      [.[] | select(.persistent != true) | select(.name == $name)]
      | if length == 0 then "missing"
        elif any(.[]; (.created_at | parse_supabase_time) >= $cutoff) then "alive"
        else "stale"
        end' <<<"$branches_json")

  reason=""
  if [ "$pr_state" = "closed" ]; then
    reason="PR #$pr is closed"
  elif [ "$branch_state" = "missing" ] && [ "$pr_state" = "unknown" ]; then
    # Two inconclusive signals at once: never stop an app on a failed PR
    # lookup plus an absent branch. Tomorrow's run retries with working APIs.
    echo "keeping $app (PR state unknown and branch pr-$pr not listed; deferring)"
    continue
  elif [ "$branch_state" = "missing" ]; then
    reason="Supabase branch pr-$pr no longer exists (PR state: $pr_state)"
  elif [ "$branch_state" = "stale" ]; then
    reason="Supabase branch pr-$pr is past the ${MAX_AGE_DAYS}-day prune cutoff (PR state: $pr_state)"
  fi

  if [ -z "$reason" ]; then
    echo "keeping $app (PR $pr_state, branch alive)"
    continue
  fi

  if [ "$DRY_RUN" = "true" ]; then
    echo "DRY RUN: would stop $app — $reason"
    continue
  fi

  echo "stopping $app — $reason"
  # Same rule as the close path: the app deletes its own trials cluster
  # before it is stopped, because the credentials live only inside it. A
  # stale app is exactly the one whose stop workflow never ran, so this is
  # the last chance to avoid an orphaned cluster -- which is why a REAL
  # teardown failure skips the stop for this app and keeps its reaper
  # alive, instead of destroying the only remaining owner.
  if ! "$script_dir/run_gke_teardown.sh" "$app"; then
    echo "::error::GKE teardown failed for $app; leaving it running so its reaper still owns the cluster"
    teardown_failures=$((teardown_failures + 1))
    continue
  fi
  modal app stop -y --env "$MODAL_ENVIRONMENT" "$app" || true
  modal secret delete -y --env "$MODAL_ENVIRONMENT" "$app-db" || true
  stopped=$((stopped + 1))
done

echo "stopped $stopped preview app(s)"
if [ "$teardown_failures" -gt 0 ]; then
  echo "::error::$teardown_failures app(s) kept running because their GKE teardown failed"
  exit 1
fi
