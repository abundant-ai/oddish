#!/usr/bin/env bash
# Stream prod's public-schema data into a freshly-created Supabase
# preview branch. Drop public-schema FK constraints first so prod's
# stray dangling refs don't roll back entire COPYs; pg_restore's
# --disable-triggers can't help here because Supabase doesn't grant
# the superuser needed to disable system FK triggers. The preview is
# throwaway, so we don't re-add the constraints.
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

psql "$branch_url" -v ON_ERROR_STOP=1 <<'SQL'
DO $$
DECLARE r RECORD;
BEGIN
  FOR r IN
    SELECT conrelid::regclass::text AS tbl, conname
    FROM pg_constraint
    WHERE contype = 'f' AND connamespace = 'public'::regnamespace
  LOOP
    EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', r.tbl, r.conname);
  END LOOP;
END $$;
SQL

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
      --dbname="$branch_url"
