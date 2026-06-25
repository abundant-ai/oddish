import os
import re
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
PREVIEW_URL = "https://abundant-ai-preview--oddish-pr-0-api.modal.run"

needs_bash = pytest.mark.skipif(shutil.which("bash") is None, reason="bash required")


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
    assert "prepare-preview-database" in _needs(jobs["deploy-preview-backend"])


def test_post_links_waits_for_backend_and_vercel():
    needs = _needs(_wf()["jobs"]["post-preview-links"])
    assert "deploy-preview-backend" in needs
    assert "update-vercel-preview" in needs


def test_deterministic_url_single_sourced_and_consumed():
    jobs = _wf()["jobs"]
    assert URL_FRAGMENT in jobs["detect-changes"]["outputs"]["preview_api_url"]
    assert "preview_api_url" in jobs["update-vercel-preview"]["env"]["MODAL_API_URL"]
    assert (
        "preview_api_url"
        in jobs["deploy-preview-backend"]["env"]["EXPECTED_MODAL_API_URL"]
    )
    assert WORKFLOW.read_text().count(URL_FRAGMENT) == 1


def test_vercel_modal_url_gated_with_prod_fallback():
    expr = _wf()["jobs"]["update-vercel-preview"]["env"]["MODAL_API_URL"]
    assert "deploy_backend == 'true'" in expr
    assert "branch_was_created == 'true'" in expr
    assert "preview_api_url" in expr
    assert expr.rstrip().endswith("|| '' }}")


def test_pr_url_threaded_to_banner_env_and_cleaned_up():
    # The preview banner links back to the PR via NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL,
    # which is only populated if the PR html_url is threaded all the way through
    # the Vercel env. Guard the full chain since the frontend has no test suite.
    job = _wf()["jobs"]["update-vercel-preview"]
    assert "html_url" in job["env"]["PR_URL"]

    update = (PREVIEW / "update_vercel_preview.sh").read_text()
    assert 'NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL "${PR_URL:-}"' in update

    stop = (PREVIEW / "stop_preview.sh").read_text()
    assert "NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL" in stop


def test_url_fragment_derives_from_modal_app_label():
    m = re.search(r'f"\{MODAL_APP_NAME\}(-\w+)"', MODAL_APP.read_text())
    assert m, "preview webhook label formula not found in modal_app.py"
    expected = f"abundant-ai-preview--oddish-pr-{{0}}{m.group(1)}.modal.run"
    assert expected == URL_FRAGMENT
    assert expected in WORKFLOW.read_text()


def test_prepare_stops_before_supabase_wait():
    s = PREPARE.read_text()
    assert "stop_modal_preview_app.sh" in s
    assert s.index("stop_modal_preview_app.sh") < s.index(
        "wait_for_supabase_branch.sh"
    )


def _run_prepare(extra_env, *, branch_was_created="false", stop_exit=0):
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

    stub("stop_modal_preview_app.sh", f'echo stop >> "{order}"\nexit {stop_exit}')
    stub(
        "wait_for_supabase_branch.sh",
        f'echo supabase >> "{order}"\n'
        'echo "branch_ref=br123" >> "$GITHUB_OUTPUT"\n'
        'echo "branch_id=id123" >> "$GITHUB_OUTPUT"\n'
        f'echo "branch_was_created={branch_was_created}" >> "$GITHUB_OUTPUT"\n'
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
    proc = subprocess.run(
        ["bash", str(pv / "prepare_preview_database.sh")],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    return order.read_text().split()


@needs_bash
def test_stop_fold_runs_first_when_deploying_backend():
    order = _run_prepare({"DEPLOY_BACKEND": "true", "RUN_MIGRATIONS": "false"})
    assert order[:2] == ["stop", "supabase"]
    assert "publish" in order
    assert "migrate" in order
    assert "seed" not in order


@needs_bash
def test_stop_fold_skipped_on_migrations_only():
    order = _run_prepare({"DEPLOY_BACKEND": "false", "RUN_MIGRATIONS": "true"})
    assert "stop" not in order
    assert order[0] == "supabase"
    assert "migrate" in order
    assert "seed" in order
    assert "publish" not in order


@needs_bash
def test_created_branch_seeds_and_publishes_without_flags():
    order = _run_prepare(
        {"DEPLOY_BACKEND": "false", "RUN_MIGRATIONS": "false"},
        branch_was_created="true",
    )
    assert order == ["supabase", "migrate", "seed", "publish"]


@needs_bash
def test_no_work_when_all_flags_false():
    order = _run_prepare({"DEPLOY_BACKEND": "false", "RUN_MIGRATIONS": "false"})
    assert order == ["supabase"]


@needs_bash
def test_full_fanout_order_when_deploy_and_migrations():
    order = _run_prepare({"DEPLOY_BACKEND": "true", "RUN_MIGRATIONS": "true"})
    assert order == ["stop", "supabase", "migrate", "seed", "publish"]


@needs_bash
def test_prepare_survives_failed_stop():
    order = _run_prepare(
        {"DEPLOY_BACKEND": "true", "RUN_MIGRATIONS": "false"}, stop_exit=1
    )
    assert order[:2] == ["stop", "supabase"]


def _run_deploy(emitted_url, expected_url=None):
    tmp = Path(tempfile.mkdtemp())
    pv = tmp / "preview"
    pv.mkdir()
    shutil.copy(DEPLOY, pv / "deploy_preview_backend.sh")
    dmp = pv / "deploy_modal_preview.sh"
    dmp.write_text(
        "#!/usr/bin/env bash\nset -e\n"
        f'echo "modal_api_url={emitted_url}" >> "$GITHUB_OUTPUT"\n'
    )
    dmp.chmod(0o755)
    (pv / "wait_for_modal_ready.py").write_text("import sys\nsys.exit(0)\n")
    for name in ("summary", "out"):
        (tmp / name).write_text("")
    env = {
        **os.environ,
        "GITHUB_STEP_SUMMARY": str(tmp / "summary"),
        "GITHUB_OUTPUT": str(tmp / "out"),
        "MODAL_ENVIRONMENT": "preview",
        "MODAL_APP_NAME": "oddish-pr-0",
    }
    if expected_url is not None:
        env["EXPECTED_MODAL_API_URL"] = expected_url
    return subprocess.run(
        ["bash", str(pv / "deploy_preview_backend.sh")],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
    )


@needs_bash
def test_deploy_guard_passes_on_match():
    assert _run_deploy(PREVIEW_URL, PREVIEW_URL).returncode == 0


@needs_bash
def test_deploy_guard_passes_on_trailing_slash_only():
    assert _run_deploy(PREVIEW_URL + "/", PREVIEW_URL).returncode == 0


@needs_bash
def test_deploy_guard_fails_on_mismatch():
    proc = _run_deploy("https://wrong.modal.run", PREVIEW_URL)
    assert proc.returncode == 1
    assert "mismatch" in proc.stderr


@needs_bash
def test_deploy_guard_skipped_when_expected_unset():
    assert _run_deploy("https://anything.modal.run").returncode == 0
