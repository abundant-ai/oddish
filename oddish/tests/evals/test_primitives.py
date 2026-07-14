from oddish.evals.primitives import TrajectoryBundle, SubAnalysis, trajectory_link


def test_trajectory_link():
    assert trajectory_link("my-task", "my-task-3") == "/tasks/my-task/probe/my-task-3"


def test_trajectory_bundle_roundtrip():
    b = TrajectoryBundle(
        trial_id="t-1", task_id="task", task_path="tasks/task", agent="claude-code",
        model="claude-opus-4-8", reward=0.0,
        trajectory=[{"step": 1}], logs={"verifier_stdout": "FAIL"},
        trajectory_summary={"summary": "did x"}, oracle_context="oracle wrote y",
        trajectory_link="/tasks/task/probe/t-1",
    )
    assert TrajectoryBundle.from_dict(b.to_dict()) == b


def test_subanalysis_roundtrip_and_classification():
    s = SubAnalysis(
        trial_id="t-1", trajectory_link="/tasks/task/probe/t-1",
        classification="BAD_FAILURE", subtype="Hardcoding",
        evidence="echo 42 > out", root_cause="hardcoded output",
        recommendation="tighten verifier", trajectory_summary=None,
    )
    assert s.classification == "BAD_FAILURE"
    assert SubAnalysis.from_dict(s.to_dict()) == s
