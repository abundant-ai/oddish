"""Prose-wrapped JSON recovery, using output shapes observed in production.

Every string below is taken from a real FAILED pre_trial analyzer block --
each one was a complete, useful audit thrown away on packaging.
"""

import pytest

from oddish.blocks.block import Block, BlockParseError
from pydantic import BaseModel


class _Items(BaseModel):
    items: list[dict] = []


class _Probe(Block):
    output_schema = _Items

    def sections(self):
        return []


# block ace535f2: preamble sentence, then a ```json fence.
_PREAMBLE_THEN_FENCE = (
    "Based on my comprehensive audit of the Harbor task source, I have "
    "identified the following defects:\n\n"
    '```json\n{\n  "items": [\n    {\n      "source": "pre_trial",\n'
    '      "dimension": "oracle",\n      "title": "Oracle proof bodies never '
    'validated"\n    }\n  ]\n}\n```\n'
)

# A bare object after a preamble, no fence at all.
_PREAMBLE_THEN_BARE = (
    "Here are the findings from my audit:\n\n"
    '{"items": [{"dimension": "verifier", "title": "Missing unsafe block test"}]}'
)


def test_recovers_json_from_a_fence_that_does_not_open_the_string():
    parsed = _Probe.parse_json(_PREAMBLE_THEN_FENCE)
    assert parsed["items"][0]["title"] == "Oracle proof bodies never validated"


def test_recovers_a_bare_object_after_a_preamble():
    parsed = _Probe.parse_json(_PREAMBLE_THEN_BARE)
    assert parsed["items"][0]["dimension"] == "verifier"


def test_braces_inside_strings_do_not_truncate_the_object():
    text = 'Findings:\n{"items": [{"detail": "the literal {\\"a\\": 1} appears"}]}'
    parsed = _Probe.parse_json(text)
    assert parsed["items"][0]["detail"] == 'the literal {"a": 1} appears'


def test_bare_array_after_prose_is_recovered():
    parsed = _Probe.parse_json('My findings:\n[{"dimension": "oracle"}]')
    assert parsed == [{"dimension": "oracle"}]


@pytest.mark.parametrize(
    "clean",
    [
        '{"items": []}',
        '```json\n{"items": []}\n```',
        '```\n{"items": []}\n```',
    ],
)
def test_already_clean_output_is_unaffected(clean):
    assert _Probe.parse_json(clean) == {"items": []}


def test_an_unparseable_brace_in_the_preamble_does_not_block_recovery():
    # Audit prose quotes code constantly, so a brace before the payload is the
    # common case, not the exotic one. Scanning only the first opener would
    # fail on `{task_id}` and discard the audit.
    text = (
        "The verifier interpolates {task_id} into the path, so:\n\n"
        '{"items": [{"dimension": "verifier", "title": "unquoted path"}]}'
    )
    assert _Probe.parse_json(text)["items"][0]["title"] == "unquoted path"


def test_an_empty_brace_in_the_preamble_does_not_shadow_the_payload():
    # `{}` parses, so taking the first opener that decodes would return an
    # empty object -- which validates cleanly and yields a silently empty
    # audit, the worst outcome of the three.
    text = (
        "The runner returns {} when the trial times out. Findings:\n\n"
        '{"items": [{"dimension": "oracle", "title": "timeout unreported"}]}'
    )
    assert _Probe.parse_json(text)["items"][0]["title"] == "timeout unreported"


def test_a_legitimately_empty_result_is_still_returned():
    # ...but an empty container is the right answer when it is all there is.
    assert _Probe.parse_json("No defects found.\n\n```json\n{}\n```") == {}


def test_genuine_prose_with_no_json_still_raises():
    """Recovery must not invent structure where the model produced none."""
    text = (
        "I need shell/bash access to run the `oddish pull` command you "
        "specified, but I don't have that tool enabled."
    )
    with pytest.raises(BlockParseError, match="non-JSON output"):
        _Probe().parse(text)


# A fence is the model delimiting its own answer, so it settles the reply before
# anything is scanned or mended out of the prose around it. The three below each
# pin one way that ordering was lost.


def test_a_repairable_example_in_the_preamble_loses_to_the_fence():
    # Opener-anchored repair used to run first, so a malformed example the model
    # quoted while explaining itself was mended and returned as the answer.
    text = (
        'For example {"a": 1 "b": 2} is malformed.\n\n'
        '```json\n{"items": [{"title": "real answer"}]}\n```'
    )
    assert _Probe.parse_json(text, expect=dict)["items"][0]["title"] == "real answer"


def test_a_broken_fence_is_repaired_even_past_json_in_the_preamble():
    # Repair only ever ran on the fence-stripped whole reply. A `{}` in the
    # prose parses as-is, so opener-anchored repair declined it and stopped --
    # leaving the broken fence unmended, though the identical body parses when
    # the fence happens to open the string.
    body = '{"items": [{"title": "mended"}] "count": 1}'
    mended = {"items": [{"title": "mended"}], "count": 1}
    assert _Probe.parse_json(f"Returns {{}} on timeout.\n\n```json\n{body}\n```") == mended
    assert _Probe.parse_json(f"```json\n{body}\n```") == mended


def test_a_fenced_answer_of_the_wrong_type_is_not_replaced_by_an_example():
    # A fenced array under expect=dict used to fall through to the scan, which
    # returned the schema quoted in the preamble -- a fabricated result that
    # validates cleanly. Surfacing the mismatch is the honest outcome.
    text = (
        'Schema is {"items": [], "count": 0}.\n\n'
        '```json\n["item one", "item two"]\n```'
    )
    assert _Probe.parse_json(text, expect=dict) == ["item one", "item two"]
    with pytest.raises(BlockParseError, match="expected object, got list"):
        _Probe().parse(text)
