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

# ``--disable-triggers`` makes pg_restore wrap the data load in
# ``SET session_replication_role = replica``, which skips FK trigger
# enforcement during COPY. Prod has a handful of dangling FK refs
# (e.g. a task whose ``current_version_id`` points at a soft-deleted
# / pruned ``task_versions`` row) that would otherwise abort the
# whole ``tasks`` COPY -- and with it the entire preview restore --
# because of a single bad row. We're loading into a throwaway
# preview, so accepting those orphans is fine; the constraint stays
# defined on the target, just unenforced for this one load.
#
# Dropping ``--exit-on-error`` for the same reason: if some other
# table hits an unrelated hiccup we'd rather get a mostly-populated
# preview than no preview at all. pg_restore still prints each
# error and exits non-zero if anything failed, so real breakage is
# still visible in the workflow log.
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
      --disable-triggers \
      --dbname="$branch_url"
