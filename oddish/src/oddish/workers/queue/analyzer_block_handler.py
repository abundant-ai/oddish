"""Queue execution for one declaratively persisted analyzer run."""

from __future__ import annotations

import asyncio

from oddish.blocks.analyzer.analyzer_block import (
    AnalyzerBlock,
    AnalyzerInput,
    AnalyzerType,
)
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType, SandboxConfig
from oddish.db import AnalyzerRunModel, JobStatus, PromptVersionModel, get_session
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
) -> tuple[str | None, str | None, str | None]:
    """The ``(analyzer_id, task_id, attribution_org_id)`` a lifecycle block needs.

    Lifecycle-typed runs land in the same cost/lineage paths as the built-in
    blocks, which assume a concrete subject: a POST_TRIAL block attributes its
    spend to the trial named by ``analyzer_id`` (mirroring the built-in
    post-trial classifier) and carries ``task_id`` for lineage; a PRE_TRIAL
    block attributes to its task. Both leave ``attribution_org_id`` unset so
    ``AnalyzerBlock._cost_attribution`` resolves that subject instead of
    short-circuiting on the org. CUSTOM_QA keeps the ad-hoc-run behavior:
    ``analyzer_id`` is the run id and spend attributes to the org.

    ``run.scope_id`` is the fallback for runs enqueued before ``run_config``
    carried these keys (post-trial scope is the trial, pre-trial the task).
    """
    if analyzer_type is AnalyzerType.POST_TRIAL:
        return config.get("trial_id") or run.scope_id, config.get("task_id"), None
    if analyzer_type is AnalyzerType.PRE_TRIAL:
        return None, config.get("task_id") or run.scope_id, None
    return run.id, None, run.org_id


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
            analyzer_id, task_id, attribution_org_id = _subject_linkage(
                analyzer_type, run, config
            )
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
        result = await block.run()
    except BaseException as exc:
        async with get_session() as session:
            run = await session.get(
                AnalyzerRunModel, analyzer_run_id, with_for_update=True
            )
            if run is not None:
                run.status = JobStatus.FAILED
                run.error = block.error or repr(exc)
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


__all__ = ["MissingPromptVersionError", "run_analyzer_block_job"]
