#!/usr/bin/env bash
# Deploy the PR-specific Modal backend and emit modal_api_url.
set -euo pipefail

: "${GITHUB_OUTPUT:?}"
: "${GITHUB_WORKSPACE:?}"

deploy_log="$(mktemp)"
uv run modal deploy deploy.py 2>&1 | tee "$deploy_log"
modal_api_url="$(python "$GITHUB_WORKSPACE/.github/scripts/preview/extract_modal_api_url.py" "$deploy_log")"
echo "modal_api_url=$modal_api_url" >> "$GITHUB_OUTPUT"
