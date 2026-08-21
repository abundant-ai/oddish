import json

from oddish.analyze.trajectory_prompt import compact_trajectory_for_prompt


def test_prompt_compaction_drops_empty_steps_and_images_and_truncates_text():
    trajectory = {
        "schema_version": "ATIF-v1.7",
        "subagent_trajectories": [{"large": "not prompt input"}],
        "steps": [
            {"step_id": 1, "source": "user", "message": ""},
            {
                "step_id": 2,
                "source": "agent",
                "message": [
                    {"type": "image", "data": "large"},
                    {"type": "text", "text": "x" * 3_000},
                ],
            },
        ],
    }

    compacted = compact_trajectory_for_prompt(trajectory)

    assert [step["step_id"] for step in compacted["steps"]] == [2]
    assert "subagent_trajectories" not in compacted
    serialized = json.dumps(compacted)
    assert "[image omitted] (x1)" in serialized
    assert "truncated" in serialized
    assert "x" * 3_000 not in serialized


def test_prompt_compaction_keeps_the_beginning_and_end_under_the_limit():
    trajectory = {
        "steps": [
            {"step_id": index, "source": "agent", "message": "x" * 100}
            for index in range(1, 33)
        ]
    }

    compacted = compact_trajectory_for_prompt(trajectory, max_chars=1_000)
    real_step_ids = [
        step["step_id"]
        for step in compacted["steps"]
        if step.get("step_id") is not None
    ]

    assert len(json.dumps(compacted)) <= 1_000
    assert real_step_ids[0] == 1
    assert real_step_ids[-1] == 32
    assert any("steps omitted" in step["message"] for step in compacted["steps"])
