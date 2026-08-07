"""Capturing RewardKit verifier output: the named-rewards map and the
per-criterion breakdown.

RewardKit tasks report multiple named scores in ``reward.json`` (surfaced by
Harbor as ``verifier_result.rewards``) and write the full breakdown -- criteria,
weights, judge reasoning -- to ``verifier/reward-details.json``. Oddish keeps
the headline scalar in ``trials.reward`` and embeds the rest in
``trials.result`` under the reserved ``_rewards`` / ``_reward_details`` keys.

Like the sibling metrics.json / ctrf.json contracts, extraction is forgiving:
a missing, malformed, oversized, or structurally invalid details file yields
None -- the settled reward is never at stake.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

from oddish.core import harbor_artifacts
from oddish.core.harbor_artifacts import (
    VERIFIER_REWARD_DETAILS_MAX_BYTES,
    extract_reward_details_summary,
    extract_rewards_map,
    sanitize_task_result,
)
from oddish.workers.harbor.outcome import HarborOutcome, merged_trial_result


def _trial_result(rewards: object) -> SimpleNamespace:
    return SimpleNamespace(verifier_result=SimpleNamespace(rewards=rewards))


# ---------------------------------------------------------------------------
# extract_rewards_map


def test_rewards_map_keeps_all_named_rewards():
    rewards = {"exact": 1.0, "llmj": 0.5, "reward": 0.0, "soft_score": 0.75}
    assert extract_rewards_map(_trial_result(rewards)) == rewards


def test_rewards_map_none_when_only_headline_reward():
    # {"reward": x} carries nothing beyond the scalar already stored in
    # trials.reward; embedding it would just duplicate the column.
    assert extract_rewards_map(_trial_result({"reward": 1.0})) is None


def test_rewards_map_keeps_sole_named_reward():
    # A single non-"reward" key is informative: the *name* explains what the
    # collapsed headline scalar measured.
    assert extract_rewards_map(_trial_result({"accuracy": 0.8})) == {"accuracy": 0.8}


def test_rewards_map_drops_nonfinite_and_noncoercible_values():
    rewards = {"a": 0.5, "b": math.nan, "c": math.inf, "d": "oops"}
    assert extract_rewards_map(_trial_result(rewards)) == {"a": 0.5}


def test_rewards_map_none_without_verifier_result():
    assert extract_rewards_map(SimpleNamespace(verifier_result=None)) is None
    assert extract_rewards_map(_trial_result(None)) is None
    assert extract_rewards_map(_trial_result([1.0])) is None


def test_rewards_map_rounds_values():
    assert extract_rewards_map(_trial_result({"a": 1 / 3, "b": 1.0})) == {
        "a": 0.3333,
        "b": 1.0,
    }


# ---------------------------------------------------------------------------
# extract_reward_details_summary


def _write_details(job_dir: Path, payload: object) -> Path:
    verifier_dir = job_dir / "trial__abc123" / "verifier"
    verifier_dir.mkdir(parents=True, exist_ok=True)
    path = verifier_dir / "reward-details.json"
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload))
    return path


def _details_payload() -> dict:
    return {
        "exact": {
            "score": 0.5,
            "kind": "programmatic",
            "criteria": [
                {
                    "name": "qa_result_shape",
                    "value": 1.0,
                    "raw": True,
                    "weight": 1.0,
                    "description": "qa_result.json has the required shape",
                },
                {
                    "name": "classification_matches_gold",
                    "value": 0.0,
                    "raw": False,
                    "weight": 1.0,
                    "error": "classification must be one of [...]",
                },
            ],
        },
        "llmj": {
            "score": 1.0,
            "kind": "llm",
            "judge": {"model": "anthropic/claude-sonnet-4-6", "files": ["/app/x"]},
            "judge_output": {"tokens": 123},
            "criteria": [
                {
                    "name": "evidence_supports_gold",
                    "value": 1.0,
                    "raw": "yes",
                    "weight": 1.0,
                    "reasoning": "The report cites the failing test output.",
                },
            ],
            "warnings": ["judge retried once"],
        },
    }


def test_details_summary_compacts_dimensions_and_criteria(tmp_path):
    _write_details(tmp_path, _details_payload())

    summary = extract_reward_details_summary(tmp_path)
    assert summary is not None
    assert summary["format"] == "rewardkit"
    assert summary["details_path"] == "trial__abc123/verifier/reward-details.json"
    assert "truncated" not in summary

    by_name = {d["name"]: d for d in summary["dimensions"]}
    assert set(by_name) == {"exact", "llmj"}

    exact = by_name["exact"]
    assert exact["score"] == 0.5
    assert exact["kind"] == "programmatic"
    assert [c["name"] for c in exact["criteria"]] == [
        "qa_result_shape",
        "classification_matches_gold",
    ]
    # The pre-normalization answer rides so the drawer can show what the
    # check/judge actually returned.
    assert exact["criteria"][0]["raw"] is True
    assert exact["criteria"][1]["error"].startswith("classification must be")

    llmj = by_name["llmj"]
    assert llmj["judge"] == "anthropic/claude-sonnet-4-6"
    # The judge's exact inputs ride too — the drawer's "Judge read" chips.
    assert llmj["judge_files"] == ["/app/x"]
    assert llmj["warnings"] == 1
    assert llmj["criteria"][0]["raw"] == "yes"
    assert llmj["criteria"][0]["reasoning"].startswith("The report cites")
    # Bulky judge payloads never ride in the row; only the compact fields do.
    assert "judge_output" not in llmj


def test_details_summary_flattens_list_form_dimensions(tmp_path):
    # rewardkit emits a list when several rewards share one name.
    _write_details(
        tmp_path,
        {
            "checks": [
                {"score": 1.0, "kind": "programmatic", "criteria": []},
                {"score": 0.0, "kind": "programmatic", "criteria": []},
            ]
        },
    )
    summary = extract_reward_details_summary(tmp_path)
    assert [d["score"] for d in summary["dimensions"]] == [1.0, 0.0]
    assert {d["name"] for d in summary["dimensions"]} == {"checks"}


def test_details_summary_caps_criteria_and_marks_total(tmp_path):
    criteria = [{"name": f"c{i}", "value": 1.0, "raw": True} for i in range(45)]
    _write_details(tmp_path, {"dim": {"score": 1.0, "criteria": criteria}})
    summary = extract_reward_details_summary(tmp_path)
    dim = summary["dimensions"][0]
    assert len(dim["criteria"]) == 40
    assert dim["criteria_total"] == 45
    # Capping is data loss: the embed must say so, or the drawer would trust
    # it as complete and never fetch the full report.
    assert summary["truncated"] is True


def test_details_summary_marks_clipped_text_as_truncated(tmp_path):
    # Long reasoning that still fits the embed budget after clipping used to
    # ship without the truncated flag, leaving clients unaware the full
    # document has more.
    _write_details(
        tmp_path,
        {
            "dim": {
                "score": 1.0,
                "criteria": [
                    {"name": "c", "value": 1.0, "raw": True, "reasoning": "r" * 900}
                ],
            }
        },
    )
    summary = extract_reward_details_summary(tmp_path)
    criterion = summary["dimensions"][0]["criteria"][0]
    assert len(criterion["reasoning"]) == 400
    assert summary["truncated"] is True


def test_details_summary_drops_only_the_malformed_criterion(tmp_path):
    # One broken check must not cost the whole breakdown: siblings stay, the
    # dropped entry counts toward criteria_total, and the summary is marked
    # lossy so the full document gets fetched.
    _write_details(
        tmp_path,
        {
            "dim": {
                "score": 0.5,
                "criteria": [
                    {"name": "good", "value": 1.0, "raw": True},
                    {"value": 0.0},
                    {"name": "also_good", "value": 0.0, "raw": False},
                ],
            }
        },
    )
    summary = extract_reward_details_summary(tmp_path)
    dim = summary["dimensions"][0]
    assert [c["name"] for c in dim["criteria"]] == ["good", "also_good"]
    assert dim["criteria_total"] == 3
    assert summary["truncated"] is True


def test_details_summary_tightens_until_it_fits(tmp_path, monkeypatch):
    # Force the embed budget low enough that the generous tier cannot fit; a
    # tighter tier must win and be flagged as truncated.
    _write_details(
        tmp_path,
        {
            "dim": {
                "score": 1.0,
                "criteria": [
                    {
                        "name": "c",
                        "value": 1.0,
                        "raw": True,
                        "reasoning": "r" * 390,
                        "description": "d" * 290,
                    }
                ],
            }
        },
    )
    monkeypatch.setattr(harbor_artifacts, "REWARD_DETAILS_EMBED_MAX_BYTES", 500)
    summary = extract_reward_details_summary(tmp_path)
    assert summary is not None
    assert summary["truncated"] is True
    criterion = summary["dimensions"][0]["criteria"][0]
    assert len(criterion.get("reasoning", "")) <= 120


def test_details_summary_rejects_malformed_candidates(tmp_path):
    _write_details(tmp_path, "{broken")
    assert extract_reward_details_summary(tmp_path) is None


def test_details_summary_rejects_non_object(tmp_path):
    _write_details(tmp_path, [1, 2, 3])
    assert extract_reward_details_summary(tmp_path) is None


def test_details_summary_rejects_nonfinite_scores(tmp_path):
    # NaN parses under stock json but cannot land in JSONB.
    _write_details(tmp_path, '{"dim": {"score": NaN, "criteria": []}}')
    assert extract_reward_details_summary(tmp_path) is None


def test_details_summary_rejects_oversized_file(tmp_path):
    _write_details(
        tmp_path,
        json.dumps({"blob": "x" * (VERIFIER_REWARD_DETAILS_MAX_BYTES + 1)}),
    )
    assert extract_reward_details_summary(tmp_path) is None


def test_details_summary_missing_file_is_none(tmp_path):
    (tmp_path / "trial__abc123" / "verifier").mkdir(parents=True)
    assert extract_reward_details_summary(tmp_path) is None


def test_invalid_candidate_does_not_mask_later_valid_details(tmp_path):
    bad_dir = tmp_path / "a-trial__x" / "verifier"
    bad_dir.mkdir(parents=True)
    (bad_dir / "reward-details.json").write_text("{broken")
    good_dir = tmp_path / "b-trial__y" / "verifier"
    good_dir.mkdir(parents=True)
    good_dir.joinpath("reward-details.json").write_text(
        json.dumps({"dim": {"score": 1.0, "criteria": []}})
    )
    summary = extract_reward_details_summary(tmp_path)
    assert summary["details_path"] == "b-trial__y/verifier/reward-details.json"


# ---------------------------------------------------------------------------
# merged_trial_result / sanitize_task_result


def test_merged_result_embeds_rewards_and_details():
    rewards = {"exact": 1.0, "llmj": 0.5, "reward": 0.0}
    details = {"format": "rewardkit", "details_path": "p", "dimensions": []}
    merged = merged_trial_result(
        {"latency_ms": 12}, None, None, None, rewards, details
    )
    assert merged == {
        "latency_ms": 12,
        "_rewards": rewards,
        "_reward_details": details,
    }


def test_merged_result_discards_task_authored_reward_keys():
    # _rewards/_reward_details are oddish-owned envelopes; a task metric of the
    # same name must never impersonate them (same rule as _verifier).
    merged = merged_trial_result(
        {"latency_ms": 12, "_rewards": {"spoof": 1.0}, "_reward_details": {}},
        None,
        None,
    )
    assert merged == {"latency_ms": 12}


def test_sanitize_task_result_strips_reserved_reward_keys():
    assert sanitize_task_result(
        {"ok": 1, "_rewards": {"a": 1.0}, "_reward_details": {}, "_verifier": {}}
    ) == {"ok": 1}


def test_outcome_reward_fields_default_none():
    outcome = HarborOutcome(
        reward=1.0,
        error=None,
        exit_code=0,
        duration_sec=1.0,
        job_result_path=None,
        job_dir=None,
    )
    assert outcome.rewards is None
    assert outcome.reward_details is None
