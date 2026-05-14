"""Find the most recent workflow run on this PR branch where each
preview component's deploy step actually succeeded, and emit the
run's head_sha as <component>_base on GITHUB_OUTPUT.

If a component has no recorded success on this branch yet (new PR,
or every prior run failed at that step), its base is left empty —
the workflow treats that as "no known base, deploy this component
now".

If the GitHub API call fails (transient hiccup, rate limit, etc.),
fall back to empty bases for every component. Mild over-deploy is
strictly better than blocking CI on a flaky API.

Inputs (env vars):
  OWNER_REPO     - e.g. "abundant-ai/oddish"
  HEAD_REF       - PR head branch name (no refs/heads/ prefix)
  GH_TOKEN       - read access for the `gh` CLI
  GITHUB_OUTPUT  - file the action runner reads outputs from
"""

import json
import os
import subprocess
import sys
import urllib.parse

WORKFLOW_FILE = "modal-preview.yml"
JOB_NAME = "deploy-preview"

# Step name -> output key. Matched as exact strings against
# job.steps[].name in the deploy-preview job. If you rename either
# step in modal-preview.yml, rename it here too — silent breakage
# otherwise (this script would always return empty bases, which
# downgrades the workflow to "always full redeploy" rather than
# erroring out visibly).
STEPS_BY_COMPONENT = {
    "Deploy preview backend": "backend_base",
    "Apply Alembic migrations to preview branch": "migrations_base",
}


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
            if job.get("name") != JOB_NAME:
                continue
            for step in job.get("steps", []) or []:
                if step.get("conclusion") != "success":
                    continue
                key = STEPS_BY_COMPONENT.get(step.get("name"))
                if key and key not in found:
                    found[key] = run["head_sha"]
    return found


def main():
    owner_repo = os.environ["OWNER_REPO"]
    head_ref = os.environ["HEAD_REF"]
    out_path = os.environ["GITHUB_OUTPUT"]

    try:
        found = find_last_deployed_shas(owner_repo, head_ref)
    except (subprocess.CalledProcessError, FileNotFoundError) as exc:
        print(
            f"gh api lookup failed ({exc}); defaulting to full redeploy",
            file=sys.stderr,
        )
        found = {}

    with open(out_path, "a") as f:
        for key in STEPS_BY_COMPONENT.values():
            f.write(f"{key}={found.get(key, '')}\n")


if __name__ == "__main__":
    main()
