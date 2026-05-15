from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oddish.cli.api import load_sweep_config  # noqa: E402
from oddish.core.sweeps import build_task_submission_from_sweep  # noqa: E402
from oddish.queue import _trial_starts_blocked_by_oracle_gate  # noqa: E402
from oddish.schemas import TaskSubmission, TaskSweepSubmission, TrialSpec  # noqa: E402


def test_oracle_failure_gate_blocks_only_non_baseline_agents():
    submission = TaskSubmission(
        task_path="/tmp/task",
        trials=[
            TrialSpec(agent="oracle"),
            TrialSpec(agent="nop"),
            TrialSpec(agent="codex", model="openai/gpt-5.2"),
        ],
        fail_all_if_oracle_fails=True,
    )

    oracle, nop, codex = submission.trials

    assert _trial_starts_blocked_by_oracle_gate(submission, oracle) is False
    assert _trial_starts_blocked_by_oracle_gate(submission, nop) is False
    assert _trial_starts_blocked_by_oracle_gate(submission, codex) is True


def test_oracle_failure_gate_is_inactive_without_oracle():
    submission = TaskSubmission(
        task_path="/tmp/task",
        trials=[TrialSpec(agent="codex", model="openai/gpt-5.2")],
        fail_all_if_oracle_fails=True,
    )

    assert (
        _trial_starts_blocked_by_oracle_gate(submission, submission.trials[0]) is False
    )


def test_sweep_submission_propagates_oracle_failure_gate():
    sweep = TaskSweepSubmission(
        task_id="task_123",
        configs=[
            {"agent": "oracle"},
            {"agent": "codex", "model": "openai/gpt-5.2"},
        ],
        fail_all_if_oracle_fails=True,
    )

    task_submission = build_task_submission_from_sweep(
        sweep,
        task_path="/tmp/task",
        trials=[
            TrialSpec(agent="oracle"),
            TrialSpec(agent="codex", model="openai/gpt-5.2"),
        ],
    )

    assert task_submission.fail_all_if_oracle_fails is True


def test_load_sweep_config_accepts_oracle_failure_gate(tmp_path):
    config_path = tmp_path / "sweep.yaml"
    config_path.write_text(
        """
fail_all_if_oracle_fails: true
agents:
  - name: oracle
    model_name: default
  - name: nop
    model_name: default
  - name: codex
    model_name: openai/gpt-5.2
""".lstrip()
    )

    config = load_sweep_config(config_path)

    assert config["fail_all_if_oracle_fails"] is True
