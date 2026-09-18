#!/usr/bin/env bash
# Dry-run rehearsal for the urgent-hotfix release path.
#
# Read-only: verifies the same promotion preconditions as
# verify_promotion_target.sh, times each gate, and prints the rollback
# commands for the current origin/main tip. Never pushes.
#
# Usage (from repo root):
#   .github/scripts/promote/rehearse_urgent_hotfix.sh [target_sha]
#
# Default target is origin/staging tip. Requires network access to origin
# and the gh CLI authenticated against abundant-ai/oddish.
set -euo pipefail

repo_root="$(cd "$(dirname "$0")/../../.." && pwd)"
cd "$repo_root"

export GITHUB_REPOSITORY="${GITHUB_REPOSITORY:-abundant-ai/oddish}"
export GITHUB_OUTPUT="${GITHUB_OUTPUT:-$(mktemp)}"
: >"$GITHUB_OUTPUT"

echo "## Urgent-hotfix release rehearsal"
echo
echo "Repository: $GITHUB_REPOSITORY"
echo "Started (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo

echo "### 1. Fetch main and staging"
fetch_start=$(date +%s)
git fetch origin main staging
fetch_end=$(date +%s)
echo "OK in $((fetch_end - fetch_start))s"
echo

main_sha="$(git rev-parse origin/main)"
staging_sha="$(git rev-parse origin/staging)"
target_raw="${1:-$staging_sha}"
target="$(git rev-parse --verify --quiet "${target_raw}^{commit}")" \
  || { echo "ERROR: '$target_raw' does not resolve to a commit"; exit 1; }

ahead="$(git rev-list --count origin/main..origin/staging)"
behind="$(git rev-list --count origin/staging..origin/main)"

echo "### 2. Branch relationship"
echo "| Branch | SHA |"
echo "|---|---|"
echo "| main | \`$main_sha\` |"
echo "| staging | \`$staging_sha\` |"
echo "| rehearsal target | \`$target\` |"
echo
echo "staging is $ahead commit(s) ahead of main, $behind behind."
if [ "$behind" -ne 0 ]; then
  echo "ERROR: main and staging have diverged. Run the Sync Preflight workflow for repair commands before any promote."
  exit 1
fi
git merge-base --is-ancestor origin/main "$target" \
  || { echo "ERROR: main is not an ancestor of $target"; exit 1; }
git merge-base --is-ancestor "$target" origin/staging \
  || { echo "ERROR: $target is not on staging"; exit 1; }
echo "OK — fast-forward promote is possible for the target."
echo

echo "### 3. Promotion preconditions (Staging Deploy green)"
verify_start=$(date +%s)
export TARGET_SHA="$target"
# verify_promotion_target.sh appends sha=… to GITHUB_OUTPUT and re-fetches.
.github/scripts/promote/verify_promotion_target.sh
verify_end=$(date +%s)
echo "OK in $((verify_end - verify_start))s"
echo

echo "### 4. Standing promotion PR"
promo_url="$(gh pr list --repo "$GITHUB_REPOSITORY" --base main --head staging \
  --state open --json url -q '.[0].url // empty')"
if [ -n "$promo_url" ]; then
  echo "OK — open promotion PR: $promo_url"
  echo "Pin or override with this target when promoting: $target"
else
  echo "MISSING — no open staging→main promotion PR."
  echo "Avoidable delay: open one before the incident (template release-promotion.md),"
  echo "or create it immediately after the hotfix squash-merge with:"
  echo "  <!-- promotion-target: $target -->"
fi
echo

echo "### 5. Verified rollback commands (not executed)"
echo "Preferred: ship a forward fix with the same urgent path."
echo "Emergency rollback to the current production tip (requires ruleset-bypass"
echo "push access; this is a non-fast-forward of main):"
echo
echo '```bash'
echo "git fetch origin main"
echo "git push --force-with-lease origin ${main_sha}:refs/heads/main"
echo '```'
echo
echo "Then confirm Production Deploy starts for $main_sha and staging still"
echo "contains the reverted commit until a follow-up hotfix or staging rebuild."
echo "Never leave main and staging diverged without running Sync Preflight."
echo

echo "### 6. Release-time budget (wall clock to measure during a real promote)"
echo "| Gate | Recent observed duration | Required for promote? |"
echo "|---|---|---|"
echo "| Require working preview (feature PR) | ~4–8 min | Yes, to land on staging (unless break-glass) |"
echo "| Staging Deploy | ~2–3 min | Yes — verify_promotion_target refuses otherwise |"
echo "| Promotion Preflight / local verify | ~15–30 s | Recommended |"
echo "| /promote or maintainer push to main | seconds | Yes |"
echo "| Production Deploy | ~4 min | Ships the release |"
echo
echo "Avoidable delays to skip on the urgent path: staging soak beyond Staging"
echo "Deploy green; waiting for unrelated staging commits; creating the"
echo "promotion PR only after the incident starts; promoting the tip when the"
echo "hotfix is not the tip (pin the sha)."
echo
echo "Rehearsal finished (UTC): $(date -u +%Y-%m-%dT%H:%M:%SZ)"
echo "No refs were updated."
