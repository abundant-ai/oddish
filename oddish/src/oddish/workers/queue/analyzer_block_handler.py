"""Queue execution for one declaratively persisted analyzer run."""

from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from sqlalchemy import select

from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType, SandboxConfig
from oddish.blocks.analyzer.claude_cli_client import CliConfig
from oddish.blocks.analyzer.pre_trial.pre_trial_block import PreTrialBlock
from oddish.config import settings
from oddish.db import AnalyzerRunModel, JobStatus, PromptVersionModel, get_session
from oddish.db.models import TaskModel, TaskVersionModel
from oddish.db.storage import resolve_task_directory
from oddish.workers.queue.worker_job_single_job import heartbeat_worker_job

_HEARTBEAT_INTERVAL_SECONDS = 30


class MissingPromptVersionError(RuntimeError):
    """The immutable prompt version referenced by an analyzer run is gone."""


def _analyzer_type_for_config(config: dict) -> AnalyzerType:
    stage = config.get("stage")
    if config.get("automatic") and stage in {
        AnalyzerType.PRE_TRIAL.value,
        AnalyzerType.POST_TRIAL.value,
    }:
        return AnalyzerType(stage)
    return AnalyzerType.CUSTOM_QA


def _subject_linkage(
    analyzer_type: AnalyzerType, run: AnalyzerRunModel, config: dict
) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """The subject columns a block needs: ``(analyzer_id, task_id,
    attribution_org_id, subject_type, subject_id)``.

    Lifecycle-typed runs land in the same cost/lineage paths as the built-in
    blocks, which assume a concrete subject: a POST_TRIAL block attributes its
    spend to the trial named by ``analyzer_id`` (mirroring the built-in
    post-trial classifier) and carries ``task_id`` for lineage; a PRE_TRIAL
    block attributes to its task. Both leave ``attribution_org_id`` unset so
    ``AnalyzerBlock._cost_attribution`` resolves that subject instead of
    short-circuiting on the org.

    CUSTOM_QA keeps the ad-hoc-run behavior: ``analyzer_id`` is the run id and
    spend attributes to the org, but the run's own ``scope_type``/``scope_id``
    is passed as an explicit ``subject_type``/``subject_id`` so the cost row is
    not left scope-less (the generic runner always sets an org override, which
    would otherwise short-circuit resolution).

    ``run.scope_id`` is the fallback for lifecycle runs enqueued before
    ``run_config`` carried these keys (post-trial scope is the trial, pre-trial
    the task).
    """
    if analyzer_type is AnalyzerType.POST_TRIAL:
        return (
            config.get("trial_id") or run.scope_id,
            config.get("task_id"),
            None,
            None,
            None,
        )
    if analyzer_type is AnalyzerType.PRE_TRIAL:
        return None, config.get("task_id") or run.scope_id, None, None, None
    return run.id, None, run.org_id, run.scope_type, run.scope_id


async def _resolve_task_source(
    task_id: str, task_version_id: str | None
) -> tuple[str | None, str | None]:
    """The task source location (S3 key / local path).

    Pins to the specific audited version when the assignment run carries one
    (mirroring the built-in synth), so a re-upload between enqueue and execution
    can't swap the source out from under the older version's event. Falls back
    to the task's latest-version mirror for runs enqueued before
    ``task_version_id`` was recorded on the config.
    """
    async with get_session() as session:
        if task_version_id:
            row = (
                await session.execute(
                    select(
                        TaskVersionModel.task_s3_key, TaskVersionModel.task_path
                    ).where(TaskVersionModel.id == task_version_id)
                )
            ).first()
        else:
            row = (
                await session.execute(
                    select(TaskModel.task_s3_key, TaskModel.task_path).where(
                        TaskModel.id == task_id
                    )
                )
            ).first()
    if row is None:
        raise RuntimeError(f"task source not found for pre-trial QA (task {task_id})")
    return row.task_s3_key, row.task_path


async def _build_pre_trial_cli_block(
    *,
    task_id: str,
    task_version_id: str | None,
    prompt_content: str,
    trial_id: str | None,
    model: str,
    triggered_by_user_id: str | None,
    block_metadata: dict,
) -> tuple[AnalyzerBlock, Path | None]:
    """Build a pre-trial assignment's block on the worker-local CLAUDE_CLI
    backend, the same way the built-in synth does: the worker downloads the task
    source and the agent reads it with Read/Glob. Pre-trial audits static source,
    so it never needs a sandbox regardless of the assignment's stored backend.
    Returns the block plus the temp dir to clean up once it has run.
    """
    task_s3_key, task_path = await _resolve_task_source(task_id, task_version_id)
    task_dir, temp_task_dir, _ = await resolve_task_directory(
        task_id, task_s3_key=task_s3_key, task_path=task_path
    )
    try:
        block_obj = PreTrialBlock(
            task_id=task_id,
            trial_ids=[trial_id] if trial_id else [],
            prompt_template=prompt_content,
        )
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.PRE_TRIAL,
            llm_client_type=LLMClientType.CLAUDE_CLI,
            input=AnalyzerInput(input={"task_id": task_id}),
            prompt=block_obj.build_prompt(),
            task_id=task_id,
            model=model,
            triggered_by_user_id=triggered_by_user_id,
            # The CLI yields a --output-format json envelope, not the model's
            # bare answer; this unwraps it before schema validation.
            output_transform=block_obj.to_action_items_from_cli,
            cli_config=CliConfig(cwd=task_dir, timeout=settings.pre_trial_timeout),
            block_metadata=block_metadata,
        )
    except BaseException:
        # The block never reaches the caller, so its finally won't see this temp
        # dir -- drop it here rather than leak the download on a build failure.
        if temp_task_dir is not None:
            shutil.rmtree(temp_task_dir, ignore_errors=True)
        raise
    return block, temp_task_dir


async def _heartbeat(worker_job_id: str, stop: asyncio.Event) -> None:
    while True:
        try:
            await asyncio.wait_for(stop.wait(), timeout=_HEARTBEAT_INTERVAL_SECONDS)
        except TimeoutError:
            try:
                await heartbeat_worker_job(worker_job_id)
            except Exception:
                # Heartbeats are advisory; the block result remains primary.
                pass
        if stop.is_set():
            return


async def run_analyzer_block_job(
    analyzer_run_id: str, *, worker_job_id: str | None = None
) -> None:
    """Reconstruct and execute the AnalyzerBlock described by an analyzer run."""
    missing_version_error: str | None = None
    temp_task_dir: Path | None = None
    block: AnalyzerBlock | None = None
    pre_trial_build: dict | None = None
    async with get_session() as session:
        run = await session.get(AnalyzerRunModel, analyzer_run_id, with_for_update=True)
        if run is None:
            raise RuntimeError(f"Analyzer run {analyzer_run_id} not found")
        version = await session.get(PromptVersionModel, run.prompt_version_id)
        if version is None:
            missing_version_error = f"Prompt version {run.prompt_version_id} not found"
            run.status = JobStatus.FAILED
            run.error = missing_version_error
        else:
            config = dict(run.run_config or {})
            oddish_cli_enabled = bool(config.get("oddish_cli_enabled"))
            client_type = LLMClientType(run.llm_client_type)
            stage = config.get("stage")
            analyzer_type = _analyzer_type_for_config(config)
            (
                analyzer_id,
                task_id,
                attribution_org_id,
                subject_type,
                subject_id,
            ) = _subject_linkage(analyzer_type, run, config)
            if analyzer_type is AnalyzerType.PRE_TRIAL:
                # Pre-trial audits static task source worker-local (CLAUDE_CLI,
                # Read/Glob over the downloaded source) like the built-in synth --
                # never a sandbox, whatever backend the assignment stored, charged
                # to the task per _subject_linkage. Defer the build, which
                # downloads the source from S3, to outside this row lock so the
                # analyzer_runs FOR UPDATE isn't held open across a network fetch;
                # the block id + RUNNING are recorded once it is built.
                pre_trial_build = {
                    "task_id": task_id,
                    "task_version_id": config.get("task_version_id"),
                    "prompt_content": version.content,
                    "trial_id": config.get("trial_id"),
                    "model": run.model,
                    "triggered_by_user_id": run.triggered_by_user_id,
                    "block_metadata": config,
                }
            else:
                sandbox_config = None
                if client_type == LLMClientType.SANDBOX:
                    sandbox_config = SandboxConfig(
                        install_oddish_cli=oddish_cli_enabled,
                        oddish_org_id=run.org_id if oddish_cli_enabled else None,
                        oddish_api_base_url=(
                            config.get("oddish_api_base_url")
                            if oddish_cli_enabled
                            else None
                        ),
                        oddish_api_scope="tasks" if oddish_cli_enabled else "read",
                        reasoning_effort=run.reasoning_effort,
                        session_id=stage or "custom-qa",
                    )

                block = AnalyzerBlock(
                    analyzer_type=analyzer_type,
                    llm_client_type=client_type,
                    input=AnalyzerInput(
                        input={
                            "scope": config.get("scope"),
                            "analyzer_run_id": run.id,
                        }
                    ),
                    prompt=version.content,
                    system_prompt=config.get("system_prompt"),
                    analyzer_id=analyzer_id,
                    task_id=task_id,
                    model=run.model,
                    triggered_by_user_id=run.triggered_by_user_id,
                    attribution_org_id=attribution_org_id,
                    # What the run is about. Lifecycle blocks resolve their
                    # subject via analyzer_id/task_id, so this is set only for
                    # custom QA -- without it the org override leaves its cost
                    # row scope-less.
                    subject_type=subject_type,
                    subject_id=subject_id,
                    sandbox_config=sandbox_config,
                    block_metadata=config,
                )
                run.analyzer_block_id = block.id
                run.status = JobStatus.RUNNING
                run.error = None

    # Raise only after get_session exits normally and commits the terminal
    # analyzer_runs state. Raising inside the context would roll it back.
    if missing_version_error is not None:
        raise MissingPromptVersionError(missing_version_error)

    heartbeat_stop = asyncio.Event()
    heartbeat_task = (
        asyncio.create_task(_heartbeat(worker_job_id, heartbeat_stop))
        if worker_job_id
        else None
    )
    try:
        if pre_trial_build is not None:
            # Download the task source and build the block outside the row lock;
            # the heartbeat keeps the worker job alive across the fetch, and a
            # build failure lands in the except below as a FAILED run.
            block, temp_task_dir = await _build_pre_trial_cli_block(**pre_trial_build)
            async with get_session() as session:
                run = await session.get(
                    AnalyzerRunModel, analyzer_run_id, with_for_update=True
                )
                if run is not None:
                    run.analyzer_block_id = block.id
                    run.status = JobStatus.RUNNING
                    run.error = None
        assert block is not None
        result = await block.run()
    except BaseException as exc:
        async with get_session() as session:
            run = await session.get(
                AnalyzerRunModel, analyzer_run_id, with_for_update=True
            )
            if run is not None:
                run.status = JobStatus.FAILED
                run.error = (block.error if block is not None else None) or repr(exc)
        raise
    else:
        async with get_session() as session:
            run = await session.get(
                AnalyzerRunModel, analyzer_run_id, with_for_update=True
            )
            if run is None:
                raise RuntimeError(
                    f"Analyzer run {analyzer_run_id} vanished after block execution"
                )
            run.status = JobStatus.SUCCESS
            run.output = result.output
            run.error = block.error
    finally:
        if heartbeat_task is not None:
            heartbeat_stop.set()
            await heartbeat_task
        if temp_task_dir is not None:
            shutil.rmtree(temp_task_dir, ignore_errors=True)


__all__ = ["MissingPromptVersionError", "run_analyzer_block_job"]
