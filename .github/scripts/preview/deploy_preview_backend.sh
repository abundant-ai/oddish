#!/usr/bin/env bash
# Deploy the Modal backend for the PR preview and verify readiness.
set -euo pipefail

: "${GITHUB_STEP_SUMMARY:?}"
: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
modal_api_url=""

read_output_value() {
  local file="$1"
  local key="$2"
  awk -F= -v key="$key" '$1 == key { value = substr($0, length(key) + 2) } END { print value }' "$file"
}

summarize_modal_phase() {
  {
    echo "## Preview backend"
    echo
    if [ -n "$modal_api_url" ]; then
      echo "- Modal API: $modal_api_url"
    else
      echo "- Modal API: deploy did not produce a URL"
    fi
  } >> "$GITHUB_STEP_SUMMARY"
}

dump_modal_logs() {
  timeout --preserve-status 45s \
    uv run modal app logs --env "$MODAL_ENVIRONMENT" --timestamps "$MODAL_APP_NAME" 2>&1 \
    | tail -300 || true
}

trap summarize_modal_phase EXIT

deploy_output="$(mktemp)"
if ! GITHUB_OUTPUT="$deploy_output" "$script_dir/deploy_modal_preview.sh"; then
  dump_modal_logs
  exit 1
fi
[ -z "$github_output" ] || cat "$deploy_output" >> "$github_output"

modal_api_url="$(read_output_value "$deploy_output" modal_api_url)"
if [ -z "$modal_api_url" ]; then
  echo "modal deploy did not emit modal_api_url" >&2
  exit 1
fi

if ! python "$script_dir/wait_for_modal_ready.py" "$modal_api_url"; then
  dump_modal_logs
  exit 1
fi
