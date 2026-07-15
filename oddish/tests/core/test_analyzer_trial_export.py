from oddish.core.analyzer_trial_export import build_trial_analyses_payload
from oddish.evals.analyzer.schemas import Finding
from oddish.evals.primitives import SubAnalysis


def _finding(trial_id: str) -> Finding:
    return Finding(
        trial_id=trial_id, bucket="bad", subcategory="3a",
        evidence_quote="q", step_ids=[1, 2], root_cause="rc",
        headroom_signal="hs", trajectory_link="link",
    )


def _sub(trial_id: str) -> SubAnalysis:
    return SubAnalysis(
        trial_id=trial_id, trajectory_link="link", classification="c",
        subtype="st", evidence="ev", root_cause="rc", recommendation="rec",
    )


def test_payload_merges_finding_and_subanalysis_by_trial():
    payload = build_trial_analyses_payload(
        analyzer_id="az1",
        findings=[_finding("t1")],
        subanalyses=[_sub("t1")],
        counts={"trials": 1, "bad": 1, "good": 0},
    )
    assert payload["analyzer_id"] == "az1"
    assert payload["counts"] == {"trials": 1, "bad": 1, "good": 0}
    assert len(payload["trials"]) == 1
    record = payload["trials"][0]
    assert record["trial_id"] == "t1"
    assert record["finding"]["subcategory"] == "3a"
    assert "trial_id" not in record["finding"]  # redundant with record key
    assert record["subanalysis"]["classification"] == "c"


def test_payload_includes_trials_with_only_one_side():
    payload = build_trial_analyses_payload(
        analyzer_id="az1",
        findings=[_finding("t_finding_only")],
        subanalyses=[_sub("t_sub_only")],
        counts={},
    )
    by_id = {r["trial_id"]: r for r in payload["trials"]}
    assert by_id["t_finding_only"]["subanalysis"] is None
    assert by_id["t_sub_only"]["finding"] is None


def test_payload_trials_sorted_and_empty_ok():
    payload = build_trial_analyses_payload(
        analyzer_id="az1",
        findings=[_finding("t2"), _finding("t1")],
        subanalyses=[],
        counts={},
    )
    assert [r["trial_id"] for r in payload["trials"]] == ["t1", "t2"]

    empty = build_trial_analyses_payload(
        analyzer_id="az1", findings=[], subanalyses=[], counts={},
    )
    assert empty["trials"] == []
