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


def _trial(**over):
    base = {
        "trial_id": "t1",
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


def test_cohort_section_shows_the_counted_subagent_dispatches():
    text = cp.cohort_section(
        "SUCCESSFUL",
        [_trial(delegation={"capable": True, "dispatches": 3})],
    )
    assert 'subagents="3"' in text


def test_cohort_section_shows_a_real_zero():
    text = cp.cohort_section(
        "FAILING", [_trial(delegation={"capable": True, "dispatches": 0})]
    )
    assert 'subagents="0"' in text


def test_cohort_section_marks_an_agent_without_the_tool_as_na():
    # Never "0": the comparison would read it as a choice not to delegate.
    text = cp.cohort_section(
        "FAILING", [_trial(delegation={"capable": False, "dispatches": None})]
    )
    assert 'subagents="n/a"' in text
    assert 'subagents="0"' not in text


def test_cohort_section_omits_the_attribute_when_it_was_never_counted():
    # Summaries written before delegation counting carry no `delegation` key.
    # Absent must read as unknown, so the attribute is left off entirely.
    text = cp.cohort_section("SUCCESSFUL", [_trial()])
    assert "subagents=" not in text


def test_template_explains_the_subagents_attribute():
    template = cp.load_cohort_prompt_template()
    assert "subagents" in template
    assert "n/a" in template


def test_template_points_the_agent_at_the_workspace():
    template = cp.load_cohort_prompt_template()
    assert "MAP.md" in template
    assert "trials/<trial_id>/" in template


def test_template_defines_both_evidence_shapes():
    template = cp.load_cohort_prompt_template()
    assert "step_id" in template
    assert "trajectory_component" in template
    # The one-shape rule is what the schema validator enforces; if the prompt
    # does not state it, every mixed citation is a rejected response.
    assert "both shapes" in template.lower()


def test_template_loads_and_carries_the_placeholder():
    assert "{{taxonomy}}" in cp.load_cohort_prompt_template()


def test_template_asks_for_the_step_form_validation_accepts():
    # cohort_section prints a compact [first-last] range (a component can span
    # hundreds of steps), and validate_evidence matches on that span. The
    # prompt has to ask for exactly those two numbers, or every citation drops.
    assert "[first-last]" in cp.load_cohort_prompt_template()
