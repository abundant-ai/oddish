"""Versioned ad-hoc QA runs backed by AnalyzerBlock."""

from __future__ import annotations

import asyncio
import hashlib

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.blocks.analyzer.analyzer_block import AnalyzerBlock, AnalyzerInput, AnalyzerType
from oddish.blocks.analyzer.analyzer_llm_client import LLMClientType
from oddish.core.prompts import get_prompt_core
from oddish.db.models import (
    ExperimentModel,
    JobStatus,
    PromptModel,
    PromptVersionModel,
    QARunModel,
    TaskModel,
    TrialModel,
)
from oddish.schemas import CustomQARunRequest, CustomQARunResponse


def _run_response(
    run: QARunModel, *, prompt: PromptModel, version: PromptVersionModel
) -> CustomQARunResponse:
    return CustomQARunResponse(
        id=run.id,
        prompt_key=prompt.key,
        prompt_version=version.version,
        prompt_version_id=version.id,
        analyzer_block_id=run.analyzer_block_id or "",
        scope_type=run.scope_type,
        scope_id=run.scope_id,
        model=run.model,
        reasoning_effort=run.reasoning_effort,
        backend=(
            "sandbox"
            if run.llm_client_type == LLMClientType.SANDBOX.value
            else "api"
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
            select(QARunModel, PromptVersionModel, PromptModel)
            .join(
                PromptVersionModel,
                PromptVersionModel.id == QARunModel.prompt_version_id,
            )
            .join(PromptModel, PromptModel.id == PromptVersionModel.prompt_id)
            .where(QARunModel.id == run_id, QARunModel.org_id == org_id)
        )
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="QA run not found")
    run, version, prompt = row
    return _run_response(run, prompt=prompt, version=version)


async def _validate_scope(
    session: AsyncSession, *, kind: str, id: str, org_id: str | None
) -> None:
    model = {"experiment": ExperimentModel, "task": TaskModel, "trial": TrialModel}[kind]
    exists = await session.scalar(
        select(model.id).where(model.id == id, model.org_id == org_id)
    )
    if not exists:
        raise HTTPException(
            status_code=404, detail=f"Unknown or inaccessible {kind}: {id}"
        )


async def run_custom_qa_core(
    session: AsyncSession,
    *,
    data: CustomQARunRequest,
    org_id: str | None,
    user_id: str | None,
    runtime_env: dict[str, str] | None = None,
) -> list[CustomQARunResponse]:
    await _validate_scope(
        session, kind=data.scope_type, id=data.scope_id, org_id=org_id
    )
    refs = [(v.key.strip(), v.version) for v in data.variants]
    if len(set(refs)) != len(refs):
        raise HTTPException(status_code=400, detail="Prompt variants must be unique")
    if data.allow_credential_forwarding and data.backend != "sandbox":
        raise HTTPException(
            status_code=400,
            detail="Credential forwarding is only available to the sandbox backend",
        )

    client_type = (
        LLMClientType.SANDBOX if data.backend == "sandbox" else LLMClientType.API
    )
    pending: list[tuple[PromptModel, PromptVersionModel, QARunModel, AnalyzerBlock]] = []
    for variant in data.variants:
        prompt, version = await get_prompt_core(
            session, variant.key.strip(), version=variant.version
        )
        digest = hashlib.sha256(version.content.encode()).hexdigest()
        system_prompt = (
            "You are running an Oddish custom QA check. "
            f"The exact scope is {data.scope_type} {data.scope_id}. "
            "Treat the user prompt as the QA hypothesis and report concrete evidence."
        )
        if data.allow_credential_forwarding:
            system_prompt += (
                " The oddish CLI is installed and authenticated in this ephemeral sandbox. "
                "You may execute it to inspect or submit runs, including oracle/nop and "
                "lazy-degenerate solution checks. Clearly report every command and created ID."
            )
        ref = f"{prompt.key}@{version.version}"
        config = {
            "scope": {"type": data.scope_type, "id": data.scope_id},
            "prompt": {
                "id": prompt.id,
                "key": prompt.key,
                "version": version.version,
                "version_id": version.id,
                "sha256": digest,
            },
            "model": data.model,
            "reasoning_effort": data.reasoning_effort,
            "backend": data.backend,
            "credential_forwarding": bool(data.allow_credential_forwarding),
            "system_prompt": system_prompt,
            "command": (
                f"oddish qa {data.scope_type} {data.scope_id} "
                f"--variant {ref} --model {data.model}"
            ),
        }
        run = QARunModel(
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
        env = dict(runtime_env or {}) if data.allow_credential_forwarding else {}
        if data.reasoning_effort and data.backend == "sandbox":
            env["CLAUDE_CODE_EFFORT_LEVEL"] = data.reasoning_effort
        block = AnalyzerBlock(
            analyzer_type=AnalyzerType.CUSTOM_QA,
            llm_client_type=client_type,
            input=AnalyzerInput(input={"scope": config["scope"], "qa_run_id": run.id}),
            prompt=version.content,
            system_prompt=system_prompt,
            analyzer_id=run.id,
            model=data.model,
            triggered_by_user_id=user_id,
            attribution_org_id=org_id,
            runtime_env=env or None,
            block_metadata=config,
        )
        run.analyzer_block_id = block.id
        pending.append((prompt, version, run, block))
    await session.commit()

    results = await asyncio.gather(
        *(block.run() for *_, block in pending), return_exceptions=True
    )
    responses = []
    for (prompt, version, run, block), result in zip(pending, results, strict=True):
        run.status = block.status
        run.error = block.error
        if not isinstance(result, BaseException):
            run.output = result.output
        responses.append(_run_response(run, prompt=prompt, version=version))
    await session.commit()
    return responses
