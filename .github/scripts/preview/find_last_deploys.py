"""Decide, per preview component, whether it needs redeploying on this push.

Everything here is driven by the GitHub API (no local git history required, so
it works with the default shallow checkout). It emits the change signals that
compute_deployment_plan.sh turns into the deploy plan:

1. Whole-PR flags (pr_backend / pr_migrations / pr_frontend): does the PR, as a
   whole, touch each component (diffed against the PR base branch). Used on the
   opened/reopened events and as the fallback when a component has no recorded
   deploy yet. Replaces the old `filter_pr` dorny step.

2. Last-successful-deploy bases (backend_base / migrations_base) and incremental
   flags (backend_changed / migrations_changed / workflow_changed): on a
   *synchronize* push, find the most recent run on this branch whose component
   step succeeded and diff only the new push against it (and the previous push,
   for workflow/scripts). This is what lets a frontend-only follow-up push skip
   the ~15-min preview DB seed.

We compute all of this ourselves rather than using dorny/paths-filter because
that action (a) *ignores* its `base` input on pull_request events -- it always
diffs the whole PR -- and (b) under its default `predicate-quantifier: some`,
a filter containing negated excludes (e.g. `!backend/alembic/**`) matches almost
any path, so the backend filter reported "changed" for unrelated changes. Both
bugs forced the preview DB seed + backend redeploy when they shouldn't have.

Fail-safe philosophy: if the API call fails, or a diff is too large to trust,
default to "changed" -- mild over-deploy is strictly better than blocking CI or
shipping a stale preview. If a component has no deploy base yet, its incremental
flag is left empty and compute_deployment_plan.sh falls back to the whole-PR flag.

Inputs (env vars):
  OWNER_REPO     - e.g. "abundant-ai/oddish"
  EVENT_ACTION   - github.event.action (opened/reopened/synchronize)
  HEAD_REF       - PR head branch name (no refs/heads/ prefix)
  HEAD_SHA       - PR head sha (github.event.pull_request.head.sha)
  PR_BASE_SHA    - PR base branch sha (github.event.pull_request.base.sha)
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

# Errors that mean "the API didn't give us a usable answer" -> be conservative.
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
    # backend/** + oddish/**, excluding alembic/** and *.md.
    if path.endswith(".md"):
        return False
    if path.startswith("backend/alembic/") or path.startswith("oddish/alembic/"):
        return False
    return path.startswith("backend/") or path.startswith("oddish/")


def migrations_matches(path):
    return path.startswith("backend/alembic/") or path.startswith("oddish/alembic/")


def frontend_matches(path):
    return path.startswith("frontend/")


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
    event_action = os.environ.get("EVENT_ACTION", "")
    head_ref = os.environ["HEAD_REF"]
    head_sha = os.environ.get("HEAD_SHA", "")
    pr_base_sha = os.environ.get("PR_BASE_SHA", "")
    before_sha = os.environ.get("BEFORE_SHA", "")
    out_path = os.environ["GITHUB_OUTPUT"]

    # Whole-PR flags: does the PR (vs its base branch) touch each component.
    pr_backend = compute_changed(owner_repo, pr_base_sha, head_sha, backend_matches)
    pr_migrations = compute_changed(owner_repo, pr_base_sha, head_sha, migrations_matches)
    pr_frontend = compute_changed(owner_repo, pr_base_sha, head_sha, frontend_matches)

    # Incremental flags only apply to synchronize pushes -- they answer "did
    # this push change the component since its last successful deploy?".
    backend_base = ""
    migrations_base = ""
    backend_changed = ""
    migrations_changed = ""
    workflow_changed = ""
    if event_action == "synchronize":
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
        backend_changed = compute_changed(
            owner_repo, backend_base, head_sha, backend_matches
        )
        migrations_changed = compute_changed(
            owner_repo, migrations_base, head_sha, migrations_matches
        )
        # Workflow/script changes are diffed against the previous push, not a
        # per-component base, because they can affect any component.
        workflow_changed = compute_changed(
            owner_repo, before_sha, head_sha, workflow_matches
        )

    outputs = {
        "backend_base": backend_base,
        "migrations_base": migrations_base,
        "backend_changed": backend_changed,
        "migrations_changed": migrations_changed,
        "workflow_changed": workflow_changed,
        "pr_backend": pr_backend,
        "pr_migrations": pr_migrations,
        "pr_frontend": pr_frontend,
    }

    with open(out_path, "a") as f:
        for key, value in outputs.items():
            f.write(f"{key}={value}\n")


if __name__ == "__main__":
    main()
