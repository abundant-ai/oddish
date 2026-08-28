#!/usr/bin/env bash
# Tear down all per-PR preview resources.
set -euo pipefail

: "${GITHUB_WORKSPACE:?}"
: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"
: "${SUPABASE_PROJECT_REF:?}"
: "${VERCEL_GIT_BRANCH:?}"

branch_name="${BRANCH_NAME:-pr-${PR_NUMBER:?}}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

is_configured_vercel() {
  [ -n "${VERCEL_TOKEN:-}" ] &&
    [ -n "${VERCEL_ORG_ID:-}" ] &&
    [ -n "${VERCEL_PROJECT_ID:-}" ]
}

# The app must delete its own auto-provisioned trials cluster BEFORE it is
# stopped: the credentials live only inside the app, and its scheduled idle
# reaper dies with it. A missing function or a missing cluster is a skip; a
# REAL teardown failure must not be followed by the stop, because stopping
# the app would destroy the one remaining owner that can still delete the
# cluster. Fail the close job instead -- the app and its reaper stay alive,
# and re-running the workflow retries the teardown.
if ! "$script_dir/run_gke_teardown.sh" "$MODAL_APP_NAME"; then
  echo "::error::GKE teardown failed; leaving $MODAL_APP_NAME running so its reaper still owns the cluster"
  exit 1
fi

modal app stop -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME" || true
# -y matters: `modal secret delete` click.confirm()s without it, which in a
# non-tty CI job raises Abort — the `|| true` then swallowed the failure, so
# this line had been silently leaking one orphaned secret per closed PR.
modal secret delete -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME-db" || true

ids=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
  | jq -r --arg name "$branch_name" '
      .[] | select(.persistent != true)
          | select(.name == $name)
          | .id')

for id in $ids; do
  echo "deleting Supabase branch $id"
  supabase branches delete "$id" --project-ref "$SUPABASE_PROJECT_REF" || true
done

if is_configured_vercel; then
  (
    cd "$GITHUB_WORKSPACE/frontend"
    vercel pull --yes --environment=preview --git-branch="$VERCEL_GIT_BRANCH" --token="$VERCEL_TOKEN"
    for name in \
      NEXT_PUBLIC_API_URL \
      NEXT_PUBLIC_ODDISH_PREVIEW \
      NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_LABEL \
      NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL \
      NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL \
      NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL \
      NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL \
      NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE; do
      vercel env rm "$name" preview "$VERCEL_GIT_BRANCH" --yes --token="$VERCEL_TOKEN" || true
    done
    if [ -n "${PREVIEW_ALIAS_HOSTNAME:-}" ]; then
      vercel alias rm "$PREVIEW_ALIAS_HOSTNAME" --scope "$VERCEL_ORG_ID" --yes --token="$VERCEL_TOKEN" || true
    fi
  )
fi
