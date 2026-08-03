#!/usr/bin/env bash
# Reclaim leaked Supabase preview branches whose GitHub PR is already closed.
#
# This is deliberately conservative: only non-persistent branches named
# exactly pr-<number> are considered, the current PR is skipped, and a branch
# is deleted only after GitHub authoritatively reports that PR as closed.
set -euo pipefail

: "${SUPABASE_PROJECT_REF:?}"
: "${GH_TOKEN:?}"
: "${GITHUB_REPOSITORY:?}"

current_branch_name="${CURRENT_BRANCH_NAME:-}"
branches_json=$(supabase branches list \
  --project-ref "$SUPABASE_PROJECT_REF" -o json)
pruned=0

while IFS=$'\t' read -r branch_id branch_name pr_number; do
  [ -n "$branch_id" ] || continue
  if [ "$branch_name" = "$current_branch_name" ]; then
    echo "keeping current Supabase branch $branch_name" >&2
    continue
  fi

  pr_url="https://api.github.com/repos/${GITHUB_REPOSITORY}/pulls/${pr_number}"
  if ! pr_json=$(curl -fsS \
    --retry 3 \
    --retry-delay 2 \
    --retry-all-errors \
    --connect-timeout 10 \
    --max-time 30 \
    -H "Authorization: Bearer $GH_TOKEN" \
    -H "Accept: application/vnd.github+json" \
    -H "X-GitHub-Api-Version: 2022-11-28" \
    "$pr_url"); then
    echo "could not read GitHub PR #$pr_number; keeping $branch_name" >&2
    continue
  fi

  state=$(jq -r '.state // empty' <<<"$pr_json")
  if [ "$state" != "closed" ]; then
    echo "keeping Supabase branch $branch_name (GitHub PR state=${state:-unknown})" >&2
    continue
  fi

  echo "deleting leaked Supabase branch $branch_id ($branch_name; PR closed)" >&2
  if supabase branches delete "$branch_id" \
    --project-ref "$SUPABASE_PROJECT_REF"; then
    pruned=$((pruned + 1))
  else
    echo "failed to delete Supabase branch $branch_id; continuing safely" >&2
  fi
done < <(
  jq -r '
    .[]
    | select(.persistent != true)
    | select(.name | test("^pr-[0-9]+$"))
    | [.id, .name, (.name | ltrimstr("pr-"))]
    | @tsv
  ' <<<"$branches_json"
)

printf '%s\n' "$pruned"
