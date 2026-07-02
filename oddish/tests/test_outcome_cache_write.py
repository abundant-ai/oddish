"""Tests for cache_write_tokens extraction from ATIF trajectory data."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from oddish.core.harbor_artifacts import (
    _cache_write_from_final_metrics,
    _sum_cache_write_from_steps,
    extract_trajectory_metrics,
)


def test_sum_cache_write_from_steps_anthropic_key() -> None:
    steps = [
        {"metrics": {"extra": {"cache_creation_input_tokens": 100}}},
        {"metrics": {"extra": {"cache_creation_input_tokens": 200}}},
    ]
    assert _sum_cache_write_from_steps(steps) == 300


def test_sum_cache_write_from_steps_kimi_key() -> None:
    steps = [
        {"metrics": {"extra": {"input_cache_creation": 50}}},
        {"metrics": {"extra": {"input_cache_creation": 75}}},
    ]
    assert _sum_cache_write_from_steps(steps) == 125


def test_sum_cache_write_from_steps_cursor_key() -> None:
    steps = [
        {"metrics": {"extra": {"cacheWriteTokens": 400}}},
    ]
    assert _sum_cache_write_from_steps(steps) == 400


def test_sum_cache_write_from_steps_snake_key() -> None:
    steps = [
        {"metrics": {"extra": {"cache_write_tokens": 10}}},
        {"metrics": {"extra": {"cache_write_tokens": 20}}},
    ]
    assert _sum_cache_write_from_steps(steps) == 30


def test_sum_cache_write_from_steps_no_key_returns_none() -> None:
    steps = [
        {"metrics": {"extra": {"other_field": 999}}},
        {"metrics": {}},
        {},
    ]
    assert _sum_cache_write_from_steps(steps) is None


def test_cache_write_from_final_metrics() -> None:
    fm = {"extra": {"cache_creation_input_tokens": 500}}
    assert _cache_write_from_final_metrics(fm) == 500


def test_cache_write_from_final_metrics_missing_returns_none() -> None:
    assert _cache_write_from_final_metrics({}) is None
    assert _cache_write_from_final_metrics({"extra": {"other": 1}}) is None
    assert _cache_write_from_final_metrics({"extra": "not-a-dict"}) is None


def test_extract_trajectory_metrics_reads_cache_write(tmp_path: Path) -> None:
    traj = {
        "final_metrics": {
            "total_prompt_tokens": 1000,
            "total_completion_tokens": 200,
            "total_cached_tokens": 300,
            "total_steps": 5,
            "total_cost_usd": 0.01,
        },
        "steps": [
            {"metrics": {"extra": {"cache_creation_input_tokens": 150}}},
            {"metrics": {"extra": {"cache_creation_input_tokens": 250}}},
        ],
    }
    traj_file = tmp_path / "trajectory.json"
    traj_file.write_text(json.dumps(traj))

    metrics = extract_trajectory_metrics(tmp_path)
    assert metrics.input_tokens == 1000
    assert metrics.output_tokens == 200
    assert metrics.cache_tokens == 300
    assert metrics.total_steps == 5
    assert metrics.cost_usd == pytest.approx(0.01)
    assert metrics.cache_write_tokens == 400  # 150 + 250


def test_extract_trajectory_metrics_falls_back_to_final_metrics(tmp_path: Path) -> None:
    traj = {
        "final_metrics": {
            "total_prompt_tokens": 500,
            "total_completion_tokens": 100,
            "total_cached_tokens": 0,
            "total_steps": 3,
            "extra": {"cache_creation_input_tokens": 600},
        },
        "steps": [
            {"metrics": {"extra": {"other": 1}}},
        ],
    }
    traj_file = tmp_path / "trajectory.json"
    traj_file.write_text(json.dumps(traj))

    assert extract_trajectory_metrics(tmp_path).cache_write_tokens == 600


def test_extract_trajectory_metrics_no_cache_write_returns_none(tmp_path: Path) -> None:
    traj = {
        "final_metrics": {
            "total_prompt_tokens": 100,
            "total_completion_tokens": 50,
        },
        "steps": [{"metrics": {"extra": {"unrelated": 1}}}],
    }
    traj_file = tmp_path / "trajectory.json"
    traj_file.write_text(json.dumps(traj))

    assert extract_trajectory_metrics(tmp_path).cache_write_tokens is None
