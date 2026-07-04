#!/usr/bin/env bash
# Delete a PR's Supabase preview branch so the next prepare run rebuilds it
# from the prod schema snapshot. The caller (preview-reset.yml) has already
# verified the PR is an open same-repo PR BEFORE checking out its code.
#
# Deletion is loud (no `|| true`): a reset that cannot delete the branch must
# fail rather than "succeed" against the old, possibly-poisoned database.
set -euo pipefail

: "${PR_NUMBER:?}"
: "${SUPABASE_ACCESS_TOKEN:?}"
: "${SUPABASE_PROJECT_REF:?}"

case "$PR_NUMBER" in
  '' | *[!0-9]*)
    echo "PR_NUMBER must be numeric, got '$PR_NUMBER'" >&2
    exit 1
    ;;
esac

branch_name="pr-$PR_NUMBER"

ids=$(supabase branches list --project-ref "$SUPABASE_PROJECT_REF" -o json \
  | jq -r --arg name "$branch_name" '
      .[] | select(.persistent != true)
          | select(.name == $name)
          | .id')

if [ -z "$ids" ]; then
  echo "no Supabase branch named $branch_name; nothing to delete" >&2
  exit 0
fi

for id in $ids; do
  echo "deleting Supabase branch $id ($branch_name)" >&2
  supabase branches delete "$id" --project-ref "$SUPABASE_PROJECT_REF"
done
