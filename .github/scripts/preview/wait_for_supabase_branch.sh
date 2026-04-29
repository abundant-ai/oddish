#!/usr/bin/env bash
# Create (or recreate) the PR's Supabase preview branch *with prod data*
# and emit its DB URL.
#
# Background: Supabase's GitHub integration auto-creates a schema-only
# branch on PR open. Migration safety needs prod-shaped data, so we
# explicitly run `supabase branches create --with-data` here. If a
# branch already exists for this PR (integration auto-created, or a
# prior run of this workflow), it's torn down first because
# `--with-data` cannot retroactively populate an existing branch.
#
# Disable the integration's auto-create in the Supabase project's
# GitHub integration settings to avoid leaving an orphan branch every
# time. Without that, the orphan is harmless but costs $.
set -uo pipefail

BRANCH_NAME="pr-${PR_NUMBER}"

list_pr_branches_json() {
  supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg gb "$GIT_BRANCH" --argjson pr "$PR_NUMBER" --arg name "$BRANCH_NAME" '
        [ .[] | select(.persistent != true)
              | select(.git_branch == $gb or .pr_number == $pr or .name == $name) ]'
}

# Tear down any existing ephemeral branch tied to this PR. Branches
# can be in DELETING for a few seconds; poll until they're gone before
# the create call.
existing_ids=$(list_pr_branches_json | jq -r '.[].id')
if [ -n "$existing_ids" ]; then
  for branch_id in $existing_ids; do
    echo "deleting existing preview branch $branch_id" >&2
    supabase branches delete "$branch_id" --project-ref "$SUPABASE_PROJECT_REF" || true
  done

  # Wait for the delete(s) to actually disappear so the create below
  # doesn't collide on name.
  delete_deadline=$(($(date +%s) + 120))
  while [ "$(date +%s)" -lt "$delete_deadline" ]; do
    remaining=$(list_pr_branches_json | jq -r 'length')
    if [ "$remaining" = "0" ]; then break; fi
    sleep 5
  done
  if [ "$(list_pr_branches_json | jq -r 'length')" != "0" ]; then
    echo "timed out waiting for old branch(es) to delete" >&2
    exit 1
  fi
fi

echo "creating $BRANCH_NAME with --with-data" >&2
supabase branches create "$BRANCH_NAME" \
  --with-data \
  --project-ref "$SUPABASE_PROJECT_REF"

# Wait until the branch is ready. Cloning prod data can take a while;
# 20 min is a generous ceiling against the GHA job timeout.
deadline=$(($(date +%s) + 1200))
ready=0
branch_id="" branch_ref="" status="" preview=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  branch_json=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg name "$BRANCH_NAME" 'first(.[] | select(.name == $name))')

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
