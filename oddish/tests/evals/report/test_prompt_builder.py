from oddish.evals.primitives import SubAnalysis, TrajectoryBundle
from oddish.evals.report.schemas import Finding
from oddish.evals.report.prompt_builder import build_map_prompt, build_reduce_prompt


def test_map_prompt_includes_cohort_link_and_taxonomy():
    bundle = TrajectoryBundle(
        trial_id="t-1", task_id="task", task_path="tasks/task", agent="cc",
        model="opus", reward=0.0, trajectory=[{"i": 1}], logs={"v": "FAIL"},
        trajectory_summary={"summary": "s"}, oracle_context="oracle did y",
        trajectory_link="/tasks/task/probe/t-1",
    )
    sa = SubAnalysis(
        trial_id="t-1", trajectory_link="/tasks/task/probe/t-1",
        classification="BAD_FAILURE", subtype="Hardcoding",
        evidence="echo 42", root_cause="rc", recommendation="rec",
    )
    roster = [
        {"trial_id": "t-1", "bucket": "bad", "subtype": "Hardcoding",
         "trajectory_link": "/tasks/task/probe/t-1"},
        {"trial_id": "t-2", "bucket": "good", "subtype": "Wrong Approach",
         "trajectory_link": "/tasks/task/probe/t-2"},
    ]
    p = build_map_prompt(bundle, sa, roster)
    assert "/tasks/task/probe/t-1" in p          # verbatim link to copy
    assert "t-2" in p and "good" in p            # cohort awareness
    assert "oracle did y" in p                    # oracle context for bad trial
    assert "1a" in p and "3a" in p                # seed taxonomy present


def test_map_prompt_uses_bucket_of_for_good_failure():
    bundle = TrajectoryBundle(
        trial_id="t-3", task_id="task", task_path="tasks/task", agent="cc",
        model="opus", reward=1.0, trajectory=[{"i": 1}], logs={"v": "PASS"},
        trajectory_summary={"summary": "s"}, oracle_context=None,
        trajectory_link="/tasks/task/probe/t-3",
    )
    sa = SubAnalysis(
        trial_id="t-3", trajectory_link="/tasks/task/probe/t-3",
        classification="GOOD_FAILURE", subtype="Wrong Approach",
        evidence="tried x", root_cause="rc", recommendation="rec",
    )
    p = build_map_prompt(bundle, sa, roster=[])
    assert "bucket: good" in p
    assert "(none — not a reward-hacking trial)" in p


def test_reduce_prompt_lists_findings_and_counts():
    f = Finding(
        trial_id="t-1", bucket="bad", subcategory="1b", evidence_quote="echo 42",
        step_indices=[7], root_cause="rc", headroom_signal="",
        trajectory_link="/tasks/task/probe/t-1",
    )
    p = build_reduce_prompt([f], {"trials": 5, "bad": 1, "good": 0})
    assert "/tasks/task/probe/t-1" in p
    assert "headroom" in p.lower()
    assert "bad_failure_content" in p  # instructs the four output keys
