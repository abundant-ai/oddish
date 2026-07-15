from api.services.cc_chat.analyzer_prompt import (
    FINDINGS_PATH,
    REDUCE_PATH,
    build_cohort_prompt,
)
from oddish.evals.analyzer.prompt_builder import section_brief
from oddish.evals.primitives import SubAnalysis


def _sa(trial_id: str, classification: str = "reward_hacking") -> SubAnalysis:
    return SubAnalysis(
        trial_id=trial_id,
        trajectory_link=f"/tasks/t1/probe/{trial_id}",
        classification=classification,
        subtype="1a",
        evidence="hardcoded the expected output",
        root_cause="test asserts a literal",
        recommendation="hide the oracle",
    )


def _roster() -> list[dict]:
    return [
        {"trial_id": "bad-1", "bucket": "bad", "subtype": "1a",
         "trajectory_link": "/tasks/t1/probe/bad-1"},
        {"trial_id": "good-1", "bucket": "good", "subtype": "3a",
         "trajectory_link": "/tasks/t1/probe/good-1"},
    ]


COUNTS = {"trials": 2, "bad": 1, "good": 1}


def test_bad_prompt_asks_only_for_bad_section():
    p = build_cohort_prompt("bad", [_sa("bad-1")], _roster(), COUNTS,
                            {"bad-1": "ORACLE TEXT HERE"})
    assert "bad_failure_content" in p
    assert "headroom_analysis" not in p
    assert "universal_capabilities_content" not in p


def test_good_prompt_asks_for_the_other_three_sections():
    p = build_cohort_prompt("good", [_sa("good-1", "capability_failure")],
                            _roster(), COUNTS, {})
    for key in ("good_failure_content", "universal_capabilities_content",
                "headroom_analysis"):
        assert key in p
    assert "bad_failure_content" not in p


def test_bad_prompt_includes_oracle_context():
    p = build_cohort_prompt("bad", [_sa("bad-1")], _roster(), COUNTS,
                            {"bad-1": "ORACLE TEXT HERE"})
    assert "ORACLE TEXT HERE" in p


def test_good_prompt_has_no_oracle_context():
    p = build_cohort_prompt("good", [_sa("good-1", "capability_failure")],
                            _roster(), COUNTS, {"bad-1": "ORACLE TEXT HERE"})
    assert "ORACLE TEXT HERE" not in p


def test_prompt_carries_full_roster_of_both_cohorts():
    """map.txt gives every analyst the full roster so they can cross-reference
    a trial in the other bucket. Only the cohort to analyze is split."""
    p = build_cohort_prompt("bad", [_sa("bad-1")], _roster(), COUNTS,
                            {"bad-1": "o"})
    assert "good-1" in p and "bad-1" in p


def test_prompt_names_the_output_files_and_cli():
    p = build_cohort_prompt("bad", [_sa("bad-1")], _roster(), COUNTS,
                            {"bad-1": "o"})
    assert REDUCE_PATH in p
    assert FINDINGS_PATH in p
    assert "node /home/daytona/workspace/oddish-query trials logs" in p


def test_prompt_uses_the_shared_section_fragments():
    """The brief must come from Task 4's fragment, not retyped prose."""
    p = build_cohort_prompt("good", [_sa("good-1", "capability_failure")],
                            _roster(), COUNTS, {})
    assert section_brief("headroom_analysis") in p


def test_no_escaped_braces_reach_the_agent():
    """map.txt escapes its literal JSON braces for str.format(); we inline it
    raw, so they must be unescaped or the agent sees malformed JSON."""
    p = build_cohort_prompt("bad", [_sa("bad-1")], _roster(), COUNTS, {"bad-1": "o"})
    assert "{{" not in p and "}}" not in p


def test_map_output_example_uses_single_braces_and_keeps_placeholders():
    p = build_cohort_prompt("bad", [_sa("bad-1")], _roster(), COUNTS, {"bad-1": "o"})
    assert '{"trial_id"' in p
    assert "{trajectory_link}" in p   # agent fills this itself


def test_good_prompt_cannot_leak_oracle_even_if_caller_passes_it():
    p = build_cohort_prompt(
        "good", [_sa("good-1", "capability_failure")], _roster(), COUNTS,
        {"good-1": "LEAKED ORACLE TEXT"},
    )
    assert "LEAKED ORACLE TEXT" not in p


def _bad_sa(trial_id: str) -> SubAnalysis:
    return SubAnalysis(
        trial_id=trial_id,
        trajectory_link=f"/tasks/t1/probe/{trial_id}",
        classification="BAD_FAILURE",
        subtype="1a",
        evidence="hardcoded the expected output",
        root_cause="test asserts a literal",
        recommendation="hide the oracle",
    )


def test_prompt_has_no_conflicting_map_template_framing():
    """map.txt is written for the one-call-per-trial API path. Inlining it told the
    agent it was one analyst on ONE trajectory and to 'return ONLY JSON' — which
    would suppress the MAP FINDING: markers the stream fallback depends on."""
    p = build_cohort_prompt("bad", [_bad_sa("bad-1")], _roster(), COUNTS, {"bad-1": "o"})
    assert "one of several analysts" not in p
    assert "return ONLY JSON" not in p
    assert "{trajectory_block}" not in p
    assert "{roster_block}" not in p


def test_prompt_keeps_the_shared_rubric_and_output_shape():
    from oddish.evals.analyzer.prompt_builder import map_rubric

    p = build_cohort_prompt("bad", [_bad_sa("bad-1")], _roster(), COUNTS, {"bad-1": "o"})
    assert map_rubric() in p                 # from the shared fragment, not retyped
    assert '{"trial_id"' in p                # single-brace shape
    assert "{trajectory_link}" in p          # agent fills this itself


def test_map_instructions_are_self_consistent():
    p = build_cohort_prompt("bad", [_bad_sa("bad-1")], _roster(), COUNTS, {"bad-1": "o"})
    assert "MAP FINDING:" in p
    assert FINDINGS_PATH in p
