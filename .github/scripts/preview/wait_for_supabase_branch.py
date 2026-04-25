"""Wait for the Supabase preview branch matching this PR and emit its DB URL.

Polls the Supabase Management API for the preview branch the GitHub
integration provisions for the PR, waits for the underlying Postgres
project to be ACTIVE_HEALTHY, fetches the branch's database credentials,
and writes them to GITHUB_ENV / GITHUB_OUTPUT.

Required env:
    SUPABASE_ACCESS_TOKEN   personal access token with branch read scope
    SUPABASE_PROJECT_REF    parent project ref (the prod project)
    GIT_BRANCH              PR head ref
    PR_NUMBER               PR number
    GITHUB_ENV              path provided by GitHub Actions
    GITHUB_OUTPUT           path provided by GitHub Actions

Outputs (GITHUB_OUTPUT):
    branch_id, branch_ref, db_host

Exports (GITHUB_ENV):
    ODDISH_DATABASE_URL     drives Alembic on the runner
    PREVIEW_DATABASE_URL    consumed by backend/modal_app.py to layer a
                            Modal secret override
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = "https://api.supabase.com/v1"
# Cloudflare in front of api.supabase.com returns HTTP 403 with error 1010
# ("browser signature banned") for the default `Python-urllib/...` User-Agent.
# Use the conventional "well-behaved bot" format so requests pass the WAF.
USER_AGENT = (
    "Mozilla/5.0 (compatible; oddish-ci-modal-preview/1.0; "
    "+https://github.com/abundant-ai/oddish)"
)
BRANCH_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 10
TERMINAL_FAILURE_STATUSES = {"MIGRATIONS_FAILED", "FUNCTIONS_FAILED"}
READY_BRANCH_STATUSES = {"MIGRATIONS_PASSED", "FUNCTIONS_DEPLOYED"}


def _request(path: str, token: str):
    req = urllib.request.Request(
        f"{API_BASE}{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as response:
        return json.load(response)


def _find_branch(branches, git_branch: str, pr_number: int):
    for candidate in branches:
        if candidate.get("persistent"):
            continue
        if (
            candidate.get("git_branch") == git_branch
            or candidate.get("pr_number") == pr_number
        ):
            return candidate
    return None


def _wait_for_branch(token: str, project_ref: str, git_branch: str, pr_number: int):
    deadline = time.time() + BRANCH_TIMEOUT_SECONDS
    last_status: tuple[str | None, str | None] | None = None

    while time.time() < deadline:
        try:
            branches = _request(f"/projects/{project_ref}/branches", token)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            print(f"List branches failed: {exc.code} {body}", file=sys.stderr)
            # 401/403 are auth/WAF rejections that won't recover from retries;
            # fail fast so the workflow surfaces the real reason instead of
            # spinning until the 10-minute timeout.
            if exc.code in (401, 403):
                raise SystemExit(
                    f"Supabase Management API rejected the request with HTTP {exc.code}. "
                    "If the body shows Cloudflare error 1010, the User-Agent is being "
                    "blocked. If it shows an auth error, regenerate SUPABASE_ACCESS_TOKEN."
                )
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
        f"(git_branch={git_branch!r}, last status={last_status!r}). Confirm the "
        "Supabase GitHub integration is installed on the repo and that branching "
        "is enabled for the project."
    )


def _build_database_url(detail: dict, pooler_entries: list[dict]) -> str:
    if not detail.get("db_pass"):
        raise SystemExit(
            "Supabase branch detail is missing db_pass. Regenerate "
            "SUPABASE_ACCESS_TOKEN with branch read scope."
        )

    # Direct hostnames (`db.<ref>.supabase.co`) are IPv6-only on Supabase's
    # current hosted plan, and GitHub Actions runners don't have IPv6
    # outbound — connections hit "Network is unreachable". The Supavisor
    # pooler is dual-stack, so we connect through it instead. Transaction
    # mode (port 6543) is fine for Alembic and matches the runtime config
    # (statement_cache_size=0 is already set in alembic/env.py).
    primary = next(
        (entry for entry in pooler_entries if entry.get("database_type") == "PRIMARY"),
        None,
    )
    if primary is None:
        raise SystemExit(
            "Supabase pooler config did not include a PRIMARY entry; cannot "
            f"construct a database URL. Got: {pooler_entries!r}"
        )

    missing = [k for k in ("db_host", "db_port", "db_user", "db_name") if not primary.get(k)]
    if missing:
        raise SystemExit(f"Supabase pooler entry is missing fields: {missing}")

    password = urllib.parse.quote(detail["db_pass"], safe="")
    return (
        f"postgresql+asyncpg://{primary['db_user']}:{password}"
        f"@{primary['db_host']}:{primary['db_port']}/{primary['db_name']}"
    )


def _append(path: str, lines: list[str]) -> None:
    with open(path, "a", encoding="utf-8") as fh:
        for line in lines:
            fh.write(f"{line}\n")


def main() -> int:
    try:
        token = os.environ["SUPABASE_ACCESS_TOKEN"]
        project_ref = os.environ["SUPABASE_PROJECT_REF"]
        git_branch = os.environ["GIT_BRANCH"]
        pr_number = int(os.environ["PR_NUMBER"])
        github_env = os.environ["GITHUB_ENV"]
        github_output = os.environ["GITHUB_OUTPUT"]
    except KeyError as exc:
        print(f"Missing required env var: {exc.args[0]}", file=sys.stderr)
        return 2

    branch = _wait_for_branch(token, project_ref, git_branch, pr_number)
    detail = _request(f"/branches/{branch['id']}", token)
    pooler = _request(f"/projects/{detail['ref']}/config/database/pooler", token)
    database_url = _build_database_url(detail, pooler)

    _append(
        github_env,
        [
            # Drives Alembic on the runner and is also picked up by the
            # `modal secret create` step, which then injects it into the
            # per-PR Modal secret that the deployed app loads.
            f"ODDISH_DATABASE_URL={database_url}",
        ],
    )
    primary = next(entry for entry in pooler if entry.get("database_type") == "PRIMARY")
    _append(
        github_output,
        [
            f"branch_id={branch['id']}",
            f"branch_ref={detail['ref']}",
            f"db_host={primary['db_host']}",
        ],
    )

    print(
        f"Supabase preview branch ready: ref={detail['ref']} "
        f"pooler={primary['db_host']}:{primary['db_port']} "
        f"(direct {detail.get('db_host')} skipped — IPv6-only)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
