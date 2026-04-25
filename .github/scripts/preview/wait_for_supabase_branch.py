"""Wait for the Supabase preview branch matching this PR and emit its DB URL.

Uses the Supabase CLI (the officially supported client for the
Management API; there is no first-party Python SDK as of writing).
Polls `supabase branches list` until the integration-provisioned branch
shows up and the underlying Postgres project is healthy, then calls
`supabase branches get` to read the pooler connection string with
credentials already embedded — no separate pooler-config or branch-detail
API calls needed.

Required env:
    SUPABASE_ACCESS_TOKEN   read by the Supabase CLI for auth
    SUPABASE_PROJECT_REF    parent project ref
    GIT_BRANCH              PR head ref
    PR_NUMBER               PR number
    GITHUB_ENV              path provided by GitHub Actions
    GITHUB_OUTPUT           path provided by GitHub Actions

Outputs (GITHUB_OUTPUT):
    branch_id, branch_ref

Exports (GITHUB_ENV):
    ODDISH_DATABASE_URL     drives Alembic on the runner; consumed by the
                            `modal secret create` step that injects it
                            into the per-PR Modal secret.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time

BRANCH_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 10
TERMINAL_FAILURE_STATUSES = {"MIGRATIONS_FAILED", "FUNCTIONS_FAILED"}
READY_BRANCH_STATUSES = {"MIGRATIONS_PASSED", "FUNCTIONS_DEPLOYED"}


def _supabase_json(*args: str) -> object:
    result = subprocess.run(
        ["supabase", *args, "-o", "json"],
        check=True,
        text=True,
        capture_output=True,
    )
    return json.loads(result.stdout)


def _find_branch(
    branches: list[dict], git_branch: str, pr_number: int
) -> dict | None:
    for candidate in branches:
        if candidate.get("persistent"):
            continue
        if (
            candidate.get("git_branch") == git_branch
            or candidate.get("pr_number") == pr_number
        ):
            return candidate
    return None


def _wait_for_branch(project_ref: str, git_branch: str, pr_number: int) -> dict:
    deadline = time.time() + BRANCH_TIMEOUT_SECONDS
    last_status: tuple[str | None, str | None] | None = None

    while time.time() < deadline:
        try:
            branches = _supabase_json(
                "branches", "list", "--project-ref", project_ref
            )
        except subprocess.CalledProcessError as exc:
            print(f"branches list failed: {exc.stderr}", file=sys.stderr)
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        branch = _find_branch(branches, git_branch, pr_number)
        if branch is None:
            print(
                f"No Supabase preview branch yet for git_branch={git_branch!r} "
                f"pr_number={pr_number}. Waiting..."
            )
            time.sleep(POLL_INTERVAL_SECONDS)
            continue

        branch_status = branch.get("status")
        preview_status = branch.get("preview_project_status")
        last_status = (branch_status, preview_status)
        print(
            f"Supabase branch {branch['id']} status={branch_status!r} "
            f"preview_project_status={preview_status!r}"
        )

        if branch_status in TERMINAL_FAILURE_STATUSES:
            raise SystemExit(
                f"Supabase branch {branch['id']} entered terminal status "
                f"{branch_status!r}. Check the Supabase dashboard for details."
            )

        if (
            preview_status == "ACTIVE_HEALTHY"
            and branch_status in READY_BRANCH_STATUSES
        ):
            return branch

        time.sleep(POLL_INTERVAL_SECONDS)

    raise SystemExit(
        "Timed out waiting for the Supabase preview branch to become healthy "
        f"(git_branch={git_branch!r}, last status={last_status!r}). Confirm "
        "the Supabase GitHub integration is installed on the repo and that "
        "branching is enabled for the project."
    )


def _to_asyncpg(url: str) -> str:
    """Turn `postgresql://` into `postgresql+asyncpg://` for SQLAlchemy."""
    if url.startswith("postgresql://") and "+asyncpg" not in url:
        return "postgresql+asyncpg://" + url[len("postgresql://") :]
    return url


def _append(path: str, lines: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{line}\n")


def main() -> int:
    try:
        project_ref = os.environ["SUPABASE_PROJECT_REF"]
        git_branch = os.environ["GIT_BRANCH"]
        pr_number = int(os.environ["PR_NUMBER"])
        github_env = os.environ["GITHUB_ENV"]
        github_output = os.environ["GITHUB_OUTPUT"]
    except KeyError as exc:
        print(f"Missing required env var: {exc.args[0]}", file=sys.stderr)
        return 2

    branch = _wait_for_branch(project_ref, git_branch, pr_number)
    creds = _supabase_json(
        "branches", "get", branch["id"], "--project-ref", project_ref
    )
    pg_url = creds.get("POSTGRES_URL")
    if not pg_url:
        raise SystemExit(
            "supabase branches get did not return POSTGRES_URL "
            f"(keys: {sorted(creds.keys())})."
        )

    database_url = _to_asyncpg(pg_url)
    _append(github_env, [f"ODDISH_DATABASE_URL={database_url}"])
    _append(
        github_output,
        [
            f"branch_id={branch['id']}",
            f"branch_ref={branch.get('project_ref', '')}",
        ],
    )
    print(
        f"Supabase preview branch ready: ref={branch.get('project_ref')} "
        f"id={branch['id']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
