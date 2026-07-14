from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harbor.models.job.config import JobConfig  # noqa: E402
from harbor.models.trial.config import TaskConfig, TrialConfig  # noqa: E402

from oddish.cli.api import trial_result_to_import_spec  # noqa: E402
from oddish.core.harbor_artifacts import (  # noqa: E402
    detect_trajectory,
    extract_trajectory_metrics,
)
from oddish.core.ingest.zip_imports import _task_name_from_harbor_model  # noqa: E402
from oddish.core.ingest.zip_imports import _task_name_from_legacy_json  # noqa: E402
from oddish.integrations.sauron.s3_uploader import SauronS3Uploader  # noqa: E402


def test_shared_trajectory_helpers_read_atif_metrics(tmp_path):
    agent_dir = tmp_path / "trial" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "final_metrics": {
                    "total_prompt_tokens": 11,
                    "total_completion_tokens": 7,
                    "total_cached_tokens": 3,
                    "total_steps": 5,
                    "total_cost_usd": 0.42,
                },
                "steps": [{"step_id": index} for index in range(99)],
            }
        ),
        encoding="utf-8",
    )

    metrics = extract_trajectory_metrics(tmp_path)

    assert detect_trajectory(tmp_path) is True
    assert metrics.input_tokens == 11
    assert metrics.output_tokens == 7
    assert metrics.cache_tokens == 3
    assert metrics.total_steps == 5
    assert metrics.cost_usd == 0.42


def test_shared_trajectory_helpers_ignore_bad_cost(tmp_path):
    agent_dir = tmp_path / "trial" / "agent"
    agent_dir.mkdir(parents=True)
    (agent_dir / "trajectory.json").write_text(
        json.dumps(
            {
                "final_metrics": {
                    "total_prompt_tokens": 11,
                    "total_cost_usd": "not-a-number",
                }
            }
        ),
        encoding="utf-8",
    )

    metrics = extract_trajectory_metrics(tmp_path)

    assert metrics.input_tokens == 11
    assert metrics.cost_usd is None


def test_trial_import_spec_reuses_shared_extraction(tmp_path):
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "trajectory.json").write_text(
        json.dumps({"steps": [{"step_id": "a"}, {"step_id": "b"}]}),
        encoding="utf-8",
    )
    verifier_dir = tmp_path / "verifier"
    verifier_dir.mkdir()
    (verifier_dir / "metrics.json").write_text(
        json.dumps({"latency_ms": 12.5}), encoding="utf-8"
    )
    (verifier_dir / "ctrf.json").write_text(
        json.dumps(
            {
                "results": {
                    "tool": {"name": "pytest"},
                    "summary": {
                        "tests": 3,
                        "passed": 2,
                        "failed": 1,
                        "skipped": 0,
                        "pending": 0,
                        "other": 0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    trial_result = SimpleNamespace(
        id="trial-id",
        agent_info=SimpleNamespace(
            name="claude-code",
            model_info=SimpleNamespace(provider="anthropic", name="claude-sonnet"),
        ),
        config=SimpleNamespace(
            agent=SimpleNamespace(model_name="anthropic/claude-sonnet")
        ),
        verifier_result=SimpleNamespace(rewards={"reward": 1.0}),
        exception_info=None,
        agent_result=SimpleNamespace(
            is_empty=lambda: False,
            n_input_tokens=10,
            n_cache_tokens=4,
            n_output_tokens=6,
            cost_usd=None,
        ),
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
        started_at=None,
        finished_at=None,
    )

    spec = trial_result_to_import_spec(
        trial_result,
        has_trajectory=detect_trajectory(tmp_path),
        artifact_dir=tmp_path,
    )

    assert spec["status"] == "success"
    assert spec["reward"] == 1.0
    assert spec["model"] == "anthropic/claude-sonnet"
    assert spec["input_tokens"] == 10
    assert spec["output_tokens"] == 6
    assert spec["total_steps"] == 2
    assert spec["has_trajectory"] is True
    assert spec["result"] == {
        "latency_ms": 12.5,
        "_verifier": {
            "format": "ctrf",
            "tests": 3,
            "passed": 2,
            "failed": 1,
            "skipped": 0,
            "pending": 0,
            "other": 0,
            "report_path": "verifier/ctrf.json",
            "tool": "pytest",
        },
    }


def test_trial_import_spec_treats_non_numeric_reward_as_missing():
    trial_result = SimpleNamespace(
        id="trial-id",
        agent_info=SimpleNamespace(name="claude-code", model_info=None),
        config=SimpleNamespace(agent=SimpleNamespace(model_name=None)),
        verifier_result=SimpleNamespace(rewards={"score": "not-a-number"}),
        exception_info=None,
        agent_result=None,
        environment_setup=None,
        agent_setup=None,
        agent_execution=None,
        verifier=None,
        started_at=None,
        finished_at=None,
    )

    spec = trial_result_to_import_spec(trial_result)

    assert spec["status"] == "failed"
    assert spec["reward"] is None


def test_task_name_inference_prefers_harbor_config_models(tmp_path):
    job_config = JobConfig(tasks=[TaskConfig(path=tmp_path / "my-task")])
    trial_config = TrialConfig(task=TaskConfig(name="org/package-task"))

    assert _task_name_from_harbor_model(job_config.model_dump(mode="json")) == "my-task"
    assert (
        _task_name_from_harbor_model(trial_config.model_dump(mode="json"))
        == "package-task"
    )


def test_task_name_inference_keeps_nested_legacy_json():
    data = {
        "outer": {
            "inner": [
                {
                    "payload": {
                        "task": {
                            "task_path": "/tmp/harbor/tasks/nested-task",
                        }
                    }
                }
            ]
        }
    }

    assert _task_name_from_legacy_json(data) == "nested-task"


def test_sauron_trial_subdir_uses_job_scanner(tmp_path):
    job_dir = tmp_path / "jobs" / "my-job"
    scanner_trial = job_dir / "plain-trial-name"
    heuristic_only = job_dir / "task-name__abc123"
    scanner_trial.mkdir(parents=True)
    heuristic_only.mkdir()
    (scanner_trial / "result.json").write_text("{}", encoding="utf-8")

    assert SauronS3Uploader._find_trial_subdir(job_dir) == scanner_trial


def test_sauron_trial_subdir_keeps_single_child_fallback(tmp_path):
    job_dir = tmp_path / "jobs" / "my-job"
    only_child = job_dir / "legacy-output"
    only_child.mkdir(parents=True)

    assert SauronS3Uploader._find_trial_subdir(job_dir) == only_child
