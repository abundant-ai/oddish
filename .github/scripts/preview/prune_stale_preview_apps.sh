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
# An app is stopped when its PR is closed OR its `pr-<N>` Supabase branch no
# longer exists. An open PR with a live branch is never touched, and the next
# push to a swept-but-still-open PR redeploys both app and branch normally.
set -euo pipefail

: "${SUPABASE_ACCESS_TOKEN:?}"
: "${SUPABASE_PROJECT_REF:?}"
: "${GITHUB_REPOSITORY:?}"
: "${GITHUB_TOKEN:?}"
MODAL_ENVIRONMENT="${MODAL_ENVIRONMENT:-preview}"
DRY_RUN="${DRY_RUN:-false}"

branches_json=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json)

# `description` carries the app name; only "deployed" apps run schedules
# (stopped/ephemeral rows in the listing have nothing to prune).
apps=$(modal app list --env "$MODAL_ENVIRONMENT" --json \
  | jq -r '.[] | select(.state == "deployed") | .description' \
  | { grep -E '^oddish-pr-[0-9]+$' || true; })

stopped=0
for app in $apps; do
  pr="${app#oddish-pr-}"

  # A failed lookup (deleted repo ref, API blip) must not stop an app on its
  # own: fall through to the branch check, which is authoritative for the
  # zombie class regardless of PR state.
  pr_state=$(curl -sf \
    -H "Authorization: Bearer $GITHUB_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    "https://api.github.com/repos/$GITHUB_REPOSITORY/pulls/$pr" \
    | jq -r '.state' || echo "unknown")

  reason=""
  if [ "$pr_state" = "closed" ]; then
    reason="PR #$pr is closed"
  else
    branch_count=$(jq -r --arg name "pr-$pr" \
      '[.[] | select(.persistent != true) | select(.name == $name)] | length' \
      <<<"$branches_json")
    if [ "$branch_count" = "0" ]; then
      reason="Supabase branch pr-$pr no longer exists (PR state: $pr_state)"
    fi
  fi

  if [ -z "$reason" ]; then
    echo "keeping $app (PR open, branch alive)"
    continue
  fi

  if [ "$DRY_RUN" = "true" ]; then
    echo "DRY RUN: would stop $app — $reason"
    continue
  fi

  echo "stopping $app — $reason"
  modal app stop -y --env "$MODAL_ENVIRONMENT" "$app" || true
  modal secret delete -y --env "$MODAL_ENVIRONMENT" "$app-db" || true
  stopped=$((stopped + 1))
done

echo "stopped $stopped preview app(s)"
