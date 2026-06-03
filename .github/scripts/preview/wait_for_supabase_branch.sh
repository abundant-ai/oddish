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
# If Supabase's clone+migrate fails for a transient reason
# (MIGRATIONS_FAILED / FUNCTIONS_FAILED), the failed branch is torn
# down and recreated once before giving up, so a flaky run doesn't
# poison every subsequent push to the PR.
#
# Disable the Supabase GitHub integration's auto-branching for this
# repo so it doesn't create a parallel data-less branch in the same
# project on PR open.
set -uo pipefail

BRANCH_NAME="pr-${PR_NUMBER}"
MAX_ATTEMPTS=2

find_branch_json() {
  supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
    | jq -c --arg name "$BRANCH_NAME" '
        first(.[] | select(.persistent != true) | select(.name == $name))'
}

delete_branch_by_id() {
  local id="$1"
  echo "deleting Supabase branch $id" >&2
  supabase branches delete "$id" --project-ref "$SUPABASE_PROJECT_REF" || true
}

ready=0
branch_was_created=false
branch_id="" branch_ref="" status="" preview=""

for attempt in $(seq 1 "$MAX_ATTEMPTS"); do
  existing=$(find_branch_json)

  # If an existing branch is already in a known-failed state from a
  # prior workflow run, tear it down so we can recreate cleanly.
  if [ -n "$existing" ] && [ "$existing" != "null" ]; then
    cur_status=$(jq -r '.status' <<<"$existing")
    cur_id=$(jq -r '.id' <<<"$existing")
    case "$cur_status" in
      MIGRATIONS_FAILED|FUNCTIONS_FAILED)
        echo "existing branch $cur_id is $cur_status; recreating" >&2
        delete_branch_by_id "$cur_id"
        existing=""
        ;;
    esac
  fi

  if [ -z "$existing" ] || [ "$existing" = "null" ]; then
    echo "creating $BRANCH_NAME with --with-data (attempt $attempt/$MAX_ATTEMPTS)" >&2
    supabase branches create "$BRANCH_NAME" \
      --with-data \
      --project-ref "$SUPABASE_PROJECT_REF"
    branch_was_created=true
  else
    echo "reusing existing branch $(jq -r '.id' <<<"$existing") ($(jq -r '.status' <<<"$existing"))" >&2
    branch_was_created=false
  fi

  # Wait until the branch is ready. First creation includes the prod
  # clone, so give it 20 min; subsequent runs short-circuit fast.
  deadline=$(($(date +%s) + 1200))
  branch_failed=0
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
          branch_failed=1
          break
          ;;
        MIGRATIONS_PASSED|FUNCTIONS_DEPLOYED)
          [ "$preview" = "ACTIVE_HEALTHY" ] && { ready=1; break; }
          ;;
      esac
    fi
    sleep 10
  done

  if [ "$ready" -eq 1 ]; then
    break
  fi

  if [ "$branch_failed" -eq 1 ] && [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    # Tear down the poisoned branch so the next attempt starts fresh.
    [ -n "$branch_id" ] && delete_branch_by_id "$branch_id"
    continue
  fi

  # Polling timed out, or we've exhausted retries on a failed clone.
  break
done

if [ "$ready" -ne 1 ]; then
  echo "Supabase preview branch never became ready (status=$status preview=$preview)" >&2
  exit 1
fi

export BRANCHES_GET_JSON
BRANCHES_GET_JSON=$(supabase branches get "$branch_id" \
  --project-ref "$SUPABASE_PROJECT_REF" -o json)
export BRANCH_REF="$branch_ref"

# `branches get` returns a redacted password — last run's psql still
# got "password authentication failed for user 'postgres'" with the
# URL it gave us. Reset the branch DB password to a known value via
# the Management API, then use that in the URL we construct below.
DB_PASSWORD=$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')
export DB_PASSWORD
echo "resetting branch DB password..." >&2
http_code=$(curl -sS -o /tmp/pwreset.json -w '%{http_code}' \
  -X PATCH \
  -H "Authorization: Bearer $SUPABASE_ACCESS_TOKEN" \
  -H "Content-Type: application/json" \
  -d "{\"password\": \"$DB_PASSWORD\"}" \
  "https://api.supabase.com/v1/projects/${branch_ref}/database/password" \
  || echo "curl_failed")
if [ "$http_code" != "200" ] && [ "$http_code" != "204" ]; then
  echo "password reset failed (HTTP $http_code):" >&2
  cat /tmp/pwreset.json >&2 || true
  exit 1
fi
echo "password reset OK" >&2

# Patch the URL via Python (simple string ops, no re-encoding) and emit
# debug info so a future regression is diagnosable from the workflow log.
db_url=$(python3 <<'PY'
import json
import os
import sys
from urllib.parse import urlsplit, quote, urlunsplit

data = json.loads(os.environ["BRANCHES_GET_JSON"])
print("branches.get keys:", sorted(data.keys()), file=sys.stderr)

raw_url = data.get("POSTGRES_URL") or ""
if not raw_url:
    print("no POSTGRES_URL", file=sys.stderr)
    sys.exit(1)

p = urlsplit(raw_url)
user = p.username or ""
host = p.hostname or ""
port = p.port
print(f"POSTGRES_URL: user={user!r} host={host!r} port={port!r}", file=sys.stderr)

# We just reset the DB password via the Management API; substitute it
# in (URL-encoded). The user (postgres.<branch_ref>) and host stay as
# Supabase returned them.
password = os.environ["DB_PASSWORD"]

# Use the pooler URL: GHA is IPv4-only and the direct port is
# IPv6-only on Supabase.
netloc = f"{quote(user, safe='')}:{quote(password, safe='')}@{host}"
if port:
    netloc += f":{port}"

url = urlunsplit(("postgresql+asyncpg", netloc, p.path, "", ""))
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
smoke_deadline=$(($(date +%s) + 300))
smoke_attempt=1
while true; do
  if PGCONNECT_TIMEOUT=15 psql "$psql_url" -c 'select 1' >/dev/null 2>/tmp/psql.err; then
    break
  fi

  if [ "$(date +%s)" -ge "$smoke_deadline" ]; then
    echo "psql connect failed:" >&2
    cat /tmp/psql.err >&2
    exit 1
  fi

  echo "psql connect failed on attempt $smoke_attempt; waiting for Supabase pooler..." >&2
  cat /tmp/psql.err >&2
  smoke_attempt=$((smoke_attempt + 1))
  sleep 10
done
echo "smoke test OK" >&2

echo "ODDISH_DATABASE_URL=$db_url" >> "$GITHUB_ENV"
{
  echo "branch_id=$branch_id"
  echo "branch_ref=$branch_ref"
  echo "branch_was_created=$branch_was_created"
} >> "$GITHUB_OUTPUT"
