"""Versioned prompt registry — pure core logic. Callers own the transaction;
these functions never commit (they ``flush`` so ids/defaults populate)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import PromptModel, PromptVersionModel


async def _get_prompt(session: AsyncSession, ref: str) -> PromptModel | None:
    """Resolve by key first, then by id. Only an unknown key can fall through,
    so an id can never shadow an existing key."""
    result = await session.execute(select(PromptModel).where(PromptModel.key == ref))
    prompt = result.scalar_one_or_none()
    if prompt is None:
        result = await session.execute(select(PromptModel).where(PromptModel.id == ref))
        prompt = result.scalar_one_or_none()
    return prompt


async def set_prompt_core(
    session: AsyncSession,
    *,
    key: str,
    content: str,
    description: str | None = None,
    activate: bool = True,
    created_by: str | None = None,
) -> PromptVersionModel:
    prompt = await _get_prompt(session, key)
    if prompt is None:
        prompt = PromptModel(key=key, description=description or "")
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
    if activate:
        prompt.active_version = next_version
    await session.flush()
    return version


async def list_prompts_core(session: AsyncSession) -> list[PromptModel]:
    result = await session.execute(select(PromptModel).order_by(PromptModel.key))
    return list(result.scalars().all())


async def list_prompt_versions_core(
    session: AsyncSession, key: str
) -> list[PromptVersionModel]:
    prompt = await _get_prompt(session, key)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{key}' not found")
    versions = await prompt.awaitable_attrs.versions
    return sorted(versions, key=lambda v: v.version)


async def get_prompt_core(
    session: AsyncSession, key: str, *, version: int | None = None
) -> tuple[PromptModel, PromptVersionModel]:
    prompt = await _get_prompt(session, key)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{key}' not found")
    target = version if version is not None else prompt.active_version
    if target is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{key}' has no active version")
    versions = await prompt.awaitable_attrs.versions
    for v in versions:
        if v.version == target:
            return prompt, v
    raise HTTPException(
        status_code=404, detail=f"Prompt '{key}' has no version {target}"
    )


async def activate_prompt_version_core(
    session: AsyncSession, key: str, version: int
) -> PromptModel:
    prompt, _ = await get_prompt_core(session, key, version=version)
    prompt.active_version = version
    await session.flush()
    return prompt


async def get_active_prompt_content(session: AsyncSession, key: str) -> str:
    _, ver = await get_prompt_core(session, key)
    return ver.content


async def get_prompt_usage_core(session: AsyncSession, ref: str) -> dict:
    """Aggregate real consumption of a prompt from the analyzer_blocks stamps.

    Zero rows means the prompt is registered but nothing runs it -- the
    honest signal for seeded-but-unwired keys.
    """
    from sqlalchemy import func

    from oddish.db.models import AnalyzerBlockModel

    prompt = await _get_prompt(session, ref)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{ref}' not found")
    rows = (
        await session.execute(
            select(
                AnalyzerBlockModel.prompt_version,
                func.count().label("usage_count"),
                func.max(AnalyzerBlockModel.created_at).label("last_used_at"),
            )
            .where(AnalyzerBlockModel.prompt_key == prompt.key)
            .group_by(AnalyzerBlockModel.prompt_version)
            .order_by(AnalyzerBlockModel.prompt_version)
        )
    ).all()
    return {
        "total": sum(r.usage_count for r in rows),
        "last_used_at": max((r.last_used_at for r in rows), default=None),
        "by_version": [
            {
                "version": r.prompt_version,
                "count": r.usage_count,
                "last_used_at": r.last_used_at,
            }
            for r in rows
        ],
    }
