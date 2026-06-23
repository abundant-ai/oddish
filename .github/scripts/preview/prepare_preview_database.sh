#!/usr/bin/env bash
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
schema_upgraded=false
schema_rebuilt=false

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
    echo "- Schema upgraded to head: \`$schema_upgraded\`"
    echo "- Schema rebuilt from base: \`$schema_rebuilt\`"
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

schema_rebuilt_file="$(mktemp)"
export SCHEMA_REBUILT_FILE="$schema_rebuilt_file"

# Migrate on any backend deploy, not just migration-file changes, so a reused
# branch can't run new code against a stale schema.
if [ "$DEPLOY_BACKEND" = "true" ] || [ "$RUN_MIGRATIONS" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/run_preview_migrations.sh"
  schema_upgraded=true
fi

[ -s "$schema_rebuilt_file" ] && schema_rebuilt=true

# Seed is expensive: only when fresh, on an explicit migration run, or after a
# rebuild dropped the branch's data.
if [ "$schema_rebuilt" = "true" ] || [ "$RUN_MIGRATIONS" = "true" ] || [ "$branch_was_created" = "true" ]; then
  ( cd "$GITHUB_WORKSPACE/backend" && uv run python "$script_dir/seed_preview_db.py" )
fi

if [ "$DEPLOY_BACKEND" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/publish_modal_db_secret.sh"
  published_modal_secret=true
fi
