from oddish.evals.analyzer.by_model import (
    build_by_model_payload,
    build_denominators,
    normalize_entries,
)
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


_DENOMS = {
    "claude-opus-4-8": {"trials": 10, "scored": 10, "solved": 6, "mean_reward": 0.6,
                        "analyzed": 4, "bad": 1, "good": 3},
    "claude-sonnet-5": {"trials": 10, "scored": 10, "solved": 3, "mean_reward": 0.3,
                        "analyzed": 7, "bad": 2, "good": 5},
}


def test_entry_model_is_normalized_to_the_host_key():
    out = normalize_entries(
        [{"model": "anthropic/claude-opus-4-8", "narrative": "n"}], _DENOMS, bucket="all"
    )
    assert out[0]["model"] == "claude-opus-4-8"


def test_hallucinated_model_is_dropped():
    out = normalize_entries(
        [
            {"model": "claude-opus-4-8", "narrative": "real"},
            {"model": "gpt-9-turbo", "narrative": "invented"},
        ],
        _DENOMS,
        bucket="all",
    )
    assert [e["model"] for e in out] == ["claude-opus-4-8"]


def test_non_string_model_is_dropped_without_raising():
    out = normalize_entries(
        [
            {"model": 42, "narrative": "bad"},
            {"model": "claude-opus-4-8", "narrative": "good"},
        ],
        _DENOMS,
        bucket="all",
    )
    assert [e["model"] for e in out] == ["claude-opus-4-8"]


def test_missing_or_none_model_is_dropped_not_attributed_to_unknown():
    out = normalize_entries(
        [
            {"narrative": "no model key at all"},
            {"model": None, "narrative": "explicit none"},
            {"model": "claude-opus-4-8", "narrative": "good"},
        ],
        {**_DENOMS, "unknown": {"trials": 1, "scored": 0, "solved": 0,
                                 "mean_reward": None, "analyzed": 0, "bad": 0,
                                 "good": 0}},
        bucket="all",
    )
    assert [e["model"] for e in out] == ["claude-opus-4-8"]


def test_entries_are_tagged_with_their_bucket():
    out = normalize_entries(
        [{"model": "claude-opus-4-8", "narrative": "n"}], _DENOMS, bucket="bad"
    )
    assert out[0]["bucket"] == "bad"


def test_duplicate_model_and_bucket_keeps_first_only():
    out = normalize_entries(
        [
            {"model": "claude-opus-4-8", "narrative": "first"},
            {"model": "claude-opus-4-8", "narrative": "second"},
        ],
        _DENOMS,
        bucket="bad",
    )
    assert len(out) == 1
    assert out[0]["narrative"] == "first"


def test_same_model_in_two_buckets_yields_two_entries():
    bad = normalize_entries(
        [{"model": "claude-opus-4-8", "narrative": "b"}], _DENOMS, bucket="bad"
    )
    good = normalize_entries(
        [{"model": "claude-opus-4-8", "narrative": "g"}], _DENOMS, bucket="good"
    )
    merged = bad + good
    assert {e["bucket"] for e in merged} == {"bad", "good"}


def test_missing_optional_fields_default_and_do_not_raise():
    out = normalize_entries([{"model": "claude-opus-4-8"}], _DENOMS, bucket="all")
    assert out[0]["narrative"] == ""
    assert out[0]["relative_strengths"] == ""
    assert out[0]["relative_weaknesses"] == ""
    assert out[0]["distinctive_failures"] == []


def test_distinctive_failures_coerced_to_list_of_str():
    out = normalize_entries(
        [{"model": "claude-opus-4-8", "distinctive_failures": "not a list"}],
        _DENOMS,
        bucket="all",
    )
    assert out[0]["distinctive_failures"] == []


def test_payload_shape_is_versioned():
    entries = normalize_entries(
        [{"model": "claude-opus-4-8", "narrative": "n"}], _DENOMS, bucket="all"
    )
    payload = build_by_model_payload(entries, "compare", _DENOMS)
    assert payload["version"] == 1
    assert payload["comparison"] == "compare"
    assert payload["denominators"] == _DENOMS
    assert payload["models"] == entries


def test_single_model_still_produces_a_payload():
    denoms = {"claude-opus-4-8": _DENOMS["claude-opus-4-8"]}
    entries = normalize_entries(
        [{"model": "claude-opus-4-8", "narrative": "n"}], denoms, bucket="all"
    )
    payload = build_by_model_payload(entries, "", denoms)
    assert len(payload["models"]) == 1
    assert payload["comparison"] == ""


def test_output_carries_by_model_payload():
    from oddish.evals.analyzer.schemas import AnalyzerEvalOutput

    out = AnalyzerEvalOutput(sections={"bad": "", "good": "", "capabilities": "",
                                       "headroom": ""})
    assert out.by_model is None

    out.by_model = {"version": 1, "comparison": "", "denominators": {}, "models": []}
    assert out.by_model["version"] == 1
