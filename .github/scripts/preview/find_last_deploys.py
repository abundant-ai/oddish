"""Decide, per preview component, whether it needs redeploying on this push.

Two responsibilities, both driven by the GitHub API (no local git history
required, so this works with the default shallow checkout):

1. Find the most recent workflow run on this PR branch where each component's
   deploy step actually succeeded, and use that run's head_sha as the diff base
   (<component>_base). If a component has no recorded success on this branch yet
   (new PR, or every prior run failed at that step), its base is left empty.

2. Diff the relevant incremental window with the GitHub *compare* API and emit
   <component>_changed (true/false) by matching the changed files against the
   same path rules that used to live in the pr-preview.yml dorny/paths-filter
   steps. We compute this ourselves because dorny/paths-filter@v3 *ignores* its
   `base` input on pull_request events -- it always diffs the whole PR against
   the base branch via the API -- which made every push look like it changed
   backend + migrations and forced the ~15-min preview DB seed every time.

   - backend_changed / migrations_changed are diffed against the per-component
     last-successful-deploy base (so only the new push's files count).
   - workflow_changed is diffed against the previous push (github.event.before),
     because workflow/script changes can affect any component.

Fail-safe philosophy: if the API call fails, or a diff is too large to trust,
default to "changed"/empty-base -- mild over-deploy is strictly better than
blocking CI or shipping a stale preview. If a component has no base yet, its
*_changed output is left empty and compute_deployment_plan.sh falls back to the
whole-PR (filter_pr) signal.

Inputs (env vars):
  OWNER_REPO     - e.g. "abundant-ai/oddish"
  HEAD_REF       - PR head branch name (no refs/heads/ prefix)
  HEAD_SHA       - PR head sha (github.event.pull_request.head.sha)
  BEFORE_SHA     - previous push sha (github.event.before)
  GH_TOKEN       - read access for the `gh` CLI
  GITHUB_OUTPUT  - file the action runner reads outputs from
"""

import json
import os
import subprocess
import sys
import urllib.parse

WORKFLOW_FILE = "pr-preview.yml"

# Step name -> output key. Matched as exact strings against job steps.
# If you rename these steps in pr-preview.yml, rename them here too.
STEPS_BY_COMPONENT = {
    "Deploy preview backend": "backend_base",
    "Prepare preview database": "migrations_base",
}

# The compare API returns at most 300 files; past that we can't trust a
# "nothing matched" result, so we treat the diff as changed (conservative).
COMPARE_FILE_CAP = 300

API_ERRORS = (subprocess.CalledProcessError, FileNotFoundError, json.JSONDecodeError)


def gh_api(path):
    result = subprocess.run(
        ["gh", "api", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def find_last_deployed_shas(owner_repo, head_ref):
    # `status=success` only returns runs whose overall conclusion was
    # success, shrinking the candidate set ~3-5x. We still inspect
    # individual step conclusions below because a successful run can
    # have legitimately *skipped* the component step on a previous
    # surgical push.
    branch = urllib.parse.quote(head_ref, safe="")
    runs = gh_api(
        f"/repos/{owner_repo}/actions/workflows/{WORKFLOW_FILE}/runs"
        f"?branch={branch}&event=pull_request&status=success&per_page=30"
    ).get("workflow_runs", [])

    found = {}
    for run in runs:
        if len(found) == len(STEPS_BY_COMPONENT):
            break
        jobs = gh_api(
            f"/repos/{owner_repo}/actions/runs/{run['id']}/jobs?per_page=100"
        ).get("jobs", [])
        for job in jobs:
            for step in job.get("steps", []) or []:
                if step.get("conclusion") != "success":
                    continue
                key = STEPS_BY_COMPONENT.get(step.get("name"))
                if key and key not in found:
                    found[key] = run["head_sha"]
    return found


def compare_files(owner_repo, base, head):
    """Filenames changed between two commits, via the compare API.

    Returns (filenames, truncated). A rename reports both old and new paths.
    `truncated` is True when the diff is too large to enumerate reliably
    (files omitted, or at the 300-file cap), so callers stay conservative.
    """
    data = gh_api(f"/repos/{owner_repo}/compare/{base}...{head}")
    files = data.get("files")
    if files is None:
        # Very large diffs omit the files array entirely.
        return [], True
    names = []
    for entry in files:
        name = entry.get("filename")
        if name:
            names.append(name)
        previous = entry.get("previous_filename")
        if previous:
            names.append(previous)
    return names, len(files) >= COMPARE_FILE_CAP


def backend_matches(path):
    # backend/** + oddish/**, excluding alembic/** and *.md (mirrors the
    # backend filter globs still used by filter_pr in pr-preview.yml).
    if path.endswith(".md"):
        return False
    if path.startswith("backend/alembic/") or path.startswith("oddish/alembic/"):
        return False
    return path.startswith("backend/") or path.startswith("oddish/")


def migrations_matches(path):
    return path.startswith("backend/alembic/") or path.startswith("oddish/alembic/")


def workflow_matches(path):
    return path == ".github/workflows/pr-preview.yml" or path.startswith(
        ".github/scripts/preview/"
    )


def compute_changed(owner_repo, base, head, matcher):
    """"true"/"false" if any file in base..head matches `matcher`.

    "" when base is empty (caller falls back to the whole-PR signal); "true"
    on API error or an untrustworthy (truncated) diff -- conservative over-deploy.
    """
    if not base:
        return ""
    if not head:
        return "true"
    try:
        files, truncated = compare_files(owner_repo, base, head)
    except API_ERRORS as exc:
        print(f"compare {base}..{head} failed ({exc}); assuming changed", file=sys.stderr)
        return "true"
    if truncated:
        print(f"compare {base}..{head} too large; assuming changed", file=sys.stderr)
        return "true"
    return "true" if any(matcher(name) for name in files) else "false"


def main():
    owner_repo = os.environ["OWNER_REPO"]
    head_ref = os.environ["HEAD_REF"]
    head_sha = os.environ.get("HEAD_SHA", "")
    before_sha = os.environ.get("BEFORE_SHA", "")
    out_path = os.environ["GITHUB_OUTPUT"]

    try:
        found = find_last_deployed_shas(owner_repo, head_ref)
    except API_ERRORS as exc:
        print(
            f"gh api lookup failed ({exc}); defaulting to full redeploy",
            file=sys.stderr,
        )
        found = {}

    backend_base = found.get("backend_base", "")
    migrations_base = found.get("migrations_base", "")

    outputs = {
        "backend_base": backend_base,
        "migrations_base": migrations_base,
        "backend_changed": compute_changed(
            owner_repo, backend_base, head_sha, backend_matches
        ),
        "migrations_changed": compute_changed(
            owner_repo, migrations_base, head_sha, migrations_matches
        ),
        # Workflow/script changes are diffed against the previous push, not a
        # per-component base, because they can affect any component.
        "workflow_changed": compute_changed(
            owner_repo, before_sha, head_sha, workflow_matches
        ),
    }

    with open(out_path, "a") as f:
        for key, value in outputs.items():
            f.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
