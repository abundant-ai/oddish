from api.services.cc_chat.analyzer_prompt import REDUCE_PATH, build_cohort_prompt
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
    assert "node /home/daytona/workspace/oddish-query trials logs" in p


def test_prompt_uses_the_shared_section_fragments():
    """The brief must come from Task 4's fragment, not retyped prose."""
    p = build_cohort_prompt("good", [_sa("good-1", "capability_failure")],
                            _roster(), COUNTS, {})
    assert section_brief("headroom_analysis") in p
