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


async def _get_prompt(session: AsyncSession, ref: str) -> PromptModel | None:
    """Resolve by kind first, then by id.

    Kind-first ordering means an opaque id can never shadow an existing kind.
    A kind may now exist at several scopes (org/user/experiment/task/trial), so
    the by-kind lookup pins the installation-wide default (NULL scope + org);
    without this the first scoped row sharing a kind makes ``scalar_one_or_none``
    raise ``MultipleResultsFound``.
    """
    result = await session.execute(
        select(PromptModel).where(
            PromptModel.kind == ref,
            PromptModel.scope_type.is_(None),
            PromptModel.scope_id.is_(None),
            PromptModel.org_id.is_(None),
        )
    )
    prompt = result.scalar_one_or_none()
    if prompt is None:
        result = await session.execute(select(PromptModel).where(PromptModel.id == ref))
        prompt = result.scalar_one_or_none()
    return prompt


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
    # The registry view lists installation-wide defaults; scoped overrides are
    # resolved per request, not surfaced as their own top-level rows.
    result = await session.execute(
        select(PromptModel)
        .where(
            PromptModel.scope_type.is_(None),
            PromptModel.scope_id.is_(None),
            PromptModel.org_id.is_(None),
        )
        .order_by(PromptModel.kind)
    )
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


async def get_prompt_usage_core(session: AsyncSession, ref: str) -> dict:
    """Aggregate real prompt consumption from analyzer-block lineage stamps."""
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
            .where(AnalyzerBlockModel.prompt_key == prompt.kind)
            .group_by(AnalyzerBlockModel.prompt_version)
            .order_by(AnalyzerBlockModel.prompt_version)
        )
    ).all()
    return {
        "total": sum(row.usage_count for row in rows),
        "last_used_at": max((row.last_used_at for row in rows), default=None),
        "by_version": [
            {
                "version": row.prompt_version,
                "count": row.usage_count,
                "last_used_at": row.last_used_at,
            }
            for row in rows
        ],
    }
