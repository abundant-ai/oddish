"""Tests for the TrajectoryBlock prompt/parse block."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
    TrajectoryBlock,
    TrajectoryInput,
    TrajectoryOutput,
)
from oddish.blocks.block import BlockParseError


_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "oddish" / "src" / "oddish" / "analyze" / "prompts" / "trajectory_summary.txt"
).read_text()


def _traj(step_ids):
    return {"steps": [{"step_id": s} for s in step_ids]}


def _input(**over):
    base = dict(
        task_name="solve_x", instruction="Do the thing.", final_reward=1.0,
        model_used="claude-x", verifier_output="PASS\n", trajectory=_traj([1, 2, 3]),
    )
    base.update(over)
    return TrajectoryInput(**base)


def _block(trajectory_input: TrajectoryInput) -> TrajectoryBlock:
    return TrajectoryBlock(trajectory_input, instructions_template=_TEMPLATE)


def test_output_schema_is_the_closed_provider_and_parser_contract():
    assert TrajectoryBlock.output_schema is TrajectoryOutput
    schema = TrajectoryBlock.output_schema.model_json_schema()

    def object_schemas(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                yield node
            for value in node.values():
                yield from object_schemas(value)
        elif isinstance(node, list):
            for value in node:
                yield from object_schemas(value)

    objects = list(object_schemas(schema))
    assert objects
    assert set(schema["required"]) == {"summary", "highlights", "components"}
    assert all(obj.get("additionalProperties") is False for obj in objects)
    assert all(set(obj["required"]) == set(obj["properties"]) for obj in objects)
    # Non-empty text is enforced when Pydantic parses the response. Keeping
    # unsupported string constraints off the wire makes the same raw schema
    # valid for the Anthropic path as well as the OpenAI SDK path.
    assert "minLength" not in json.dumps(schema)


def test_build_prompt_has_task_outcome_and_taxonomy():
    prompt = _block(_input()).build_prompt()
    assert "<task>" in prompt and "Name: solve_x" in prompt
    assert "Instruction: Do the thing." in prompt
    assert "Final reward: 1.0" in prompt and "Verifier output: PASS" in prompt
    assert "Model: claude-x" in prompt
    assert "segment the run into components" in prompt
    assert "reading_files" in prompt and "debugging" in prompt  # taxonomy listed
    assert "<trajectory>" in prompt


def test_build_prompt_marks_missing_unavailable():
    prompt = _block(_input(
        instruction=None, final_reward=None, model_used=None, verifier_output=None,
    )).build_prompt()
    assert "Instruction: [unavailable]" in prompt
    assert "Final reward: [unavailable]" in prompt
    assert "Model: [unavailable]" in prompt


def test_parse_keeps_valid_components_and_drops_unknown_step_ids():
    raw = json.dumps({
        "summary": "did stuff",
        "highlights": [{"step_id": 1, "title": "t", "why": "w"},
                       {"step_id": 99, "title": "x", "why": "y"}],
        "components": [
            {"step_ids": [1, 2], "trajectory_component": "reading_files", "summary": "read"},
            {"step_ids": [99], "trajectory_component": "debugging", "summary": "nope"},
        ],
    })
    out = _block(_input()).parse(raw)
    assert out.summary == "did stuff"
    assert [h.step_id for h in out.highlights] == [1]
    assert len(out.components) == 1
    assert out.components[0].trajectory_component.value == "reading_files"
    assert out.components[0].step_ids == [1, 2]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            {
                "summary": "s",
                "highlights": [],
                "components": [
                    {
                        "step_ids": [1],
                        "trajectory_component": "not_a_real_label",
                        "summary": "x",
                    }
                ],
            },
            id="unknown-taxonomy",
        ),
        pytest.param(
            {
                "summary": "ok",
                "highlights": ["not-an-object"],
                "components": [],
            },
            id="wrong-nested-type",
        ),
        pytest.param(
            {"summary": 123, "highlights": [], "components": []},
            id="wrong-field-type",
        ),
        pytest.param(
            {"summary": "", "highlights": [], "components": []},
            id="empty-summary",
        ),
        pytest.param(
            {"summary": "ok", "highlights": [], "components": [], "extra": True},
            id="extra-field",
        ),
    ],
)
def test_parse_rejects_output_outside_provider_schema(payload):
    with pytest.raises(BlockParseError, match="structured output mismatch"):
        _block(_input()).parse(json.dumps(payload))


def test_parse_raises_on_malformed_json():
    with pytest.raises(BlockParseError):
        _block(_input()).parse("not json")


def test_to_summary_is_schema_v5_with_component_metadata():
    raw = json.dumps({
        "summary": "s", "highlights": [],
        "components": [{"step_ids": [1, 2], "trajectory_component": "implementing", "summary": "y"}],
    })
    trajectory = {"steps": [
        {"step_id": 1, "timestamp": "2026-01-01T00:00:00Z", "tool_calls": [{"id": "a"}]},
        {"step_id": 2, "timestamp": "2026-01-01T00:00:02.500Z", "tool_calls": [{"id": "b"}, {"id": "c"}]},
        {"step_id": 3, "timestamp": "2026-01-01T00:00:04Z"},
    ]}
    d = _block(_input(trajectory=trajectory)).to_summary(raw, model="claude-x")
    assert d["schema_version"] == "5"
    assert d["model"] == "claude-x"
    assert "generated_at" in d
    assert "phases" not in d
    assert d["components"][0]["trajectory_component"] == "implementing"
    assert len(d["components"][0]["step_ids"]) == 2
    assert "step_count" not in d["components"][0]
    assert d["components"][0]["tool_count"] == 3
    assert d["components"][0]["duration_ms"] == 2500


def test_instructions_template_renders_taxonomy():
    from api.services.blocks.analyzer.trajectory import trajectory_prompts as tp

    REPO_ROOT = Path(__file__).resolve().parents[2]
    template = (
        REPO_ROOT / "oddish" / "src" / "oddish" / "analyze" / "prompts" / "trajectory_summary.txt"
    ).read_text()
    rendered = tp.instructions_section(template, ["reading_files"], ["debugging"])
    assert "{{taxonomy}}" not in rendered
    assert rendered.startswith("Write a summary of 2-3 sentences")
    assert rendered.rstrip().endswith("Order the highlights by `step_id`, lowest first.")
    # Every label reaches the model with its definition, under its group.
    assert "THINKING / EXPLORING" in rendered
    assert "IMPLEMENTING / TESTING" in rendered
    assert "- `reading_files`: opens, lists, or searches files" in rendered
    assert "- `debugging`: investigates a failure that already occurred" in rendered


def test_every_taxonomy_label_has_a_description():
    """A label the enum offers but the prompt never defines must not ship."""
    from api.services.blocks.analyzer.trajectory import trajectory_prompts as tp
    from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
        TrajectoryBlockTaxonomy,
    )

    missing = [
        m.value
        for m in TrajectoryBlockTaxonomy
        if m.value not in tp.TAXONOMY_DESCRIPTIONS
    ]
    assert missing == []


def test_render_taxonomy_rejects_an_undescribed_label():
    from api.services.blocks.analyzer.trajectory import trajectory_prompts as tp

    with pytest.raises(ValueError, match="without a description"):
        tp.render_taxonomy(["reading_files"], ["a_label_nobody_defined"])
