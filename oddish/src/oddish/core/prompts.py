"""Versioned prompt registry — pure core logic. The latest version is always
live: editing appends a new version and readers resolve ``max(version)``, so
there is no activation pointer to drift. ``kind`` is string-typed here (the
``PromptKind`` enum is enforced at the router) so tests can use throwaway
kinds. Callers own the transaction; these functions never commit (they
``flush`` so ids/defaults populate)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import PromptModel, PromptVersionModel


async def _get_prompt(session: AsyncSession, kind: str) -> PromptModel | None:
    result = await session.execute(select(PromptModel).where(PromptModel.kind == kind))
    return result.scalar_one_or_none()


async def set_prompt_core(
    session: AsyncSession,
    *,
    kind: str,
    content: str,
    description: str | None = None,
    created_by: str | None = None,
) -> PromptVersionModel:
    prompt = await _get_prompt(session, kind)
    if prompt is None:
        prompt = PromptModel(kind=kind, description=description or "")
        session.add(prompt)
        await session.flush()
        next_version = 1
    else:
        if description is not None:
            prompt.description = description
        versions = await prompt.awaitable_attrs.versions
        next_version = (max((v.version for v in versions), default=0)) + 1

    version = PromptVersionModel(
        prompt_id=prompt.id,
        version=next_version,
        content=content,
        created_by=created_by,
    )
    session.add(version)
    await session.flush()
    return version


async def list_prompts_core(session: AsyncSession) -> list[PromptModel]:
    result = await session.execute(select(PromptModel).order_by(PromptModel.kind))
    return list(result.scalars().all())


async def list_prompt_versions_core(
    session: AsyncSession, kind: str
) -> list[PromptVersionModel]:
    prompt = await _get_prompt(session, kind)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{kind}' not found")
    versions = await prompt.awaitable_attrs.versions
    return sorted(versions, key=lambda v: v.version)


async def get_prompt_core(
    session: AsyncSession, kind: str, *, version: int | None = None
) -> tuple[PromptModel, PromptVersionModel]:
    prompt = await _get_prompt(session, kind)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{kind}' not found")
    versions = await prompt.awaitable_attrs.versions
    if not versions:
        raise HTTPException(status_code=404, detail=f"Prompt '{kind}' has no versions")
    if version is None:
        return prompt, max(versions, key=lambda v: v.version)
    for v in versions:
        if v.version == version:
            return prompt, v
    raise HTTPException(
        status_code=404, detail=f"Prompt '{kind}' has no version {version}"
    )


async def get_latest_prompt_content(session: AsyncSession, kind: str) -> str:
    _, ver = await get_prompt_core(session, kind)
    return ver.content
