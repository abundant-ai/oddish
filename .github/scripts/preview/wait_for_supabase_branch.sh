#!/usr/bin/env bash
# Wait for the PR's Supabase preview branch and emit its DB URL.
set -uo pipefail

deadline=$(($(date +%s) + 600))
ready=0
branch_id="" branch_ref="" status="" preview=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  branch_json=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg gb "$GIT_BRANCH" --argjson pr "$PR_NUMBER" '
        first(.[] | select(.persistent != true)
                  | select(.git_branch == $gb or .pr_number == $pr))')

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
{ echo "branch_id=$branch_id"; echo "branch_ref=$branch_ref"; } >> "$GITHUB_OUTPUT"
