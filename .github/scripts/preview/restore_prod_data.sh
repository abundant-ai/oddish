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
# Restore order is auth-data-first then public-schema-and-data, so
# foreign keys from public.* into auth.users(id) validate.
# Supabase provisions auth.* DDL on every new branch, so we only need
# to load row data into it; the public schema is empty on the branch
# (Supabase migrations dir is intentionally empty — Alembic is the
# source of truth) so we restore both DDL and data.
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

echo "dumping auth schema (data only) from prod..." >&2
PGCONNECT_TIMEOUT=30 pg_dump \
  --format=custom \
  --schema=auth \
  --data-only \
  --no-owner --no-acl \
  --no-publications --no-subscriptions \
  --file="$dump_dir/auth.dump" \
  "$prod_url"

echo "dumping public schema (DDL + data) from prod..." >&2
PGCONNECT_TIMEOUT=30 pg_dump \
  --format=custom \
  --schema=public \
  --no-owner --no-acl \
  --no-publications --no-subscriptions \
  --file="$dump_dir/public.dump" \
  "$prod_url"

ls -lh "$dump_dir" >&2

# --disable-triggers needs superuser on the destination, which the
# Supabase branch `postgres` role has. Without it, restoring rows
# into auth.* tables that have internal triggers may fail.
echo "restoring auth data to preview branch..." >&2
pg_restore \
  --no-owner --no-acl \
  --data-only \
  --disable-triggers \
  --single-transaction \
  --dbname="$branch_url" \
  "$dump_dir/auth.dump"

echo "restoring public schema + data to preview branch..." >&2
pg_restore \
  --no-owner --no-acl \
  --jobs=4 \
  --dbname="$branch_url" \
  "$dump_dir/public.dump"

echo "restore complete" >&2
