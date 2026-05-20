#!/usr/bin/env bash
# Tear down all per-PR preview resources.
set -euo pipefail

: "${GITHUB_WORKSPACE:?}"
: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"
: "${SUPABASE_PROJECT_REF:?}"
: "${VERCEL_GIT_BRANCH:?}"

branch_name="${BRANCH_NAME:-pr-${PR_NUMBER:?}}"

is_configured_vercel() {
  [ -n "${VERCEL_TOKEN:-}" ] &&
    [ -n "${VERCEL_ORG_ID:-}" ] &&
    [ -n "${VERCEL_PROJECT_ID:-}" ]
}

modal app stop -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME" || true
modal secret delete --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME-db" || true

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
      NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_LABEL \
      NEXT_PUBLIC_ODDISH_PREVIEW_COMMIT_SHA; do
      vercel env rm "$name" preview "$VERCEL_GIT_BRANCH" --yes --token="$VERCEL_TOKEN" || true
    done
    if [ -n "${PREVIEW_ALIAS_HOSTNAME:-}" ]; then
      vercel alias rm "$PREVIEW_ALIAS_HOSTNAME" --scope "$VERCEL_ORG_ID" --yes --token="$VERCEL_TOKEN" || true
    fi
  )
fi
