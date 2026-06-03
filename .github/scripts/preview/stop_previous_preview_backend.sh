#!/usr/bin/env bash
# Stop the running PR Modal app before the database branch password rotates.
set -euo pipefail

: "${GITHUB_STEP_SUMMARY:?}"
: "${MODAL_APP_NAME:?}"

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

"$script_dir/stop_modal_preview_app.sh" || true

{
  echo "## Previous preview backend"
  echo
  echo "- Modal app stopped if it existed: \`$MODAL_APP_NAME\`"
} >> "$GITHUB_STEP_SUMMARY"
