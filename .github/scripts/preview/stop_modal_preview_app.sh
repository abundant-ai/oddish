#!/usr/bin/env bash
# Stop the existing PR Modal app before preview DB password rotation.
#
# A previous app may still hold connections with the old branch password.
# Stopping it before the database step prevents reconnect storms against
# Supavisor after the password is rotated.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"

modal app stop -y --env "$MODAL_ENVIRONMENT" "$MODAL_APP_NAME" || true
