"""Exercise CSV exports through the CLI against HTTP task-detail responses."""

from __future__ import annotations

import copy
import csv
import json
import threading

import httpx
import pytest
from typer.testing import CliRunner

from oddish.cli import app


@pytest.fixture
def detail():
    def item(item_id, tier, source):
        return {
            "id": item_id,
            "tier": tier,
            "source": source,
            "problem_type": "mismatch",
            "dimension": "verifier",
            "title": 'Requires "exact", output',
            "detail": "First line\nSecond line — café",
            "recommendation": "Match the stated requirement.",
            "file": "tests/test_output.py",
            "line_start": 12,
            "line_end": 18,
            "links_to": None,
            "exploited": False,
            "causal": True,
        }

    return {
        "task": {
            "id": "example-ab12",
            "name": "Example task",
            "status": "completed",
            "current_version": 2,
            "current_version_id": "version-2",
            "verdict_status": "completed",
            "verdict_error": None,
            "verdict": {
                "verdict": "reject",
                "reasoning": "A hidden requirement.",
                "recommendations": ["Fix the verifier."],
            },
            "trials": [
                {
                    "id": "example-ab12-1",
                    "task_version_id": "version-1",
                    "kind": "agent",
                    "analysis_status": "completed",
                    "analysis": {
                        "action_items": [item("old-run", "must_fix", "post_trial")]
                    },
                },
                {
                    "id": "example-ab12-2",
                    "task_version_id": "version-2",
                    "kind": "agent",
                    "analysis_status": "completed",
                    "analysis": {
                        "classification": "BAD_FAILURE",
                        "subtype": "hidden_requirement",
                        "action_items": [
                            item("post", "should_fix", "post_trial"),
                            item("optional", "optional", "post_trial"),
                        ],
                    },
                },
                {
                    "id": "example-ab12-3",
                    "task_version_id": "version-2",
                    "kind": "agent",
                    "analysis_status": None,
                    "analysis": None,
                },
                {
                    "id": "example-ab12-4",
                    "task_version_id": "version-2",
                    "kind": "qa",
                    "status": "failed",
                    "error_message": "Provider timeout",
                    "analysis": {
                        "action_items": [item("platform", "must_fix", "post_trial")]
                    },
                },
            ],
        },
        "versions": [
            {
                "id": "version-1",
                "version": 1,
                "pre_trial_status": "completed",
                "pre_trial_findings": [item("old-audit", "must_fix", "pre_trial")],
            },
            {
                "id": "version-2",
                "version": 2,
                "pre_trial_status": "completed",
                "pre_trial_findings": [item("audit", "must_fix", "pre_trial")],
            },
        ],
    }


@pytest.fixture
def invoke(monkeypatch, tmp_path):
    monkeypatch.setenv("ODDISH_API_KEY", "test-export-key")
    monkeypatch.setenv("ODDISH_API_URL", "https://default.example")
    real_client = httpx.Client
    calls = []

    def run(routes, args=None):
        def handle(request):
            calls.append(request)
            assert request.method == "GET"
            assert request.headers["Authorization"] == "Bearer test-export-key"
            result = routes[request.url.path]
            if isinstance(result, Exception):
                raise result
            status, payload = result
            return httpx.Response(status, json=payload)

        monkeypatch.setattr(
            "oddish.cli.qa.httpx.Client",
            lambda **kwargs: real_client(
                **kwargs, transport=httpx.MockTransport(handle)
            ),
        )
        output = tmp_path / "findings.csv"
        result = CliRunner().invoke(
            app, ["qa", "export", "--output", str(output), *(args or [])]
        )

        def read(path):
            with path.open(newline="", encoding="utf-8") as stream:
                return list(csv.DictReader(stream))

        return (
            result,
            read(output) if output.exists() else [],
            read(tmp_path / "findings-tasks.csv")
            if (tmp_path / "findings-tasks.csv").exists()
            else [],
            calls,
        )

    return run


def test_export_combines_sources_preserves_text_and_reports_status(invoke, detail):
    result, rows, summaries, calls = invoke(
        {"/tasks/example-ab12/detail": (200, detail)}, ["example-ab12"]
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert [row["id"] for row in rows] == ["audit", "post"]
    assert all(row["task_version"] == "2" for row in rows)
    assert rows[0]["trial_id"] == ""
    assert rows[1]["trial_id"] == "example-ab12-2"
    assert rows[1]["classification"] == "BAD_FAILURE"
    assert rows[1]["title"] == 'Requires "exact", output'
    assert rows[1]["detail"] == "First line\nSecond line — café"
    assert rows[1]["line_start"] == "12"
    assert rows[1]["assignee"] == ""
    (summary,) = summaries
    assert summary["must_fix_count"] == summary["should_fix_count"] == "1"
    assert summary["optional_count"] == "0"
    assert summary["fetch_error"] == ""
    assert json.loads(summary["qa_runs"])[0]["error"] == "Provider timeout"
    assert json.loads(summary["analysis_status_counts"]) == {
        "completed": 1,
        "not_analyzed": 1,
    }
    assert json.loads(summary["current_verdict_detail"]) == detail["task"]["verdict"]


def test_all_versions_and_repeated_tiers(invoke, detail):
    result, rows, summaries, _ = invoke(
        {"/tasks/example-ab12/detail": (200, detail)},
        [
            "example-ab12",
            "--all-versions",
            "--tier",
            "must_fix",
            "--tier",
            "optional",
        ],
    )
    assert result.exit_code == 0, result.output
    assert {row["id"] for row in rows} == {"old-audit", "old-run", "audit", "optional"}
    assert {row["task_version"] for row in rows} == {"1", "2"}
    assert all(row["current_version"] == "2" for row in rows)
    assert summaries[0]["must_fix_count"] == "3"
    assert summaries[0]["should_fix_count"] == "0"


def test_keeps_distinct_trial_observations_of_same_finding(invoke, detail):
    repeat = copy.deepcopy(detail["task"]["trials"][1])
    repeat["id"] = "example-ab12-5"
    detail["task"]["trials"].append(repeat)
    result, rows, _, _ = invoke(
        {"/tasks/example-ab12/detail": (200, detail)}, ["example-ab12"]
    )
    assert result.exit_code == 0
    assert [r["trial_id"] for r in rows if r["id"] == "post"] == [
        "example-ab12-2",
        "example-ab12-5",
    ]
    assert sum(r["id"] == "audit" for r in rows) == 1


def test_deduplicates_ids_and_keeps_summary_for_errors_and_zero_findings(
    invoke, detail, tmp_path
):
    ids = tmp_path / "ids.txt"
    ids.write_text(
        "\ufeffmissing\n example-ab12 \n\ntimeout\nexample-ab12\n", encoding="utf-8"
    )
    detail["versions"][1]["pre_trial_findings"] = []
    detail["versions"][1]["pre_trial_status"] = "failed"
    detail["versions"][1]["pre_trial_error"] = "Invalid audit output"
    detail["task"]["trials"] = []
    detail["task"]["verdict"] = None
    detail["task"]["verdict_status"] = "pending"
    result, rows, summaries, calls = invoke(
        {
            "/tasks/missing/detail": (404, {"detail": "Not found"}),
            "/tasks/example-ab12/detail": (200, detail),
            "/tasks/timeout/detail": httpx.ReadTimeout("Timed out"),
        },
        ["missing", "--ids-file", str(ids), "--api", "https://override.example/"],
    )
    assert result.exit_code == 1, result.output
    assert len(calls) == 3
    assert all(c.url.host == "override.example" for c in calls)
    assert rows == []
    assert [s["task_id"] for s in summaries] == ["missing", "example-ab12", "timeout"]
    assert "404" in summaries[0]["fetch_error"]
    assert summaries[0]["must_fix_count"] == ""  # unknown, not zero
    assert summaries[1]["must_fix_count"] == "0"
    assert summaries[1]["current_verdict_status"] == "pending"
    assert summaries[1]["current_verdict"] == ""
    assert json.loads(summaries[1]["audits"])[0]["error"] == "Invalid audit output"
    assert "Timed out" in summaries[2]["fetch_error"]


def test_missing_current_version_is_an_export_error(invoke, detail):
    detail["task"]["current_version_id"] = "absent"
    result, rows, summaries, _ = invoke(
        {"/tasks/example-ab12/detail": (200, detail)}, ["example-ab12"]
    )
    assert result.exit_code == 1
    assert rows == []
    assert "no versions matching" in summaries[0]["fetch_error"]


@pytest.mark.parametrize("status", [403, 429, 500])
def test_http_errors_do_not_discard_successful_tasks(invoke, detail, status):
    result, rows, summaries, _ = invoke(
        {
            "/tasks/broken/detail": (status, {"detail": "Cannot fetch task"}),
            "/tasks/example-ab12/detail": (200, detail),
        },
        ["broken", "example-ab12"],
    )
    assert result.exit_code == 1
    assert len(rows) == 2
    assert str(status) in summaries[0]["fetch_error"]
    assert summaries[1]["fetch_error"] == ""


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["a", "--tier", "urgent"],
        ["a", "--concurrency", "0"],
        ["a", "--concurrency", "17"],
    ],
)
def test_invalid_arguments_do_not_fetch_or_write(invoke, args):
    result, rows, summaries, calls = invoke({}, args)
    assert result.exit_code == 2, result.output
    assert not rows and not summaries and not calls


def test_input_file_cannot_be_overwritten(invoke, tmp_path):
    ids = tmp_path / "findings-tasks.csv"
    ids.write_text("example-ab12\n")
    result, _, _, calls = invoke({}, ["--ids-file", str(ids)])
    assert result.exit_code == 2
    assert "must not overwrite" in result.output
    assert ids.read_text() == "example-ab12\n"
    assert not calls


def test_concurrency_limit_and_stable_output_order(monkeypatch, tmp_path, detail):
    monkeypatch.setenv("ODDISH_API_KEY", "test-export-key")
    real_client = httpx.Client
    second_finished = threading.Event()
    active = peak = 0
    lock = threading.Lock()

    def handle(request):
        nonlocal active, peak
        task_id = request.url.path.split("/")[2]
        with lock:
            active += 1
            peak = max(active, peak)
        if task_id == "first":
            assert second_finished.wait(5)
        else:
            second_finished.set()
        payload = copy.deepcopy(detail)
        payload["task"]["id"] = task_id
        with lock:
            active -= 1
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        "oddish.cli.qa.httpx.Client",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    output = tmp_path / "batch.csv"
    result = CliRunner().invoke(
        app,
        [
            "qa",
            "export",
            "first",
            "second",
            "third",
            "--concurrency",
            "2",
            "--output",
            str(output),
        ],
    )
    assert result.exit_code == 0, result.output
    assert peak == 2
    with (tmp_path / "batch-tasks.csv").open(newline="") as stream:
        assert [r["task_id"] for r in csv.DictReader(stream)] == [
            "first",
            "second",
            "third",
        ]
