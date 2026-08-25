#!/usr/bin/env bash
# Run the Modal-side GKE cleanup under a deadline that bounds the entire CLI
# process. Modal SDK result timeouts do not release `modal run` while a remote
# call is still active, so the workflow must bound the process it waits for.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"

app_name="${1:?app name is required}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
timeout_seconds="${GKE_TEARDOWN_TIMEOUT_SECONDS:-300}"

# The deployed cleanup waits at most 240 seconds for cluster deletion. Five
# minutes leaves one minute for a Modal cold start while preserving most of a
# preview job's 20-minute budget for database and deployment work. If this
# exits 124, callers deliberately keep the old app and its scheduled reaper
# alive rather than stopping the cluster's last owner.
timeout --foreground --kill-after=15s "${timeout_seconds}s" \
  modal run --env "$MODAL_ENVIRONMENT" \
  "$script_dir/teardown_gke_cluster.py" \
  --app-name "$app_name"
