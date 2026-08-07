#!/usr/bin/env bash
# Decide which preview components to redeploy this push, based on:
#   - the PR event action (opened/reopened/synchronize)
#   - per-component change flags emitted by find_last_deploys.py,
#     each diffed against a per-component last-successful-deploy SHA
#   - a workflow/scripts-changed flag (forces a full redeploy)
#
# Writes deploy_backend / run_migrations / deploy_frontend / any_change to GITHUB_OUTPUT
# and a human-readable summary to GITHUB_STEP_SUMMARY.
#
# All inputs come from env vars set by the caller:
#   EVENT_ACTION          - github.event.action
#   BACKEND_BASE          - SHA of last successful backend deploy (or "")
#   MIGRATIONS_BASE       - SHA of last successful migrations run (or "")
#   BACKEND_CHANGED       - "true"/"false"/"" from find_last_deploys.py (diff vs backend base)
#   MIGRATIONS_CHANGED    - "true"/"false"/"" from find_last_deploys.py (diff vs migrations base)
#   PR_BACKEND_CHANGED    - "true"/"false" whole-PR fallback (find_last_deploys.py, diff vs PR base)
#   PR_MIGRATIONS_CHANGED - "true"/"false" whole-PR fallback (find_last_deploys.py, diff vs PR base)
#   PR_FRONTEND_CHANGED   - "true"/"false" whole-PR fallback (find_last_deploys.py, diff vs PR base)
#   WORKFLOW_CHANGED      - "true"/"false"/"" from find_last_deploys.py (diff vs previous push)
set -euo pipefail

: "${EVENT_ACTION:?}"
: "${GITHUB_OUTPUT:?}"
: "${GITHUB_STEP_SUMMARY:?}"
BACKEND_BASE="${BACKEND_BASE:-}"
MIGRATIONS_BASE="${MIGRATIONS_BASE:-}"
BACKEND_CHANGED="${BACKEND_CHANGED:-}"
MIGRATIONS_CHANGED="${MIGRATIONS_CHANGED:-}"
PR_BACKEND_CHANGED="${PR_BACKEND_CHANGED:-false}"
PR_MIGRATIONS_CHANGED="${PR_MIGRATIONS_CHANGED:-false}"
PR_FRONTEND_CHANGED="${PR_FRONTEND_CHANGED:-false}"
WORKFLOW_CHANGED="${WORKFLOW_CHANGED:-}"

deploy_backend=false
run_migrations=false
deploy_frontend=false

if [ "$WORKFLOW_CHANGED" = "true" ]; then
  deploy_backend=true
  run_migrations=true
  deploy_frontend=true
else
  if [ "$EVENT_ACTION" != "synchronize" ]; then
    BACKEND_CHANGED="$PR_BACKEND_CHANGED"
    MIGRATIONS_CHANGED="$PR_MIGRATIONS_CHANGED"
  fi

  # No prior successful deploy on this branch -> deploy only if this PR
  # actually changed that component. This keeps frontend-only PRs from
  # provisioning Supabase/Modal just to show a Vercel preview.
  if { [ -z "$BACKEND_BASE" ] && [ "$PR_BACKEND_CHANGED" = "true" ]; } ||
    [ "$BACKEND_CHANGED" = "true" ]; then
    deploy_backend=true
  fi
  if { [ -z "$MIGRATIONS_BASE" ] && [ "$PR_MIGRATIONS_CHANGED" = "true" ]; } ||
    [ "$MIGRATIONS_CHANGED" = "true" ]; then
    run_migrations=true
  fi
  if [ "$PR_FRONTEND_CHANGED" = "true" ] ||
    [ "$deploy_backend" = "true" ] ||
    [ "$run_migrations" = "true" ]; then
    deploy_frontend=true
  fi
fi

# A migrated schema must be boot-tested by the preview backend, and the branch
# DB password rotated by wait_for_supabase_branch.sh must be re-published to
# the Modal secret -- so any migration run also redeploys the backend.
if [ "$run_migrations" = "true" ]; then
  deploy_backend=true
fi

# The same password rotation happens on every prepare-preview-database run,
# and that job also runs on frontend-only pushes whenever a preview backend
# is live (BACKEND_BASE set) to keep the branch smoke-tested. A live backend
# left holding the pre-rotation password 500s every DB-backed request until
# something redeploys it -- so a run that will prepare the branch against an
# existing backend must redeploy that backend too, mirroring the migrations
# rule above.
if [ -n "$BACKEND_BASE" ] && [ "$deploy_backend" != "true" ]; then
  deploy_backend=true
fi

any_change=false
if [ "$deploy_backend" = "true" ] ||
  [ "$run_migrations" = "true" ] ||
  [ "$deploy_frontend" = "true" ]; then
  any_change=true
fi

# A preview backend is live after this run iff we deploy it now or a prior run
# already deployed one (BACKEND_BASE is that run's sha). The Vercel job uses this
# to keep NEXT_PUBLIC_API_URL pointed at the preview backend on frontend-only
# follow-up pushes -- otherwise a blank MODAL_API_URL falls back to prod and the
# preview frontend starts querying production. Stays false for frontend-only PRs
# that never provisioned a backend, so those correctly fall back to prod.
preview_backend_live=false
if [ "$deploy_backend" = "true" ] || [ -n "$BACKEND_BASE" ]; then
  preview_backend_live=true
fi

{
  echo "deploy_backend=$deploy_backend"
  echo "run_migrations=$run_migrations"
  echo "deploy_frontend=$deploy_frontend"
  echo "any_change=$any_change"
  echo "preview_backend_live=$preview_backend_live"
} >> "$GITHUB_OUTPUT"

{
  echo "## Preview deployment plan"
  echo
  echo "- Event action: \`$EVENT_ACTION\`"
  echo "- Last successful backend deploy: \`${BACKEND_BASE:-(none)}\`"
  echo "- Last successful migration run: \`${MIGRATIONS_BASE:-(none)}\`"
  echo "- Backend code changed since: \`${BACKEND_CHANGED:-n/a}\`"
  echo "- Migrations changed since: \`${MIGRATIONS_CHANGED:-n/a}\`"
  echo "- PR backend changes: \`$PR_BACKEND_CHANGED\`"
  echo "- PR migration changes: \`$PR_MIGRATIONS_CHANGED\`"
  echo "- PR frontend changes: \`$PR_FRONTEND_CHANGED\`"
  echo "- Workflow/scripts changed since previous push: \`${WORKFLOW_CHANGED:-n/a}\`"
  echo "- Plan: deploy_backend=\`$deploy_backend\` run_migrations=\`$run_migrations\` deploy_frontend=\`$deploy_frontend\` any_change=\`$any_change\`"
} >> "$GITHUB_STEP_SUMMARY"
