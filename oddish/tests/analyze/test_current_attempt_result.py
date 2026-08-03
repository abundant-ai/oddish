"""A retried trial's downloaded tree holds one job dir per attempt.

Harbor mints a fresh ``task-<slug>__<id>`` directory per attempt, and every
attempt re-uploads into the same S3 prefix without clearing it, so the
classifier sees every attempt side by side. Only the last attempt overwrites
the job-level ``result.json``; picking any other dir classifies a stale
attempt and can label a passing trial HARNESS_ERROR.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from oddish.analyze.classifier import (  # noqa: E402
    _attempt_dirs_named_by,
    _current_attempt_result,
)


def _job_dir(trial_dir: Path, name: str, payload: dict) -> Path:
    job = trial_dir / name
    job.mkdir(parents=True)
    (job / "result.json").write_text(json.dumps(payload))
    return job


def _root_reward(dir_name: str, reward: str = "1.0") -> dict:
    return {
        "n_total_trials": 1,
        "stats": {
            "evals": {
                "grok-build__m__adhoc": {
                    "reward_stats": {"reward": {reward: [dir_name]}},
                    "exception_stats": {},
                }
            }
        },
    }


def _root_exception(dir_name: str, exc: str = "EnvironmentStartTimeoutError") -> dict:
    return {
        "n_total_trials": 1,
        "stats": {
            "evals": {
                "grok-build__m__adhoc": {
                    "reward_stats": {},
                    "exception_stats": {exc: [dir_name]},
                }
            }
        },
    }


def test_picks_the_dir_the_job_result_names_not_the_first(monkeypatch, tmp_path):
    """The regression, pinned against an adversarial directory order.

    ``iterdir`` yields in filesystem order, so the old "first ``task-*`` dir
    wins" scan picked an arbitrary attempt. Force the stale attempt to come
    first: only a scan that consults the job-level result gets this right.
    """
    _job_dir(tmp_path, "task-x__AAAA", {"verifier_result": None})
    _job_dir(
        tmp_path, "task-x__ZZZZ", {"verifier_result": {"rewards": {"reward": 1.0}}}
    )

    real_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda self: iter(sorted(real_iterdir(self))))

    picked = _current_attempt_result(tmp_path, _root_reward("task-x__ZZZZ"))

    assert picked == tmp_path / "task-x__ZZZZ" / "result.json"


def test_named_dir_wins_for_an_errored_final_attempt(tmp_path):
    _job_dir(
        tmp_path, "task-x__AAAA", {"verifier_result": {"rewards": {"reward": 1.0}}}
    )
    _job_dir(tmp_path, "task-x__BBBB", {"exception_info": "boom"})

    picked = _current_attempt_result(tmp_path, _root_exception("task-x__BBBB"))

    assert picked == tmp_path / "task-x__BBBB" / "result.json"


def test_single_attempt_is_unchanged(tmp_path):
    _job_dir(tmp_path, "task-x__ONLY", {"verifier_result": None})

    picked = _current_attempt_result(tmp_path, _root_reward("task-x__ONLY"))

    assert picked == tmp_path / "task-x__ONLY" / "result.json"


def test_falls_back_deterministically_when_the_job_result_names_nothing(tmp_path):
    """Older layouts name no dirs; the pick must still be stable, not readdir order."""
    _job_dir(tmp_path, "task-x__BBBB", {})
    _job_dir(tmp_path, "task-x__AAAA", {})

    picked = _current_attempt_result(tmp_path, {"n_total_trials": 1, "stats": {}})

    assert picked == tmp_path / "task-x__AAAA" / "result.json"


def test_ignores_dirs_without_a_result(tmp_path):
    (tmp_path / "task-x__AAAA").mkdir()
    _job_dir(tmp_path, "task-x__BBBB", {})

    picked = _current_attempt_result(tmp_path, {"n_total_trials": 1, "stats": {}})

    assert picked == tmp_path / "task-x__BBBB" / "result.json"


def test_no_job_dirs_yields_none(tmp_path):
    assert _current_attempt_result(tmp_path, _root_reward("task-x__AAAA")) is None


def test_named_dirs_survive_both_stat_shapes():
    payload = {
        "stats": {
            "evals": {
                "e1": {
                    "reward_stats": {"reward": {"1.0": ["a"], "0.0": ["b"]}},
                    "exception_stats": {"Boom": ["c"]},
                },
                "e2": {"reward_stats": {}, "exception_stats": {}},
            }
        }
    }

    assert _attempt_dirs_named_by(payload) == {"a", "b", "c"}


def test_malformed_job_result_names_nothing():
    for payload in ({}, {"stats": None}, {"stats": {"evals": []}}):
        assert _attempt_dirs_named_by(payload) == set()
