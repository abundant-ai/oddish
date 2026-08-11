from api.services.blocks.analyzer.cohort.cohort_taxonomy import (
    CATEGORY_DEFINITIONS,
    DISCOVERY_CAP,
    BehaviorCategory,
)


def test_discovery_is_first_category():
    # Render order matters: discovery is the only category that can name
    # something the taxonomy did not anticipate, so it leads.
    assert list(BehaviorCategory)[0] is BehaviorCategory.BEHAVIOR_DISCOVERY


def test_every_category_has_a_definition():
    # The trajectory-summary prompt ships bare labels with no definitions and
    # mislabels systematically as a result. Every label here must be defined.
    for member in BehaviorCategory:
        assert member in CATEGORY_DEFINITIONS
        assert len(CATEGORY_DEFINITIONS[member]) > 40


def test_discovery_cap():
    assert DISCOVERY_CAP == 2
