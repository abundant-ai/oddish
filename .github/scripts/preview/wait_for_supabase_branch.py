"""Wait for the PR's Supabase preview branch and emit its DB URL."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.parse

TIMEOUT_SECONDS = 600
POLL_SECONDS = 10
TERMINAL = {"MIGRATIONS_FAILED", "FUNCTIONS_FAILED"}
READY = {"MIGRATIONS_PASSED", "FUNCTIONS_DEPLOYED"}


def supabase(*args: str):
    out = subprocess.run(
        ["supabase", *args, "-o", "json"],
        check=True, text=True, capture_output=True,
    ).stdout
    return json.loads(out)


def matches(branch, git_branch, pr_number):
    return not branch.get("persistent") and (
        branch.get("git_branch") == git_branch
        or branch.get("pr_number") == pr_number
    )


def main() -> None:
    project_ref = os.environ["SUPABASE_PROJECT_REF"]
    git_branch = os.environ["GIT_BRANCH"]
    pr_number = int(os.environ["PR_NUMBER"])

    deadline = time.time() + TIMEOUT_SECONDS
    branch = None
    while time.time() < deadline:
        branches = supabase("branches", "list", "--project-ref", project_ref)
        branch = next(
            (b for b in branches if matches(b, git_branch, pr_number)), None
        )
        if branch:
            status = branch.get("status")
            preview = branch.get("preview_project_status")
            print(f"branch {branch['id']} status={status} preview={preview}")
            if status in TERMINAL:
                sys.exit(f"branch entered terminal status {status}")
            if preview == "ACTIVE_HEALTHY" and status in READY:
                break
        else:
            print(f"no branch yet for {git_branch!r}")
        time.sleep(POLL_SECONDS)
    else:
        sys.exit(f"timed out waiting for branch (last={branch})")

    creds = supabase("branches", "get", branch["id"], "--project-ref", project_ref)
    parsed = urllib.parse.urlparse(creds["POSTGRES_URL"])
    # Strip query string — Supabase passes libpq params (connect_timeout=)
    # that asyncpg rejects.
    db_url = urllib.parse.urlunparse(
        ("postgresql+asyncpg", parsed.netloc, parsed.path, "", "", "")
    )

    with open(os.environ["GITHUB_ENV"], "a") as fh:
        fh.write(f"ODDISH_DATABASE_URL={db_url}\n")
    with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
        fh.write(f"branch_id={branch['id']}\n")
        fh.write(f"branch_ref={branch.get('project_ref', '')}\n")
    print(f"ready: {branch.get('project_ref')}")


if __name__ == "__main__":
    main()
