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

branch_ref = os.environ["BRANCH_REF"]
data = json.loads(os.environ["BRANCHES_GET_JSON"])
print("branches.get keys:", sorted(data.keys()), file=sys.stderr)

raw_url = data.get("POSTGRES_URL") or ""
host_match = re.search(r"@([^/?]+)", raw_url)
print("POSTGRES_URL host:port:",
      host_match.group(1) if host_match else "<none>", file=sys.stderr)

# Drop the query string — asyncpg doesn't grok pgbouncer-style params.
url = raw_url.split("?", 1)[0]

# Supabase pooler (port 6543) authenticates with "postgres.<branch_ref>"
# so it can route. .POSTGRES_URL returns just "postgres". Patch only
# when (a) we're hitting the pooler and (b) the user is the bare form.
if ":6543/" in url and branch_ref:
    new_url, n = re.subn(r"(://)postgres(:)",
                         rf"\1postgres.{branch_ref}\2", url, count=1)
    if n:
        print(f"patched pooler user -> postgres.{branch_ref}", file=sys.stderr)
    else:
        print("pooler URL but user already non-bare; no patch", file=sys.stderr)
    url = new_url

# Force asyncpg driver.
url = re.sub(r"^postgresql://", "postgresql+asyncpg://", url, count=1)

print(url)
PY
)

if [ -z "$db_url" ]; then
  echo "failed to build db_url" >&2
  exit 1
fi

echo "ODDISH_DATABASE_URL=$db_url" >> "$GITHUB_ENV"
{
  echo "branch_id=$branch_id"
  echo "branch_ref=$branch_ref"
} >> "$GITHUB_OUTPUT"
