#!/usr/bin/env bash
# Stop the existing PR Modal app before preview DB password rotation.
#
# A previous app may still hold connections with the old branch password.
# Stopping it before the database step prevents reconnect storms against
# Supavisor after the password is rotated.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"

# Delete the app's auto-provisioned trials cluster BEFORE stopping it, same
# as PR-close teardown: the credentials live only inside the app, and once
# the app is stopped nothing can delete the cluster -- if the redeploy that
# follows this stop fails, the cluster would otherwise bill with no owner
# until someone noticed. The cost is deliberate: every redeploy drops the
# cluster and the next trial re-provisions it. Correct and billing-safe
# beats warm.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
modal run --env "$MODAL_ENVIRONMENT" \
  "$script_dir/teardown_gke_cluster.py" \
  --app-name "$MODAL_APP_NAME" || true

modal app stop -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME" || true
