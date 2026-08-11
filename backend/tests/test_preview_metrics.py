"""Tests for .github/scripts/preview/record_preview_metrics.py.

The module is loaded by path (it is a workflow script, not a package), and its
pure functions are exercised directly: generation identity, redaction, timing
assembly from Jobs API fixtures, and Markdown rendering.
"""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".github/scripts/preview/record_preview_metrics.py"
)
spec = importlib.util.spec_from_file_location("record_preview_metrics", SCRIPT)
metrics = importlib.util.module_from_spec(spec)
spec.loader.exec_module(metrics)

HEAD_SHA = "608d0e7f1abc2345deadbeef678901234567"


def test_generation_format_is_pinned():
    assert metrics.preview_generation("1138", HEAD_SHA) == "pr1138-608d0e7f1abc"


def test_generation_changes_with_head_sha():
    other = "f" * 40
    assert metrics.preview_generation("1138", other) != metrics.preview_generation(
        "1138", HEAD_SHA
    )


@pytest.mark.parametrize(
    "raw, expected",
    [
        # Credentials and query strings never survive into the artifact.
        (
            "postgres://seed:hunter2@db.example.com:5432/postgres?sslmode=require",
            "postgres://db.example.com:5432/postgres",
        ),
        ("https://user:token@example.modal.run/path#frag", "https://example.modal.run/path"),
        ("https://pr-1.oddish.app", "https://pr-1.oddish.app"),
        # Non-URL values pass through untouched.
        ("oddish-pr-1138", "oddish-pr-1138"),
        ("br_123abc", "br_123abc"),
    ],
)
def test_sanitize_value(raw, expected):
    assert metrics.sanitize_value(raw) == expected


def test_identifiers_are_allowlisted_and_sanitized():
    env = {
        "SUPABASE_BRANCH_REF": "abcd1234",
        "MODAL_API_URL": "https://u:pw@abundant-ai-preview--x.modal.run",
        "SUPABASE_ACCESS_TOKEN": "sbp_secret",  # decoy: not in the allowlist
        "VERCEL_TOKEN": "vercel_secret",  # decoy
    }
    identifiers = metrics.collect_identifiers(env)
    assert identifiers == {
        "supabase_branch_ref": "abcd1234",
        "modal_api_url": "https://abundant-ai-preview--x.modal.run",
    }


def test_parse_seed_stats_tolerates_garbage():
    assert metrics.parse_seed_stats("") == {}
    assert metrics.parse_seed_stats("not json") == {}
    assert metrics.parse_seed_stats('["a list"]') == {}


def test_parse_seed_stats_round_trips_the_run_report():
    raw = json.dumps(
        {
            "batches_attempted": 12,
            "batches_split": 2,
            "rows_skipped": 5,
            "skips": {"UniqueViolationError (SQLSTATE 23505)": ["tasks.a"]},
            "tables": {"tasks": [1000, 1005]},
            "seed_seconds": 3.1,
        }
    )
    report = metrics.parse_seed_stats(raw)
    assert report["rows_skipped"] == 5
    assert report["tables"] == {"tasks": [1000, 1005]}
    assert report["skips"]["UniqueViolationError (SQLSTATE 23505)"] == ["tasks.a"]
    assert metrics.parse_timings('{"vercel_alias_seconds": "nope"}') == {}
    assert metrics.parse_timings(
        '{"vercel_alias_seconds": 4, "unrelated": 1, "vercel_build_wait_seconds": 62.5}'
    ) == {"vercel_alias_seconds": 4.0, "vercel_build_wait_seconds": 62.5}


def _step(name, start, end, conclusion="success"):
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": f"2026-08-10T{start}Z",
        "completed_at": f"2026-08-10T{end}Z" if end else None,
    }


def _job(name, start, end, steps=(), conclusion="success"):
    return {
        "name": name,
        "conclusion": conclusion,
        "started_at": f"2026-08-10T{start}Z",
        "completed_at": f"2026-08-10T{end}Z" if end else None,
        "steps": list(steps),
    }


RUN = {"created_at": "2026-08-10T10:00:00Z"}
NOW = datetime(2026, 8, 10, 10, 10, 0, tzinfo=timezone.utc)

FULL_JOBS = [
    _job("Detect preview changes", "10:00:03", "10:00:15"),
    _job(
        "Prepare preview database",
        "10:00:20",
        "10:05:20",
        steps=[
            _step("Initialize containers", "10:00:20", "10:01:00"),
            _step("Checkout repository", "10:01:00", "10:01:02"),
            _step("Sync backend dependencies", "10:01:02", "10:01:05"),
            _step("Prepare preview database", "10:01:05", "10:05:15"),
        ],
    ),
    _job(
        "Deploy preview backend",
        "10:05:25",
        "10:07:25",
        steps=[
            _step("Initialize containers", "10:05:25", "10:06:13"),
            _step("Deploy preview backend", "10:06:13", "10:07:23"),
        ],
    ),
    _job(
        "Update Vercel preview",
        "10:07:30",
        "10:09:30",
        steps=[
            _step("Initialize containers", "10:07:30", "10:08:28"),
            _step("Update Vercel preview", "10:08:28", "10:09:28"),
        ],
    ),
    _job(
        "Require working preview",
        "10:09:35",
        None,
        steps=[_step("Verify preview deployment", "10:09:40", "10:10:00")],
        conclusion=None,
    ),
]


def _env(**overrides):
    env = {
        "GITHUB_REPOSITORY": "abundant-ai/oddish",
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "1",
        "PR_NUMBER": "1138",
        "HEAD_SHA": HEAD_SHA,
        "EVENT_ACTION": "synchronize",
        "DEPLOY_BACKEND": "true",
        "RUN_MIGRATIONS": "false",
        "DEPLOY_FRONTEND": "true",
    }
    env.update(overrides)
    return env


def test_build_report_full_run():
    report = metrics.build_report(
        RUN,
        FULL_JOBS,
        _env(
            VERCEL_TIMINGS='{"vercel_configure_seconds": 20, '
            '"vercel_build_wait_seconds": 70, "vercel_alias_seconds": 10}',
            SUPABASE_BRANCH_ID="br_1",
            GITHUB_DEPLOYMENT_ID="987",
        ),
        now=NOW,
    )
    assert report["schema_version"] == 1
    assert report["generation"] == "pr1138-608d0e7f1abc"
    assert report["plan"] == {
        "deploy_backend": "true",
        "run_migrations": "false",
        "deploy_frontend": "true",
    }
    assert report["identifiers"] == {
        "supabase_branch_id": "br_1",
        "github_deployment_id": "987",
    }
    phases = report["phases"]
    assert phases["queue_delay_seconds"] == 3.0
    assert phases["container_init_seconds"] == 40 + 48 + 58
    assert phases["checkout_seconds"] == 2.0
    assert phases["dependency_sync_seconds"] == 3.0
    assert phases["database_seconds"] == 250.0
    assert phases["modal_deploy_seconds"] == 70.0
    assert phases["vercel_seconds"] == 60.0
    assert phases["vercel_configure_seconds"] == 20.0
    assert phases["vercel_build_wait_seconds"] == 70.0
    assert phases["vercel_alias_seconds"] == 10.0
    assert phases["gate_verify_seconds"] == 20.0
    assert phases["total_wall_seconds"] == 600.0


def test_build_report_wait_attribution_uses_needs():
    report = metrics.build_report(RUN, FULL_JOBS, _env(), now=NOW)
    jobs = report["jobs"]
    assert jobs["detect-changes"]["wait_seconds"] == 3.0
    assert jobs["prepare-preview-database"]["wait_seconds"] == 5.0
    assert jobs["deploy-preview-backend"]["wait_seconds"] == 5.0
    assert jobs["update-vercel-preview"]["wait_seconds"] == 5.0


def test_build_report_skipped_jobs_are_skipped_not_zero():
    jobs = [
        FULL_JOBS[0],
        _job("Prepare preview database", "10:00:15", "10:00:15", conclusion="skipped"),
        _job("Deploy preview backend", "10:00:15", "10:00:15", conclusion="skipped"),
        FULL_JOBS[3],
    ]
    report = metrics.build_report(RUN, jobs, _env(), now=NOW)
    phases = report["phases"]
    assert phases["database_seconds"] == "skipped"
    assert phases["modal_deploy_seconds"] == "skipped"
    assert phases["vercel_seconds"] == 60.0
    assert report["jobs"]["prepare-preview-database"] == {
        "name": "Prepare preview database",
        "conclusion": "skipped",
    }


def test_build_report_tolerates_missing_gate_and_phases():
    report = metrics.build_report(RUN, [FULL_JOBS[0]], _env(), now=NOW)
    assert report["phases"]["queue_delay_seconds"] == 3.0
    assert "database_seconds" not in report["phases"]
    assert "gate_verify_seconds" not in report["phases"]


def test_build_report_folds_seed_stats_into_the_artifact():
    env = _env(SEED_STATS='{"batches_attempted": 4, "rows_skipped": 1}')
    report = metrics.build_report(RUN, [FULL_JOBS[0]], env, now=NOW)
    assert report["seed"]["rows_skipped"] == 1


def test_build_report_seed_section_is_empty_without_the_output():
    report = metrics.build_report(RUN, [FULL_JOBS[0]], _env(), now=NOW)
    assert report["seed"] == {}


def test_render_markdown_tolerates_missing_fields():
    report = metrics.build_report(RUN, [FULL_JOBS[0]], _env(), now=NOW)
    text = metrics.render_markdown(report)
    assert "pr1138-608d0e7f1abc" in text
    assert "| Queue delay | 3.0s |" in text
    assert "Database prepare" not in text


def test_render_markdown_formats_durations_and_skipped():
    report = metrics.build_report(RUN, FULL_JOBS, _env(), now=NOW)
    text = metrics.render_markdown(report)
    assert "| Database prepare | 4m10s |" in text
    assert "| Total wall time | 10m00s |" in text
    skipped = metrics.build_report(
        RUN,
        [_job("Prepare preview database", "10:00:15", "10:00:15", conclusion="skipped")],
        _env(),
        now=NOW,
    )
    assert "| Database prepare | skipped |" in metrics.render_markdown(skipped)


def test_render_identity_block():
    text = metrics.render_identity(_env())
    assert "- PR: #1138" in text
    assert f"- Head SHA: `{HEAD_SHA}`" in text
    assert "- Generation: `pr1138-608d0e7f1abc`" in text
    assert "- Event: `synchronize`" in text
    assert "deploy_backend=`true`" in text
    # Missing plan flags degrade to `?` rather than dropping the line.
    assert "deploy_frontend=`?`" in metrics.render_identity(
        {"PR_NUMBER": "1", "HEAD_SHA": HEAD_SHA}
    )


def test_record_writes_artifact_and_summary(tmp_path, monkeypatch, capsys):
    payloads = {
        f"/repos/abundant-ai/oddish/actions/runs/123": RUN,
        f"/repos/abundant-ai/oddish/actions/runs/123/jobs?per_page=100": {
            "jobs": FULL_JOBS
        },
    }
    monkeypatch.setattr(metrics, "gh_api", lambda path: payloads[path])
    summary = tmp_path / "summary.md"
    summary.write_text("")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    for key, value in _env().items():
        monkeypatch.setenv(key, value)
    out = tmp_path / "preview-timing.json"
    metrics.cmd_record(str(out))
    artifact = json.loads(out.read_text())
    assert artifact["schema_version"] == 1
    assert artifact["phases"]["queue_delay_seconds"] == 3.0
    assert "## Preview timing" in summary.read_text()
    assert str(out) in capsys.readouterr().out
