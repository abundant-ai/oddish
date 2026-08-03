import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github/workflows/pr-preview.yml"
RESET_WORKFLOW = REPO / ".github/workflows/preview-reset.yml"
PREVIEW = REPO / ".github/scripts/preview"
PREPARE = PREVIEW / "prepare_preview_database.sh"
PRUNE_CLOSED_BRANCHES = PREVIEW / "prune_closed_supabase_branches.sh"
COMPUTE_PLAN = PREVIEW / "compute_deployment_plan.sh"
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


def test_prepare_can_prune_closed_pr_branches_at_supabase_capacity():
    job = _wf()["jobs"]["prepare-preview-database"]
    assert job["permissions"]["pull-requests"] == "read"
    assert "GITHUB_TOKEN" in job["env"]["GH_TOKEN"]

    wait_script = (PREVIEW / "wait_for_supabase_branch.sh").read_text()
    assert "active branch projects" in wait_script
    assert PRUNE_CLOSED_BRANCHES.name in wait_script


@needs_bash
def test_capacity_pruner_deletes_only_closed_non_current_pr_branches():
    tmp = Path(tempfile.mkdtemp())
    bins = tmp / "bin"
    bins.mkdir()
    delete_log = tmp / "deleted"
    curl_log = tmp / "curl-log"
    reopen_count = tmp / "reopen-count"
    reopen_count.write_text("0")
    recently_closed_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    branches = [
        {"id": "closed-id", "name": "pr-101", "persistent": False},
        {"id": "open-id", "name": "pr-102", "persistent": False},
        {"id": "error-id", "name": "pr-103", "persistent": False},
        {"id": "persistent-id", "name": "pr-104", "persistent": True},
        {"id": "current-id", "name": "pr-105", "persistent": False},
        {"id": "fresh-id", "name": "pr-106", "persistent": False},
        {"id": "null-persistent-id", "name": "pr-107", "persistent": None},
        {"id": "missing-persistent-id", "name": "pr-108"},
        {"id": "reopened-id", "name": "pr-109", "persistent": False},
        {"id": "other-id", "name": "feature-branch", "persistent": False},
    ]

    fake_supabase = bins / "supabase"
    fake_supabase.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'if [ "$1 $2" = "branches list" ]; then\n'
        "  printf '%s\\n' \"$BRANCHES_JSON\"\n"
        'elif [ "$1 $2" = "branches delete" ]; then\n'
        "  while IFS= read -r _; do :; done\n"
        '  printf \'%s\\n\' "$3" >> "$DELETE_LOG"\n'
        "else\n"
        "  exit 2\n"
        "fi\n"
    )
    fake_supabase.chmod(0o755)

    fake_curl = bins / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        'printf \'%s\\n\' "$*" >> "$CURL_LOG"\n'
        'case "$*" in\n'
        "  */pulls/101*) printf '%s\\n' "
        '\'{"state":"closed","closed_at":"2000-01-01T00:00:00Z"}\' ;;\n'
        '  */pulls/102*) printf \'%s\\n\' \'{"state":"open","closed_at":null}\' ;;\n'
        "  */pulls/103*) exit 22 ;;\n"
        "  */pulls/106*) printf '%s\\n' "
        '"{\\"state\\":\\"closed\\",\\"closed_at\\":\\"'
        + recently_closed_at
        + '\\"}" ;;\n'
        "  */pulls/109*)\n"
        '    count=$(cat "$REOPEN_COUNT")\n'
        "    count=$((count + 1))\n"
        '    printf \'%s\' "$count" > "$REOPEN_COUNT"\n'
        '    if [ "$count" -eq 1 ]; then\n'
        "      printf '%s\\n' "
        '\'{"state":"closed","closed_at":"2000-01-01T00:00:00Z"}\'\n'
        "    else\n"
        '      printf \'%s\\n\' \'{"state":"open","closed_at":null}\'\n'
        "    fi\n"
        "    ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake_curl.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "BRANCHES_JSON": json.dumps(branches),
        "DELETE_LOG": str(delete_log),
        "CURL_LOG": str(curl_log),
        "REOPEN_COUNT": str(reopen_count),
        "SUPABASE_PROJECT_REF": "project-ref",
        "GH_TOKEN": "test-token",
        "GITHUB_REPOSITORY": "abundant-ai/oddish",
        "CURRENT_BRANCH_NAME": "pr-105",
    }
    proc = subprocess.run(
        ["bash", str(PRUNE_CLOSED_BRANCHES)],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "1"
    assert delete_log.read_text().splitlines() == ["closed-id"]
    assert "keeping Supabase branch pr-102" in proc.stderr
    assert "could not read GitHub PR #103" in proc.stderr
    assert "keeping current Supabase branch pr-105" in proc.stderr
    assert (
        "keeping Supabase branch pr-106 (GitHub PR closed too recently)" in proc.stderr
    )
    requested = curl_log.read_text()
    assert "/pulls/107" not in requested
    assert "/pulls/108" not in requested
    assert reopen_count.read_text() == "2"
    assert "keeping Supabase branch pr-109 (GitHub PR state=open)" in proc.stderr


@needs_bash
def test_capacity_error_prunes_then_retries_branch_creation_end_to_end():
    tmp = Path(tempfile.mkdtemp())
    bins = tmp / "bin"
    bins.mkdir()
    state = tmp / "state"
    state.write_text("leaked")
    create_count = tmp / "create-count"
    create_count.write_text("0")
    delete_log = tmp / "deleted"
    github_env = tmp / "github-env"
    github_output = tmp / "github-output"

    fake_supabase = bins / "supabase"
    fake_supabase.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'case "$1 $2" in\n'
        '  "branches list")\n'
        '    case "$(cat "$STATE_FILE")" in\n'
        "      leaked) printf '%s\\n' "
        '\'[{"id":"leaked-id","name":"pr-123","persistent":false}]\' ;;\n'
        "      current) printf '%s\\n' "
        '\'[{"id":"current-id","name":"pr-999","persistent":false,'
        '"project_ref":"preview-ref","status":"MIGRATIONS_PASSED",'
        '"preview_project_status":"ACTIVE_HEALTHY"}]\' ;;\n'
        "      *) printf '%s\\n' '[]' ;;\n"
        "    esac\n"
        "    ;;\n"
        '  "branches create")\n'
        '    count=$(cat "$CREATE_COUNT")\n'
        "    count=$((count + 1))\n"
        '    printf \'%s\' "$count" > "$CREATE_COUNT"\n'
        '    if [ "$count" -eq 1 ]; then\n'
        "      echo 'Cannot create more than 50 active branch projects' >&2\n"
        "      exit 1\n"
        "    fi\n"
        "    printf '%s' current > \"$STATE_FILE\"\n"
        "    ;;\n"
        '  "branches delete")\n'
        '    printf \'%s\\n\' "$3" >> "$DELETE_LOG"\n'
        "    printf '%s' empty > \"$STATE_FILE\"\n"
        "    ;;\n"
        '  "branches get")\n'
        "    printf '%s\\n' "
        '\'{"POSTGRES_URL":"postgresql://postgres.preview:old@db.example.test:5432/postgres"}\'\n'
        "    ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake_supabase.chmod(0o755)

    fake_curl = bins / "curl"
    fake_curl.write_text(
        "#!/usr/bin/env bash\n"
        'case "$*" in\n'
        "  */pulls/123*) printf '%s\\n' "
        '\'{"state":"closed","closed_at":"2000-01-01T00:00:00Z"}\' ;;\n'
        "  *api.supabase.com*) printf '204' ;;\n"
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    fake_curl.chmod(0o755)

    fake_psql = bins / "psql"
    fake_psql.write_text("#!/usr/bin/env bash\nexit 0\n")
    fake_psql.chmod(0o755)

    env = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "STATE_FILE": str(state),
        "CREATE_COUNT": str(create_count),
        "DELETE_LOG": str(delete_log),
        "PR_NUMBER": "999",
        "SUPABASE_PROJECT_REF": "project-ref",
        "SUPABASE_ACCESS_TOKEN": "test-token",
        "GH_TOKEN": "test-token",
        "GITHUB_REPOSITORY": "abundant-ai/oddish",
        "GITHUB_ENV": str(github_env),
        "GITHUB_OUTPUT": str(github_output),
        "MAX_ATTEMPTS": "1",
        "CAPACITY_RETRY_DELAY_SECONDS": "0",
    }
    proc = subprocess.run(
        ["bash", str(PREVIEW / "wait_for_supabase_branch.sh")],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
    )

    assert proc.returncode == 0, proc.stderr
    assert create_count.read_text() == "2"
    assert delete_log.read_text().splitlines() == ["leaked-id"]
    assert "Supabase branch capacity is full" in proc.stderr
    assert "pruned 1 closed-PR Supabase preview branch(es)" in proc.stderr
    assert "retrying pr-999 creation after capacity pruning" in proc.stderr
    assert "branch_id=current-id" in github_output.read_text()


def test_vercel_waits_for_backend_deploy():
    job = _wf()["jobs"]["update-vercel-preview"]
    needs = _needs(job)
    assert "deploy-preview-backend" in needs
    assert "prepare-preview-database" in needs
    assert "detect-changes" in needs
    condition = job.get("if", "")
    assert "needs.deploy-preview-backend.result == 'success'" in condition
    assert "needs.deploy-preview-backend.result == 'skipped'" in condition


def test_backend_and_vercel_are_siblings():
    jobs = _wf()["jobs"]
    assert "update-vercel-preview" not in _needs(jobs["deploy-preview-backend"])
    assert "prepare-preview-database" in _needs(jobs["deploy-preview-backend"])


def test_post_links_waits_for_backend_and_vercel():
    job = _wf()["jobs"]["post-preview-links"]
    needs = _needs(job)
    assert "deploy-preview-backend" in needs
    assert "update-vercel-preview" in needs
    modal_url = job["env"]["MODAL_API_URL"]
    assert "needs.deploy-preview-backend.outputs.modal_api_url" in modal_url
    assert "needs.update-vercel-preview.outputs.backend_api_url" in modal_url


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
    assert "preview_backend_live == 'true'" in expr
    assert "preview_api_url" in expr
    assert expr.rstrip().endswith("|| '' }}")


def test_banner_env_threaded_to_vercel_and_cleaned_up():
    # The frontend has no test suite, so guard the whole chain here: a banner
    # var is only useful if it's threaded through the Vercel env and cleaned up.
    job_env = _wf()["jobs"]["update-vercel-preview"]["env"]
    assert "html_url" in job_env["PR_URL"]
    assert "title" in job_env["PR_TITLE"]
    assert "SUPABASE_BRANCH_REF" in job_env

    update = (PREVIEW / "update_vercel_preview.sh").read_text()
    stop = (PREVIEW / "stop_preview.sh").read_text()
    assert "dashboard/project/${SUPABASE_BRANCH_REF}" in update
    for name in (
        "NEXT_PUBLIC_ODDISH_PREVIEW_PR_URL",
        "NEXT_PUBLIC_ODDISH_PREVIEW_PR_TITLE",
        "NEXT_PUBLIC_ODDISH_PREVIEW_BACKEND_URL",
        "NEXT_PUBLIC_ODDISH_PREVIEW_DATABASE_URL",
    ):
        assert name in update, f"{name} not published in update_vercel_preview.sh"
        assert name in stop, f"{name} not cleaned up in stop_preview.sh"


def test_url_fragment_derives_from_modal_app_label():
    m = re.search(r'f"\{MODAL_APP_NAME\}(-\w+)"', MODAL_APP.read_text())
    assert m, "preview webhook label formula not found in modal_app.py"
    expected = f"abundant-ai-preview--oddish-pr-{{0}}{m.group(1)}.modal.run"
    assert expected == URL_FRAGMENT
    assert expected in WORKFLOW.read_text()


def test_prepare_stops_before_supabase_wait():
    s = PREPARE.read_text()
    assert "stop_modal_preview_app.sh" in s
    assert s.index("stop_modal_preview_app.sh") < s.index("wait_for_supabase_branch.sh")


def _run_prepare(
    extra_env,
    *,
    branch_was_created="false",
    stop_exit=0,
    rebuild=False,
    want_summary=False,
):
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
    # rebuild=True mimics the bootstrap taking the snapshot-rebuild path (it
    # seeds internally and writes the sentinel prepare exports for it).
    migrate_body = f'echo migrate >> "{order}"'
    if rebuild:
        migrate_body += '\nprintf 1 > "$SCHEMA_REBUILT_FILE"'
    stub("run_preview_migrations.sh", migrate_body)
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
    order_list = order.read_text().split()
    if want_summary:
        return order_list, (tmp / "summary").read_text()
    return order_list


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


@needs_bash
def test_rebuild_skips_shell_seed():
    # A snapshot rebuild already seeded inside the bootstrap (before the PR's
    # migrations ran), so the shell-level convergence seed must not re-run.
    order, summary = _run_prepare(
        {"DEPLOY_BACKEND": "true", "RUN_MIGRATIONS": "true"},
        rebuild=True,
        want_summary=True,
    )
    assert order == ["stop", "supabase", "migrate", "publish"]
    assert "seed" not in order
    assert "Schema rebuilt from prod snapshot: `true`" in summary


@needs_bash
def test_created_branch_rebuild_skips_shell_seed():
    order = _run_prepare(
        {"DEPLOY_BACKEND": "false", "RUN_MIGRATIONS": "false"},
        branch_was_created="true",
        rebuild=True,
    )
    assert order == ["supabase", "migrate", "publish"]


def _run_compute_plan(extra_env):
    tmp = Path(tempfile.mkdtemp())
    for name in ("summary", "out"):
        (tmp / name).write_text("")
    env = {
        **os.environ,
        "GITHUB_STEP_SUMMARY": str(tmp / "summary"),
        "GITHUB_OUTPUT": str(tmp / "out"),
        "EVENT_ACTION": "synchronize",
        "BACKEND_BASE": "abc123",
        "MIGRATIONS_BASE": "abc123",
        "BACKEND_CHANGED": "false",
        "MIGRATIONS_CHANGED": "false",
        "PR_BACKEND_CHANGED": "false",
        "PR_MIGRATIONS_CHANGED": "false",
        "PR_FRONTEND_CHANGED": "false",
        "WORKFLOW_CHANGED": "false",
        **extra_env,
    }
    proc = subprocess.run(
        ["bash", str(COMPUTE_PLAN)],
        env=env,
        cwd=str(tmp),
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stderr
    outputs = {}
    for line in (tmp / "out").read_text().splitlines():
        key, _, value = line.partition("=")
        outputs[key] = value
    return outputs


@needs_bash
def test_migrations_imply_backend_deploy():
    # A migrated schema must be boot-tested, and the rotated branch password
    # re-published to the Modal secret -- migrations always redeploy the backend.
    outputs = _run_compute_plan({"MIGRATIONS_CHANGED": "true"})
    assert outputs["run_migrations"] == "true"
    assert outputs["deploy_backend"] == "true"
    assert outputs["deploy_frontend"] == "true"
    assert outputs["any_change"] == "true"


@needs_bash
def test_no_migrations_no_backend_implication():
    outputs = _run_compute_plan({})
    assert outputs["deploy_backend"] == "false"
    assert outputs["run_migrations"] == "false"
    assert outputs["deploy_frontend"] == "false"
    assert outputs["any_change"] == "false"


def _reset_wf():
    return yaml.safe_load(RESET_WORKFLOW.read_text())


def _on(wf):
    # pyyaml parses a bare `on:` key as boolean True.
    return wf.get("on", wf.get(True))


def test_reset_workflow_dispatch_input():
    wf = _reset_wf()
    inputs = _on(wf)["workflow_dispatch"]["inputs"]
    assert inputs["pr_number"]["required"] is True


def test_reset_shares_preview_concurrency_group():
    reset = _reset_wf()
    preview = _wf()
    assert str(reset["concurrency"]["group"]).startswith("pr-preview-")
    assert str(preview["concurrency"]["group"]).startswith("pr-preview-")
    assert reset["concurrency"]["cancel-in-progress"] is False


def test_reset_guard_runs_before_checkout():
    # The reset runs PR code with secrets: the same-repo/open guard must reject
    # fork PRs BEFORE any PR code is checked out.
    steps = _reset_wf()["jobs"]["reset"]["steps"]
    guard_idx = next(
        i for i, s in enumerate(steps) if ".head.repo.full_name" in s.get("run", "")
    )
    checkout_idx = next(
        i for i, s in enumerate(steps) if "actions/checkout" in s.get("uses", "")
    )
    assert guard_idx < checkout_idx
    # Container jobs default to sh (dash); the guard's `set -euo pipefail`
    # needs bash (its absence failed the first live dispatch closed).
    assert steps[guard_idx].get("shell") == "bash"
    assert _reset_wf()["jobs"]["reset"]["permissions"]["pull-requests"] == "read"
    ref = steps[checkout_idx]["with"]["ref"]
    assert "refs/pull/" in ref and "/merge" in ref


def test_reset_reuses_preview_scripts():
    steps = _reset_wf()["jobs"]["reset"]["steps"]
    runs = "\n".join(s.get("run", "") for s in steps)
    for script in (
        "reset_preview_branch.sh",
        "prepare_preview_database.sh",
        "deploy_preview_backend.sh",
    ):
        assert script in runs, f"{script} not invoked by preview-reset.yml"
    prepare_step = next(
        s for s in steps if "prepare_preview_database.sh" in s.get("run", "")
    )
    assert prepare_step["env"]["DEPLOY_BACKEND"] == "true"
    assert prepare_step["env"]["RUN_MIGRATIONS"] == "true"
