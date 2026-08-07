"""Literal control characters in model JSON, using shapes observed in production.

Every string below mirrors a real FAILED trajectory_summary analyzer block.
Models write multi-paragraph prose into a JSON string value and leave the
newlines literal, which ``json.loads`` rejects under its default strict mode.
Of 40 failed blocks sampled from prod on 2026-08-05, 37 died this way.
"""

import pytest
from pydantic import BaseModel

from oddish.blocks.block import Block, BlockParseError


class _Summary(BaseModel):
    summary: str = ""
    highlights: list[dict] = []


class _Probe(Block):
    output_schema = _Summary

    def sections(self):
        return []


# block 30a1a28b: a literal blank line inside the "summary" string value.
_LITERAL_NEWLINES = (
    "```json\n"
    "{\n"
    '  "summary": "The agent tried to rebuild financial statements.\n'
    "\n"
    '  The final reward was 0.0.",\n'
    '  "highlights": [\n'
    '    {"step_id": 8, "title": "Analyze presentation TSV structure"}\n'
    "  ]\n"
    "}\n"
    "```\n"
)

# block 2c8099bb: the summary string is never closed -- the model runs straight
# into the next key, so no amount of control-character leniency parses it.
_UNTERMINATED_STRING = (
    "```json\n"
    "{\n"
    '  "summary": "The agent inspected a cold-chain workbook system.\n'
    "\n"
    '  "highlights": [\n'
    '    {"step_id": 5, "title": "Dump workbook structure"}\n'
    "  ]\n"
    "}\n"
    "```\n"
)


def test_literal_newlines_inside_a_string_value_parse():
    parsed = _Probe.parse_json(_LITERAL_NEWLINES)
    assert parsed["summary"].startswith("The agent tried to rebuild")
    assert "The final reward was 0.0." in parsed["summary"]
    assert parsed["highlights"][0]["step_id"] == 8


def test_literal_newlines_survive_the_full_parse():
    out = _Probe().parse(_LITERAL_NEWLINES)
    assert out.highlights[0]["title"] == "Analyze presentation TSV structure"


def test_unterminated_string_is_repaired():
    """The dominant residual after ``strict=False``: 97 of 149 in a 250-block
    prod replay. The model closes the summary early and runs into the next key,
    so the missing comma and property quote are inserted back."""
    out = _Probe().parse(_UNTERMINATED_STRING)
    assert out.summary.startswith("The agent inspected a cold-chain workbook")
    assert out.highlights[0]["step_id"] == 5


def test_unterminated_string_never_degrades_into_the_nested_array():
    """Recovering the nested ``highlights`` array turned a broken object into a
    list and reported a type error, hiding the real defect."""
    parsed = _Probe.parse_json(_UNTERMINATED_STRING, expect=dict)
    assert isinstance(parsed, dict)


def test_unrepairable_output_reports_the_real_decode_error():
    with pytest.raises(BlockParseError) as excinfo:
        _Probe().parse('{"summary": "x", "highlights": [{{{{ ')
    message = str(excinfo.value)
    assert "expected object, got list" not in message
    assert "non-JSON output" in message


def test_repair_never_invents_an_object_from_prose():
    with pytest.raises(BlockParseError):
        _Probe().parse("I was unable to summarize this trajectory.")


def test_a_schema_expecting_an_object_never_recovers_a_bare_array():
    with pytest.raises(BlockParseError):
        _Probe().parse('Here are the highlights:\n[{"step_id": 1}]')


# ---------------------------------------------------------------------------
# reconciliation with the prose-recovery scan (#950)
#
# Prose recovery walks every opener so a brace quoted in the audit cannot
# shadow the payload; the leniency work adds a type filter and whole-text
# repair. The three below pin where those two interact -- each fails against
# either change on its own.
# ---------------------------------------------------------------------------


def test_prose_punctuation_before_the_payload_is_skipped_under_expect():
    """The every-opener scan must survive the ``expect`` filter.

    Restricting recovery to the earliest span (the leniency branch's first
    shape) hands back the quoted ``{}`` and loses the summary.
    """
    parsed = _Probe.parse_json(
        'The runner returns {} on timeout.\n{"summary": "real", "highlights": []}',
        expect=dict,
    )
    assert parsed["summary"] == "real"


def test_a_nested_object_is_not_mined_out_of_a_well_formed_array():
    """A reply of the wrong shape is an error, not a source of fragments.

    The every-opener scan alone walks into the array and returns the inner
    object, which validates to a summary with every field empty.
    """
    with pytest.raises(BlockParseError):
        _Probe().parse('Here are the highlights:\n[{"step_id": 1}, {"step_id": 2}]')


def test_repair_of_the_whole_reply_beats_a_nested_object():
    """A top-level object missing one delimiter is mended, not abandoned.

    The nesting guard cannot help here: the outer object never decodes, so it
    marks nothing as consumed and its inner object is reachable. Prose recovery
    answers with that fragment -- a summary that validates with every field
    empty -- so repair has to run ahead of it.
    """
    parsed = _Probe.parse_json(
        '{"summary": "kept" "meta": {"model": "opus"}}',
        expect=dict,
    )
    assert parsed["summary"] == "kept"


# ---------------------------------------------------------------------------
# Bugbot findings on #1103, both High Severity.
# ---------------------------------------------------------------------------


def test_an_empty_fenced_answer_beats_json_quoted_in_the_preamble():
    """A fence is the model delimiting its own answer, so it wins outright --
    including when that answer is empty. #950 established this; the leniency
    merge reintroduced the "keep scanning" fallthrough that breaks it, so a
    quoted example in the prose was returned as the real result."""
    text = (
        'Example error payload looks like {"error": "bad"}. No defects found.\n\n'
        "```json\n{}\n```\n"
    )
    assert _Probe.parse_json(text, expect=dict) == {}


def test_an_empty_fenced_array_also_wins():
    text = 'For reference the shape is [{"step_id": 1}].\n\n```json\n[]\n```\n'
    assert _Probe.parse_json(text) == []


def test_a_broken_prose_wrapped_object_is_repaired_not_mined():
    """Repair ran only on the fence-stripped whole reply, so a prose-wrapped
    object missing one delimiter was never mended -- the opener scan decoded
    past it and returned a nested fragment, which validates as an all-default
    summary and persists as SUCCESS with every field empty."""
    text = (
        "Here is the summary you asked for:\n"
        '{\n  "summary": "The agent fixed the flaky test.\n\n'
        '  "highlights": [{"step_id": 4, "title": "Repro"}]\n}\n'
    )
    parsed = _Probe.parse_json(text, expect=dict)
    assert parsed["summary"].startswith("The agent fixed the flaky test.")
    assert parsed["highlights"][0]["step_id"] == 4


def test_a_broken_prose_wrapped_object_survives_the_full_parse():
    text = (
        "Findings below.\n"
        '{\n  "summary": "Reconstructed the workbook formulas.\n\n'
        '  "highlights": [{"step_id": 9, "title": "Dump structure"}]\n}\n'
    )
    out = _Probe().parse(text)
    assert out.summary.startswith("Reconstructed the workbook formulas.")
    assert out.highlights[0]["step_id"] == 9


def test_a_fragment_is_not_mined_out_of_an_unrepairable_outer_object():
    """The shape behind all 6 empty-summary recoveries in the prod replay.

    The outer object is broken beyond repair, so the only value that decodes
    is one nested `highlights` entry. Returning it yields `summary=""` with no
    highlights and no components, which persists as SUCCESS -- a summary that
    says nothing, indistinguishable from a real one. Failing is correct.

    Distinct from a `{task_id}` placeholder in a preamble, which is brace-
    balanced on its own and must not block the real payload after it.
    """
    text = (
        "```json\n"
        '{\n  "summary": "The agent recovered the missing formulas.\n\n'
        '  "highlights": [\n'
        '    {"step_id": 2, "title": "Initial scan", "why": "Listed files."}\n'
        "  ,\n}\n"
        "```\n"
    )
    with pytest.raises(BlockParseError):
        _Probe().parse(text)


def test_an_unparseable_placeholder_still_does_not_block_the_real_payload():
    text = (
        "The runner substitutes {task_id} into the path.\n"
        '{"summary": "Done.", "highlights": []}'
    )
    parsed = _Probe.parse_json(text, expect=dict)
    assert parsed["summary"] == "Done."
