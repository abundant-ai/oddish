from __future__ import annotations

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.core.task_provenance import detect_ci_context, detect_provenance


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
