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
schema_healed=false
seed_stats_file=""

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

summarize_seed_line() {
  if [ -z "$seed_stats_file" ] || [ ! -s "$seed_stats_file" ]; then
    echo "- Seed stats: unavailable (seed did not run)"
    return
  fi
  python3 -c '
import json, sys
d = json.load(open(sys.argv[1]))
print("- Seed: `{batches_attempted}` batches attempted, `{batches_split}` split,"
      " `{rows_skipped}` rows skipped in `{seed_seconds}s`".format(
          **{**d, "seed_seconds": d.get("seed_seconds", "?")}))
' "$seed_stats_file" 2>/dev/null || echo "- Seed stats: unparseable"
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
    echo "- Schema rebuilt from prod snapshot: \`$schema_rebuilt\`"
    echo "- Auto-healed after a failed incremental upgrade: \`$schema_healed\`"
    summarize_seed_line
    echo "- Modal DB secret published: \`$published_modal_secret\`"
  } >> "$GITHUB_STEP_SUMMARY"
}

trap summarize_database_phase EXIT

if [ "$DEPLOY_BACKEND" = "true" ]; then
  "$script_dir/stop_modal_preview_app.sh" || true
fi

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
seed_stats_file="$(mktemp)"
export PREVIEW_SEED_STATS_FILE="$seed_stats_file"

# Migrate on any backend deploy, not just migration-file changes, so a reused
# branch can't run new code against a stale schema.
if [ "$DEPLOY_BACKEND" = "true" ] || [ "$RUN_MIGRATIONS" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/run_preview_migrations.sh"
  schema_upgraded=true
fi

if [ -s "$schema_rebuilt_file" ]; then
  schema_rebuilt=true
  [ "$(cat "$schema_rebuilt_file")" = "healed" ] && schema_healed=true
fi

# The bootstrap seeds internally when it rebuilds -- before the PR's migrations
# run, so they execute against realistic data. Here we only converge the sample
# on a reused, trusted branch (fresh branches always rebuild).
if [ "$schema_rebuilt" != "true" ] && { [ "$RUN_MIGRATIONS" = "true" ] || [ "$branch_was_created" = "true" ]; }; then
  ( cd "$GITHUB_WORKSPACE/backend" && uv run python "$script_dir/seed_preview_db.py" )
fi

if [ "$DEPLOY_BACKEND" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/publish_modal_db_secret.sh"
  published_modal_secret=true
fi

# Hand the run-level seed report to the gate's timing artifact. Single-line
# JSON written by seed_preview_db.py (both the rebuild-internal and the
# converge seed paths); whichever ran last is the run's authoritative draw.
if [ -s "$seed_stats_file" ] && [ -n "$github_output" ]; then
  echo "seed_stats=$(cat "$seed_stats_file")" >> "$github_output"
fi
