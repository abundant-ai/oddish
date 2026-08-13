import json

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


def test_a_malformed_shape_parses_rather_than_raising():
    # `model_json_schema` cannot say "exactly one of these", so constrained
    # decoding can produce both or neither. Raising here would fail
    # `model_validate` for the whole payload and throw away a minutes-long
    # run over one citation; the shape rule is enforced by dropping instead.
    both = BehaviorEvidence(
        trial_id="t1",
        trajectory_component="implementing",
        step_ids=[1, 2],
        step_id=7,
        quote="x",
    )
    assert both.step_id == 7
    assert BehaviorEvidence(trial_id="t1", quote="x").step_id is None


def test_a_quote_copied_with_the_files_json_escaping_still_resolves():
    # The component files are written with json.dumps, so the agent reads
    # `I will run \"mvn -q test\" first.\nThen read the log.` -- the prompt
    # says to copy verbatim, and a citation that does carries the escaping
    # while step_index holds the decoded string. Dropping it would punish the
    # most faithful copy of all.
    raw = 'I will run "mvn -q test" first.\nThen read the log.'
    as_written = json.dumps(raw)[1:-1]
    assert as_written not in raw  # the mismatch this guards
    out, drops = validate_evidence(
        _output([{"trial_id": "t1", "step_id": 7, "quote": as_written}]),
        [_trial()],
        [],
        step_index={("t1", 7): raw},
    )
    assert drops["evidence"] == 0
    assert len(out["categories"]) == 1


def test_decoding_does_not_let_an_unrelated_quote_through():
    # Containment against the real step text is still the bar -- decoding only
    # changes the spelling of the quote, never what it has to match.
    out, drops = validate_evidence(
        _output(
            [{"trial_id": "t1", "step_id": 7, "quote": 'never said\\nthis either'}]
        ),
        [_trial()],
        [],
        step_index={("t1", 7): "I will run the tests.\nThen read the log."},
    )
    assert out["categories"] == []
    assert drops["evidence"] == 1


def test_a_citation_naming_both_shapes_is_dropped():
    # Checked against neither: whichever source we picked, a quote matching
    # only the other would pass. Both quotes below are individually valid.
    out, drops = validate_evidence(
        _output(
            [
                {
                    "trial_id": "t1",
                    "trajectory_component": "implementing",
                    "step_ids": [1, 2],
                    "step_id": 7,
                    "quote": "Wrote the adapter.",
                }
            ]
        ),
        [_trial()],
        [],
        step_index={("t1", 7): "Wrote the adapter."},
    )
    assert out["categories"] == []
    assert drops["evidence"] == 1


def test_a_citation_with_neither_shape_is_dropped():
    out, drops = validate_evidence(
        _output([{"trial_id": "t1", "quote": "Wrote the adapter."}]),
        [_trial()],
        [],
        step_index={("t1", 7): "Wrote the adapter."},
    )
    assert out["categories"] == []
    assert drops["evidence"] == 1


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
