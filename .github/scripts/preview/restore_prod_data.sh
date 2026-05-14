#!/usr/bin/env bash
# Populate a freshly-created Supabase preview branch with prod data
# by streaming a pg_dump/pg_restore through the runner. Replaces
# Supabase's `branches create --with-data`, whose logical-replication
# clone was the dominant cost (~20 min) of preview spin-up.
#
# The dump is written only to the runner's tmpfs and deleted on exit —
# it never lands in a release/artifact/bucket, which matters because
# the repo is public.
#
# Supabase branch creation already clones the project's full DDL
# (public schema, types, indexes, constraints, triggers, etc.), so we
# only dump and restore *data*. No --schema=auth: public.* tables
# don't FK into auth.*, the postgres role on the branch isn't owner
# of auth.audit_log_entries so --disable-triggers fails there, and
# preview app flows don't need prod auth rows.
#
# Restore is single-threaded because pg_dump --data-only writes COPY
# statements in FK-dependency order — parallel restore (--jobs >1)
# would interleave them and trip foreign-key constraints.
set -uo pipefail

: "${PROD_DATABASE_URL:?PROD_DATABASE_URL not set}"
: "${ODDISH_DATABASE_URL:?ODDISH_DATABASE_URL not set (run after wait_for_supabase_branch.sh)}"

# pg_* tools don't understand SQLAlchemy's +asyncpg dialect prefix.
strip_driver() {
  local u="$1"
  u="${u#postgresql+asyncpg://}"
  u="${u#postgresql://}"
  printf 'postgresql://%s' "$u"
}
prod_url=$(strip_driver "$PROD_DATABASE_URL")
branch_url=$(strip_driver "$ODDISH_DATABASE_URL")

dump_dir=$(mktemp -d)
trap 'rm -rf "$dump_dir"' EXIT

echo "dumping prod public-schema data..." >&2
PGCONNECT_TIMEOUT=30 pg_dump \
  --format=custom \
  --data-only \
  --schema=public \
  --no-owner --no-acl \
  --no-publications --no-subscriptions \
  --file="$dump_dir/data.dump" \
  "$prod_url"

ls -lh "$dump_dir" >&2

echo "restoring data to preview branch..." >&2
pg_restore \
  --no-owner --no-acl \
  --data-only \
  --exit-on-error \
  --dbname="$branch_url" \
  "$dump_dir/data.dump"

echo "restore complete" >&2
