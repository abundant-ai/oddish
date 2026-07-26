"""Versioned ad-hoc QA runs queued as AnalyzerBlock jobs."""

from __future__ import annotations

import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.config import settings
from oddish.core.prompts import resolve_prompt_core
from oddish.db.models import (
    ExperimentModel,
    JobStatus,
    PromptModel,
    PromptVersionModel,
    AnalyzerRunModel,
    TaskModel,
    TrialModel,
)
from oddish.schemas import CustomQARunRequest, CustomQARunResponse
from oddish.workers.jobs import EnqueueRequest, enqueue_worker_job
from oddish.db.models import WorkerJobKind


def _run_response(
    run: AnalyzerRunModel, *, prompt: PromptModel, version: PromptVersionModel
) -> CustomQARunResponse:
    return CustomQARunResponse(
        id=run.id,
        prompt_kind=prompt.kind,
        prompt_version=version.version,
        prompt_version_id=version.id,
        analyzer_block_id=run.analyzer_block_id or "",
        scope_type=run.scope_type,
        scope_id=run.scope_id,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        backend=(
            "sandbox" if run.llm_client_type == LLMClientType.SANDBOX.value else "api"
        ),
        status=run.status.value,
        output=run.output,
        error=run.error,
        run_config=run.run_config,
    )


async def get_custom_qa_run_core(
    session: AsyncSession, *, run_id: str, org_id: str | None
) -> CustomQARunResponse:
    row = (
        await session.execute(
            select(AnalyzerRunModel, PromptVersionModel, PromptModel)
            .join(
                PromptVersionModel,
                PromptVersionModel.id == AnalyzerRunModel.prompt_version_id,
            )
            .join(PromptModel, PromptModel.id == PromptVersionModel.prompt_id)
            .where(AnalyzerRunModel.id == run_id, AnalyzerRunModel.org_id == org_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="QA run not found")
    run, version, prompt = row
    return _run_response(run, prompt=prompt, version=version)


async def _validate_scope(
    session: AsyncSession, *, kind: str, id: str, org_id: str | None
) -> ExperimentModel | TaskModel | TrialModel:
    model = {"experiment": ExperimentModel, "task": TaskModel, "trial": TrialModel}[
        kind
    ]
    target = await session.scalar(
        select(model).where(model.id == id, model.org_id == org_id)
    )
    if target is None:
        raise HTTPException(
            status_code=404, detail=f"Unknown or inaccessible {kind}: {id}"
        )
    return target


def _assert_prompt_visible(prompt: PromptModel, *, org_id: str | None) -> None:
    """Mirror ``_assert_org_access`` in the prompts router: ``variant.kind`` is
    a free-form string that can also resolve as a prompt id, so a row scoped
    to another org must stay invisible here too."""
    if prompt.org_id and prompt.org_id != org_id:
        raise HTTPException(status_code=404, detail="Prompt not found")


async def _resolve_variant(
    session: AsyncSession,
    kind: str,
    *,
    version: int | None,
    org_id: str | None,
    user_id: str | None,
    scope_type: str,
    scope_id: str,
    target: ExperimentModel | TaskModel | TrialModel,
) -> tuple[PromptModel, PromptVersionModel]:
    """Resolve a QA variant using the target's complete available hierarchy."""
    domain_scope = {"experiment_id": None, "task_id": None, "trial_id": None}
    domain_scope[f"{scope_type}_id"] = scope_id
    if isinstance(target, TrialModel):
        domain_scope["task_id"] = target.task_id
        domain_scope["experiment_id"] = target.experiment_id
    prompt, latest = await resolve_prompt_core(
        session, kind, org_id=org_id, user_id=user_id, **domain_scope
    )
    if version is None:
        return prompt, latest
    versions = await prompt.awaitable_attrs.versions
    for v in versions:
        if v.version == version:
            return prompt, v
    raise HTTPException(
        status_code=404, detail=f"Prompt '{kind}' has no version {version}"
    )


async def run_custom_qa_core(
    session: AsyncSession,
    *,
    data: CustomQARunRequest,
    org_id: str | None,
    user_id: str | None,
    oddish_api_base_url: str | None = None,
) -> list[CustomQARunResponse]:
    target = await _validate_scope(
        session, kind=data.scope_type, id=data.scope_id, org_id=org_id
    )
    refs = [(v.kind.strip(), v.version) for v in data.variants]
    if len(set(refs)) != len(refs):
        raise HTTPException(status_code=400, detail="Prompt variants must be unique")
    if data.allow_oddish_cli and data.backend != "sandbox":
        raise HTTPException(
            status_code=400,
            detail="Oddish CLI access is only available to the sandbox backend",
        )

    client_type = (
        LLMClientType.SANDBOX if data.backend == "sandbox" else LLMClientType.API
    )
    pending: list[tuple[PromptModel, PromptVersionModel, AnalyzerRunModel]] = []
    for variant in data.variants:
        prompt, version = await _resolve_variant(
            session,
            variant.kind.strip(),
            version=variant.version,
            org_id=org_id,
            user_id=user_id,
            scope_type=data.scope_type,
            scope_id=data.scope_id,
            target=target,
        )
        _assert_prompt_visible(prompt, org_id=org_id)
        digest = hashlib.sha256(version.content.encode()).hexdigest()
        system_prompt = (
            "You are running an Oddish custom QA check. "
            f"The exact scope is {data.scope_type} {data.scope_id}. "
            "Treat the user prompt as the QA hypothesis and report concrete evidence."
        )
        if data.allow_oddish_cli:
            system_prompt += (
                " The oddish CLI is installed and authenticated in this ephemeral sandbox. "
                "It has read-only access and may be used only to inspect existing data. "
                "Do not submit or mutate any Oddish resource. Clearly report every command."
            )
        ref = f"{prompt.kind}@{version.version}"
        config = {
            "scope": {"type": data.scope_type, "id": data.scope_id},
            "prompt_id": prompt.id,
            "prompt_key": prompt.kind,
            "prompt_version": version.version,
            "prompt": {
                "id": prompt.id,
                "kind": prompt.kind,
                "version": version.version,
                "version_id": version.id,
                "sha256": digest,
            },
            "model": data.model,
            "reasoning_effort": data.reasoning_effort,
            "backend": data.backend,
            "oddish_cli_enabled": data.allow_oddish_cli,
            "oddish_api_scope": "read" if data.allow_oddish_cli else None,
            "oddish_cli_policy": (
                "read-only; inspection only; submissions and mutations forbidden"
                if data.allow_oddish_cli
                else None
            ),
            "oddish_api_base_url": (
                oddish_api_base_url if data.allow_oddish_cli else None
            ),
            "system_prompt": system_prompt,
            "command": (
                f"oddish qa {data.scope_type} {data.scope_id} "
                f"--variant {ref} --model {data.model}"
            ),
        }
        run = AnalyzerRunModel(
            org_id=org_id,
            prompt_version_id=version.id,
            triggered_by_user_id=user_id,
            scope_type=data.scope_type,
            scope_id=data.scope_id,
            model=data.model,
            reasoning_effort=data.reasoning_effort,
            llm_client_type=client_type.value,
            status=JobStatus.QUEUED,
            run_config=config,
        )
        session.add(run)
        await session.flush()
        await enqueue_worker_job(
            session,
            EnqueueRequest(
                kind=WorkerJobKind.ANALYZER_BLOCK,
                queue_key=settings.normalize_queue_key(data.model),
                payload={"analyzer_run_id": run.id},
                subject_table="analyzer_runs",
                subject_id=run.id,
                org_id=org_id,
            ),
        )
        pending.append((prompt, version, run))
    await session.commit()
    return [
        _run_response(run, prompt=prompt, version=version)
        for prompt, version, run in pending
    ]
