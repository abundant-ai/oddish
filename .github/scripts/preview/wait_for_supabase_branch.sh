#!/usr/bin/env bash
# Ensure the PR's Supabase preview branch (cloned from prod via
# --with-data) exists and is healthy, and emit its DB URL.
#
# On first invocation for a PR, creates the branch with prod data so
# the subsequent `alembic upgrade head` runs against prod-shaped data
# (the migration-safety check). On later pushes within the same PR
# the existing branch is reused — append-only migrations apply
# incrementally, which is the common case. If the dev rewrites
# Alembic history mid-PR and the incremental upgrade can't handle it,
# delete the branch via the Supabase dashboard (or close+reopen the
# PR) to force a fresh prod clone.
#
# Disable the Supabase GitHub integration's auto-branching for this
# repo so it doesn't create a parallel data-less branch in the same
# project on PR open.
set -uo pipefail

BRANCH_NAME="pr-${PR_NUMBER}"

find_branch_json() {
  supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg gb "$GIT_BRANCH" --argjson pr "$PR_NUMBER" --arg name "$BRANCH_NAME" '
        first(.[] | select(.persistent != true)
                  | select(.git_branch == $gb or .pr_number == $pr or .name == $name))'
}

existing=$(find_branch_json)
if [ -z "$existing" ] || [ "$existing" = "null" ]; then
  echo "creating $BRANCH_NAME with --with-data" >&2
  supabase branches create "$BRANCH_NAME" \
    --with-data \
    --project-ref "$SUPABASE_PROJECT_REF"
else
  echo "reusing existing branch $(echo "$existing" | jq -r '.id')" >&2
fi

# Wait until the branch is ready. First creation includes the prod
# clone, so give it 20 min; subsequent runs short-circuit fast.
deadline=$(($(date +%s) + 1200))
ready=0
branch_id="" branch_ref="" status="" preview=""

while [ "$(date +%s)" -lt "$deadline" ]; do
  branch_json=$(find_branch_json)

  if [ -n "$branch_json" ] && [ "$branch_json" != "null" ]; then
    read -r branch_id branch_ref status preview < <(
      jq -r '[.id, .project_ref, .status, .preview_project_status] | @tsv' <<<"$branch_json"
    )
    case "$status" in
      MIGRATIONS_FAILED|FUNCTIONS_FAILED)
        echo "branch $branch_id failed: $status" >&2
        exit 1
        ;;
      MIGRATIONS_PASSED|FUNCTIONS_DEPLOYED)
        [ "$preview" = "ACTIVE_HEALTHY" ] && { ready=1; break; }
        ;;
    esac
  fi
  sleep 10
done

if [ "$ready" -ne 1 ]; then
  echo "timed out (status=$status preview=$preview)" >&2
  exit 1
fi

export BRANCHES_GET_JSON
BRANCHES_GET_JSON=$(supabase branches get "$branch_id" \
  --project-ref "$SUPABASE_PROJECT_REF" -o json)
export BRANCH_REF="$branch_ref"

# Patch the URL via Python (simple string ops, no re-encoding) and emit
# debug info so a future regression is diagnosable from the workflow log.
db_url=$(python3 <<'PY'
import json
import os
import re
import sys
from urllib.parse import urlsplit

data = json.loads(os.environ["BRANCHES_GET_JSON"])
print("branches.get keys:", sorted(data.keys()), file=sys.stderr)

def describe(label, raw):
    if not raw:
        print(f"{label}: <missing>", file=sys.stderr)
        return
    p = urlsplit(raw)
    print(f"{label}: user={p.username!r} host={p.hostname!r} port={p.port!r} "
          f"db={p.path.lstrip('/')!r} pwd_len={len(p.password or '')}",
          file=sys.stderr)

describe("POSTGRES_URL", data.get("POSTGRES_URL"))
describe("POSTGRES_URL_NON_POOLING", data.get("POSTGRES_URL_NON_POOLING"))

# Use the pooler URL: GHA runners are IPv4-only and Supabase's direct
# port is IPv6-only, so the non-pooling URL is unreachable from CI.
raw_url = data.get("POSTGRES_URL") or ""
if not raw_url:
    print("no POSTGRES_URL", file=sys.stderr)
    sys.exit(1)

# Drop query string (pgbouncer / prisma flags asyncpg doesn't understand).
url = raw_url.split("?", 1)[0]
# Force asyncpg driver.
url = re.sub(r"^postgresql://", "postgresql+asyncpg://", url, count=1)

print(url)
PY
)

if [ -z "$db_url" ]; then
  echo "failed to build db_url" >&2
  exit 1
fi

# Smoke-test the URL with libpq's psql so a credential issue surfaces
# here, before alembic — pg's own error message is more diagnostic
# than asyncpg's generic InvalidPasswordError. Strip the asyncpg
# driver prefix because psql doesn't understand it.
echo "smoke-testing connection to branch DB..." >&2
psql_url="postgresql://${db_url#postgresql+asyncpg://}"
if ! PGCONNECT_TIMEOUT=15 psql "$psql_url" -c 'select 1' >/dev/null 2>/tmp/psql.err; then
  echo "psql connect failed:" >&2
  cat /tmp/psql.err >&2
  exit 1
fi
echo "smoke test OK" >&2

echo "ODDISH_DATABASE_URL=$db_url" >> "$GITHUB_ENV"
{
  echo "branch_id=$branch_id"
  echo "branch_ref=$branch_ref"
} >> "$GITHUB_OUTPUT"
