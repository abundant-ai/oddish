import pytest
from pydantic import ValidationError

from api.services.blocks.analyzer.cohort.cohort_comparison_block import (
    BehaviorEvidence,
    CategoryComparison,
    CohortComparisonOutput,
)
from api.services.blocks.analyzer.cohort.cohort_taxonomy import BehaviorCategory


def _evidence(trial="t1", steps=(4, 5)):
    return {
        "trial_id": trial,
        "trajectory_component": "testing_public",
        "step_ids": list(steps),
        "quote": "The agent ran mvn test to see the baseline compile failures.",
    }


def test_evidence_accepts_a_retired_taxonomy_value():
    # Stored summaries still carry labels retired from TrajectoryBlockTaxonomy,
    # the prompt shows them verbatim, and the model is told to copy them
    # exactly -- so the schema must not reject them. Whether the label is real
    # is settled by validate_evidence against the stored components, not here
    # (see test_cohort_evidence_validation.py).
    ev = BehaviorEvidence(
        **{**_evidence(), "trajectory_component": "thinking_diagnose"}
    )
    assert ev.trajectory_component == "thinking_diagnose"


def test_evidence_with_no_usable_shape_parses_and_is_dropped_later():
    # A blank component or an empty step_ids leaves nothing to check the quote
    # against, but the schema handed to claude-code cannot express that, so
    # raising here would fail model_validate for the whole payload and discard
    # a minutes-long run over one citation. validate_evidence drops it instead
    # (test_evidence_without_step_ids_is_dropped and
    # test_evidence_with_a_blank_component_is_dropped).
    assert BehaviorEvidence(
        **{**_evidence(), "trajectory_component": "  "}
    ).trajectory_component == "  "
    assert BehaviorEvidence(**{**_evidence(), "step_ids": []}).step_ids == []


def test_discovery_requires_a_label():
    with pytest.raises(ValidationError):
        CategoryComparison(
            category=BehaviorCategory.BEHAVIOR_DISCOVERY,
            label=None,
            successful=[],
            failing=[],
        )


def test_fixed_category_drops_a_stray_label():
    # Dropped, not raised on. The first real generation put a label on four of
    # five categories; because the output is parsed as a whole, raising threw
    # away an otherwise good comparison over a cosmetic field.
    cat = CategoryComparison(
        category=BehaviorCategory.PLANNING,
        label="Sub-agent delegation",
        successful=[],
        failing=[],
    )
    assert cat.label is None


def test_discovery_keeps_its_label():
    cat = CategoryComparison(
        category=BehaviorCategory.BEHAVIOR_DISCOVERY,
        label="Sub-agent delegation",
        successful=[],
        failing=[],
    )
    assert cat.label == "Sub-agent delegation"


def test_full_output_parses():
    out = CohortComparisonOutput(
        schema_version=1,
        cohort_success=["t1"],
        cohort_failure=["t2"],
        summary="Agents took a test baseline before editing.",
        categories=[
            CategoryComparison(
                category=BehaviorCategory.TESTING_VERIFICATION,
                label=None,
                successful=[
                    {
                        "behavior_description": "ran the verifier before writing anything",
                        "evidence": [_evidence()],
                    }
                ],
                failing=[],
            )
        ],
    )
    assert out.categories[0].successful[0].evidence[0].trial_id == "t1"
