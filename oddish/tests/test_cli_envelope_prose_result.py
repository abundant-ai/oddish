"""The CLI envelope's free-text `result` must get the same JSON recovery
`Block.parse_json` gives every other path.

#949 taught `Block.parse_json` to recover JSON embedded in prose, but the
CLAUDE_CLI envelope path never calls it -- `extract_claude_result` had its own
`json.loads(strip_code_fences(...))`. Pre-trial runs on that path, so 96% of
audits kept dying on "Expecting value: line 1 column 1 (char 0)" after #949
shipped. Both strings below are real `result` fields from FAILED prod blocks.
"""

import json

import pytest

from oddish.blocks.analyzer.claude_cli_client import extract_claude_result

# block 849a89c2 -- preamble sentence, then a fenced object with findings.
_PREAMBLE_THEN_FENCE = (
    "Based on my comprehensive audit of this Harbor task source, I found one "
    "issue related to verifier completeness:\n\n"
    '```json\n{\n  "items": [\n    {\n      "source": "pre_trial",\n'
    '      "dimension": "verifier",\n      "problem_type": "incompleteness"\n'
    "    }\n  ]\n}\n```"
)

# block bf1c59a2 -- a long clean-bill-of-health writeup, then an empty result.
_PREAMBLE_THEN_EMPTY = (
    "Based on my thorough examination of the task source materials, I have not "
    "identified any actionable defects meeting the audit taxonomy.\n\n"
    "- ✓ All 13 holes correctly inventoried in holes.json\n\n"
    '```json\n{\n  "items": []\n}\n```'
)


def _envelope(result: str) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "result": result}


def test_findings_survive_a_prose_preamble():
    out = extract_claude_result(_envelope(_PREAMBLE_THEN_FENCE))
    assert out["items"][0]["dimension"] == "verifier"


def test_a_clean_audit_survives_a_prose_preamble():
    """An empty `items` is a real verdict -- losing it looks like a crash."""
    assert extract_claude_result(_envelope(_PREAMBLE_THEN_EMPTY)) == {"items": []}


def test_structured_output_still_wins_over_result():
    payload = {"structured_output": {"items": [{"dimension": "oracle"}]}, "result": "x"}
    assert extract_claude_result(payload)["items"][0]["dimension"] == "oracle"


def test_explicit_null_structured_output_falls_through_to_result():
    payload = {"structured_output": None, "result": '{"items": []}'}
    assert extract_claude_result(payload) == {"items": []}


@pytest.mark.parametrize(
    "result",
    ['{"items": []}', '```json\n{"items": []}\n```', '```\n{"items": []}\n```'],
)
def test_clean_result_shapes_are_unaffected(result):
    assert extract_claude_result(_envelope(result)) == {"items": []}


def test_result_with_no_json_at_all_still_raises():
    with pytest.raises(json.JSONDecodeError):
        extract_claude_result(_envelope("I could not read the task directory."))


def test_envelope_without_a_usable_result_still_raises():
    with pytest.raises(RuntimeError, match="no structured result"):
        extract_claude_result({"type": "result"})
