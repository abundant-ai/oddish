"""The CLI and GitHub output use the same task-result vocabulary."""

from io import StringIO

import pytest
from rich.console import Console

from oddish.cli.api import _build_experiment_table
from oddish.integrations.github.formatter import (
    TaskSummary,
    format_experiment_comment,
    format_task_comment,
)
from oddish.verdict import verdict_label


@pytest.mark.parametrize(
    "status, verdict, matches, expected",
    [
        (None, None, None, "No verdict"),
        ("success", None, True, "No verdict"),
        ("success", {"is_good": None}, True, "No verdict"),
        ("success", {"is_good": True}, True, "Accepted"),
        ("success", {"is_good": False}, True, "Rejected"),
        ("success", {"verdict": "accept", "is_good": False}, True, "Accepted"),
        ("success", {"verdict": "reject", "is_good": True}, True, "Rejected"),
        ("failed", {"is_good": True}, True, "No verdict"),
        ("success", {"is_good": True}, False, "No verdict"),
        ("pending", {"is_good": True}, True, "Verdict queued"),
        ("queued", None, True, "Verdict queued"),
        ("running", {"is_good": False}, True, "Verdict running"),
    ],
)
def test_verdict_labels_in_cli_and_github(status, verdict, matches, expected):
    assert verdict_label(status, verdict, version_matches=matches) == expected
    contextual = {"Verdict queued": "Queued", "Verdict running": "Running"}.get(
        expected, expected
    )
    assert (
        verdict_label(status, verdict, version_matches=matches, standalone=False)
        == contextual
    )
    output = StringIO()
    Console(file=output, width=150, color_system=None).print(
        _build_experiment_table(
            "exp-1",
            [
                {
                    "id": "task-1",
                    "name": "Task",
                    "status": "completed",
                    "total": 0,
                    "completed": 0,
                    "failed": 0,
                    "verdict_status": status,
                    "verdict": verdict,
                    "review_version_matches": matches,
                }
            ],
        )
    )
    assert contextual in output.getvalue()
    assert "Verdict queued" not in output.getvalue()
    assert "Verdict running" not in output.getvalue()
    task = TaskSummary(
        "task-1", "Task", "https://example.test/task", [], status, verdict, matches
    )
    for text in [
        format_task_comment(task, "exp", "https://example.test/exp"),
        format_experiment_comment([task], "exp", "https://example.test/exp"),
    ]:
        assert contextual in text
        assert "Verdict: **Verdict" not in text
        assert "Computing" not in text
        if expected == "No verdict":
            assert "Accepted" not in text
            assert "Rejected" not in text


@pytest.mark.parametrize("outcome", ["accept", "reject"])
def test_github_hides_mismatched_verdict_details(outcome):
    task = TaskSummary(
        "task-1",
        "Task",
        "https://example.test/task",
        [],
        "success",
        {
            "verdict": outcome,
            "is_good": outcome == "accept",
            "primary_issue": "Old issue",
            "recommendations": ["Old fix"],
        },
        review_version_matches=False,
    )
    for text in [
        format_task_comment(task, "exp", "https://example.test/exp"),
        format_experiment_comment([task], "exp", "https://example.test/exp"),
    ]:
        assert "No verdict" in text
        assert "Accepted" not in text
        assert "Rejected" not in text
        assert "Old issue" not in text
        assert "Old fix" not in text
