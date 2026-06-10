#!/usr/bin/env bash
# Ensure the PR's Supabase preview branch (data-less) exists and is
# healthy, and emit its DB URL. The branch starts empty; Alembic applies
# the full schema (bootstrap_preview_db.py) and a curated seed
# (seed_preview_db.py) populates it. On later pushes the branch is reused;
# the seed is idempotent + convergent so re-running is safe.
#
# If a branch lands in a terminal-failed state (status MIGRATIONS_FAILED /
# FUNCTIONS_FAILED, or preview_project_status INIT_FAILED / PAUSE_FAILED)
# or never becomes ready within the deadline, it is torn down and recreated
# (up to MAX_ATTEMPTS) so a flaky run doesn't poison every push to the PR.
set -uo pipefail

BRANCH_NAME="pr-${PR_NUMBER}"
MAX_ATTEMPTS=3

# Branch lifecycle states we treat as terminal failures and recover from
# by deleting + recreating the branch:
# - `status` is the branch's migration/functions pipeline state.
# - `preview_project_status` is the underlying preview project's compute
#   state; INIT_FAILED / PAUSE_FAILED there mean the branch will never
#   become ready, so we match them to retry instead of polling until the
#   deadline. (RESTORE_FAILED is also matched but only applies to branches
#   created with data; data-less preview branches have no restore.)
is_failed_status() {
  case "$1" in
    MIGRATIONS_FAILED|FUNCTIONS_FAILED) return 0 ;;
  esac
  return 1
}

is_failed_preview() {
  case "$1" in
    RESTORE_FAILED|INIT_FAILED|PAUSE_FAILED) return 0 ;;
  esac
  return 1
}

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
    cur_preview=$(jq -r '.preview_project_status' <<<"$existing")
    cur_id=$(jq -r '.id' <<<"$existing")
    if is_failed_status "$cur_status" || is_failed_preview "$cur_preview"; then
      echo "existing branch $cur_id is in failed state (status=$cur_status preview=$cur_preview); recreating" >&2
      delete_branch_by_id "$cur_id"
      existing=""
    fi
  fi

  if [ -z "$existing" ] || [ "$existing" = "null" ]; then
    echo "creating $BRANCH_NAME (data-less, attempt $attempt/$MAX_ATTEMPTS)" >&2
    supabase branches create "$BRANCH_NAME" \
      --project-ref "$SUPABASE_PROJECT_REF"
    branch_was_created=true
  else
    echo "reusing existing branch $(jq -r '.id' <<<"$existing") ($(jq -r '.status' <<<"$existing"))" >&2
    branch_was_created=false
  fi

  # Data-less branch: only Supabase's no-op migration runner + compute
  # provisioning, so it goes ACTIVE_HEALTHY in 1-2 min. 5 min is slack.
  deadline=$(($(date +%s) + 300))
  branch_failed=0
  branch_id="" branch_ref="" status="" preview=""

  while [ "$(date +%s)" -lt "$deadline" ]; do
    branch_json=$(find_branch_json)

    if [ -n "$branch_json" ] && [ "$branch_json" != "null" ]; then
      read -r branch_id branch_ref status preview < <(
        jq -r '[.id, .project_ref, .status, .preview_project_status] | @tsv' <<<"$branch_json"
      )
      if is_failed_status "$status" || is_failed_preview "$preview"; then
        echo "branch $branch_id failed: status=$status preview=$preview" >&2
        branch_failed=1
        break
      fi
      case "$status" in
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

  # If the inner loop fell out via the readiness deadline rather
  # than a terminal failure, treat that as a failed attempt too —
  # otherwise a branch stuck mid-creation burns the whole retry
  # budget on a single 20-minute wait.
  if [ "$branch_failed" -eq 0 ]; then
    echo "branch ${branch_id:-<unknown>} did not become ready within deadline (status=$status preview=$preview); treating as failed" >&2
    branch_failed=1
  fi

  if [ "$attempt" -lt "$MAX_ATTEMPTS" ]; then
    # Tear down the poisoned branch so the next attempt starts fresh.
    [ -n "$branch_id" ] && delete_branch_by_id "$branch_id"
    continue
  fi

  # Exhausted retries (terminal failure or deadline on every attempt).
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
