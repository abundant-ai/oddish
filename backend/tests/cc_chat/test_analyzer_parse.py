import json

import pytest

from api.services.cc_chat.analyzer_parse import CohortParseError, parse_cohort_result

def _host(trial_id="bad-1", **over) -> dict:
    """The host-owned Finding fields the parser must stamp on, mirroring what
    the API map path fills in from the trial bundle and subanalysis."""
    return {
        "trajectory_link": f"/tasks/t1/probe/{trial_id}",
        "model": "anthropic/claude-opus-4-8",
        "classification": "BAD_FAILURE",
        "subtype": "1a",
        "task_id": "task-1",
        "task_path": "tasks/t1",
        **over,
    }


LINKS = {"bad-1": _host("bad-1")}


def _finding(trial_id="bad-1") -> dict:
    return {
        "trial_id": trial_id, "bucket": "bad", "subcategory": "1a",
        "evidence_quote": "q", "step_ids": [3], "root_cause": "rc",
        "headroom_signal": "hs", "trajectory_link": "MODEL-ECHOED-JUNK",
    }


def test_parses_files_when_present():
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding()) + "\n").encode()
    findings, sections, _proposals = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert sections == {"bad_failure_content": "# Bad"}
    assert len(findings) == 1
    assert findings[0].trial_id == "bad-1"
    # Not incidental: the agent emits step_ids, and a parser still reading the
    # old step_indices key would silently produce [] here.
    assert findings[0].step_ids == [3]


def test_trajectory_link_comes_from_host_not_the_model():
    """Never trust the model's echo of the link."""
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding()) + "\n").encode()
    findings, _, _proposals = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert findings[0].trajectory_link == "/tasks/t1/probe/bad-1"


def test_tolerates_code_fences_in_the_reduce_file():
    reduce_b = b'```json\n{"bad_failure_content": "# Bad"}\n```'
    findings_b = (json.dumps(_finding()) + "\n").encode()
    _, sections, _proposals = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert sections == {"bad_failure_content": "# Bad"}


def test_falls_back_to_stream_when_reduce_file_missing():
    """FakeDaytonaClient and a real missing file both surface as b''."""
    stream = (
        "MAP FINDING: " + json.dumps(_finding()) + "\n"
        "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# From stream"}) + "\n"
    )
    findings, sections, _proposals = parse_cohort_result("bad", b"", b"", stream, LINKS)
    assert sections == {"bad_failure_content": "# From stream"}
    assert len(findings) == 1


def test_falls_back_to_stream_when_reduce_file_is_corrupt():
    stream = "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# From stream"})
    _, sections, _proposals = parse_cohort_result("bad", b"not json at all", b"", stream, LINKS)
    assert sections == {"bad_failure_content": "# From stream"}


def test_raises_when_both_channels_fail():
    with pytest.raises(CohortParseError):
        parse_cohort_result("bad", b"", b"", "the agent just rambled", LINKS)


def test_raises_when_reduce_has_no_section_keys_for_the_bucket():
    reduce_b = json.dumps({"headroom_analysis": "wrong bucket"}).encode()
    with pytest.raises(CohortParseError):
        parse_cohort_result("bad", reduce_b, b"", "", LINKS)


def test_findings_carry_the_host_classifier_facts():
    """The rollup derives lanes from these host-side fields rather than trusting
    the model's bucket/subcategory echo, so the sandbox path must fill them in
    exactly like the API map path does -- leaving them None strands the rollup."""
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding()) + "\n").encode()
    findings, _, _proposals = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    f = findings[0]
    assert f.model == "anthropic/claude-opus-4-8"
    assert f.classification == "BAD_FAILURE"
    assert f.subtype == "1a"
    assert f.task_id == "task-1"
    assert f.task_path == "tasks/t1"


def test_map_stream_fallback_handles_pretty_printed_json():
    """Symmetry with the REDUCE fallback: a multiline `MAP FINDING:` object is a
    result the agent really did emit, and dropping it fails the whole cohort with
    'no findings' despite perfectly good reduce content."""
    stream = (
        "MAP FINDING:\n" + json.dumps(_finding(), indent=2) + "\n"
        "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# X"})
    )
    findings, _, _proposals = parse_cohort_result("bad", b"", b"", stream, LINKS)
    assert [f.trial_id for f in findings] == ["bad-1"]


def test_map_stream_fallback_recovers_every_multiline_finding():
    """Each marker must consume exactly its own object: a whole-text scan that
    grabbed the outermost braces would swallow the narration and the second
    finding along with the first."""
    hosts = {"bad-1": _host("bad-1"), "bad-2": _host("bad-2")}
    stream = "\n".join([
        "MAP FINDING:\n" + json.dumps(_finding("bad-1"), indent=2),
        "...narrating between findings, with a stray } brace...",
        "MAP FINDING:\n" + json.dumps(_finding("bad-2"), indent=2),
        "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# X"}),
    ])
    findings, _, _proposals = parse_cohort_result("bad", b"", b"", stream, hosts)
    assert [f.trial_id for f in findings] == ["bad-1", "bad-2"]


def test_missing_section_key_defaults_to_empty_string():
    reduce_b = json.dumps({"good_failure_content": "# Good"}).encode()
    findings_b = (json.dumps({**_finding("good-1"), "bucket": "good"}) + "\n").encode()
    _, sections, _proposals = parse_cohort_result(
        "good", reduce_b, findings_b, "", {"good-1": _host("good-1")}
    )
    assert sections["good_failure_content"] == "# Good"
    assert sections["universal_capabilities_content"] == ""
    assert sections["headroom_analysis"] == ""


def test_skips_unparseable_finding_lines():
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = b"{not json}\n" + json.dumps(_finding()).encode() + b"\n\n"
    findings, _, _proposals = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
    assert len(findings) == 1


def test_drops_findings_for_trials_outside_the_cohort():
    reduce_b = json.dumps({"bad_failure_content": "# Bad"}).encode()
    findings_b = (json.dumps(_finding("hallucinated-trial")) + "\n").encode()
    findings, _, _proposals = parse_cohort_result("bad", reduce_b, findings_b, "", LINKS)
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
    _, sections, _proposals = parse_cohort_result("good", reduce_b, b"", "", {})
    assert sections["good_failure_content"] == ""
    assert sections["headroom_analysis"] == "# Real"


def test_non_string_section_value_is_dropped_not_reprd():
    reduce_b = json.dumps({"good_failure_content": {"x": 1},
                           "headroom_analysis": "# Real"}).encode()
    _, sections, _proposals = parse_cohort_result("good", reduce_b, b"", "", {})
    assert sections["good_failure_content"] == ""


def test_partial_blank_sections_are_allowed():
    """Only ALL-blank is fatal; one real section is a legitimate result."""
    reduce_b = json.dumps({"good_failure_content": "# Real",
                           "headroom_analysis": ""}).encode()
    _, sections, _proposals = parse_cohort_result("good", reduce_b, b"", "", {})
    assert sections["good_failure_content"] == "# Real"
    assert sections["headroom_analysis"] == ""


def test_stream_fallback_handles_pretty_printed_json():
    """Files are primary; the stream is the last net. It must not lose a
    result the agent really did emit, just across several lines."""
    stream = "REDUCE RESULT:\n" + json.dumps({"bad_failure_content": "# Multi"}, indent=2)
    _, sections, _proposals = parse_cohort_result("bad", b"", b"", stream, {})
    assert sections["bad_failure_content"] == "# Multi"


def test_finding_bucket_comes_from_the_cohort_not_the_model():
    reduce_b = json.dumps({"bad_failure_content": "# B"}).encode()
    bad_echo = {"trial_id": "bad-1", "bucket": "good", "subcategory": "1a",
                "evidence_quote": "q", "step_ids": [], "root_cause": "rc",
                "headroom_signal": "h", "trajectory_link": "junk"}
    findings, _, _proposals = parse_cohort_result(
        "bad", reduce_b, (json.dumps(bad_echo) + "\n").encode(), "",
        {"bad-1": _host("bad-1")})
    assert findings[0].bucket == "bad"


def test_falls_back_to_stream_when_reduce_file_parses_but_is_unusable():
    """A well-formed reduce file with no usable content is the same situation as a
    malformed one — the file gave us nothing — so it must reach the stream too.
    Only the malformed path used to fall back, so a perfectly good streamed result
    was discarded whenever the agent wrote valid JSON of the wrong shape."""
    reduce_b = json.dumps({"headroom_analysis": "wrong bucket"}).encode()
    stream = "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# From stream"})

    findings, sections, _proposals = parse_cohort_result("bad", reduce_b, b"", stream, LINKS)

    assert sections == {"bad_failure_content": "# From stream"}


def test_falls_back_to_stream_when_reduce_file_sections_are_blank():
    """Same for a file whose keys are right but whose values are all blank."""
    reduce_b = json.dumps({"bad_failure_content": "   "}).encode()
    stream = "REDUCE RESULT: " + json.dumps({"bad_failure_content": "# From stream"})

    _findings, sections, _proposals = parse_cohort_result("bad", reduce_b, b"", stream, LINKS)

    assert sections == {"bad_failure_content": "# From stream"}


GOOD_LINKS = {"good-1": _host("good-1", classification="GOOD_FAILURE", subtype="3a"),
              "good-2": _host("good-2", classification="GOOD_FAILURE", subtype="3a")}


def _good_finding(trial_id="good-1", **over) -> dict:
    return {
        "trial_id": trial_id, "bucket": "good", "subcategory": "3a",
        "capability_slug": "agent-early-stop",
        "evidence_quote": "q", "step_ids": [1], "root_cause": "rc",
        "headroom_signal": "hs", "trajectory_link": "JUNK", **over,
    }


def test_capability_slug_is_kept_from_the_model():
    """Unlike trajectory_link, there is no host fact to override this with --
    only the map agent can tell these capabilities apart."""
    b = (json.dumps(_good_finding()) + "\n").encode()
    findings, _, _ = parse_cohort_result(
        "good", json.dumps({"good_failure_content": "# G"}).encode(), b, "", GOOD_LINKS)
    assert findings[0].capability_slug == "agent-early-stop"


def test_proposal_is_lifted_off_the_finding():
    prop = {"name": "Hypothesis Fixation", "description": "d", "example": "e",
            "categories": ["verification"]}
    b = (json.dumps(_good_finding(capability_slug="hypothesis-fixation",
                                  capability_proposal=prop)) + "\n").encode()
    findings, _, proposals = parse_cohort_result(
        "good", json.dumps({"good_failure_content": "# G"}).encode(), b, "", GOOD_LINKS)
    assert len(proposals) == 1
    assert proposals[0].slug == "hypothesis-fixation"
    assert proposals[0].categories == ["verification"]
    assert proposals[0].trial_ids == ["good-1"]
    # The proposal is not a per-trial fact; storing it on Finding would
    # duplicate it across every citing trial.
    assert not hasattr(findings[0], "capability_proposal")


def test_duplicate_proposals_across_batches_merge_by_slug():
    """One cohort is one continuous session, but a single session can still
    re-propose the same capability across several similar trials -- dedup is
    on the model's output, not on process boundaries. Blind insert would make
    one row per re-proposal."""
    prop = {"name": "Hypothesis Fixation", "description": "d", "example": "e",
            "categories": ["verification"]}
    lines = "".join(
        json.dumps(_good_finding(t, capability_slug="hypothesis-fixation",
                                 capability_proposal=prop)) + "\n"
        for t in ("good-1", "good-2")
    ).encode()
    _, _, proposals = parse_cohort_result(
        "good", json.dumps({"good_failure_content": "# G"}).encode(), lines, "",
        GOOD_LINKS)
    assert len(proposals) == 1
    assert proposals[0].trial_ids == ["good-1", "good-2"]


def test_proposals_survive_stream_fallback_when_findings_file_missing():
    """_download() swallows any exception and returns b'' on a missing file, so
    this path is genuinely reachable. Findings already recover via the stream
    fallback; proposals must stay symmetric with them, or a run that streamed
    MAP FINDING lines with a capability_proposal but failed to upload
    findings.jsonl silently loses every proposal."""
    prop = {"name": "Hypothesis Fixation", "description": "d", "example": "e",
            "categories": ["verification"]}
    stream = (
        "MAP FINDING: " + json.dumps(_good_finding(
            capability_slug="hypothesis-fixation", capability_proposal=prop)) + "\n"
        "REDUCE RESULT: " + json.dumps({"good_failure_content": "# From stream"})
    )
    findings, sections, proposals = parse_cohort_result(
        "good", b"", b"", stream, GOOD_LINKS)
    assert sections["good_failure_content"] == "# From stream"
    assert len(findings) == 1
    assert len(proposals) == 1
    assert proposals[0].slug == "hypothesis-fixation"
    assert proposals[0].trial_ids == ["good-1"]


def test_bad_bucket_proposals_are_dropped():
    """The bad bucket classifies task defects, not agent capabilities. Gate
    structurally so prompt drift cannot leak one in."""
    prop = {"name": "Nope", "description": "d", "example": "e",
            "categories": ["verification"]}
    b = (json.dumps({**_finding(), "capability_slug": "nope",
                     "capability_proposal": prop}) + "\n").encode()
    findings, _, proposals = parse_cohort_result(
        "bad", json.dumps({"bad_failure_content": "# B"}).encode(), b, "", LINKS)
    assert proposals == []
    assert findings[0].capability_slug is None
