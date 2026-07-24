"""The classifier's trajectory-index affordance and the CLI selectors behind it."""

from __future__ import annotations

import io
import json

import pytest
from rich.console import Console

from oddish.analyze.classifier import (
    build_classify_prompt,
    build_trajectory_index_context,
)
from oddish.cli import trajectory
from oddish.cli.trajectory import _fmt_step_range, _parse_step_selector


def test_index_context_names_every_command_the_prompt_promises():
    context = build_trajectory_index_context("trial-abc")
    assert context is not None
    for command in ("trajectory summary", "trajectory components", "trajectory steps"):
        assert f"oddish {command} trial-abc" in context


def test_no_index_context_without_a_trial_to_query():
    # Rendering the CLI block without a trial id would advertise commands that
    # cannot be run.
    assert build_trajectory_index_context(None) is None
    assert build_trajectory_index_context("") is None


def test_prompt_carries_the_index_when_supplied():
    prompt = build_classify_prompt(
        result_str="fail",
        task_dir="/task",
        trial_dir="/trial",
        trial_agent_context="",
        trajectory_index_context=build_trajectory_index_context("trial-abc"),
    )
    assert "oddish trajectory components trial-abc" in prompt


def test_prompt_renders_without_an_index_and_keeps_the_full_read_instruction():
    """No index means no CLI -- the agent must still be told to read it all."""
    prompt = build_classify_prompt(
        result_str="fail",
        task_dir="/task",
        trial_dir="/trial",
        trial_agent_context="",
    )
    assert "oddish trajectory" not in prompt
    assert "{trajectory_index_context}" not in prompt
    assert "start to finish" in prompt


def test_registry_prompt_without_the_placeholder_still_renders():
    """Prompts stored before this placeholder existed must not start raising."""
    rendered = build_classify_prompt(
        template="result={result} task={task_dir}",
        result_str="pass",
        task_dir="/task",
        trial_dir="/trial",
        trial_agent_context="",
        trajectory_index_context=build_trajectory_index_context("t1"),
    )
    assert rendered == "result=pass task=/task"


@pytest.mark.parametrize(
    "step_ids,expected",
    [
        ([], "-"),
        ([4], "4"),
        ([1, 2, 3], "1-3"),
        ([1, 2, 3, 7, 9, 10, 11], "1-3,7,9-11"),
        # Unsorted input must still collapse, not fragment into singletons.
        ([11, 1, 10, 3, 2, 9, 7], "1-3,7,9-11"),
    ],
)
def test_step_ranges_collapse(step_ids, expected):
    assert _fmt_step_range(step_ids) == expected


@pytest.mark.parametrize(
    "selector,expected",
    [
        ("3", [3]),
        ("3,5", [3, 5]),
        ("8-12", [8, 9, 10, 11, 12]),
        ("3,5,8-10", [3, 5, 8, 9, 10]),
        # Reversed bounds are a typo, not an empty selection.
        ("10-8", [8, 9, 10]),
        # Overlaps must not duplicate steps into the agent's context twice.
        ("1-3,2-4", [1, 2, 3, 4]),
        (" 3 , 5 ", [3, 5]),
    ],
)
def test_step_selectors_parse(selector, expected):
    assert _parse_step_selector(selector) == expected


def test_filtered_components_keep_their_original_indices(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(trajectory, "console", Console(file=output, width=120))
    monkeypatch.setattr(trajectory, "_resolve", lambda _: "https://api.test")
    monkeypatch.setattr(
        trajectory,
        "_fetch_summary",
        lambda *_: {
            "components": [
                {"trajectory_component": "setup", "step_ids": [1]},
                {"trajectory_component": "debug", "step_ids": [2, 3]},
            ]
        },
    )

    trajectory.components("trial-1", label="debug")

    assert "1   debug" in output.getvalue()
    assert "0   debug" not in output.getvalue()


def test_filtered_components_json_carries_original_index(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(trajectory, "console", Console(file=output, width=120))
    monkeypatch.setattr(trajectory, "_resolve", lambda _: "https://api.test")
    monkeypatch.setattr(
        trajectory,
        "_fetch_summary",
        lambda *_: {
            "components": [
                {"trajectory_component": "setup", "step_ids": [1]},
                {"trajectory_component": "debug", "step_ids": [2, 3]},
            ]
        },
    )

    trajectory.components("trial-1", label="debug", json_output=True)

    emitted = json.loads(output.getvalue())
    assert [c["index"] for c in emitted] == [1]
    assert emitted[0]["trajectory_component"] == "debug"


def test_steps_print_bracketed_content_without_rich_markup(monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(trajectory, "console", Console(file=output, width=120))
    monkeypatch.setattr(trajectory, "_resolve", lambda _: "https://api.test")
    monkeypatch.setattr(
        trajectory,
        "_get",
        lambda *_: {
            "steps": [
                {
                    "step_id": 1,
                    "message": "[bold]literal[/bold] and [not-a-rich-tag]",
                }
            ]
        },
    )

    trajectory.steps("trial-1", step_selector="1")

    rendered = output.getvalue()
    assert "[bold]literal[/bold]" in rendered
    assert "[not-a-rich-tag]" in rendered
