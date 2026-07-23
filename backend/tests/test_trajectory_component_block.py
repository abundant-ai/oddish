"""Tests for the TrajectoryBlock prompt/parse block."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from api.services.blocks.analyzer.trajectory.trajectory_component_block import (
    TrajectoryBlock,
    TrajectoryInput,
)
from oddish.blocks.block import BlockParseError


_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "oddish" / "src" / "oddish" / "analyze" / "prompts" / "trajectory_summary.v1.txt"
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


def test_build_prompt_has_task_outcome_and_taxonomy():
    prompt = _block(_input()).build_prompt()
    assert "<task>" in prompt and "Name: solve_x" in prompt
    assert "Instruction: Do the thing." in prompt
    assert "Final reward: 1.0" in prompt and "Verifier output: PASS" in prompt
    assert "Model: claude-x" in prompt
    assert '"components"' in prompt
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
    assert [h["step_id"] for h in out.highlights] == [1]
    assert len(out.components) == 1
    assert out.components[0]["trajectory_component"] == "reading_files"
    assert out.components[0]["step_ids"] == [1, 2]


def test_parse_drops_component_with_bad_taxonomy(caplog):
    raw = json.dumps({
        "summary": "s", "highlights": [],
        "components": [
            {"step_ids": [1], "trajectory_component": "not_a_real_label", "summary": "x"},
            {"step_ids": [2], "trajectory_component": "implementing", "summary": "y"},
        ],
    })
    with caplog.at_level(logging.ERROR):
        out = _block(_input()).parse(raw)
    assert [c["trajectory_component"] for c in out.components] == ["implementing"]


def test_parse_drops_non_dict_list_elements_without_failing():
    # A single stray non-dict element in highlights/components must degrade
    # gracefully (dropped), not abort the whole summary with a 502.
    raw = json.dumps({
        "summary": "ok",
        "highlights": [{"step_id": 1, "title": "t", "why": "w"}, "oops"],
        "components": [
            "junk",
            {"step_ids": [1], "trajectory_component": "implementing", "summary": "y"},
        ],
    })
    out = _block(_input()).parse(raw)
    assert [h["step_id"] for h in out.highlights] == [1]
    assert [c["trajectory_component"] for c in out.components] == ["implementing"]


def test_parse_coerces_non_string_summary():
    raw = json.dumps({"summary": 123, "highlights": [], "components": []})
    out = _block(_input()).parse(raw)
    assert out.summary == "123"


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


def test_instructions_seed_template_matches_legacy_text():
    from api.services.blocks.analyzer.trajectory import trajectory_prompts as tp

    REPO_ROOT = Path(__file__).resolve().parents[2]
    template = (
        REPO_ROOT / "oddish" / "src" / "oddish" / "analyze" / "prompts" / "trajectory_summary.v1.txt"
    ).read_text()
    labels = ["reading_files", "debugging"]
    rendered = tp.instructions_section(template, labels)
    assert "reading_files, debugging" in rendered
    assert "{{taxonomy}}" not in rendered
    assert rendered.startswith("Produce a 2-3 sentence summary")
    assert rendered.rstrip().endswith("Highlights must be ordered by step_id ascending.")
