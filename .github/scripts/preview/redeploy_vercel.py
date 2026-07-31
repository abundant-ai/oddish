"""Resolve the Vercel preview deployment for the pushed commit, force-
rebuilding it only when the per-branch env actually changed.

Vercel's GitHub integration creates a preview deployment on every push
using whatever env vars existed at push time. On first deploy for a
PR the per-branch NEXT_PUBLIC_API_URL hasn't been set yet, so we set
it and then force a new deployment from the same source so the
preview actually points at the Modal preview backend. On later pushes
the branch env usually already matches; update_vercel_preview.sh then
sets VERCEL_FORCE_REDEPLOY=false and we reuse the auto-created
deployment instead of building the same commit a second time (unless
that build failed, in which case we still force a fresh one).

Writes preview_url to GITHUB_OUTPUT.

Inputs (env vars):
  VERCEL_TOKEN, VERCEL_ORG_ID, VERCEL_PROJECT_ID,
  VERCEL_GIT_BRANCH, VERCEL_GIT_COMMIT_SHA,
  VERCEL_FORCE_REDEPLOY ("false" to reuse the auto deployment),
  GITHUB_OUTPUT
"""

import json
import os
import sys
import time
import urllib.parse
import urllib.request

MAX_LOOKUP_ATTEMPTS = 18
LOOKUP_POLL_INTERVAL_S = 10

# Deployments in these states can't serve the preview; reusing one would pin
# the alias to a dead build, so fall back to a forced rebuild.
FAILED_STATES = {"ERROR", "CANCELED", "DELETED", "BLOCKED"}


def deployment_state(deployment):
    return deployment.get("readyState") or deployment.get("state") or ""


def deployment_commit_sha(deployment):
    meta = deployment.get("meta") or {}
    git_source = deployment.get("gitSource") or {}
    for candidate in (
        meta.get("githubCommitSha"),
        meta.get("githubCommitSHA"),
        meta.get("gitCommitSha"),
        git_source.get("sha"),
    ):
        if candidate:
            return candidate
    return None


def find_existing_deployment(token, project_id, team_id, branch, commit_sha):
    params = urllib.parse.urlencode(
        {
            "projectId": project_id,
            "teamId": team_id,
            "limit": 20,
            "target": "preview",
            "branch": branch,
        }
    )
    url = f"https://api.vercel.com/v6/deployments?{params}"
    headers = {"Authorization": f"Bearer {token}"}

    for _ in range(MAX_LOOKUP_ATTEMPTS):
        request = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(request) as response:
            payload = json.load(response)
        for deployment in payload.get("deployments", []):
            if deployment_commit_sha(deployment) == commit_sha:
                return deployment
        time.sleep(LOOKUP_POLL_INTERVAL_S)
    raise SystemExit(
        "No Vercel preview deployment found yet for "
        f"branch {branch!r} at commit {commit_sha!r}"
    )


def branch_env_last_updated_ms(token, project_id, team_id, branch):
    """Newest updatedAt across the branch's env records, in ms epoch.

    Returns 0 when no branch-scoped records exist or the response shape is
    unexpected -- callers treat 0 as "cannot prove the auto build is
    current" and force a rebuild.
    """
    params = urllib.parse.urlencode({"teamId": team_id})
    url = (
        "https://api.vercel.com/v9/projects/"
        f"{urllib.parse.quote(project_id, safe='')}/env?{params}"
    )
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(request) as response:
        payload = json.load(response)
    stamps = [
        env.get("updatedAt") or env.get("createdAt") or 0
        for env in payload.get("envs", [])
        if env.get("gitBranch") == branch
    ]
    return max(stamps, default=0)


def auto_deployment_is_current(token, project_id, team_id, branch, deployment):
    """Whether the auto-created deployment was built after the branch env
    was last written.

    An env diff alone can't prove the build is current: a cancelled run may
    have written the env and died before rebuilding, leaving the push-time
    auto build baked with stale values while the stored env already matches.
    Any doubt (missing timestamps, API error) counts as stale.
    """
    deployed_at = deployment.get("createdAt") or deployment.get("created") or 0
    try:
        env_updated_at = branch_env_last_updated_ms(
            token, project_id, team_id, branch
        )
    except Exception as exc:  # noqa: BLE001
        print(f"env freshness check failed ({exc}); forcing a rebuild")
        return False
    if not deployed_at or not env_updated_at:
        return False
    return env_updated_at < deployed_at


def redeploy(token, team_id, project_name, deployment_id):
    url = (
        "https://api.vercel.com/v13/deployments"
        f"?teamId={urllib.parse.quote(team_id, safe='')}&forceNew=1"
    )
    body = json.dumps(
        {"name": project_name, "deploymentId": deployment_id}
    ).encode()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request) as response:
        return json.load(response)


def main():
    token = os.environ["VERCEL_TOKEN"]
    project_id = os.environ["VERCEL_PROJECT_ID"]
    team_id = os.environ["VERCEL_ORG_ID"]
    branch = os.environ["VERCEL_GIT_BRANCH"]
    commit_sha = os.environ["VERCEL_GIT_COMMIT_SHA"]

    force = os.environ.get("VERCEL_FORCE_REDEPLOY", "true").lower() != "false"

    deployment = find_existing_deployment(
        token, project_id, team_id, branch, commit_sha
    )
    reuse = (
        not force
        and deployment_state(deployment) not in FAILED_STATES
        and auto_deployment_is_current(
            token, project_id, team_id, branch, deployment
        )
    )
    if reuse:
        preview_url = "https://" + deployment["url"]
        print(f"Reusing auto-created deployment (branch env unchanged): {preview_url}")
    else:
        redeployed = redeploy(
            token, team_id, deployment["name"], deployment["uid"]
        )
        preview_url = "https://" + redeployed["url"]

    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"preview_url={preview_url}\n")

    print(preview_url)


if __name__ == "__main__":
    main()
