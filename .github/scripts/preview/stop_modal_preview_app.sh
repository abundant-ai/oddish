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
# On a real teardown failure the old app must KEEP RUNNING: the redeploy
# that motivates this stop is not guaranteed to arrive (a migration
# failure or a cancelled workflow ends the run first), a stopped app has
# no scheduled reaper, and the stale-app pruner ignores stopped apps --
# so stopping here would leave the undeleted cluster with no owner at
# all. The deploy that does arrive replaces the app in place, reaper
# included; skipping the stop only forgoes the reconnect-storm hygiene
# for this one rare case.
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if ! modal run --env "$MODAL_ENVIRONMENT" \
  "$script_dir/teardown_gke_cluster.py" \
  --app-name "$MODAL_APP_NAME"; then
  echo "::warning::GKE teardown failed; leaving $MODAL_APP_NAME running so its reaper still owns the cluster until the redeploy replaces it"
  exit 0
fi

modal app stop -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME" || true
