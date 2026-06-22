#!/usr/bin/env bash
# Apply both Oddish Alembic stacks to the PR preview database.
set -euo pipefail

: "${GITHUB_WORKSPACE:?}"

cd "$GITHUB_WORKSPACE/backend"
uv run python "$GITHUB_WORKSPACE/.github/scripts/preview/bootstrap_preview_db.py"
