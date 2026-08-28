"""Trial task-preparation failures use the normal attempt settlement path."""

from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from oddish.db import TrialStatus
from oddish.workers.queue import trial_handler


@pytest.mark.asyncio
async def test_analysis_probe_env_pins_task_version(monkeypatch, tmp_path):
    temp_root = tmp_path / "download"
    source_task = temp_root / "task"
    source_task.mkdir(parents=True)
    prepared = trial_handler.PreparedTrialRun(
        task_path=str(source_task),
        task_s3_key="tasks/task-1/v7.tar.gz",
        task_id="task-1",
        trial_agent="claude-code",
        trial_model="anthropic/claude-sonnet-4-6",
        trial_environment="docker",
        trial_harbor_config={
            "extra_instructions": "brief",
            "analysis_payload": {
                "trial_ids": ["source-1"],
                "with_verdict": False,
            },
        },
        trial_kind="qa_eval",
        task_version=7,
        org_id="org-1",
    )

    async def resolve_task_directory(**_kwargs):
        return source_task, temp_root, prepared.task_s3_key

    async def mint_probe_creds(**_kwargs):
        return "key-1", {"ODDISH_API_KEY": "secret"}

    monkeypatch.setattr(trial_handler, "resolve_task_directory", resolve_task_directory)
    monkeypatch.setattr(trial_handler, "apply_analysis_overlay", lambda *_a, **_k: None)
    monkeypatch.setattr(trial_handler, "enable_local_internet", lambda *_a: None)
    monkeypatch.setattr(trial_handler, "mint_probe_creds", mint_probe_creds)

    task = await trial_handler._prepare_trial_task(
        trial_id="task-1-4", prepared_trial=prepared
    )

    assert task.probe_agent_env["ODDISH_PROBE_TASK_ID"] == "task-1"
    assert task.probe_agent_env["ODDISH_PROBE_TASK_VERSION"] == "7"


@pytest.mark.asyncio
async def test_summarize_materialization_failure_removes_task_copy(
    monkeypatch, tmp_path
):
    source_task = tmp_path / "source-task"
    source_task.mkdir()
    (source_task / "instruction.md").write_text("solve")
    prepared = trial_handler.PreparedTrialRun(
        task_path=str(source_task),
        task_s3_key=None,
        task_id="task-1",
        trial_agent="single-llm",
        trial_model="anthropic/claude-sonnet-4-6",
        trial_environment="docker",
        trial_harbor_config={
            "extra_instructions": "materialized at pickup",
        },
        trial_kind="summarize",
        org_id="org-1",
    )
    copy_root = tmp_path / "prepared-copy"
    copy_root.mkdir()

    async def resolve_task_directory(**_kwargs):
        return source_task, None, None

    async def materialize_summarize_brief(_harbor_config):
        raise ValueError("summarize target trajectory is missing")

    from oddish.workers import analysis_trials

    monkeypatch.setattr(trial_handler, "resolve_task_directory", resolve_task_directory)
    monkeypatch.setattr(
        trial_handler.tempfile, "mkdtemp", lambda **_kwargs: str(copy_root)
    )
    monkeypatch.setattr(
        analysis_trials,
        "materialize_summarize_brief",
        materialize_summarize_brief,
    )

    with pytest.raises(ValueError, match="target trajectory is missing"):
        await trial_handler._prepare_trial_task(
            trial_id="task-1-4", prepared_trial=prepared
        )

    assert source_task.exists()
    assert not copy_root.exists()


@pytest.mark.asyncio
async def test_task_copy_failure_removes_owned_temp_root(monkeypatch, tmp_path):
    source_task = tmp_path / "source-task"
    source_task.mkdir()
    prepared = trial_handler.PreparedTrialRun(
        task_path=str(source_task),
        task_s3_key=None,
        task_id="task-1",
        trial_agent="single-llm",
        trial_model="anthropic/claude-sonnet-4-6",
        trial_environment="docker",
        trial_harbor_config={"extra_instructions": "prepare a writable copy"},
        org_id="org-1",
    )
    copy_root = tmp_path / "failed-copy"

    async def resolve_task_directory(**_kwargs):
        return source_task, None, None

    def make_temp_root(**_kwargs):
        copy_root.mkdir()
        return str(copy_root)

    def fail_copy(*_args, **_kwargs):
        raise OSError("task copy failed")

    monkeypatch.setattr(trial_handler, "resolve_task_directory", resolve_task_directory)
    monkeypatch.setattr(trial_handler.tempfile, "mkdtemp", make_temp_root)
    monkeypatch.setattr(trial_handler.shutil, "copytree", fail_copy)

    with pytest.raises(OSError, match="task copy failed"):
        await trial_handler._prepare_trial_task(
            trial_id="task-1-4", prepared_trial=prepared
        )

    assert source_task.exists()
    assert not copy_root.exists()


@pytest.mark.asyncio
async def test_run_trial_job_settles_task_preparation_error(monkeypatch):
    trial_id = "task-1-4"
    prepared = trial_handler.PreparedTrialRun(
        task_path="/unused",
        task_s3_key=None,
        task_id="task-1",
        trial_agent="single-llm",
        trial_model="anthropic/claude-sonnet-4-6",
        trial_environment="docker",
        trial_harbor_config={"extra_instructions": "materialized at pickup"},
        trial_kind="summarize",
        org_id="org-1",
        trial_attempt=2,
    )

    @asynccontextmanager
    async def trial_session(_trial_id, **_kwargs):
        yield (
            SimpleNamespace(),
            SimpleNamespace(
                id=trial_id,
                status=TrialStatus.RUNNING,
                agent="single-llm",
                idempotency_key=None,
            ),
        )

    async def prepare_trial_run(**_kwargs):
        return prepared

    async def prepare_claimed_trial_attempt(**_kwargs):
        raise ValueError("summarize target trajectory is missing")

    settlements = []

    async def settle_trial_attempt(**kwargs):
        settlements.append(kwargs)
        return False

    monkeypatch.setattr(trial_handler, "_trial_session", trial_session)
    monkeypatch.setattr(trial_handler, "_prepare_trial_run", prepare_trial_run)
    monkeypatch.setattr(
        trial_handler,
        "_prepare_claimed_trial_attempt",
        prepare_claimed_trial_attempt,
    )
    monkeypatch.setattr(trial_handler, "_settle_trial_attempt", settle_trial_attempt)

    await trial_handler.run_trial_job(
        trial_id,
        "anthropic/claude-sonnet-4-6",
        worker_id="worker-1",
        worker_job_id="job-1",
        worker_job_attempt=2,
    )

    assert len(settlements) == 1
    settlement = settlements[0]
    assert settlement["trial_id"] == trial_id
    assert settlement["prepared_trial"] is prepared
    assert settlement["execution"].outcome is None
    assert settlement["execution"].retryable is True
    assert settlement["execution"].execution_error == (
        "ValueError: summarize target trajectory is missing"
    )
