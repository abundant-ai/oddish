from api.services.blocks.analyzer.cohort import cohort_prompts as cp
from api.services.blocks.analyzer.cohort.cohort_taxonomy import (
    CATEGORY_DEFINITIONS,
    BehaviorCategory,
)


def test_taxonomy_section_defines_every_category():
    # A bare label list is what makes the trajectory summariser mislabel.
    text = cp.taxonomy_section()
    for member in BehaviorCategory:
        assert member.value in text
        assert CATEGORY_DEFINITIONS[member][:30] in text


def test_taxonomy_section_states_the_discovery_cap():
    assert "at most 2" in cp.taxonomy_section()


def test_cohort_section_renders_components_with_step_ranges():
    trials = [
        {
            "trial_id": "t1",
            "components": [
                {
                    "trajectory_component": "testing_public",
                    "step_ids": [34, 35],
                    "summary": "Ran mvn test for a baseline.",
                }
            ],
        }
    ]
    text = cp.cohort_section("SUCCESSFUL", trials)
    assert "SUCCESSFUL" in text
    assert "t1" in text
    assert "testing_public" in text
    assert "[34-35]" in text
    assert "Ran mvn test for a baseline." in text


def test_template_loads_and_carries_the_placeholder():
    assert "{{taxonomy}}" in cp.load_cohort_prompt_template()


def test_template_asks_for_the_step_form_validation_accepts():
    # cohort_section prints a compact [first-last] range (a component can span
    # hundreds of steps), and validate_evidence matches on that span. The
    # prompt has to ask for exactly those two numbers, or every citation drops.
    assert "[first-last]" in cp.load_cohort_prompt_template()
