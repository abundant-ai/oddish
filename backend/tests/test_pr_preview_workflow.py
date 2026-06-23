import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/pr-preview.yml"
PREVIEW = REPO / ".github/scripts/preview"
PREPARE = PREVIEW / "prepare_preview_database.sh"
DEPLOY = PREVIEW / "deploy_preview_backend.sh"
MODAL_APP = REPO / "backend/modal_app.py"

URL_FRAGMENT = "abundant-ai-preview--oddish-pr-{0}-api.modal.run"


def _wf():
    return yaml.safe_load(WORKFLOW.read_text())


def _needs(job):
    n = job.get("needs", [])
    return [n] if isinstance(n, str) else list(n)


def test_stop_job_removed_core_jobs_present():
    jobs = _wf()["jobs"]
    assert "stop-previous-preview-backend" not in jobs
    for j in (
        "detect-changes",
        "prepare-preview-database",
        "deploy-preview-backend",
        "update-vercel-preview",
        "post-preview-links",
        "stop-preview",
    ):
        assert j in jobs


def test_prepare_only_needs_detect():
    job = _wf()["jobs"]["prepare-preview-database"]
    assert _needs(job) == ["detect-changes"]
    assert "stop-previous" not in job.get("if", "")


def test_vercel_decoupled_from_backend_deploy():
    job = _wf()["jobs"]["update-vercel-preview"]
    needs = _needs(job)
    assert "deploy-preview-backend" not in needs
    assert "prepare-preview-database" in needs
    assert "detect-changes" in needs
    assert "deploy-preview-backend" not in job.get("if", "")


def test_backend_and_vercel_are_siblings():
    jobs = _wf()["jobs"]
    assert "update-vercel-preview" not in _needs(jobs["deploy-preview-backend"])
    assert "deploy-preview-backend" not in _needs(jobs["update-vercel-preview"])
    assert "prepare-preview-database" in _needs(jobs["deploy-preview-backend"])


def test_post_links_waits_for_backend_and_vercel():
    needs = _needs(_wf()["jobs"]["post-preview-links"])
    assert "deploy-preview-backend" in needs
    assert "update-vercel-preview" in needs


def test_deterministic_url_used_by_vercel_and_guarded_by_deploy():
    jobs = _wf()["jobs"]
    assert URL_FRAGMENT in jobs["update-vercel-preview"]["env"]["MODAL_API_URL"]
    deploy_env = jobs["deploy-preview-backend"]["env"]
    assert "EXPECTED_MODAL_API_URL" in deploy_env
    assert URL_FRAGMENT in deploy_env["EXPECTED_MODAL_API_URL"]


def test_url_format_tied_to_modal_app_label():
    assert 'f"{MODAL_APP_NAME}-api"' in MODAL_APP.read_text()
    assert URL_FRAGMENT in WORKFLOW.read_text()


def test_deploy_script_asserts_expected_url():
    assert "EXPECTED_MODAL_API_URL" in DEPLOY.read_text()


def test_prepare_stops_before_supabase_wait():
    s = PREPARE.read_text()
    assert "stop_modal_preview_app.sh" in s
    assert s.index("stop_modal_preview_app.sh") < s.index("wait_for_supabase_branch.sh")


def _run_prepare(extra_env):
    tmp = Path(tempfile.mkdtemp())
    pv = tmp / "preview"
    pv.mkdir()
    (tmp / "backend").mkdir()
    bins = tmp / "bin"
    bins.mkdir()
    order = tmp / "order"
    shutil.copy(PREPARE, pv / "prepare_preview_database.sh")

    def stub(name, body):
        p = pv / name
        p.write_text(f"#!/usr/bin/env bash\nset -e\n{body}\n")
        p.chmod(0o755)

    stub("stop_modal_preview_app.sh", f'echo stop >> "{order}"')
    stub(
        "wait_for_supabase_branch.sh",
        f'echo supabase >> "{order}"\n'
        'echo "branch_ref=br123" >> "$GITHUB_OUTPUT"\n'
        'echo "branch_id=id123" >> "$GITHUB_OUTPUT"\n'
        'echo "branch_was_created=false" >> "$GITHUB_OUTPUT"\n'
        'echo "ODDISH_DATABASE_URL=postgresql://x" >> "$GITHUB_ENV"',
    )
    stub("run_preview_migrations.sh", f'echo migrate >> "{order}"')
    stub("publish_modal_db_secret.sh", f'echo publish >> "{order}"')
    (pv / "seed_preview_db.py").write_text("")
    fake_uv = bins / "uv"
    fake_uv.write_text(f'#!/usr/bin/env bash\necho seed >> "{order}"\n')
    fake_uv.chmod(0o755)

    for name in ("summary", "out", "env"):
        (tmp / name).write_text("")
    env = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "GITHUB_STEP_SUMMARY": str(tmp / "summary"),
        "GITHUB_WORKSPACE": str(tmp),
        "GITHUB_OUTPUT": str(tmp / "out"),
        "GITHUB_ENV": str(tmp / "env"),
        "MODAL_ENVIRONMENT": "preview",
        "MODAL_APP_NAME": "oddish-pr-0",
        **extra_env,
    }
    subprocess.run(
        ["bash", str(pv / "prepare_preview_database.sh")],
        env=env,
        cwd=str(tmp),
        check=True,
    )
    return order.read_text().split() if order.exists() else []


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_stop_fold_runs_first_when_deploying_backend():
    order = _run_prepare({"DEPLOY_BACKEND": "true", "RUN_MIGRATIONS": "false"})
    assert order[:2] == ["stop", "supabase"]
    assert "publish" in order
    assert "migrate" in order
    assert "seed" not in order


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")
def test_stop_fold_skipped_on_migrations_only():
    order = _run_prepare({"DEPLOY_BACKEND": "false", "RUN_MIGRATIONS": "true"})
    assert "stop" not in order
    assert order[0] == "supabase"
    assert "migrate" in order
    assert "seed" in order
    assert "publish" not in order
