#!/usr/bin/env bash
# Point the Vercel preview deployment at the selected backend target.
set -euo pipefail

: "${GITHUB_STEP_SUMMARY:?}"
: "${GITHUB_WORKSPACE:?}"
: "${VERCEL_GIT_BRANCH:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
github_output="${GITHUB_OUTPUT:-}"
preview_url=""
preview_alias_url=""
backend_api_url="${MODAL_API_URL:-${PROD_API_URL:-}}"
backend_label="${PREVIEW_BACKEND_LABEL:-}"
database_label="${PREVIEW_DATABASE_LABEL:-}"

if [ -z "$backend_label" ]; then
  if [ -n "${MODAL_API_URL:-}" ]; then
    backend_label="${MODAL_APP_NAME:-preview Modal backend}"
  else
    backend_label="production"
  fi
fi

if [ -z "$database_label" ]; then
  if [ -n "${SUPABASE_BRANCH_REF:-}" ]; then
    database_label="project ${SUPABASE_BRANCH_REF}"
  elif [ -n "${MODAL_API_URL:-}" ]; then
    database_label="preview Supabase"
  else
    database_label="production"
  fi
fi

# branch_ref is the preview branch's own Supabase project ref, so link it to
# that project's dashboard. Prod DB stays plain text (no per-PR dashboard).
database_url=""
if [ -n "${SUPABASE_BRANCH_REF:-}" ]; then
  database_url="https://supabase.com/dashboard/project/${SUPABASE_BRANCH_REF}"
fi

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

wait_for_url_ready() {
  local url="$1"
  local attempt
  local status

  for attempt in $(seq 1 60); do
    status="$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 10 "$url" || true)"
    case "$status" in
      2*|3*|401|403)
        return 0
        ;;
    esac
    echo "Waiting for Vercel preview to answer at $url (attempt $attempt/60, status ${status:-none})"
    sleep 5
  done

  echo "Vercel preview never became reachable at $url" >&2
  return 1
}

summarize_vercel_phase() {
  {
    echo "## Vercel preview"
    echo
    if [ -n "$preview_url" ]; then
      echo "- Vercel preview: $preview_url"
      if [ -n "$preview_alias_url" ]; then
        echo "- Stable alias: $preview_alias_url"
      fi
    elif is_configured_vercel; then
      echo "- Vercel preview: redeploy did not produce a URL"
    else
      echo "- Vercel preview: skipped because Vercel credentials are not configured"
    fi
    echo "- Backend target: $backend_label"
    echo "- Database target: $database_label"
  } >> "$GITHUB_STEP_SUMMARY"
}

set_vercel_env() {
  local name="$1"
  local value="$2"

  [ -n "$value" ] || return 0
  printf '%s' "$value" \
    | vercel env add "$name" preview "$VERCEL_GIT_BRANCH" --force --no-sensitive --token="$VERCEL_TOKEN"
}

trap summarize_vercel_phase EXIT

if ! is_configured_vercel; then
  exit 0
fi

# Vercel's GitHub integration auto-builds every push with the branch env vars
# that existed at push time. When those vars already match what we'd write,
# skip the rewrite AND the force-redeploy: the auto-created build is already
# correct, and forcing a second build of the same commit only burns minutes.
# Any drift (or any doubt) takes the old rewrite + force-redeploy path.
force_redeploy_file="$(mktemp)"
echo true > "$force_redeploy_file"

(
  cd "$GITHUB_WORKSPACE/frontend"
  vercel pull --yes --environment=preview --git-branch="$VERCEL_GIT_BRANCH" --token="$VERCEL_TOKEN"

  desired_env=(
    "NEXT_PUBLIC_ODDISH_PREVIEW=true"
    "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL=$backend_label"
    "NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL=$database_label"
  )
  if [ -n "$backend_api_url" ]; then
    desired_env+=(
      "NEXT_PUBLIC_API_URL=$backend_api_url"
      "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL=$backend_api_url"
    )
  fi
  [ -z "$database_url" ] || desired_env+=("NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL=$database_url")
  [ -z "${PR_URL:-}" ] || desired_env+=("NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL=$PR_URL")
  [ -z "${PR_TITLE:-}" ] || desired_env+=("NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE=$PR_TITLE")

  if [ -n "$backend_api_url" ] &&
    python "$script_dir/vercel_env_diff.py" .vercel/.env.preview.local "${desired_env[@]}"; then
    echo "Per-branch Vercel env already up to date; reusing the auto-created deployment."
    echo false > "$force_redeploy_file"
    exit 0
  fi

  if [ -n "$backend_api_url" ]; then
    set_vercel_env NEXT_PUBLIC_API_URL "$backend_api_url"
  else
    vercel env rm NEXT_PUBLIC_API_URL preview "$VERCEL_GIT_BRANCH" --yes --token="$VERCEL_TOKEN" || true
  fi
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW true
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL "$backend_label"
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL "$backend_api_url"
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL "$database_label"
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL "$database_url"
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL "${PR_URL:-}"
  set_vercel_env NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE "${PR_TITLE:-}"
)

vercel_output="$(mktemp)"
GITHUB_OUTPUT="$vercel_output" VERCEL_FORCE_REDEPLOY="$(cat "$force_redeploy_file")" \
  python "$script_dir/redeploy_vercel.py"
[ -z "$github_output" ] || cat "$vercel_output" >> "$github_output"
preview_url="$(read_output_value "$vercel_output" preview_url)"
if [ -n "$preview_url" ]; then
  # The reused auto-created deployment may still be building; block until it
  # is READY before aliasing or probing it.
  vercel inspect "$preview_url" --wait --timeout=10m --scope "$VERCEL_ORG_ID" --token="$VERCEL_TOKEN" >/dev/null
fi
if [ -n "${PREVIEW_ALIAS_HOSTNAME:-}" ] && [ -n "$preview_url" ]; then
  vercel alias set "$preview_url" "$PREVIEW_ALIAS_HOSTNAME" --scope "$VERCEL_ORG_ID" --token="$VERCEL_TOKEN"
  preview_alias_url="https://$PREVIEW_ALIAS_HOSTNAME"
  wait_for_url_ready "$preview_alias_url"
elif [ -n "$preview_url" ]; then
  wait_for_url_ready "$preview_url"
fi
if [ -n "$github_output" ]; then
  {
    echo "preview_alias_url=$preview_alias_url"
    echo "backend_api_url=$backend_api_url"
    echo "backend_label=$backend_label"
    echo "database_label=$database_label"
  } >> "$github_output"
fi
