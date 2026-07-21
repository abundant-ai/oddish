from __future__ import annotations

import os
import subprocess
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core import task_provenance
from oddish.core.task_provenance import (
    _normalize_remote,
    detect_ci_context,
    detect_provenance,
)


def test_detect_ci_context_github_actions():
    env = {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "abundant-ai/harbor-lh",
        "GITHUB_RUN_ID": "12345",
        "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REF_NAME": "feat/my-task",
    }
    ctx = detect_ci_context(env)
    assert ctx.ci_provider == "github_actions"
    assert ctx.uploader_is_ci is True
    assert ctx.ci_run_id == "12345"
    assert ctx.ci_run_url == "https://github.com/abundant-ai/harbor-lh/actions/runs/12345"


def test_detect_ci_context_local_laptop():
    ctx = detect_ci_context({})
    assert ctx.uploader_is_ci is False
    assert ctx.ci_provider is None
    assert ctx.ci_run_id is None


def test_detect_ci_context_generic_ci_without_github():
    ctx = detect_ci_context({"CI": "true"})
    assert ctx.uploader_is_ci is True
    assert ctx.ci_provider is None


def test_detect_provenance_does_not_raise_outside_git_repo(tmp_path: Path):
    ctx = detect_provenance(tmp_path, env={})
    assert ctx.source_repo is None
    assert ctx.source_commit is None
    assert ctx.source_ref is None


# --- Finding 2: ci_pr_number ------------------------------------------------


def test_detect_ci_context_pull_request_ref_yields_pr_number():
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "abundant-ai/harbor-lh",
        "GITHUB_REF": "refs/pull/482/merge",
    }
    ctx = detect_ci_context(env)
    assert ctx.ci_pr_number == 482


def test_detect_ci_context_branch_ref_yields_no_pr_number():
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "abundant-ai/harbor-lh",
        "GITHUB_REF": "refs/heads/main",
    }
    ctx = detect_ci_context(env)
    assert ctx.ci_pr_number is None


def test_detect_ci_context_malformed_ref_yields_no_pr_number_not_raise():
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "abundant-ai/harbor-lh",
        "GITHUB_REF": "refs/pull/not-a-number/merge",
    }
    ctx = detect_ci_context(env)
    assert ctx.ci_pr_number is None


# --- Finding 3: CI precedence, _normalize_remote, source_path, uploader_host


def _run_git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.com",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.com",
        },
    )


def _init_git_worktree(tmp_path: Path) -> Path:
    """Build a real git repo with a remote and a branch, distinct from any CI env."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-q")
    _run_git(repo, "checkout", "-q", "-b", "local-branch")
    _run_git(repo, "remote", "add", "origin", "git@github.com:local-org/local-repo.git")
    (repo / "task.toml").write_text("version = \"1.0\"\n", encoding="utf-8")
    _run_git(repo, "add", "task.toml")
    _run_git(repo, "commit", "-q", "-m", "init")
    return repo


def test_ci_values_win_over_git_worktree_when_both_present(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    env = {
        "GITHUB_ACTIONS": "true",
        "GITHUB_REPOSITORY": "abundant-ai/ci-repo",
        "GITHUB_REF_NAME": "ci-ref",
    }
    ctx = detect_provenance(repo, env=env)
    local_only = detect_provenance(repo, env={})

    # Prove the git worktree really carries conflicting values, so the
    # precedence assertion below is not vacuously true.
    assert local_only.source_repo == "local-org/local-repo"
    assert local_only.source_ref == "local-branch"

    assert ctx.source_repo == "abundant-ai/ci-repo"
    assert ctx.source_ref == "ci-ref"


@pytest.mark.parametrize(
    "url,expected",
    [
        ("git@github.com:abundant-ai/harbor-lh.git", "abundant-ai/harbor-lh"),
        ("https://github.com/abundant-ai/harbor-lh.git", "abundant-ai/harbor-lh"),
        ("https://github.com/abundant-ai/harbor-lh", "abundant-ai/harbor-lh"),
        ("garbage", None),
        (None, None),
    ],
)
def test_normalize_remote(url, expected):
    assert _normalize_remote(url) == expected


def test_uploader_host_none_when_ci(monkeypatch):
    monkeypatch.setattr(task_provenance.socket, "gethostname", lambda: "runner-1")
    ctx = detect_ci_context({"GITHUB_ACTIONS": "true"})
    assert ctx.uploader_is_ci is True
    full = detect_provenance(Path("/tmp"), env={"GITHUB_ACTIONS": "true"})
    assert full.uploader_host is None


def test_uploader_host_set_when_not_ci(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(task_provenance.socket, "gethostname", lambda: "laptop-1")
    ctx = detect_provenance(tmp_path, env={})
    assert ctx.uploader_is_ci is False
    assert ctx.uploader_host == "laptop-1"


def test_source_path_relative_to_repo_root(tmp_path: Path):
    repo = _init_git_worktree(tmp_path)
    task_dir = repo / "tasks" / "my-task"
    task_dir.mkdir(parents=True)
    ctx = detect_provenance(task_dir, env={})
    assert ctx.source_path == "tasks/my-task"


# --- Finding 4: UnicodeDecodeError from subprocess.run does not propagate --


def test_git_returns_none_on_decode_error(monkeypatch, tmp_path: Path):
    def _raise(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(task_provenance.subprocess, "run", _raise)
    assert task_provenance._git(tmp_path, "rev-parse", "HEAD") is None


def test_detect_provenance_does_not_raise_on_decode_error(monkeypatch, tmp_path: Path):
    def _raise(*_args, **_kwargs):
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")

    monkeypatch.setattr(task_provenance.subprocess, "run", _raise)
    ctx = detect_provenance(tmp_path, env={})
    assert ctx.source_repo is None
    assert ctx.source_commit is None


# --- Finding 4: an over-length detected value degrades instead of raising --


def test_detect_provenance_drops_overlong_field_instead_of_raising(
    monkeypatch, tmp_path: Path
):
    real_git = task_provenance._git

    def _fake_git(task_path, *args):
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            # Exceeds TaskProvenance.source_ref's max_length=255.
            return "x" * 300
        return real_git(task_path, *args)

    monkeypatch.setattr(task_provenance, "_git", _fake_git)
    monkeypatch.setattr(task_provenance.socket, "gethostname", lambda: "laptop-1")

    ctx = detect_provenance(tmp_path, env={})

    assert ctx.source_ref is None
    # Only the offending field is dropped -- the rest of provenance survives.
    assert ctx.uploader_host == "laptop-1"
