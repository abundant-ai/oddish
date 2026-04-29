#!/usr/bin/env bash
# Ensure the PR's Supabase preview branch (cloned from prod via
# --with-data) exists and is healthy, and emit its DB URL.
#
# On first invocation for a PR, creates the branch with prod data so
# the subsequent `alembic upgrade head` runs against prod-shaped data
# (the migration-safety check). On later pushes within the same PR
# the existing branch is reused — append-only migrations apply
# incrementally, which is the common case. If the dev rewrites
# Alembic history mid-PR and the incremental upgrade can't handle it,
# delete the branch via the Supabase dashboard (or close+reopen the
# PR) to force a fresh prod clone.
#
# Disable the Supabase GitHub integration's auto-branching for this
# repo so it doesn't create a parallel data-less branch in the same
# project on PR open.
set -uo pipefail

BRANCH_NAME="pr-${PR_NUMBER}"

find_branch_json() {
  supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg gb "$GIT_BRANCH" --argjson pr "$PR_NUMBER" --arg name "$BRANCH_NAME" '
        first(.[] | select(.persistent != true)
                  | select(.git_branch == $gb or .pr_number == $pr or .name == $name))'
}

existing=$(find_branch_json)
if [ -z "$existing" ] || [ "$existing" = "null" ]; then
  echo "creating $BRANCH_NAME with --with-data" >&2
  supabase branches create "$BRANCH_NAME" \
    --with-data \
    --project-ref "$SUPABASE_PROJECT_REF"
else
  echo "reusing existing branch $(echo "$existing" | jq -r '.id')" >&2
fi

# Wait until the branch is ready. First creation includes the prod
# clone, so give it 20 min; subsequent runs short-circuit fast.
deadline=$(($(date +%s) + 1200))
ready=0
branch_id="" branch_ref="" status="" preview=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  branch_json=$(find_branch_json)

  if [ -n "$branch_json" ] && [ "$branch_json" != "null" ]; then
    read -r branch_id branch_ref status preview < <(
      jq -r '[.id, .project_ref, .status, .preview_project_status] | @tsv' <<<"$branch_json"
    )
    case "$status" in
      MIGRATIONS_FAILED|FUNCTIONS_FAILED)
        echo "branch $branch_id failed: $status" >&2
        exit 1
        ;;
      MIGRATIONS_PASSED|FUNCTIONS_DEPLOYED)
        [ "$preview" = "ACTIVE_HEALTHY" ] && { ready=1; break; }
        ;;
    esac
  fi
  sleep 10
done

if [ "$ready" -ne 1 ]; then
  echo "timed out (status=$status preview=$preview)" >&2
  exit 1
fi

pg_url=$(supabase branches get "$branch_id" --project-ref "$SUPABASE_PROJECT_REF" -o json \
         | jq -r '.POSTGRES_URL')
db_url="${pg_url%%\?*}"
db_url="postgresql+asyncpg://${db_url#postgresql://}"

echo "ODDISH_DATABASE_URL=$db_url" >> "$GITHUB_ENV"
{
  echo "branch_id=$branch_id"
  echo "branch_ref=$branch_ref"
} >> "$GITHUB_OUTPUT"
