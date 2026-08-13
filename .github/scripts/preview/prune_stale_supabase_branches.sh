#!/usr/bin/env bash
# Delete Supabase preview branches created more than MAX_AGE_DAYS ago. The
# project caps active branch projects at 50 and a PR that is abandoned or
# force-closed never runs the teardown that frees its branch, so the pool drains
# until new PRs cannot provision a preview database at all. Deletion is
# recoverable: the PR's next preview run rebuilds the branch from the prod
# schema snapshot.
set -euo pipefail

: "${SUPABASE_ACCESS_TOKEN:?}"
: "${SUPABASE_PROJECT_REF:?}"

MAX_AGE_DAYS="${MAX_AGE_DAYS:-7}"
DRY_RUN="${DRY_RUN:-false}"

case "$MAX_AGE_DAYS" in
  '' | *[!0-9]*)
    echo "MAX_AGE_DAYS must be a whole number of days, got '$MAX_AGE_DAYS'" >&2
    exit 1
    ;;
esac

cutoff=$(($(date +%s) - MAX_AGE_DAYS * 86400))

# jq aborts on a branch whose created_at it cannot parse, which fails the whole
# script before any delete. That is deliberate: an unreadable listing must never
# be read as "nothing is stale".
stale=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
  | jq -r --argjson cutoff "$cutoff" '
      def parse_supabase_time:
        sub("\\+00:00$"; "Z")
        | sub("\\.[0-9]+Z$"; "Z")
        | fromdateiso8601;

      .[] | select(.persistent != true)
          | select(.name | test("^pr-[0-9]+$"))
          | select((.created_at | parse_supabase_time) < $cutoff)
          | [.id, .name, .created_at] | @tsv')

if [ -z "$stale" ]; then
  echo "no preview branches older than $MAX_AGE_DAYS days"
  exit 0
fi

failed=0
while IFS=$'\t' read -r id name created_at; do
  if [ "$DRY_RUN" = "true" ]; then
    echo "would delete $name ($id, created $created_at)"
    continue
  fi
  echo "deleting $name ($id, created $created_at)"
  # </dev/null so the interactive CLI cannot swallow the list this loop reads.
  supabase branches delete "$id" --project-ref "$SUPABASE_PROJECT_REF" </dev/null || failed=1
done <<<"$stale"

exit "$failed"
