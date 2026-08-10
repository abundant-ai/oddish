"""One malformed action item must not discard the whole audit.

Every malformed shape below is copied from a real FAILED pre_trial block. The
model reaches for the prompt's *section headings* when it fills the JSON in:
"SEVERITY" becomes a ``severity`` key, and "1. VERIFIER COMPLETENESS" becomes
``dimension="verifier_completeness"``. Both are the model naming the right
concept, so both are recovered rather than dropped.

The two output paths (bare string vs CLAUDE_CLI envelope) are asserted
together: they forked once already (#959) and a fix applied to only one of
them silently does nothing in prod.
"""

import json

import pytest

from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock
from oddish.blocks.block import BlockParseError

_GOOD = {
    "source": "pre_trial",
    "problem_type": "incompleteness",
    "dimension": "verifier",
    "file": "verifier.py",
    "line_start": 12,
    "line_end": 14,
    "title": "stderr ignored",
    "detail": "The verifier never checks stderr.",
    "recommendation": "Assert stderr is empty.",
    "tier": "should_fix",
}

# block f51e0172 (log2-three-irrational-weights-audit-v2): tier reported under
# the prompt's "SEVERITY" heading instead of the field name.
_SEVERITY_NOT_TIER = {**_GOOD, "title": "severity key"}
_SEVERITY_NOT_TIER.pop("tier")
_SEVERITY_NOT_TIER["severity"] = "should_fix"

# blocks 75abe6ac / 4ea0dd91: dimension given as the taxonomy heading text.
_HEADING_DIMENSION = {**_GOOD, "title": "heading dimension",
                      "dimension": "verifier_completeness"}

# block 888fdc2f: values shifted a field over -- tier holds source's value.
# Nothing to recover; the item is corrupt.
_CORRUPT_TIER = {**_GOOD, "title": "corrupt tier", "tier": "pre_trial"}

# block 3d55d145: no recommendation, and no way to invent one.
_MISSING_RECOMMENDATION = {**_GOOD, "title": "no recommendation"}
_MISSING_RECOMMENDATION.pop("recommendation")


def _block() -> PreTrialBlock:
    return PreTrialBlock(task_id="t1", trial_ids=[], prompt_template="x")


def _via_string(items) -> dict:
    return _block().to_action_items(json.dumps({"items": items}))


def _via_cli(items) -> dict:
    envelope = json.dumps(
        {
            "type": "result",
            "subtype": "success",
            "structured_output": None,
            "result": json.dumps({"items": items}),
            "usage": {"input_tokens": 10, "output_tokens": 20},
        }
    )
    return _block().to_action_items_from_cli(envelope)


BOTH_PATHS = pytest.mark.parametrize("run", [_via_string, _via_cli],
                                     ids=["string", "cli_envelope"])


@BOTH_PATHS
def test_severity_is_accepted_as_tier(run):
    out = run([_SEVERITY_NOT_TIER])
    assert [i["tier"] for i in out["items"]] == ["should_fix"]


@BOTH_PATHS
def test_heading_spelling_of_dimension_is_normalized(run):
    out = run([_HEADING_DIMENSION])
    assert [i["dimension"] for i in out["items"]] == ["verifier"]


@BOTH_PATHS
@pytest.mark.parametrize("bad", [_CORRUPT_TIER, _MISSING_RECOMMENDATION],
                         ids=["corrupt_tier", "missing_recommendation"])
def test_one_unrecoverable_item_does_not_discard_the_others(run, bad):
    out = run([_GOOD, bad, {**_GOOD, "title": "second good"}])
    assert [i["title"] for i in out["items"]] == ["stderr ignored", "second good"]


@BOTH_PATHS
def test_an_all_unrecoverable_audit_raises_instead_of_reporting_clean(run):
    # The dangerous failure: dropping every item would persist {"items": []},
    # which the task page renders as "no defects found".
    with pytest.raises(BlockParseError, match="every action item"):
        run([_CORRUPT_TIER, _MISSING_RECOMMENDATION])


@BOTH_PATHS
def test_a_genuinely_clean_audit_still_reports_no_findings(run):
    assert run([])["items"] == []


@BOTH_PATHS
def test_well_formed_items_are_untouched(run):
    out = run([_GOOD])
    assert out["items"][0]["title"] == "stderr ignored"
    assert out["items"][0]["tier"] == "should_fix"
    assert out["items"][0]["dimension"] == "verifier"


def test_a_bare_array_of_items_is_still_wrapped():
    """``Block.parse`` now asks for objects only; pre-trial opts out.

    The registry prompt asks for "the structured list of action items", so
    models return a bare array often enough that ``_normalize`` wraps it. If
    this block ever propagates the object-only hint, every such audit fails
    shape validation and reports zero findings.
    """
    out = _block().to_action_items(json.dumps([_GOOD]))
    assert [item["title"] for item in out["items"]] == ["stderr ignored"]


def test_a_bare_array_after_prose_is_still_wrapped():
    out = _block().to_action_items("Here is my audit:\n" + json.dumps([_GOOD]))
    assert [item["title"] for item in out["items"]] == ["stderr ignored"]
