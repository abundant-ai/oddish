"""Detect where a task upload is coming from.

Git and CI detection are best-effort: a task directory outside a git repo,
or a git binary that is absent, yields empty provenance rather than an error.
Upload must never fail because provenance could not be determined.
"""

from __future__ import annotations

import re
import socket
import subprocess
from collections.abc import Mapping
from pathlib import Path

from oddish.schemas import TaskProvenance

_PR_REF_PATTERN = re.compile(r"^refs/pull/(\d+)/")


def _git(task_path: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(task_path), *args],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, UnicodeDecodeError):
        # UnicodeDecodeError subclasses ValueError, not OSError/SubprocessError,
        # so it needs its own catch to preserve the never-raises contract.
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _normalize_remote(url: str | None) -> str | None:
    """Reduce a git remote URL to 'owner/repo'."""
    if not url:
        return None
    trimmed = url.removesuffix(".git")
    if trimmed.startswith("git@") and ":" in trimmed:
        return trimmed.split(":", 1)[1] or None
    parts = [segment for segment in trimmed.split("/") if segment]
    if len(parts) >= 2:
        return f"{parts[-2]}/{parts[-1]}"
    return None


def _parse_pr_number(ref: str | None) -> int | None:
    """Extract the PR number from a GitHub Actions pull_request ``GITHUB_REF``.

    ``GITHUB_REF`` is ``refs/pull/<N>/merge`` (or ``/head``) on pull_request
    events; any other ref (branch, tag, malformed) yields None rather than
    raising.
    """
    if not ref:
        return None
    match = _PR_REF_PATTERN.match(ref)
    if not match:
        return None
    return int(match.group(1))


def detect_ci_context(env: Mapping[str, str]) -> TaskProvenance:
    """Derive CI fields from environment variables."""
    is_ci = env.get("CI", "").lower() in {"true", "1", "yes"}
    if env.get("GITHUB_ACTIONS", "").lower() != "true":
        return TaskProvenance(uploader_is_ci=is_ci)

    repo = env.get("GITHUB_REPOSITORY")
    run_id = env.get("GITHUB_RUN_ID")
    server = env.get("GITHUB_SERVER_URL", "https://github.com")
    run_url = f"{server}/{repo}/actions/runs/{run_id}" if repo and run_id else None
    return TaskProvenance(
        ci_provider="github_actions",
        ci_run_id=run_id,
        ci_run_url=run_url,
        ci_pr_number=_parse_pr_number(env.get("GITHUB_REF")),
        uploader_is_ci=True,
        source_repo=repo,
        source_ref=env.get("GITHUB_REF_NAME"),
    )


def detect_provenance(
    task_path: Path, *, env: Mapping[str, str], cli_version: str | None = None
) -> TaskProvenance:
    """Combine git worktree state and CI environment into one record.

    CI values win over locally-detected git values: in CI the environment is
    authoritative about which ref is being built, while the checkout may be
    detached or shallow.
    """
    ci = detect_ci_context(env)

    repo_root = _git(task_path, "rev-parse", "--show-toplevel")
    source_path = None
    if repo_root:
        try:
            source_path = str(task_path.resolve().relative_to(Path(repo_root)))
        except ValueError:
            source_path = None

    try:
        host = socket.gethostname() or None
    except OSError:
        host = None

    return TaskProvenance(
        source_repo=ci.source_repo
        or _normalize_remote(_git(task_path, "remote", "get-url", "origin")),
        source_commit=_git(task_path, "rev-parse", "HEAD"),
        source_ref=ci.source_ref or _git(task_path, "rev-parse", "--abbrev-ref", "HEAD"),
        source_path=source_path,
        ci_provider=ci.ci_provider,
        ci_run_id=ci.ci_run_id,
        ci_run_url=ci.ci_run_url,
        ci_pr_number=ci.ci_pr_number,
        uploader_is_ci=ci.uploader_is_ci,
        uploader_cli_version=cli_version,
        uploader_host=None if ci.uploader_is_ci else host,
    )
