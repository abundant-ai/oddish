import json

from api.services.blocks.analyzer.cohort.cohort_workspace import (
    build_workspace,
    component_filename,
    step_text,
)


def _traj(steps):
    return {"agent": {"name": "claude-code"}, "steps": steps}


def _cohort_trial(trial_id="t1", **over):
    base = {
        "trial_id": trial_id,
        "model": "claude-opus-4-8",
        "components": [
            {
                "trajectory_component": "implementing",
                "step_ids": [1, 2],
                "summary": "Wrote the adapter.",
            }
        ],
    }
    base.update(over)
    return base


def test_step_text_flattens_message_reasoning_tools_and_observation():
    step = {
        "step_id": 1,
        "message": "I will read the config.",
        "reasoning_content": "The failure smells like a missing bean.",
        "tool_calls": [{"function_name": "Bash", "arguments": {"command": "mvn test"}}],
        "observation": {"results": [{"content": "BUILD FAILURE"}]},
    }
    text = step_text(step)
    for fragment in (
        "I will read the config.",
        "missing bean",
        "Bash",
        "mvn test",
        "BUILD FAILURE",
    ):
        assert fragment in text


def test_step_text_handles_block_style_message_content():
    step = {
        "step_id": 1,
        "message": [
            {"type": "text", "text": "Dispatching a subagent."},
            {"type": "image", "source": {}},
        ],
    }
    assert "Dispatching a subagent." in step_text(step)


def test_step_text_tolerates_an_empty_step():
    assert step_text({"step_id": 1}) == ""
    assert step_text(None) == ""


def test_build_workspace_writes_one_file_per_component(tmp_path):
    trajectories = {
        "t1": _traj(
            [
                {"step_id": 1, "message": "first"},
                {"step_id": 2, "message": "second"},
                {"step_id": 9, "message": "elsewhere"},
            ]
        )
    }
    build_workspace(
        tmp_path,
        task_name="cargotracker",
        successful=[_cohort_trial()],
        failing=[],
        trajectories=trajectories,
    )
    files = sorted((tmp_path / "trials" / "t1").glob("*.json"))
    assert len(files) == 1
    steps = json.loads(files[0].read_text())["steps"]
    # Only the component's own span, and the full raw step, not a summary.
    assert [s["step_id"] for s in steps] == [1, 2]
    assert steps[0]["message"] == "first"


def test_build_workspace_map_names_every_component_and_its_file(tmp_path):
    build_workspace(
        tmp_path,
        task_name="cargotracker",
        successful=[_cohort_trial()],
        failing=[_cohort_trial("t2")],
        trajectories={
            "t1": _traj([{"step_id": 1}, {"step_id": 2}]),
            "t2": _traj([{"step_id": 1}, {"step_id": 2}]),
        },
    )
    text = (tmp_path / "MAP.md").read_text()
    assert "cargotracker" in text
    assert "SUCCESSFUL" in text and "FAILING" in text
    assert "t1" in text and "t2" in text
    assert "implementing" in text
    assert "[1-2]" in text
    # The map must name the readable file, or the agent cannot find it.
    assert component_filename(0, "implementing", 1, 2) in text


def test_build_workspace_map_shortens_the_model_id(tmp_path):
    # The agent reads MAP.md and the prompt together; the prompt shortens, so
    # an unshortened id here is a second spelling of the same model.
    build_workspace(
        tmp_path,
        task_name="cargotracker",
        successful=[_cohort_trial(model="global.anthropic.claude-opus-4-8")],
        failing=[],
        trajectories={"t1": _traj([{"step_id": 1}, {"step_id": 2}])},
    )
    text = (tmp_path / "MAP.md").read_text()
    assert "model: claude-opus-4-8" in text
    assert "global.anthropic" not in text


def test_build_workspace_returns_a_step_index_for_validation(tmp_path):
    index = build_workspace(
        tmp_path,
        task_name="x",
        successful=[_cohort_trial()],
        failing=[],
        trajectories={"t1": _traj([{"step_id": 1, "message": "hello world"}])},
    )
    assert "hello world" in index[("t1", 1)]


def test_build_workspace_indexes_every_step_not_just_covered_ones(tmp_path):
    # A citation may name a step no component claimed -- the summariser drops
    # inert steps and long runs are only partly covered. Validation must still
    # be able to check it, so the index spans the whole trajectory.
    index = build_workspace(
        tmp_path,
        task_name="x",
        successful=[_cohort_trial()],
        failing=[],
        trajectories={
            "t1": _traj([{"step_id": 1}, {"step_id": 2}, {"step_id": 77, "message": "late"}])
        },
    )
    assert ("t1", 77) in index


def test_build_workspace_skips_a_trial_with_no_trajectory(tmp_path):
    # S3 read can fail or the prefix can be empty; the comparison still runs
    # on the summaries rather than dying.
    index = build_workspace(
        tmp_path,
        task_name="x",
        successful=[_cohort_trial()],
        failing=[],
        trajectories={},
    )
    assert index == {}
    assert not (tmp_path / "trials" / "t1").exists()
    assert "MAP.md" in [p.name for p in tmp_path.iterdir()]


def test_component_filename_is_stable_and_path_safe():
    name = component_filename(3, "testing_public", 10, 42)
    assert name.endswith(".json")
    assert "/" not in name and ".." not in name
    assert "testing_public" in name and "10" in name and "42" in name
