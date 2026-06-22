#!/usr/bin/env bash
# Apply both Oddish Alembic stacks to the PR preview database.
set -euo pipefail

: "${GITHUB_WORKSPACE:?}"

# Run from backend/ so bootstrap_preview_db.py's STACKS resolve to the backend
# stack (cwd) and the sibling oddish stack (cwd/../oddish) -- mirrors the explicit
# cd the seed step uses in prepare_preview_database.sh rather than relying on the
# workflow's working-directory default.
cd "$GITHUB_WORKSPACE/backend"
uv run python "$GITHUB_WORKSPACE/.github/scripts/preview/bootstrap_preview_db.py"
