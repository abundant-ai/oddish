import pytest
from pydantic import ValidationError

from api.services.blocks.analyzer.cohort.cohort_comparison_block import BehaviorEvidence
from api.services.cohort_comparison import validate_evidence


def _trial(trial_id="t1"):
    return {
        "trial_id": trial_id,
        "components": [
            {
                "trajectory_component": "implementing",
                "step_ids": [1, 2],
                "summary": "Wrote the adapter.",
            }
        ],
    }


def _output(evidence):
    return {
        "categories": [
            {
                "category": "planning",
                "label": None,
                "successful": [
                    {"behavior_description": "Agents delegated.", "evidence": evidence}
                ],
                "failing": [],
            }
        ]
    }


# ---- schema ----

def test_a_step_level_citation_needs_only_a_step_id():
    ev = BehaviorEvidence(trial_id="t1", step_id=7, quote="spawning a subagent")
    assert ev.step_id == 7
    assert ev.trajectory_component is None


def test_a_summary_level_citation_still_validates():
    ev = BehaviorEvidence(
        trial_id="t1",
        trajectory_component="implementing",
        step_ids=[1, 2],
        quote="Wrote the adapter.",
    )
    assert ev.step_id is None


def test_a_citation_must_pick_one_shape():
    # Both shapes at once is ambiguous about what the quote is checked against.
    with pytest.raises(ValidationError):
        BehaviorEvidence(
            trial_id="t1",
            trajectory_component="implementing",
            step_ids=[1, 2],
            step_id=7,
            quote="x",
        )


def test_a_citation_with_neither_shape_is_rejected():
    with pytest.raises(ValidationError):
        BehaviorEvidence(trial_id="t1", quote="x")


# ---- validation ----

def test_a_step_quote_is_kept_when_it_appears_in_that_step():
    index = {("t1", 7): "I will dispatch a subagent to audit the verifier."}
    out, drops = validate_evidence(
        _output([{"trial_id": "t1", "step_id": 7, "quote": "dispatch a subagent"}]),
        [_trial()],
        [],
        step_index=index,
    )
    assert drops["evidence"] == 0
    assert out["categories"][0]["successful"][0]["evidence"][0]["step_id"] == 7


def test_a_step_quote_the_step_does_not_contain_is_dropped():
    # The anti-fabrication property has to hold for the new shape too.
    index = {("t1", 7): "I will read the config."}
    out, drops = validate_evidence(
        _output([{"trial_id": "t1", "step_id": 7, "quote": "dispatch a subagent"}]),
        [_trial()],
        [],
        step_index=index,
    )
    assert drops["evidence"] == 1
    assert out["categories"] == []


def test_a_step_citation_naming_the_wrong_side_is_dropped():
    # t2 is on the failing side; citing it under `successful` must not resolve.
    index = {("t2", 7): "I will dispatch a subagent."}
    out, drops = validate_evidence(
        _output([{"trial_id": "t2", "step_id": 7, "quote": "dispatch a subagent"}]),
        [_trial("t1")],
        [_trial("t2")],
        step_index=index,
    )
    assert drops["evidence"] == 1


def test_a_step_citation_for_an_unfetched_trial_is_dropped():
    # No trajectory on disk means nothing to check the quote against, and an
    # unverifiable citation is exactly what this feature refuses to serve.
    out, drops = validate_evidence(
        _output([{"trial_id": "t1", "step_id": 7, "quote": "anything"}]),
        [_trial()],
        [],
        step_index={},
    )
    assert drops["evidence"] == 1


def test_summary_citations_still_validate_with_no_step_index():
    # The API path passes no index; the existing contract must be untouched.
    out, drops = validate_evidence(
        _output(
            [
                {
                    "trial_id": "t1",
                    "trajectory_component": "implementing",
                    "step_ids": [1, 2],
                    "quote": "Wrote the adapter.",
                }
            ]
        ),
        [_trial()],
        [],
    )
    assert drops["evidence"] == 0


def test_whitespace_around_a_step_quote_is_tolerated():
    index = {("t1", 7): "I will dispatch a subagent."}
    out, drops = validate_evidence(
        _output([{"trial_id": "t1", "step_id": 7, "quote": "  dispatch a subagent  "}]),
        [_trial()],
        [],
        step_index=index,
    )
    assert drops["evidence"] == 0
