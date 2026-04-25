#!/usr/bin/env bash
# Wait for the PR's Supabase preview branch and emit its DB URL.
set -euo pipefail

deadline=$(($(date +%s) + 600))
branch=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  branch=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg gb "$GIT_BRANCH" --argjson pr "$PR_NUMBER" '
        first(.[] | select(.persistent != true)
                  | select(.git_branch == $gb or .pr_number == $pr))')

  if [ "$branch" = "null" ] || [ -z "$branch" ]; then
    echo "no branch yet for $GIT_BRANCH"
  else
    status=$(echo "$branch"  | jq -r '.status')
    preview=$(echo "$branch" | jq -r '.preview_project_status')
    echo "branch $(echo "$branch" | jq -r '.id') status=$status preview=$preview"
    case "$status" in
      MIGRATIONS_FAILED|FUNCTIONS_FAILED)
        echo "branch entered terminal status $status" >&2
        exit 1 ;;
      MIGRATIONS_PASSED|FUNCTIONS_DEPLOYED)
        [ "$preview" = "ACTIVE_HEALTHY" ] && break ;;
    esac
  fi
  sleep 10
done

if [ "$branch" = "null" ] || [ -z "$branch" ]; then
  echo "timed out waiting for branch" >&2
  exit 1
fi

branch_id=$(echo "$branch"  | jq -r '.id')
branch_ref=$(echo "$branch" | jq -r '.project_ref')
pg_url=$(supabase branches get "$branch_id" --project-ref "$SUPABASE_PROJECT_REF" -o json \
         | jq -r '.POSTGRES_URL')
# Strip query string (libpq params asyncpg rejects) and switch driver.
db_url=$(printf '%s' "$pg_url" | sed -E 's|^postgresql://|postgresql+asyncpg://|; s|\?.*$||')

{
  echo "ODDISH_DATABASE_URL=$db_url"
} >> "$GITHUB_ENV"
{
  echo "branch_id=$branch_id"
  echo "branch_ref=$branch_ref"
} >> "$GITHUB_OUTPUT"
echo "ready: $branch_ref"
