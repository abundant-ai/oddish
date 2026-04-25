"""Delete the PR's Supabase preview branch (idempotent)."""

import json
import os
import subprocess


def main() -> None:
    project_ref = os.environ["SUPABASE_PROJECT_REF"]
    git_branch = os.environ["GIT_BRANCH"]
    pr_number = int(os.environ["PR_NUMBER"])

    branches = json.loads(subprocess.run(
        ["supabase", "branches", "list", "--project-ref", project_ref, "-o", "json"],
        check=True, text=True, capture_output=True,
    ).stdout)

    target = next(
        (
            b for b in branches
            if not b.get("persistent")
            and (b.get("git_branch") == git_branch or b.get("pr_number") == pr_number)
        ),
        None,
    )
    if not target:
        print(f"no branch for {git_branch!r}; nothing to delete")
        return

    subprocess.run(
        ["supabase", "branches", "delete", target["id"], "--project-ref", project_ref],
        text=True, input="y\n",
    )
    print(f"deleted {target['id']}")


if __name__ == "__main__":
    main()
