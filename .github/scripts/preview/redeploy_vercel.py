"""Find the Vercel preview deployment for this PR's commit and redeploy it.

Vercel auto-creates a preview deployment when GitHub fires the PR webhook,
but those deployments are built before this workflow gets a chance to set
NEXT_PUBLIC_MODAL_API_URL etc. We locate that auto-deployment by branch +
commit SHA, then POST a redeploy so the new env vars take effect.

Required env:
    VERCEL_TOKEN
    VERCEL_PROJECT_ID
    VERCEL_ORG_ID                   team id
    VERCEL_GIT_BRANCH               PR head ref
    VERCEL_GIT_COMMIT_SHA           PR head SHA
    GITHUB_OUTPUT                   path provided by GitHub Actions

Outputs (GITHUB_OUTPUT):
    preview_url                     https://<deployment>.vercel.app
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request

VERCEL_API = "https://api.vercel.com"
LIST_RETRIES = 18
LIST_INTERVAL_SECONDS = 10
LIST_LIMIT = 20


def _commit_sha(deployment: dict) -> str | None:
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


def _get_json(url: str, token: str):
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {token}"}
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def _post_json(url: str, token: str, body: dict):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def _find_deployment(token: str, project_id: str, team_id: str, branch: str, commit_sha: str):
    params = urllib.parse.urlencode(
        {
            "projectId": project_id,
            "teamId": team_id,
            "limit": LIST_LIMIT,
            "target": "preview",
            "branch": branch,
        }
    )
    url = f"{VERCEL_API}/v6/deployments?{params}"

    for _ in range(LIST_RETRIES):
        payload = _get_json(url, token)
        for candidate in payload.get("deployments", []):
            if _commit_sha(candidate) == commit_sha:
                return candidate
        time.sleep(LIST_INTERVAL_SECONDS)
    return None


def main() -> int:
    try:
        token = os.environ["VERCEL_TOKEN"]
        project_id = os.environ["VERCEL_PROJECT_ID"]
        team_id = os.environ["VERCEL_ORG_ID"]
        branch = os.environ["VERCEL_GIT_BRANCH"]
        commit_sha = os.environ["VERCEL_GIT_COMMIT_SHA"]
        github_output = os.environ["GITHUB_OUTPUT"]
    except KeyError as exc:
        print(f"Missing required env var: {exc.args[0]}", file=sys.stderr)
        return 2

    deployment = _find_deployment(token, project_id, team_id, branch, commit_sha)
    if deployment is None:
        print(
            f"No Vercel preview deployment found for branch {branch!r} at commit {commit_sha!r}",
            file=sys.stderr,
        )
        return 1

    redeploy_url = f"{VERCEL_API}/v13/deployments?teamId={team_id}&forceNew=1"
    redeploy = _post_json(
        redeploy_url,
        token,
        {"name": deployment["name"], "deploymentId": deployment["uid"]},
    )
    preview_url = f"https://{redeploy['url']}"

    with open(github_output, "a", encoding="utf-8") as fh:
        fh.write(f"preview_url={preview_url}\n")
    print(preview_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
