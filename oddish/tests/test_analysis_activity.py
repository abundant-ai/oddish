"""Pure tests for deterministic analysis-trial activity summaries."""

import pytest

from oddish.analyze.analysis_activity import (
    ANALYSIS_ACTIVITY_VERSION,
    build_analysis_activity_summary,
    classify_step,
    trial_mention_steps,
)

PATH_KEYS = (
    "file_path",
    "filePath",
    "path",
    "filename",
    "file",
    "target_file",
    "absolute_path",
)
COMMAND_KEYS = ("command", "cmd", "script", "shell_command")


def _call(name: str, **arguments: object) -> dict:
    return {"name": name, "arguments": arguments}


@pytest.mark.parametrize("key", PATH_KEYS)
def test_every_path_argument_spelling_identifies_a_result_write(key: str):
    assert (
        classify_step({"tool_calls": [_call("Write", **{key: "/logs/qa_result.json"})]})
        == "writing_result"
    )


@pytest.mark.parametrize("key", COMMAND_KEYS)
def test_every_command_argument_spelling_identifies_a_data_fetch(key: str):
    assert (
        classify_step(
            {"tool_calls": [_call("Bash", **{key: "oddish-query task demo"})]}
        )
        == "fetching_trial_data"
    )


def test_mixed_tools_use_the_ordered_rule_precedence():
    calls = [
        _call("Read", path="/tmp/a"),
        _call("Agent", prompt="inspect"),
        _call("Bash", cmd="oddish-query task t"),
    ]
    assert classify_step({"tool_calls": calls}) == "fetching_trial_data"
    calls.append(_call("Write", target_file="/logs/out"))
    assert classify_step({"tool_calls": calls}) == "writing_result"


def test_summary_segments_steps_and_names_only_observed_actions():
    summary = build_analysis_activity_summary(
        kind="qa",
        task_name="apache-kafka",
        trial_count=2,
        status="success",
        artifact_name="qa_result.json",
        trajectory={
            "steps": [
                {"step_id": 1, "tool_calls": []},
                {
                    "step_id": 2,
                    "tool_calls": [_call("Bash", command="oddish-query task demo")],
                },
                {"step_id": 3, "tool_calls": [_call("Read", file_path="/tmp/a")]},
                {
                    "step_id": 4,
                    "tool_calls": [
                        _call("Read", file_path="/tmp/b"),
                        _call("Bash", command="oddish-query logs trial-2"),
                    ],
                },
                {"step_id": 5, "tool_calls": [_call("Bash", command="jq . /tmp/a")]},
                {
                    "step_id": 6,
                    "tool_calls": [
                        _call("Write", absolute_path="/logs/qa_result.json")
                    ],
                },
            ]
        },
    )
    assert summary is not None
    assert summary["generator"] == "analysis-activity"
    assert ANALYSIS_ACTIVITY_VERSION == "analysis-activity:v1"
    assert "2 agent trials" in summary["summary"]
    assert "fetched task and trial data" in summary["summary"]
    assert "wrote qa_result.json" in summary["summary"]
    labels = [
        (component["trajectory_component"], component["step_ids"])
        for component in summary["components"]
    ]
    assert labels == [
        ("reasoning", [1]),
        ("fetching_trial_data", [2]),
        ("reading_files", [3]),
        ("fetching_trial_data", [4]),
        ("inspecting_data", [5]),
        ("writing_result", [6]),
    ]
    assert [highlight["step_id"] for highlight in summary["highlights"]] == [2, 6]


def test_failed_partial_run_does_not_claim_an_unobserved_fetch_or_write():
    summary = build_analysis_activity_summary(
        kind="qa",
        task_name="partial",
        trial_count=1,
        status="failed",
        artifact_name="qa_result.json",
        trajectory={
            "steps": [
                {"step_id": 1, "tool_calls": []},
                {"step_id": 2, "tool_calls": [_call("Read", path="/tmp/input")]},
            ]
        },
    )
    assert summary is not None
    assert "reasoned without tools" in summary["summary"]
    assert "read or searched files" in summary["summary"]
    assert "fetched" not in summary["summary"]
    assert "wrote" not in summary["summary"]
    assert "qa_result.json" not in summary["summary"]
    assert summary["summary"].endswith("The run finished FAILED.")
    assert summary["highlights"] == []


def test_empty_trajectory_has_no_summary():
    assert (
        build_analysis_activity_summary(
            kind="qa",
            task_name="t",
            trial_count=0,
            status="failed",
            artifact_name="qa_result.json",
            trajectory={"steps": []},
        )
        is None
    )


def test_graded_step_anchors_come_from_string_arguments_only():
    a, b = "a" * 32, "b" * 32
    trajectory = {
        "steps": [
            {"step_id": 1, "tool_calls": [_call("Read", target_file=f"/tmp/{a}")]},
            {"step_id": 2, "tool_calls": [_call("Bash", command=f"logs {b}")]},
            {"step_id": 3, "tool_calls": [_call("Read", count=3)]},
        ]
    }
    assert trial_mention_steps(trajectory, [a, b, "c" * 32]) == {a: [1], b: [2]}
    assert trial_mention_steps(trajectory, ["ab"]) == {}
    assert trial_mention_steps(None, [a]) == {}


def test_graded_step_anchors_do_not_match_a_longer_trial_id():
    short = "task-id-1234"
    longer = "task-id-12345"
    trajectory = {
        "steps": [
            {
                "step_id": 1,
                "tool_calls": [_call("Read", path=f"/tmp/{longer}.json")],
            }
        ]
    }
    assert trial_mention_steps(trajectory, [short, longer]) == {longer: [1]}
