import json

from oddish.evals.analyzer.by_model import normalize_entries

_DENOMS = {
    "claude-opus-4-8": {"trials": 4, "scored": 4, "solved": 1, "mean_reward": 0.25,
                        "analyzed": 2, "bad": 1, "good": 1},
}


def test_bucket_entries_merge_into_one_payload_per_model():
    bad_raw = [{"model": "claude-opus-4-8", "narrative": "hacked the verifier"}]
    good_raw = [{"model": "claude-opus-4-8", "narrative": "lost the thread"}]

    entries = (
        normalize_entries(bad_raw, _DENOMS, bucket="bad")
        + normalize_entries(good_raw, _DENOMS, bucket="good")
    )

    assert len(entries) == 2
    assert {e["bucket"] for e in entries} == {"bad", "good"}
    assert {e["model"] for e in entries} == {"claude-opus-4-8"}


def test_sandbox_reduce_json_carrying_by_model_round_trips():
    raw = json.loads(json.dumps({
        "bad_failure_content": "md",
        "by_model": [{"model": "claude-opus-4-8", "narrative": "n",
                      "distinctive_failures": ["oracle copying"]}],
        "cross_model_comparison": "",
    }))
    entries = normalize_entries(raw["by_model"], _DENOMS, bucket="bad")
    assert entries[0]["distinctive_failures"] == ["oracle copying"]
