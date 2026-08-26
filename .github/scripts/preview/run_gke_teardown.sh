#!/usr/bin/env bash
# Ask a preview app to delete its GKE cluster, but never wait more than five
# minutes for Modal or the remote teardown function to return.
set -euo pipefail

: "${MODAL_ENVIRONMENT:?}"

app_name="${1:?usage: run_gke_teardown.sh APP_NAME}"
script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

timeout --foreground --kill-after=15s 300s \
  modal run --env "$MODAL_ENVIRONMENT" \
  "$script_dir/teardown_gke_cluster.py" \
  --app-name "$app_name"
