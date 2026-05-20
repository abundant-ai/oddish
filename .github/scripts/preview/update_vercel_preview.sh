#!/usr/bin/env bash
# Point the Vercel preview deployment at the PR-specific Modal backend.
set -euo pipefail

: "${GITHUB_STEP_SUMMARY:?}"
: "${GITHUB_WORKSPACE:?}"
: "${MODAL_API_URL:?}"
: "${VERCEL_GIT_BRANCH:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
preview_url=""

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

summarize_vercel_phase() {
  {
    echo "## Vercel preview"
    echo
    if [ -n "$preview_url" ]; then
      echo "- Vercel preview: $preview_url"
    elif is_configured_vercel; then
      echo "- Vercel preview: redeploy did not produce a URL"
    else
      echo "- Vercel preview: skipped because Vercel credentials are not configured"
    fi
    echo "- Modal API target: $MODAL_API_URL"
  } >> "$GITHUB_STEP_SUMMARY"
}

trap summarize_vercel_phase EXIT

if ! is_configured_vercel; then
  exit 0
fi

(
  cd "$GITHUB_WORKSPACE/frontend"
  vercel pull --yes --environment=preview --git-branch="$VERCEL_GIT_BRANCH" --token="$VERCEL_TOKEN"
  printf '%s' "$MODAL_API_URL" \
    | vercel env add NEXT_PUBLIC_API_URL preview "$VERCEL_GIT_BRANCH" --force --no-sensitive --token="$VERCEL_TOKEN"
)

vercel_output="$(mktemp)"
GITHUB_OUTPUT="$vercel_output" python "$script_dir/redeploy_vercel.py"
[ -z "$github_output" ] || cat "$vercel_output" >> "$github_output"
preview_url="$(read_output_value "$vercel_output" preview_url)"
