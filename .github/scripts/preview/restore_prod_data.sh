#!/usr/bin/env bash
# Stream prod's public-schema data into a freshly-created Supabase
# preview branch. Replaces `branches create --with-data` (~20 min
# clone). Single-threaded so pg_dump's FK-dependency ordering holds.
set -uo pipefail

: "${PROD_DATABASE_URL:?PROD_DATABASE_URL not set}"
: "${ODDISH_DATABASE_URL:?ODDISH_DATABASE_URL not set}"

strip_driver() {
  local u="$1"
  u="${u#postgresql+asyncpg://}"
  u="${u#postgresql://}"
  printf 'postgresql://%s' "$u"
}
prod_url=$(strip_driver "$PROD_DATABASE_URL")
branch_url=$(strip_driver "$ODDISH_DATABASE_URL")

PGCONNECT_TIMEOUT=30 pg_dump \
  --format=custom \
  --data-only \
  --schema=public \
  --no-owner --no-acl \
  --no-publications --no-subscriptions \
  "$prod_url" \
  | pg_restore \
      --no-owner --no-acl \
      --data-only \
      --exit-on-error \
      --dbname="$branch_url"
