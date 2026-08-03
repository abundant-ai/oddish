#!/usr/bin/env bash
# Reclaim leaked Supabase preview branches whose GitHub PR is already closed.
#
# This is deliberately conservative: only non-persistent branches named
# exactly pr-<number> are considered, the current PR is skipped, and a branch
# is deleted only after GitHub authoritatively reports that PR as closed beyond
# a grace period. The PR state is read again immediately before deletion.
set -euo pipefail

: "${SUPABASE_PROJECT_REF:?}"
: "${GH_TOKEN:?}"
: "${GITHUB_REPOSITORY:?}"

current_branch_name="${CURRENT_BRANCH_NAME:-}"
closed_grace_seconds="${PRUNE_CLOSED_GRACE_SECONDS:-600}"
if [[ ! "$closed_grace_seconds" =~ ^[0-9]+$ ]]; then
  echo "PRUNE_CLOSED_GRACE_SECONDS must be a non-negative integer" >&2
  exit 2
fi

branches_json=$(supabase branches list \
  --project-ref "$SUPABASE_PROJECT_REF" -o json)
pruned=0

read_pr() {
  local pr_number="$1"
  curl -fsS \
    --retry 3 \
    --retry-delay 2 \
    --retry-all-errors \
    --connect-timeout 10 \
    --max-time 30 \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "https://api.github.com/repos/${GITHUB_REPOSITORY}/pulls/${pr_number}"
}

is_old_closed_pr() {
  local pr_json="$1" branch_name="$2"
  local state closed_at closed_epoch now_epoch age_seconds

  state=$(jq -r '.state // empty' <<<"$pr_json")
  if [ "$state" != "closed" ]; then
    echo "keeping Supabase branch $branch_name (GitHub PR state=${state:-unknown})" >&2
    return 1
  fi

  closed_at=$(jq -r '.closed_at // empty' <<<"$pr_json")
  closed_epoch=$(jq -r 'try (.closed_at | fromdateiso8601) catch empty' <<<"$pr_json")
  if [ -z "$closed_at" ] || [ -z "$closed_epoch" ]; then
    echo "keeping Supabase branch $branch_name (GitHub PR has no valid closed_at)" >&2
    return 1
  fi

  now_epoch=$(date +%s)
  age_seconds=$((now_epoch - closed_epoch))
  if [ "$age_seconds" -lt "$closed_grace_seconds" ]; then
    echo "keeping Supabase branch $branch_name (GitHub PR closed too recently)" >&2
    return 1
  fi
}

while IFS=$'\t' read -r branch_id branch_name pr_number; do
  [ -n "$branch_id" ] || continue
  if [ "$branch_name" = "$current_branch_name" ]; then
    echo "keeping current Supabase branch $branch_name" >&2
    continue
  fi

  if ! pr_json=$(read_pr "$pr_number"); then
    echo "could not read GitHub PR #$pr_number; keeping $branch_name" >&2
    continue
  fi

  if ! is_old_closed_pr "$pr_json" "$branch_name"; then
    continue
  fi

  # Close/reopen can race this cleanup. Narrow that window by confirming the
  # authoritative state once more immediately before the destructive call.
  if ! confirmed_pr_json=$(read_pr "$pr_number"); then
    echo "could not confirm GitHub PR #$pr_number; keeping $branch_name" >&2
    continue
  fi
  if ! is_old_closed_pr "$confirmed_pr_json" "$branch_name"; then
    continue
  fi

  echo "deleting leaked Supabase branch $branch_id ($branch_name; PR closed)" >&2
  if supabase branches delete "$branch_id" \
    --project-ref "$SUPABASE_PROJECT_REF" >&2; then
    pruned=$((pruned + 1))
  else
    echo "failed to delete Supabase branch $branch_id; continuing safely" >&2
  fi
done < <(
  jq -r '
    .[]
    | select(.persistent == false)
    | select(.name | test("^pr-[0-9]+$"))
    | [.id, .name, (.name | ltrimstr("pr-"))]
    | @tsv
  ' <<<"$branches_json"
)

printf '%s\n' "$pruned"
