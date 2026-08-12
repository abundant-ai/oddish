import json

import pytest

from oddish.blocks.block import BlockParseError

from api.services.blocks.analyzer.cohort.cohort_comparison_block import (
    SCHEMA_VERSION,
    CohortComparisonBlock,
    CohortInput,
)
from api.services.blocks.analyzer.cohort import cohort_prompts as cp

TRIAL = {
    "trial_id": "t1",
    "components": [
        {
            "trajectory_component": "testing_public",
            "step_ids": [34, 35],
            "summary": "Ran mvn test for a baseline.",
        }
    ],
    "covered_steps": 2,
    "span": 35,
    "coverage": 1.0,
}


def _block(**overrides):
    return CohortComparisonBlock(
        CohortInput(
            task_name="demo-task",
            successful=overrides.get("successful", [TRIAL]),
            failing=overrides.get("failing", [{**TRIAL, "trial_id": "t2"}]),
        ),
        instructions_template=cp.load_cohort_prompt_template(),
    )


def _raw(evidence, schema_version=99):
    return json.dumps(
        {
            "schema_version": schema_version,
            "cohort_success": ["t1"],
            "cohort_failure": ["t2"],
            "summary": "Agents took a test baseline before editing.",
            "categories": [
                {
                    "category": "testing_verification",
                    "label": None,
                    "successful": [
                        {
                            "behavior_description": "ran the verifier first",
                            "evidence": evidence,
                        }
                    ],
                    "failing": [],
                }
            ],
        }
    )


GOOD_EVIDENCE = {
    "trial_id": "t1",
    "trajectory_component": "testing_public",
    "step_ids": [34, 35],
    "quote": "Ran mvn test for a baseline.",
}


def test_prompt_contains_both_cohorts_and_definitions():
    prompt = _block().build_prompt()
    assert "SUCCESSFUL" in prompt and "FAILING" in prompt
    assert "t1" in prompt and "t2" in prompt
    assert "behavior_discovery:" in prompt  # definition, not a bare label


def test_to_output_parses_and_stamps_schema_version():
    out = _block().to_output(_raw([GOOD_EVIDENCE]))
    # The block owns schema_version; a model-supplied value is overwritten.
    # Asserted against the constant: pinning the literal made a deliberate
    # version bump look like a regression.
    assert out["schema_version"] == SCHEMA_VERSION
    assert SCHEMA_VERSION != 99
    assert out["categories"][0]["category"] == "testing_verification"


def test_to_output_validates_citations_before_the_block_persists():
    """Validation must happen in the transform, not after block.run().

    The AnalyzerBlock persists whatever the transform returns, so a citation
    filtered downstream would still be served on every later cache hit.
    """
    fabricated = {**GOOD_EVIDENCE, "trial_id": "does-not-exist"}
    out = _block().to_output(_raw([fabricated]))
    assert out["categories"] == []
    assert out["dropped"]["evidence"] == 1
    assert out["dropped"]["observations"] == 1


def test_to_output_reports_thin_coverage():
    thin = {**TRIAL, "trial_id": "t9", "coverage": 0.1}
    out = _block(successful=[TRIAL, thin]).to_output(_raw([GOOD_EVIDENCE]))
    assert out["thin_coverage"] == ["t9"]


def test_to_output_accepts_a_retired_taxonomy_value():
    # Stored summaries still carry retired labels; the citation resolves
    # because the component really is on the trial, not because an enum
    # happened to list it.
    retired = {
        "trajectory_component": "thinking_diagnose",
        "step_ids": [7, 9],
        "summary": "Worked out why the assertion tripped.",
    }
    trial = {**TRIAL, "components": [retired]}
    cited = {
        "trial_id": "t1",
        "trajectory_component": "thinking_diagnose",
        "step_ids": [7, 9],
        "quote": "Worked out why the assertion tripped.",
    }
    out = _block(successful=[trial]).to_output(_raw([cited]))
    assert out["dropped"]["evidence"] == 0
    assert len(out["categories"]) == 1


def test_to_output_rejects_malformed_json():
    # BlockParseError subclasses ValueError, so AnalyzerBlock's transform
    # contract still holds now that parsing goes through Block.parse.
    with pytest.raises(BlockParseError):
        _block().to_output("not json")


def test_to_output_rejects_a_non_object_reply():
    # Previously reached CohortComparisonOutput(**data) and raised TypeError,
    # which is not a ValueError and so escaped the transform contract.
    with pytest.raises(BlockParseError):
        _block().to_output("[]")
