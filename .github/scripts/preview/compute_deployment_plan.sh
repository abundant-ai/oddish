#!/usr/bin/env bash
# Decide which preview components to redeploy this push, based on:
#   - the PR event action (opened/reopened/synchronize)
#   - per-component change flags emitted by dorny/paths-filter steps,
#     each diffed against a per-component last-successful-deploy SHA
#   - a workflow/scripts-changed flag (forces a full redeploy)
#
# Writes deploy_backend / run_migrations / any_change to GITHUB_OUTPUT
# and a human-readable summary to GITHUB_STEP_SUMMARY.
#
# All inputs come from env vars set by the caller:
#   EVENT_ACTION          - github.event.action
#   BACKEND_BASE          - SHA of last successful backend deploy (or "")
#   MIGRATIONS_BASE       - SHA of last successful migrations run (or "")
#   BACKEND_CHANGED       - "true"/"false"/"" from filter_backend
#   MIGRATIONS_CHANGED    - "true"/"false"/"" from filter_migrations
#   WORKFLOW_CHANGED      - "true"/"false"/"" from filter_workflow
set -euo pipefail

: "${EVENT_ACTION:?}"
: "${GITHUB_OUTPUT:?}"
: "${GITHUB_STEP_SUMMARY:?}"
BACKEND_BASE="${BACKEND_BASE:-}"
MIGRATIONS_BASE="${MIGRATIONS_BASE:-}"
BACKEND_CHANGED="${BACKEND_CHANGED:-}"
MIGRATIONS_CHANGED="${MIGRATIONS_CHANGED:-}"
WORKFLOW_CHANGED="${WORKFLOW_CHANGED:-}"

deploy_backend=false
run_migrations=false

if [ "$EVENT_ACTION" != "synchronize" ] || [ "$WORKFLOW_CHANGED" = "true" ]; then
  deploy_backend=true
  run_migrations=true
else
  # No prior successful deploy on this branch -> deploy now.
  if [ -z "$BACKEND_BASE" ] || [ "$BACKEND_CHANGED" = "true" ]; then
    deploy_backend=true
  fi
  if [ -z "$MIGRATIONS_BASE" ] || [ "$MIGRATIONS_CHANGED" = "true" ]; then
    run_migrations=true
  fi
  # New migrations imply schema change -> backend must redeploy too.
  if [ "$run_migrations" = "true" ]; then
    deploy_backend=true
  fi
fi

any_change=false
if [ "$deploy_backend" = "true" ] || [ "$run_migrations" = "true" ]; then
  any_change=true
fi

{
  echo "deploy_backend=$deploy_backend"
  echo "run_migrations=$run_migrations"
  echo "any_change=$any_change"
} >> "$GITHUB_OUTPUT"

{
  echo "## Preview deployment plan"
  echo
  echo "- Event action: \`$EVENT_ACTION\`"
  echo "- Last successful backend deploy: \`${BACKEND_BASE:-(none)}\`"
  echo "- Last successful migration run: \`${MIGRATIONS_BASE:-(none)}\`"
  echo "- Backend code changed since: \`${BACKEND_CHANGED:-n/a}\`"
  echo "- Migrations changed since: \`${MIGRATIONS_CHANGED:-n/a}\`"
  echo "- Workflow/scripts changed since previous push: \`${WORKFLOW_CHANGED:-n/a}\`"
  echo "- Plan: deploy_backend=\`$deploy_backend\` run_migrations=\`$run_migrations\` any_change=\`$any_change\`"
} >> "$GITHUB_STEP_SUMMARY"
