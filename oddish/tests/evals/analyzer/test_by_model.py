from oddish.evals.analyzer.by_model import build_denominators
from oddish.evals.analyzer.schemas import Finding


def _finding(trial_id: str, model: str, classification: str) -> Finding:
    return Finding(
        trial_id=trial_id, bucket="good", subcategory="3b", evidence_quote="",
        step_ids=[], root_cause="", headroom_signal="", trajectory_link="",
        model=model, classification=classification, subtype="Logic Error",
        task_id="t1", task_path="tasks/t1",
    )


def test_denominators_count_trials_scored_solved_and_mean_reward():
    trials = [
        ("claude-opus-4-8", 1.0),
        ("claude-opus-4-8", 0.5),
        ("claude-opus-4-8", None),
    ]
    findings = [_finding("a", "claude-opus-4-8", "GOOD_FAILURE")]

    out = build_denominators(trials, findings)

    assert out["claude-opus-4-8"] == {
        "trials": 3, "scored": 2, "solved": 1, "mean_reward": 0.75,
        "analyzed": 1, "bad": 0, "good": 1,
    }


def test_mean_reward_is_none_when_nothing_scored():
    out = build_denominators([("claude-opus-4-8", None)], [])
    assert out["claude-opus-4-8"]["mean_reward"] is None


def test_model_spellings_collapse_to_one_key():
    trials = [
        ("claude-opus-4-8", 1.0),
        ("anthropic/claude-opus-4-8", 0.0),
    ]
    out = build_denominators(trials, [])
    assert len(out) == 1
    assert next(iter(out.values()))["trials"] == 2


def test_bad_and_good_counted_from_classification():
    trials = [("claude-opus-4-8", 0.0), ("claude-opus-4-8", 0.0)]
    findings = [
        _finding("a", "claude-opus-4-8", "BAD_FAILURE"),
        _finding("b", "claude-opus-4-8", "GOOD_FAILURE"),
    ]
    out = build_denominators(trials, findings)
    assert out["claude-opus-4-8"]["bad"] == 1
    assert out["claude-opus-4-8"]["good"] == 1
    assert out["claude-opus-4-8"]["analyzed"] == 2


def test_unattributable_model_grouped_as_unknown():
    out = build_denominators([(None, 1.0)], [])
    assert out["unknown"]["trials"] == 1


def test_trial_model_rewards_reads_model_and_reward_from_rows():
    from types import SimpleNamespace
    from oddish.core.analyzer_inputs import trial_model_rewards

    rows = [
        (SimpleNamespace(model="claude-opus-4-8", reward=1.0), "tasks/a"),
        (SimpleNamespace(model=None, reward=None), "tasks/b"),
    ]
    assert trial_model_rewards(rows) == [("claude-opus-4-8", 1.0), (None, None)]
