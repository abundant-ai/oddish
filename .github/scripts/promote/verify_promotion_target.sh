#!/usr/bin/env bash
# Shared promotion preconditions for Promotion Preflight and /promote.
#
# Resolves the target — TARGET_SHA if set, else the sha pinned in PR_BODY's
# `promotion-target` marker, else the staging tip — and verifies the
# fast-forward invariants and the staging deploy, then emits `sha=<target>`
# to GITHUB_OUTPUT. Read-only: the caller decides whether to push.
set -euo pipefail

git fetch origin main staging

raw="${TARGET_SHA:-}"
if [ -z "$raw" ]; then
  body="$(printf '%s' "${PR_BODY:-}" | tr -d '\r')"
  if printf '%s' "$body" | grep -q '<!--[[:space:]]*promotion-target:'; then
    # A pin that is present but not a sha must fail, never fall through to
    # the tip — an unfilled template placeholder is not consent to ship more.
    raw="$(printf '%s' "$body" \
        | grep -m1 -oE '<!--[[:space:]]*promotion-target:[[:space:]]*[0-9a-fA-F]{7,40}[[:space:]]*-->' \
        | grep -oE '[0-9a-fA-F]{7,40}' | head -n1)" \
      || { echo "::error::the promotion-target pin in the PR body is not a commit sha; fix the pin or use an explicit target sha"; exit 1; }
    echo "promoting the sha pinned in the PR body: $raw"
  else
    echo "::notice::no promotion-target pin in the PR body — promoting the staging tip"
  fi
fi
target="${raw:-$(git rev-parse origin/staging)}"
target="$(git rev-parse --verify --quiet "${target}^{commit}")" \
  || { echo "::error::'$raw' does not resolve to a commit"; exit 1; }

git merge-base --is-ancestor "$target" origin/staging \
  || { echo "::error::$target is not on staging"; exit 1; }
git merge-base --is-ancestor origin/main "$target" \
  || { echo "::error::main is not an ancestor of $target — fast-forward impossible; run Sync Preflight for the repair steps"; exit 1; }

if gh workflow view "Staging Deploy" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1; then
  gh run list --repo "$GITHUB_REPOSITORY" --workflow "Staging Deploy" \
      --branch staging --commit "$target" --json conclusion -q '.[0].conclusion' \
    | grep -qx success \
    || {
         echo "::error::Staging Deploy is not green on $target"
         echo "::notice::A queued deploy is superseded when a newer commit lands (GitHub keeps one pending run per concurrency group), so a commit that staging moved past may never have deployed. A dispatched deploy always runs the tip of staging, not an older commit, so promote the staging tip instead of this sha."
         exit 1
       }
else
  echo "::warning::Staging Deploy workflow not found — skipping deploy-green precondition (bootstrap)"
fi

echo "sha=$target" >> "$GITHUB_OUTPUT"
echo "checks passed for $target"
