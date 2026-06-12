"""FastAPI router for the tag system.

Every endpoint resolves the org from ``auth.org_id`` (cross-org access is
rejected up front). USE-plane actions reuse the existing ``write access
to the target task/experiment`` helper; DEFINITION-plane actions go
through ``can_definition_capability`` + ``can_manage_grants``.
"""
from __future__ import annotations

import json
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path
from sqlalchemy import text

from auth import APIKeyScope, AuthContext, require_admin, require_auth
from oddish.core.tag_permissions import (
    can_definition_capability,
    can_manage_grants,
    can_use_apply,
    is_org_admin,
)
from oddish.core.tag_policies_core import (
    TagCapExceededError,
    assert_under_tag_cap,
    get_or_create_tag_policy,
    update_tag_policy_core,
)
from oddish.core.tag_naming import TagNameError, is_reserved_prefix
from oddish.core.tags_core import (
    TagConcurrencyError,
    TagMergeChainError,
    TagPolicyError,
    TagProfanityError,
    TagReservedNamespaceError,
    apply_experiment_tag_core,
    archive_tag_core,
    assign_tag_core,
    create_tag_core,
    delete_tag_core,
    edit_tag_core,
    exclude_tag_core,
    grant_tag_capability_core,
    merge_tag_core,
    remove_experiment_tag_core,
    revoke_tag_capability_core,
    set_tag_visibility_core,
    unassign_tag_core,
    unexclude_tag_core,
)
from oddish.core.saved_tag_filters_core import (
    create_saved_tag_filter_core,
    delete_saved_tag_filter_core,
    list_saved_tag_filters_core,
    update_saved_tag_filter_core,
)
from oddish.db import get_session
from oddish.schemas import (
    ProfanityReportCreateRequest,
    ProfanityReportItem,
    ProfanityReportListResponse,
    SavedTagFilterCreateRequest,
    SavedTagFilterItem,
    SavedTagFilterListResponse,
    SavedTagFilterUpdateRequest,
    TagAssignRequest,
    TagAssignResponse,
    TagArchiveRequest,
    TagCreateRequest,
    TagExcludeRequest,
    TagGrantCreateRequest,
    TagGrantListItem,
    TagGrantListResponse,
    TagListItem,
    TagListResponse,
    TagMergeRequest,
    TagPolicyResponse,
    TagPolicyUpdateRequest,
    TagSetVisibilityRequest,
    TagUnassignRequest,
    TagUpdateRequest,
    UserTagRef,
)
from oddish.core.tags_projection import list_direct_target_tags

router = APIRouter(tags=["Tags"])


_TARGET_ORG_QUERIES = {
    "TASK": """
        SELECT 1 FROM tasks
        WHERE id = :target_id
          AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
    """,
    "VERSION": """
        SELECT 1 FROM task_versions tv
        JOIN tasks t ON t.id = tv.task_id
        WHERE tv.id = :target_id
          AND COALESCE(t.org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
    """,
    "EXPERIMENT": """
        SELECT 1 FROM experiments
        WHERE id = :target_id
          AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
    """,
}


async def _assert_target_in_org(
    session, *, scope: str, target_id: str, org_id: str | None
) -> None:
    """404 unless the assignment target belongs to the caller's org.

    The tag itself is org-scoped via ``_load_tag``, but without this check a
    caller could attach their own tag to ANOTHER org's task/version/experiment
    id, polluting that org's effective-tag projection.
    """
    query = _TARGET_ORG_QUERIES.get(scope)
    if query is None:
        raise HTTPException(status_code=400, detail=f"invalid scope: {scope}")
    row = await session.scalar(
        text(query), {"target_id": target_id, "org_id": org_id}
    )
    if row is None:
        raise HTTPException(status_code=404, detail="target not found")


async def _load_tag(session, tag_id: str, org_id: str | None) -> dict:
    row = (
        await session.execute(
            text(
                """
                SELECT id, org_id, key, normalized_key, value, normalized_value,
                       color, description, visibility::text AS visibility,
                       state::text AS state, merged_into_id, owner_user_id,
                       row_version
                FROM tags
                WHERE id = :tag_id
                  AND deleted_at IS NULL
                  AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                """
            ),
            {"tag_id": tag_id, "org_id": org_id},
        )
    ).all()
    if not row:
        raise HTTPException(status_code=404, detail=f"Tag {tag_id} not found")
    r = row[0]
    return dict(getattr(r, "_mapping", r))


def _handle_known_errors(exc: Exception) -> None:
    if isinstance(exc, TagProfanityError):
        raise HTTPException(status_code=400, detail=f"profanity: {exc}")
    if isinstance(exc, TagReservedNamespaceError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, TagPolicyError):
        raise HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, TagNameError):
        raise HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, TagConcurrencyError):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TagCapExceededError):
        raise HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, TagMergeChainError):
        raise HTTPException(status_code=409, detail=str(exc))


@router.get("/tags", response_model=TagListResponse)
async def list_tags(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagListResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT t.id, t.key, t.value, t.color,
                           t.visibility::text, t.state::text,
                           COALESCE(usage.count, 0) AS usage_count,
                           t.row_version, t.owner_user_id
                    FROM tags t
                    LEFT JOIN (
                        SELECT tag_id, COUNT(*) AS count
                        FROM tag_assignments
                        WHERE deleted_at IS NULL AND state = 'ACTIVE'
                        GROUP BY tag_id
                    ) usage ON usage.tag_id = t.id
                    WHERE t.deleted_at IS NULL
                      AND t.state <> 'DELETED'
                      AND COALESCE(t.org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                    ORDER BY t.normalized_key
                    """
                ),
                {"org_id": auth.org_id},
            )
        ).all()
    items = [
        TagListItem(
            id=str(r[0]),
            key=str(r[1]),
            value=r[2],
            color=r[3],
            visibility=str(r[4]),
            state=str(r[5]),
            usage_count=int(r[6]),
            row_version=int(r[7]),
            owner_user_id=r[8],
        )
        for r in rows
    ]
    return TagListResponse(items=items)


@router.post("/tags", response_model=TagListItem)
async def create_tag(
    payload: TagCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagListItem:
    auth.require_scope(APIKeyScope.TASKS)
    if not auth.org_id:
        raise HTTPException(status_code=400, detail="org_id required")
    async with get_session() as session:
        policy = await get_or_create_tag_policy(session, org_id=auth.org_id)
        try:
            tag_id = await create_tag_core(
                session,
                key=payload.key,
                value=payload.value,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
                policy=policy,
                is_admin=is_org_admin(auth),
                color=payload.color,
                description=payload.description,
                visibility=payload.visibility,
            )
        except Exception as exc:
            _handle_known_errors(exc)
            raise
        await session.commit()
    return TagListItem(
        id=tag_id,
        key=payload.key,
        value=payload.value,
        color=payload.color,
        visibility=payload.visibility,
        state="ACTIVE",
        usage_count=0,
        row_version=1,
        owner_user_id=auth.user_id,
    )


@router.get("/tags/for-target", response_model=list[UserTagRef])
async def list_tags_for_target(
    auth: Annotated[AuthContext, Depends(require_auth)],
    scope: str,
    target_id: str,
) -> list[UserTagRef]:
    """Direct tags on one target — hydrates the experiment header editor.

    Registered BEFORE ``GET /tags/{tag_id}`` so the literal path isn't
    swallowed by the parameterized route.
    """
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        await _assert_target_in_org(
            session, scope=scope, target_id=target_id, org_id=auth.org_id
        )
        views = await list_direct_target_tags(
            session, scope=scope, target_id=target_id
        )
    return [
        UserTagRef(
            tag_id=v.tag_id,
            key=v.key,
            value=v.value,
            color=v.color,
            visibility=v.visibility,
            current=v.current,
            older=v.older,
        )
        for v in views
    ]


@router.get("/tags/{tag_id}", response_model=TagListItem)
async def get_tag(
    tag_id: Annotated[str, Path(...)],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagListItem:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
    return TagListItem(
        id=tag["id"],
        key=tag["key"],
        value=tag.get("value"),
        color=tag.get("color"),
        visibility=tag["visibility"],
        state=tag["state"],
        usage_count=0,
        row_version=int(tag["row_version"]),
        owner_user_id=tag.get("owner_user_id"),
    )


@router.patch("/tags/{tag_id}", response_model=TagListItem)
async def update_tag(
    tag_id: Annotated[str, Path(...)],
    payload: TagUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagListItem:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        capability = "RENAME" if payload.key else "EDIT"
        if not await can_definition_capability(
            session, auth, tag=tag, capability=capability
        ):
            raise HTTPException(status_code=403, detail="not allowed to edit this tag")
        updates: dict = {}
        if payload.key is not None:
            updates["key"] = payload.key
        if payload.color is not None:
            updates["color"] = payload.color
        if payload.description is not None:
            updates["description"] = payload.description
        policy = await get_or_create_tag_policy(session, org_id=auth.org_id)
        try:
            await edit_tag_core(
                session,
                tag_id=tag_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
                expected_row_version=payload.expected_row_version,
                updates=updates,
                policy=policy,
                is_admin=is_org_admin(auth),
            )
        except Exception as exc:
            _handle_known_errors(exc)
            raise
        await session.commit()
        tag = await _load_tag(session, tag_id, auth.org_id)
    return TagListItem(
        id=tag["id"],
        key=tag["key"],
        value=tag.get("value"),
        color=tag.get("color"),
        visibility=tag["visibility"],
        state=tag["state"],
        usage_count=0,
        row_version=int(tag["row_version"]),
        owner_user_id=tag.get("owner_user_id"),
    )


@router.delete("/tags/{tag_id}")
async def delete_tag(
    tag_id: Annotated[str, Path(...)],
    payload: TagArchiveRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        if not await can_definition_capability(
            session, auth, tag=tag, capability="DELETE"
        ):
            raise HTTPException(status_code=403, detail="not allowed to delete this tag")
        try:
            await delete_tag_core(
                session,
                tag_id=tag_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
                expected_row_version=payload.expected_row_version,
            )
        except Exception as exc:
            _handle_known_errors(exc)
            raise
        await session.commit()
    return {"deleted": True}


@router.post("/tags/{tag_id}/archive")
async def archive_tag(
    tag_id: Annotated[str, Path(...)],
    payload: TagArchiveRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        if not await can_definition_capability(
            session, auth, tag=tag, capability="EDIT"
        ):
            raise HTTPException(status_code=403, detail="not allowed to archive this tag")
        try:
            await archive_tag_core(
                session,
                tag_id=tag_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
                expected_row_version=payload.expected_row_version,
            )
        except Exception as exc:
            _handle_known_errors(exc)
            raise
        await session.commit()
    return {"archived": True}


@router.post("/tags/{tag_id}/merge")
async def merge_tag(
    tag_id: Annotated[str, Path(...)],
    payload: TagMergeRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        if not await can_definition_capability(
            session, auth, tag=tag, capability="MERGE"
        ):
            raise HTTPException(status_code=403, detail="not allowed to merge this tag")
        try:
            await merge_tag_core(
                session,
                source_tag_id=tag_id,
                target_tag_id=payload.target_tag_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
                expected_row_version=payload.expected_row_version,
            )
        except Exception as exc:
            _handle_known_errors(exc)
            raise
        await session.commit()
    return {"merged_into": payload.target_tag_id}


@router.post("/tags/{tag_id}/visibility")
async def set_visibility(
    tag_id: Annotated[str, Path(...)],
    payload: TagSetVisibilityRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        if not await can_definition_capability(
            session, auth, tag=tag, capability="EDIT"
        ):
            raise HTTPException(status_code=403, detail="not allowed to change visibility")
        try:
            await set_tag_visibility_core(
                session,
                tag_id=tag_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
                new_visibility=payload.visibility,
                expected_row_version=payload.expected_row_version,
            )
        except Exception as exc:
            _handle_known_errors(exc)
            raise
        await session.commit()
    return {"visibility": payload.visibility}


@router.post("/tags/assign", response_model=TagAssignResponse)
async def assign_tag(
    payload: TagAssignRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagAssignResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, payload.tag_id, auth.org_id)
        await _assert_target_in_org(
            session,
            scope=payload.scope,
            target_id=payload.target_id,
            org_id=auth.org_id,
        )
        policy = await get_or_create_tag_policy(session, org_id=auth.org_id or "")
        reserved = is_reserved_prefix(
            tag.get("normalized_key", ""),
            reserved_prefixes=list(policy.get("reserved_prefixes", []) or []),
        )
        if not can_use_apply(
            auth, target_org_id=auth.org_id, reserved=reserved
        ):
            raise HTTPException(status_code=403, detail="not allowed to apply tags")
        try:
            await assert_under_tag_cap(
                session,
                scope=payload.scope,
                target_id=payload.target_id,
                org_id=auth.org_id,
                max_tags_per_entity=int(policy.get("max_tags_per_entity", 10)),
            )
        except TagCapExceededError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if payload.scope == "EXPERIMENT":
            assert payload.mode is not None
            result = await apply_experiment_tag_core(
                session,
                tag_id=payload.tag_id,
                experiment_id=payload.target_id,
                mode=payload.mode,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
            )
            await session.commit()
            return TagAssignResponse(
                tag_id=payload.tag_id,
                mode=result.get("mode"),
                materialized=result.get("materialized"),
            )
        assignment_id = await assign_tag_core(
            session,
            tag_id=payload.tag_id,
            scope=payload.scope,
            target_id=payload.target_id,
            task_id=payload.task_id,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
        )
        await session.commit()
        return TagAssignResponse(tag_id=payload.tag_id, assignment_id=assignment_id)


@router.post("/tags/unassign")
async def unassign_tag(
    payload: TagUnassignRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await _assert_target_in_org(
            session,
            scope=payload.scope,
            target_id=payload.target_id,
            org_id=auth.org_id,
        )
        if payload.scope == "EXPERIMENT":
            await remove_experiment_tag_core(
                session,
                tag_id=payload.tag_id,
                experiment_id=payload.target_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
            )
        else:
            await unassign_tag_core(
                session,
                tag_id=payload.tag_id,
                scope=payload.scope,
                target_id=payload.target_id,
                task_id=payload.task_id,
                org_id=auth.org_id,
                actor_user_id=auth.user_id,
            )
        await session.commit()
    return {"unassigned": True}


@router.post("/tags/exclude")
async def exclude_tag(
    payload: TagExcludeRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await _assert_target_in_org(
            session,
            scope=payload.scope,
            target_id=payload.target_id,
            org_id=auth.org_id,
        )
        await exclude_tag_core(
            session,
            tag_id=payload.tag_id,
            experiment_id=payload.experiment_id,
            scope=payload.scope,
            target_id=payload.target_id,
            task_id=payload.task_id,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
        )
        await session.commit()
    return {"excluded": True}


@router.post("/tags/unexclude")
async def unexclude_tag(
    payload: TagExcludeRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await _assert_target_in_org(
            session,
            scope=payload.scope,
            target_id=payload.target_id,
            org_id=auth.org_id,
        )
        await unexclude_tag_core(
            session,
            tag_id=payload.tag_id,
            experiment_id=payload.experiment_id,
            scope=payload.scope,
            target_id=payload.target_id,
            task_id=payload.task_id,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
        )
        await session.commit()
    return {"unexcluded": True}


@router.get("/tags/{tag_id}/grants", response_model=TagGrantListResponse)
async def list_grants(
    tag_id: Annotated[str, Path(...)],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagGrantListResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, principal_type::text, principal_user_id,
                           capability::text
                    FROM tag_grants
                    WHERE deleted_at IS NULL
                      AND tag_id = :tag_id
                      AND COALESCE(org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                    ORDER BY created_at
                    """
                ),
                {"tag_id": tag_id, "org_id": auth.org_id},
            )
        ).all()
    return TagGrantListResponse(
        items=[
            TagGrantListItem(
                id=str(r[0]),
                principal_type=str(r[1]),
                principal_user_id=r[2],
                capability=str(r[3]),
            )
            for r in rows
        ]
    )


@router.post("/tags/{tag_id}/grants", response_model=TagGrantListItem)
async def create_grant(
    tag_id: Annotated[str, Path(...)],
    payload: TagGrantCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagGrantListItem:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        if not can_manage_grants(auth, tag=tag):
            raise HTTPException(status_code=403, detail="MANAGE_GRANTS is owner/admin only")
        grant_id = await grant_tag_capability_core(
            session,
            tag_id=tag_id,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
            principal_type=payload.principal_type,
            principal_user_id=payload.principal_user_id,
            capability=payload.capability,
        )
        await session.commit()
    return TagGrantListItem(
        id=grant_id,
        principal_type=payload.principal_type,
        principal_user_id=payload.principal_user_id,
        capability=payload.capability,
    )


@router.delete("/tags/{tag_id}/grants/{grant_id}")
async def delete_grant(
    tag_id: Annotated[str, Path(...)],
    grant_id: Annotated[str, Path(...)],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, tag_id, auth.org_id)
        if not can_manage_grants(auth, tag=tag):
            raise HTTPException(status_code=403, detail="MANAGE_GRANTS is owner/admin only")
        await revoke_tag_capability_core(
            session,
            grant_id=grant_id,
            tag_id=tag_id,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
        )
        await session.commit()
    return {"revoked": True}


@router.get("/tag-policy", response_model=TagPolicyResponse)
async def get_policy(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagPolicyResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        policy = await get_or_create_tag_policy(session, org_id=auth.org_id or "")
        await session.commit()
    return TagPolicyResponse(
        org_id=str(auth.org_id),
        max_tags_per_entity=int(policy.get("max_tags_per_entity", 10)),
        name_max_len=int(policy.get("name_max_len", 64)),
        name_charset=str(policy.get("name_charset", "[a-z0-9._-]")),
        reserved_prefixes=list(policy.get("reserved_prefixes", []) or []),
        who_can_create=str(policy.get("who_can_create", "ANY_MEMBER")),
        profanity_mode=str(policy.get("profanity_mode", "ENFORCE")),
        profanity_allowlist=list(policy.get("profanity_allowlist", []) or []),
        profanity_denylist=list(policy.get("profanity_denylist", []) or []),
    )


@router.put("/tag-policy", response_model=TagPolicyResponse)
async def put_policy(
    payload: TagPolicyUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> TagPolicyResponse:
    async with get_session() as session:
        await update_tag_policy_core(
            session,
            org_id=auth.org_id or "",
            updates={k: v for k, v in payload.model_dump(exclude_none=True).items()},
            actor_user_id=auth.user_id,
        )
        await session.commit()
    return await get_policy(auth)  # type: ignore[return-value]


@router.get("/tag-policy/profanity-reports", response_model=ProfanityReportListResponse)
async def list_profanity_reports(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> ProfanityReportListResponse:
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT id, tag_id, org_id, actor_user_id, reason, payload,
                           occurred_at
                    FROM tag_events
                    WHERE org_id = :org_id
                      AND (
                          (action = 'CREATE'
                           AND COALESCE(payload->>'profanity_hit', '') <> '')
                       OR payload @> '{"profanity_report": true}'::jsonb
                      )
                    ORDER BY occurred_at DESC
                    LIMIT 200
                    """
                ),
                {"org_id": auth.org_id},
            )
        ).all()
    items = [
        ProfanityReportItem(
            event_id=int(r[0]),
            tag_id=r[1],
            org_id=r[2],
            actor_user_id=r[3],
            reason=r[4],
            payload=r[5] if isinstance(r[5], dict) else (r[5] or {}),
            occurred_at=r[6],
        )
        for r in rows
    ]
    return ProfanityReportListResponse(items=items)


@router.post("/tag-policy/profanity-reports")
async def report_tag_profanity(
    payload: ProfanityReportCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Member-facing: report a tag for profanity / policy violation.

    Writes a ``tag_events(action='POLICY_CHANGE', source='UI')`` row with a
    ``payload.profanity_report=true`` flag so admins see it via the
    existing ``GET /tag-policy/profanity-reports`` queue.
    """
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        await session.execute(
            text(
                """
                INSERT INTO tag_events (
                    event_uuid, org_id, action, tag_id,
                    actor_user_id, actor_type, source, reason, payload
                )
                VALUES (
                    :event_uuid, :org_id, 'POLICY_CHANGE', :tag_id,
                    :actor_user_id, 'USER', 'UI', :reason,
                    CAST(:payload AS JSONB)
                )
                """
            ),
            {
                "event_uuid": uuid.uuid4().hex,
                "org_id": auth.org_id,
                "tag_id": payload.tag_id,
                "actor_user_id": auth.user_id,
                "reason": payload.reason,
                "payload": json.dumps({"profanity_report": True}),
            },
        )
        await session.commit()
    return {"reported": True}


@router.get("/tag-filters", response_model=SavedTagFilterListResponse)
async def list_filters(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> SavedTagFilterListResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        items = await list_saved_tag_filters_core(
            session, org_id=auth.org_id or "", actor_user_id=auth.user_id or ""
        )
    return SavedTagFilterListResponse(
        items=[SavedTagFilterItem(**i) for i in items]
    )


@router.post("/tag-filters", response_model=SavedTagFilterItem)
async def create_filter(
    payload: SavedTagFilterCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> SavedTagFilterItem:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        new_id = await create_saved_tag_filter_core(
            session,
            org_id=auth.org_id or "",
            owner_user_id=auth.user_id or "",
            name=payload.name,
            filter_ast=payload.filter_ast,
            visibility=payload.visibility,
        )
        await session.commit()
    return SavedTagFilterItem(
        id=new_id,
        name=payload.name,
        filter_ast=payload.filter_ast,
        visibility=payload.visibility,
        owner_user_id=auth.user_id or "",
    )


@router.put("/tag-filters/{filter_id}")
async def put_filter(
    filter_id: Annotated[str, Path(...)],
    payload: SavedTagFilterUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await update_saved_tag_filter_core(
            session,
            filter_id=filter_id,
            actor_user_id=auth.user_id or "",
            updates={k: v for k, v in payload.model_dump(exclude_none=True).items()},
        )
        await session.commit()
    return {"updated": True}


@router.patch("/tag-filters/{filter_id}")
async def patch_filter(
    filter_id: Annotated[str, Path(...)],
    payload: SavedTagFilterUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await update_saved_tag_filter_core(
            session,
            filter_id=filter_id,
            actor_user_id=auth.user_id or "",
            updates={k: v for k, v in payload.model_dump(exclude_none=True).items()},
        )
        await session.commit()
    return {"updated": True}


@router.delete("/tag-filters/{filter_id}")
async def remove_filter(
    filter_id: Annotated[str, Path(...)],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await delete_saved_tag_filter_core(
            session, filter_id=filter_id, actor_user_id=auth.user_id or ""
        )
        await session.commit()
    return {"deleted": True}


@router.get("/tasks/{task_id}/tags", response_model=TagListResponse)
async def list_task_tags(
    task_id: Annotated[str, Path(...)],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagListResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT DISTINCT t.id, t.key, t.value, t.color,
                           t.visibility::text, t.state::text, 0, t.row_version,
                           t.owner_user_id
                    FROM tag_assignments a
                    JOIN tags t ON t.id = COALESCE(
                        (SELECT merged_into_id FROM tags WHERE id = a.tag_id),
                        a.tag_id
                    )
                    WHERE a.deleted_at IS NULL
                      AND a.state = 'ACTIVE'
                      AND t.state <> 'DELETED'
                      AND COALESCE(t.org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                      AND (
                          (a.scope = 'TASK' AND a.target_id = :task_id)
                       OR (a.scope = 'VERSION'
                           AND a.target_id IN (
                               SELECT id FROM task_versions WHERE task_id = :task_id
                           ))
                      )
                    ORDER BY t.key
                    """
                ),
                {"task_id": task_id, "org_id": auth.org_id},
            )
        ).all()
    return TagListResponse(
        items=[
            TagListItem(
                id=str(r[0]), key=str(r[1]), value=r[2], color=r[3],
                visibility=str(r[4]), state=str(r[5]), usage_count=int(r[6]),
                row_version=int(r[7]), owner_user_id=r[8],
            )
            for r in rows
        ]
    )


@router.get("/experiments/{experiment_id}/tags", response_model=TagListResponse)
async def list_experiment_tags(
    experiment_id: Annotated[str, Path(...)],
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagListResponse:
    auth.require_scope(APIKeyScope.READ)
    async with get_session() as session:
        rows = (
            await session.execute(
                text(
                    """
                    SELECT t.id, t.key, t.value, t.color,
                           t.visibility::text, t.state::text, 0, t.row_version,
                           t.owner_user_id
                    FROM tag_assignments a
                    JOIN tags t ON t.id = a.tag_id
                    WHERE a.deleted_at IS NULL
                      AND a.state = 'ACTIVE'
                      AND a.scope = 'EXPERIMENT'
                      AND a.target_id = :exp_id
                      AND t.state <> 'DELETED'
                      AND COALESCE(t.org_id, '') = COALESCE(CAST(:org_id AS TEXT), '')
                    ORDER BY t.key
                    """
                ),
                {"exp_id": experiment_id, "org_id": auth.org_id},
            )
        ).all()
    return TagListResponse(
        items=[
            TagListItem(
                id=str(r[0]), key=str(r[1]), value=r[2], color=r[3],
                visibility=str(r[4]), state=str(r[5]), usage_count=int(r[6]),
                row_version=int(r[7]), owner_user_id=r[8],
            )
            for r in rows
        ]
    )


@router.post("/experiments/{experiment_id}/tags", response_model=TagAssignResponse)
async def add_experiment_tag(
    experiment_id: Annotated[str, Path(...)],
    payload: TagAssignRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> TagAssignResponse:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        tag = await _load_tag(session, payload.tag_id, auth.org_id)
        policy = await get_or_create_tag_policy(session, org_id=auth.org_id or "")
        reserved = is_reserved_prefix(
            tag.get("normalized_key", ""),
            reserved_prefixes=list(policy.get("reserved_prefixes", []) or []),
        )
        if not can_use_apply(auth, target_org_id=auth.org_id, reserved=reserved):
            raise HTTPException(status_code=403, detail="not allowed to apply tags")
        mode = payload.mode or "MATERIALIZED"
        result = await apply_experiment_tag_core(
            session,
            tag_id=payload.tag_id,
            experiment_id=experiment_id,
            mode=mode,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
        )
        await session.commit()
    return TagAssignResponse(
        tag_id=payload.tag_id,
        mode=result.get("mode"),
        materialized=result.get("materialized"),
    )


@router.delete("/experiments/{experiment_id}/tags")
async def remove_experiment_tag(
    experiment_id: Annotated[str, Path(...)],
    payload: TagUnassignRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    auth.require_scope(APIKeyScope.TASKS)
    async with get_session() as session:
        await remove_experiment_tag_core(
            session,
            tag_id=payload.tag_id,
            experiment_id=experiment_id,
            org_id=auth.org_id,
            actor_user_id=auth.user_id,
        )
        await session.commit()
    return {"removed": True}
