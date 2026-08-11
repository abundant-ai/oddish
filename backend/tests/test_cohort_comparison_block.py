import json

import pytest

from api.services.blocks.analyzer.cohort.cohort_comparison_block import (
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
}


def _block():
    return CohortComparisonBlock(
        CohortInput(
            task_name="demo-task",
            successful=[TRIAL],
            failing=[{**TRIAL, "trial_id": "t2"}],
        ),
        instructions_template=cp.load_cohort_prompt_template(),
    )


def test_prompt_contains_both_cohorts_and_definitions():
    prompt = _block().build_prompt()
    assert "SUCCESSFUL" in prompt and "FAILING" in prompt
    assert "t1" in prompt and "t2" in prompt
    assert "behavior_discovery:" in prompt  # definition, not a bare label


def test_to_output_parses_and_stamps_schema_version():
    raw = json.dumps(
        {
            "schema_version": 99,
            "cohort_success": ["t1"],
            "cohort_failure": ["t2"],
            "categories": [
                {
                    "category": "testing_verification",
                    "label": None,
                    "successful": [
                        {
                            "behavior_description": "ran the verifier first",
                            "evidence": [
                                {
                                    "trial_id": "t1",
                                    "trajectory_component": "testing_public",
                                    "step_ids": [34, 35],
                                    "quote": "Ran mvn test for a baseline.",
                                }
                            ],
                        }
                    ],
                    "failing": [],
                }
            ],
        }
    )
    out = _block().to_output(raw)
    # The block owns schema_version; a model-supplied value is overwritten.
    assert out["schema_version"] == 1
    assert out["categories"][0]["category"] == "testing_verification"


def test_to_output_rejects_malformed_json():
    with pytest.raises(ValueError):
        _block().to_output("not json")
