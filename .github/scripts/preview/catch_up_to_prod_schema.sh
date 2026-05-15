#!/usr/bin/env bash
# Upgrade the freshly-created Supabase preview branch to prod's exact
# alembic revision for each project, so pg_restore sees a matching
# schema. The subsequent `alembic upgrade head` step in the workflow
# then applies only this PR's net-new revisions, against the
# just-restored prod-shaped data.
set -euo pipefail

: "${PROD_DATABASE_URL:?PROD_DATABASE_URL not set}"
: "${ODDISH_DATABASE_URL:?ODDISH_DATABASE_URL not set}"

# psql doesn't understand the +asyncpg driver tag.
strip_driver() {
  local u="$1"
  u="${u#postgresql+asyncpg://}"
  u="${u#postgresql://}"
  printf 'postgresql://%s' "$u"
}
prod_libpq=$(strip_driver "$PROD_DATABASE_URL")

read_prod_head() {
  local table="$1"
  PGCONNECT_TIMEOUT=30 psql "$prod_libpq" -tAc \
    "SELECT version_num FROM ${table}"
}

upgrade_project() {
  local project_dir="$1" version_table="$2"
  local head
  head=$(read_prod_head "$version_table")
  if [ -z "$head" ]; then
    echo "no row in ${version_table} on prod; skipping ${project_dir}" >&2
    return
  fi
  echo "upgrading ${project_dir} to prod head ${head}" >&2
  ( cd "$project_dir" && alembic upgrade "$head" )
}

upgrade_project "$GITHUB_WORKSPACE/oddish" "alembic_version_oddish"
upgrade_project "$GITHUB_WORKSPACE/backend" "alembic_version_backend"
