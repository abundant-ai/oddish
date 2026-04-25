"""Delete the Supabase preview branch matching this PR (idempotent).

The Supabase GitHub integration auto-deletes branches when PRs close,
but this is a belt-and-suspenders pass that runs from the workflow's
`stop-preview` job after the integration has had a chance. A 404 just
means the integration already cleaned up.

Required env:
    SUPABASE_ACCESS_TOKEN   personal access token with branch read+delete
    SUPABASE_PROJECT_REF    parent project ref (the prod project)
    GIT_BRANCH              PR head ref
    PR_NUMBER               PR number

Exits 0 on success, success-with-already-gone, or any non-fatal API
error so the cleanup workflow doesn't fail noisily on a transient
issue. Real errors are still logged to stderr.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

API_BASE = "https://api.supabase.com/v1"
USER_AGENT = (
    "Mozilla/5.0 (compatible; oddish-ci-modal-preview/1.0; "
    "+https://github.com/abundant-ai/oddish)"
)


def _request(method: str, path: str, token: str) -> tuple[int, bytes]:
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def main() -> int:
    try:
        token = os.environ["SUPABASE_ACCESS_TOKEN"]
        project_ref = os.environ["SUPABASE_PROJECT_REF"]
        git_branch = os.environ["GIT_BRANCH"]
        pr_number = int(os.environ["PR_NUMBER"])
    except KeyError as exc:
        print(f"Missing required env var: {exc.args[0]}", file=sys.stderr)
        return 0  # cleanup job should not fail on env misconfiguration

    status, body = _request("GET", f"/projects/{project_ref}/branches", token)
    if status != 200:
        print(f"List branches failed: {status} {body!r}", file=sys.stderr)
        return 0
    branches = json.loads(body)

    target = None
    for candidate in branches:
        if candidate.get("persistent"):
            continue
        if (
            candidate.get("git_branch") == git_branch
            or candidate.get("pr_number") == pr_number
        ):
            target = candidate
            break

    if target is None:
        print(
            f"No Supabase branch found for git_branch={git_branch!r} "
            f"pr_number={pr_number} (Supabase GitHub integration likely "
            "already deleted it)."
        )
        return 0

    status, body = _request("DELETE", f"/branches/{target['id']}", token)
    if status in (200, 202, 204):
        print(f"Deleted Supabase branch {target['id']} ({target.get('name')!r}).")
    elif status == 404:
        print(f"Supabase branch {target['id']} already gone.")
    else:
        print(
            f"Delete returned HTTP {status} for branch {target['id']}: {body!r}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
