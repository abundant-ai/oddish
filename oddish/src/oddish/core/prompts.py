"""Versioned prompt registry — pure core logic. The latest version is always
live: editing appends a new version and readers resolve ``max(version)``, so
there is no activation pointer to drift. ``kind`` is string-typed here (the
``PromptKind`` enum is enforced at the router) so tests can use throwaway
kinds. Callers own the transaction; these functions never commit (they
``flush`` so ids/defaults populate)."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from oddish.db import PromptModel, PromptVersionModel


PROMPT_SCOPE_TYPES = frozenset({"org", "user", "experiment", "task", "trial"})


async def _get_prompt(
    session: AsyncSession,
    ref: str,
    *,
    scope_type: str | None = None,
    scope_id: str | None = None,
    org_id: str | None = None,
) -> PromptModel | None:
    """Resolve by kind within the given scope, then by id.

    Kind-first ordering means an opaque id can never shadow an existing kind.
    The id lookup is deliberately NOT scope-filtered: an id already identifies
    exactly one row including its scope, so filtering it by the caller's scope
    would make scoped prompts unfetchable by id.
    """
    scope_filter = (
        and_(PromptModel.scope_type.is_(None), PromptModel.scope_id.is_(None))
        if scope_type is None
        else and_(
            PromptModel.scope_type == scope_type,
            PromptModel.scope_id == scope_id,
            PromptModel.org_id == org_id,
        )
    )
    result = await session.execute(
        select(PromptModel).where(PromptModel.kind == ref, scope_filter)
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
    scope_type: str | None = None,
    scope_id: str | None = None,
    org_id: str | None = None,
) -> PromptVersionModel:
    if (scope_type is None) != (scope_id is None):
        raise ValueError("scope_type and scope_id must be provided together")
    if scope_type is not None and scope_type not in PROMPT_SCOPE_TYPES:
        raise ValueError(f"unsupported prompt scope: {scope_type}")
    if scope_type is not None and not org_id:
        raise ValueError("org_id is required when scope_type is set")
    prompt = await _get_prompt(
        session,
        kind,
        scope_type=scope_type,
        scope_id=scope_id,
        org_id=org_id,
    )
    if prompt is not None and (prompt.scope_type, prompt.scope_id) != (
        scope_type,
        scope_id,
    ):
        # `kind` resolved via _get_prompt's unscoped id fallback to a row at a
        # *different* scope than requested -- appending to it would silently
        # rewrite someone else's (or the global) prompt instead of creating
        # the requested scope's own row.
        raise ValueError(
            f"prompt '{kind}' resolves to scope "
            f"({prompt.scope_type!r}, {prompt.scope_id!r}), not the requested "
            f"({scope_type!r}, {scope_id!r})"
        )
    if prompt is not None and prompt.org_id and prompt.org_id != org_id:
        # `user` scope_id is a bare user id, not org-partitioned like task/
        # experiment/trial ids -- without this, a user who is FULL in org B
        # could append a version to their own ("user", user_id) row stamped
        # org_id=A from a prior membership.
        raise ValueError(
            f"prompt '{kind}' at scope ({scope_type!r}, {scope_id!r}) belongs "
            f"to org {prompt.org_id!r}, not the requesting org {org_id!r}"
        )
    if prompt is None:
        prompt = PromptModel(
            kind=kind,
            description=description or "",
            scope_type=scope_type,
            scope_id=scope_id,
            org_id=org_id,
        )
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


async def list_prompts_core(
    session: AsyncSession, *, org_id: str | None = None
) -> list[PromptModel]:
    stmt = select(PromptModel)
    if org_id is None:
        stmt = stmt.where(PromptModel.scope_type.is_(None))
    else:
        stmt = stmt.where(
            or_(PromptModel.scope_type.is_(None), PromptModel.org_id == org_id)
        )
    result = await session.execute(
        stmt.order_by(PromptModel.kind, PromptModel.scope_type, PromptModel.scope_id)
    )
    return list(result.scalars().all())


async def list_prompt_versions_core(
    session: AsyncSession,
    kind: str,
    *,
    scope_type: str | None = None,
    scope_id: str | None = None,
    org_id: str | None = None,
) -> list[PromptVersionModel]:
    prompt = await _get_prompt(
        session,
        kind,
        scope_type=scope_type,
        scope_id=scope_id,
        org_id=org_id,
    )
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{kind}' not found")
    versions = await prompt.awaitable_attrs.versions
    return sorted(versions, key=lambda v: v.version)


async def get_prompt_core(
    session: AsyncSession,
    kind: str,
    *,
    version: int | None = None,
    scope_type: str | None = None,
    scope_id: str | None = None,
    org_id: str | None = None,
) -> tuple[PromptModel, PromptVersionModel]:
    prompt = await _get_prompt(
        session,
        kind,
        scope_type=scope_type,
        scope_id=scope_id,
        org_id=org_id,
    )
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


async def resolve_prompt_core(
    session: AsyncSession,
    kind: str,
    *,
    org_id: str | None,
    user_id: str | None,
    experiment_id: str | None,
    task_id: str | None,
    trial_id: str | None,
) -> tuple[PromptModel, PromptVersionModel]:
    """Resolve the narrowest available override, then the global default.

    Strict narrowest-wins: exactly one prompt is returned per kind. This is
    deliberately NOT how ``qa_job_assignments`` composes scopes -- that layer
    UNIONs, so a task-level check cannot disable the org's default QA. The two
    answer different questions ("which checks run" vs "what does this check
    say"), and only the latter can have one winner.
    """
    # task and experiment are NOT nested (a task spans experiments, an experiment
    # spans tasks), so this order is a deliberate call, not a containment fact.
    #
    # Task wins. A task override encodes durable knowledge about that task (a
    # brittle verifier, a known failure mode); an experiment override encodes
    # intent for one run. Letting the broader scope win would silently suppress
    # the task's knowledge exactly where attention is highest, with no signal.
    #
    # This costs A/B hermeticity, but less than it appears: a task override
    # applies equally to both arms of a comparison that shares a task set, so it
    # is a constant, not a confound -- it costs that task's signal, visibly,
    # rather than corrupting the result. (An A/B whose arms run DIFFERENT task
    # sets does not get that guarantee; pin at trial scope for those.)
    #
    # Keeping every tier narrowest-wins also preserves the invariant the
    # assignments layer relies on: a narrower scope never silently removes a
    # broader scope's coverage.
    candidates = [
        ("trial", trial_id),
        ("task", task_id),
        ("experiment", experiment_id),
        ("user", user_id),
        ("org", org_id),
    ]
    for scope_type, scope_id in candidates:
        if not scope_id:
            continue
        prompt = await _get_prompt(
            session,
            kind,
            scope_type=scope_type,
            scope_id=scope_id,
            org_id=org_id,
        )
        if prompt is not None and (prompt.org_id is None or prompt.org_id == org_id):
            versions = await prompt.awaitable_attrs.versions
            if versions:
                return prompt, max(versions, key=lambda v: v.version)
    return await get_prompt_core(session, kind)


async def get_prompt_usage_core(session: AsyncSession, ref: str) -> dict:
    """Aggregate real prompt consumption from analyzer-block lineage stamps."""
    from sqlalchemy import func

    from oddish.db.models import AnalyzerBlockModel

    prompt = await _get_prompt(session, ref)
    if prompt is None:
        raise HTTPException(status_code=404, detail=f"Prompt '{ref}' not found")
    # Prefer the explicit prompt_id stamp. Unstamped rows — history written
    # before this column, plus any block path not yet passing prompt_id —
    # fall back to matching on kind, but ONLY for the global prompt, so an
    # unstamped block can never be counted against a scoped override.
    attribution = AnalyzerBlockModel.prompt_id == prompt.id
    if prompt.scope_type is None:
        attribution = or_(
            attribution,
            and_(
                AnalyzerBlockModel.prompt_id.is_(None),
                AnalyzerBlockModel.prompt_key == prompt.kind,
            ),
        )
    rows = (
        await session.execute(
            select(
                AnalyzerBlockModel.prompt_version,
                func.count().label("usage_count"),
                func.max(AnalyzerBlockModel.created_at).label("last_used_at"),
            )
            .where(attribution)
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
