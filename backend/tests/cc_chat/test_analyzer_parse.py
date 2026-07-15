import json

import pytest

from api.services.cc_chat.analyzer_parse import CohortParseError, parse_cohort_result

LINKS = {"bad-1": "/tasks/t1/probe/bad-1"}


def _finding(trial_id="bad-1") -> dict:
    return {
        "trial_id": trial_id, "bucket": "bad", "subcategory": "1a",
        "evidence_quote": "q", "step_indices": [3], "root_cause": "rc",
        "headroom_signal": "hs", "trajectory_link": "MODEL-ECHOED-JUNK",
    }


def test_parses_files_when_present():
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding()) + "\n").encode()
    findings, sections = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert sections == {"bad_failure_content": "# Bad"}
    assert len(findings) == 1
    assert findings[0].trial_id == "bad-1"


def test_trajectory_link_comes_from_host_not_the_model():
    """Never trust the model's echo of the link."""
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding()) + "\n").encode()
    findings, _ = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert findings[0].trajectory_link == "/tasks/t1/probe/bad-1"


def test_tolerates_code_fences_in_the_reduce_file():
    reduce_b = b'```json\n{"bad_failure_content": "# Bad"}\n```'
    findings_b = (json.dumps(_finding()) + "\n").encode()
    _, sections = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert sections == {"bad_failure_content": "# Bad"}


def test_falls_back_to_stream_when_reduce_file_missing():
    """FakeDaytonaClient and a real missing file both surface as b''."""
    stream = (
        "MAP FINDING: " + json.dumps(_finding()) + "\n"
        "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# From stream"}) + "\n"
    )
    findings, sections = parse_cohort_result("bad", b"", b"", stream, LINKS)
    assert sections == {"bad_failure_content": "# From stream"}
    assert len(findings) == 1


def test_falls_back_to_stream_when_reduce_file_is_corrupt():
    stream = "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# From stream"})
    _, sections = parse_cohort_result("bad", b"not json at all", b"", stream, LINKS)
    assert sections == {"bad_failure_content": "# From stream"}


def test_raises_when_both_channels_fail():
    with pytest.raises(CohortParseError):
        parse_cohort_result("bad", b"", b"", "the agent just rambled", LINKS)


def test_raises_when_reduce_has_no_section_keys_for_the_bucket():
    reduce_b = json.dumps({"headroom_analysis": "wrong bucket"}).encode()
    with pytest.raises(CohortParseError):
        parse_cohort_result("bad", reduce_b, b"", "", LINKS)


def test_missing_section_key_defaults_to_empty_string():
    reduce_b = json.dumps({"good_failure_content": "# Good"}).encode()
    findings_b = (json.dumps({**_finding("good-1"), "bucket": "good"}) + "\n").encode()
    _, sections = parse_cohort_result(
        "good", reduce_b, findings_b, "", {"good-1": "/tasks/t1/probe/good-1"}
    )
    assert sections["good_failure_content"] == "# Good"
    assert sections["universal_capabilities_content"] == ""
    assert sections["headroom_analysis"] == ""


def test_skips_unparseable_finding_lines():
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = b"{not json}\n" + json.dumps(_finding()).encode() + b"\n\n"
    findings, _ = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert len(findings) == 1


def test_drops_findings_for_trials_outside_the_cohort():
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding("hallucinated-trial")) + "\n").encode()
    findings, _ = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert findings == []


def test_all_blank_sections_is_fatal():
    """Blank sections still look like a completed analysis downstream."""
    reduce_b = json.dumps({"good_failure_content": ""}).encode()
    with pytest.raises(CohortParseError):
        parse_cohort_result("good", reduce_b, b"", "", {})


def test_whitespace_only_sections_is_fatal():
    reduce_b = json.dumps({"good_failure_content": "   "}).encode()
    with pytest.raises(CohortParseError):
        parse_cohort_result("good", reduce_b, b"", "", {})


def test_null_section_value_does_not_become_the_string_None():
    reduce_b = json.dumps({"good_failure_content": None,
                           "headroom_analysis": "# Real"}).encode()
    _, sections = parse_cohort_result("good", reduce_b, b"", "", {})
    assert sections["good_failure_content"] == ""
    assert sections["headroom_analysis"] == "# Real"


def test_non_string_section_value_is_dropped_not_reprd():
    reduce_b = json.dumps({"good_failure_content": {"x": 1},
                           "headroom_analysis": "# Real"}).encode()
    _, sections = parse_cohort_result("good", reduce_b, b"", "", {})
    assert sections["good_failure_content"] == ""


def test_partial_blank_sections_are_allowed():
    """Only ALL-blank is fatal; one real section is a legitimate result."""
    reduce_b = json.dumps({"good_failure_content": "# Real",
                           "headroom_analysis": ""}).encode()
    _, sections = parse_cohort_result("good", reduce_b, b"", "", {})
    assert sections["good_failure_content"] == "# Real"
    assert sections["headroom_analysis"] == ""


def test_stream_fallback_handles_pretty_printed_json():
    """Files are primary; the stream is the last net. It must not lose a
    result the agent really did emit, just across several lines."""
    stream = "REDUCE RESULT:\n" + json.dumps({"bad_failure_content": "# Multi"}, indent=2)
    _, sections = parse_cohort_result("bad", b"", b"", stream, {})
    assert sections["bad_failure_content"] == "# Multi"


def test_finding_bucket_comes_from_the_cohort_not_the_model():
    reduce_b = json.dumps({"bad_failure_content": "# B"}).encode()
    bad_echo = {"trial_id": "bad-1", "bucket": "good", "subcategory": "1a",
                "evidence_quote": "q", "step_indices": [], "root_cause": "rc",
                "headroom_signal": "h", "trajectory_link": "junk"}
    findings, _ = parse_cohort_result(
        "bad", reduce_b, (json.dumps(bad_echo) + "\n").encode(), "",
        {"bad-1": "/tasks/t1/probe/bad-1"})
    assert findings[0].bucket == "bad"
