#!/usr/bin/env bash
# Ensure the PR database exists, run pending migrations, seed curated
# fixtures, and publish the Modal DB secret needed by the backend deploy phase.
set -euo pipefail

: "${DEPLOY_BACKEND:?}"
: "${RUN_MIGRATIONS:?}"
: "${GITHUB_STEP_SUMMARY:?}"
: "${GITHUB_WORKSPACE:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
github_env="${GITHUB_ENV:-}"
branch_ref=""
branch_was_created=""
published_modal_secret=false

read_output_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$file"
}

load_env_file() {
  local file="$1"
  local key value

  while IFS='=' read -r key value; do
    [ -n "$key" ] || continue
    export "$key=$value"
  done < "$file"
}

summarize_database_phase() {
  {
    echo "## Preview database"
    echo
    if [ -n "$branch_ref" ]; then
    echo "- Supabase branch: \`$branch_ref\`"
    fi
    echo "- Branch created: \`${branch_was_created:-unknown}\`"
    echo "- Migrations requested: \`$RUN_MIGRATIONS\`"
    echo "- Modal DB secret published: \`$published_modal_secret\`"
  } >> "$GITHUB_STEP_SUMMARY"
}

trap summarize_database_phase EXIT

supabase_env="$(mktemp)"
supabase_output="$(mktemp)"
GITHUB_ENV="$supabase_env" GITHUB_OUTPUT="$supabase_output" "$script_dir/wait_for_supabase_branch.sh"
load_env_file "$supabase_env"
[ -z "$github_env" ] || cat "$supabase_env" >> "$github_env"
[ -z "$github_output" ] || cat "$supabase_output" >> "$github_output"

branch_ref="$(read_output_value "$supabase_output" branch_ref)"
branch_was_created="$(read_output_value "$supabase_output" branch_was_created)"

# bootstrap_preview_db.py stamps the inherited schema to the parent's revision
# only for a brand-new branch (so the upgrade applies just this PR's migrations
# instead of replaying history). Export the flag so it reaches that subprocess.
export BRANCH_WAS_CREATED="$branch_was_created"

if [ "$RUN_MIGRATIONS" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/run_preview_migrations.sh"
fi

# Seed runs on EVERY prepare (not gated by RUN_MIGRATIONS/branch_was_created):
# branches are reused across pushes, so a backend-only push would otherwise
# skip seeding and leave stale fixtures. The seed is idempotent + convergent.
( cd "$GITHUB_WORKSPACE/backend" && uv run python "$script_dir/seed_preview_db.py" )

if [ "$DEPLOY_BACKEND" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/publish_modal_db_secret.sh"
  published_modal_secret=true
fi
