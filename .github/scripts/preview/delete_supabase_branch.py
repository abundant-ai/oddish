"""Delete the Supabase preview branch matching this PR (idempotent).

The Supabase GitHub integration auto-deletes branches when PRs close,
but this is a belt-and-suspenders pass that runs from the workflow's
`stop-preview` job. A "not found" from the CLI means the integration
already cleaned up.

Uses the Supabase CLI (no first-party Python SDK exists for the
Management API).

Required env: SUPABASE_ACCESS_TOKEN, SUPABASE_PROJECT_REF, GIT_BRANCH,
PR_NUMBER. Always exits 0 — cleanup should not fail noisy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys


def main() -> int:
    try:
        project_ref = os.environ["SUPABASE_PROJECT_REF"]
        git_branch = os.environ["GIT_BRANCH"]
        pr_number = int(os.environ["PR_NUMBER"])
    except KeyError as exc:
        print(f"Missing required env var: {exc.args[0]}", file=sys.stderr)
        return 0

    try:
        listing = subprocess.run(
            ["supabase", "branches", "list", "--project-ref", project_ref, "-o", "json"],
            check=True,
            text=True,
            capture_output=True,
        )
    except subprocess.CalledProcessError as exc:
        print(f"branches list failed: {exc.stderr}", file=sys.stderr)
        return 0
    branches = json.loads(listing.stdout)

    target = next(
        (
            b
            for b in branches
            if not b.get("persistent")
            and (b.get("git_branch") == git_branch or b.get("pr_number") == pr_number)
        ),
        None,
    )

    if target is None:
        print(
            f"No Supabase branch found for git_branch={git_branch!r} "
            f"pr_number={pr_number} (Supabase GitHub integration likely "
            "already deleted it)."
        )
        return 0

    try:
        subprocess.run(
            [
                "supabase", "branches", "delete", target["id"],
                "--project-ref", project_ref,
            ],
            check=True,
            text=True,
            capture_output=True,
            input="y\n",  # confirm prompt
        )
        print(f"Deleted Supabase branch {target['id']} ({target.get('name')!r}).")
    except subprocess.CalledProcessError as exc:
        msg = (exc.stderr or "").lower()
        if "not found" in msg or "404" in msg:
            print(f"Supabase branch {target['id']} already gone.")
        else:
            print(
                f"Delete failed for branch {target['id']}: {exc.stderr}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
