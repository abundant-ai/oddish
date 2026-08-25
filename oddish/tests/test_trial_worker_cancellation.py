from __future__ import annotations

import asyncio

import pytest

from oddish.workers.queue import trial_handler


@pytest.mark.asyncio
async def test_execute_trial_propagates_worker_cancellation(
    monkeypatch, tmp_path
) -> None:
    async def cancelled_run(**kwargs):
        raise asyncio.CancelledError

    shutdown_trials: list[str] = []

    async def shutdown(trial_id: str) -> int:
        shutdown_trials.append(trial_id)
        return 1

    monkeypatch.setattr(trial_handler, "run_harbor_trial_async", cancelled_run)
    monkeypatch.setattr(trial_handler.live_tail, "shutdown", shutdown)

    temp_task_dir = tmp_path / "download"
    task_dir = temp_task_dir / "task"
    task_dir.mkdir(parents=True)
    prepared = trial_handler.PreparedTrialRun(
        task_path=str(task_dir),
        task_s3_key=None,
        task_id="task-1",
        trial_agent="codex",
        trial_model="openai/gpt-5.5",
        trial_environment="archil",
        trial_harbor_config=None,
    )

    with pytest.raises(asyncio.CancelledError):
        await trial_handler._execute_trial(
            trial_id="trial-1",
            task_path_to_run=task_dir,
            temp_task_dir=temp_task_dir,
            prepared_trial=prepared,
            worker_id="worker-1",
        )

    assert shutdown_trials == ["trial-1"]
    assert not temp_task_dir.exists()
