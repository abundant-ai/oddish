#!/usr/bin/env bash
# Orchestrate the PR preview deployment from the GitHub Actions job.
set -euo pipefail

: "${DEPLOY_BACKEND:?}"
: "${RUN_MIGRATIONS:?}"
: "${GITHUB_STEP_SUMMARY:?}"
: "${GITHUB_WORKSPACE:?}"
: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
github_env="${GITHUB_ENV:-}"

modal_api_url=""
preview_url=""
branch_ref=""
branch_was_created=""

is_configured_vercel() {
  [ -n "${VERCEL_TOKEN:-}" ] &&
    [ -n "${VERCEL_ORG_ID:-}" ] &&
    [ -n "${VERCEL_PROJECT_ID:-}" ]
}

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

summarize_preview() {
  {
    echo "## Preview deployment"
    echo
    if [ -n "$modal_api_url" ]; then
      echo "- Modal API: $modal_api_url (redeployed)"
    else
      echo "- Modal API: not redeployed this push (no backend changes since last successful deploy)"
    fi
    if [ -n "$preview_url" ]; then
      echo "- Vercel preview: $preview_url (force-redeployed)"
    else
      echo "- Vercel preview: handled by Vercel's git integration"
    fi
    if [ -n "$branch_ref" ]; then
      echo "- Supabase branch: \`$branch_ref\`"
    fi
    echo "- PR head SHA: ${VERCEL_GIT_COMMIT_SHA:-unknown}"
    echo "- Plan: deploy_backend=\`$DEPLOY_BACKEND\` run_migrations=\`$RUN_MIGRATIONS\` branch_was_created=\`${branch_was_created:-unknown}\`"
  } >> "$GITHUB_STEP_SUMMARY"
}

dump_modal_logs() {
  timeout --preserve-status 45s \
    uv run modal app logs --env "$MODAL_ENVIRONMENT" --timestamps "$MODAL_APP_NAME" 2>&1 \
    | tail -300 || true
}

trap summarize_preview EXIT

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

if [ "$RUN_MIGRATIONS" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/run_preview_migrations.sh"
fi

if [ "$branch_was_created" = "true" ]; then
  "$script_dir/cancel_cloned_preview_work.sh"
fi

if [ "$DEPLOY_BACKEND" = "true" ] || [ "$branch_was_created" = "true" ]; then
  "$script_dir/publish_modal_db_secret.sh"

  deploy_output="$(mktemp)"
  if ! GITHUB_OUTPUT="$deploy_output" "$script_dir/deploy_modal_preview.sh"; then
    dump_modal_logs
    exit 1
  fi
  [ -z "$github_output" ] || cat "$deploy_output" >> "$github_output"

  modal_api_url="$(read_output_value "$deploy_output" modal_api_url)"
  if [ -n "$modal_api_url" ]; then
    if ! python "$script_dir/wait_for_modal_ready.py" "$modal_api_url"; then
      dump_modal_logs
      exit 1
    fi
  fi
fi

if [ -n "$modal_api_url" ] && is_configured_vercel; then
  (
    cd "$GITHUB_WORKSPACE/frontend"
    vercel pull --yes --environment=preview --git-branch="$VERCEL_GIT_BRANCH" --token="$VERCEL_TOKEN"
    printf '%s' "$modal_api_url" \
      | vercel env add NEXT_PUBLIC_API_URL preview "$VERCEL_GIT_BRANCH" --force --no-sensitive --token="$VERCEL_TOKEN"
  )

  vercel_output="$(mktemp)"
  GITHUB_OUTPUT="$vercel_output" python "$script_dir/redeploy_vercel.py"
  [ -z "$github_output" ] || cat "$vercel_output" >> "$github_output"
  preview_url="$(read_output_value "$vercel_output" preview_url)"
fi
