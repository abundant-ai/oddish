#!/usr/bin/env bash
# Publish the per-PR preview database URL as a Modal secret.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"
: "${MODAL_APP_NAME:?}"
: "${ODDISH_DATABASE_URL:?}"

uv run modal secret create \
  --env "$MODAL_ENVIRONMENT" --force \
  "$MODAL_APP_NAME-db" \
  "ODDISH_DATABASE_URL=$ODDISH_DATABASE_URL"
