from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select
from svix import Webhook, WebhookVerificationError

from models import OrganizationModel, UserModel, UserRole, generate_id
from oddish.db import get_session

logger = logging.getLogger(__name__)

CLERK_WEBHOOK_SECRET = os.getenv("CLERK_WEBHOOK_SECRET", "")

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.strip().lower())
    slug = slug.strip("-")
    return slug or "org"


async def _ensure_unique_org_slug(session, base_slug: str) -> str:
    slug = base_slug or "org"
    suffix = 1

    while True:
        result = await session.execute(
            select(OrganizationModel)
            .where(OrganizationModel.slug == slug)
            .where(OrganizationModel.is_active == True)  # noqa: E712
        )
        if result.scalar_one_or_none() is None:
            return slug
        slug = f"{base_slug}-{suffix}"
        suffix += 1


def _map_role(role: str | None) -> UserRole:
    normalized = (role or "").lower()
    if normalized in {"owner", "org:owner", "admin", "org:admin"}:
        return UserRole.ADMIN
    return UserRole.MEMBER


def _resolve_org_id(payload: dict[str, Any]) -> str | None:
    org = payload.get("organization") or {}
    return (
        org.get("id")
        or payload.get("organization_id")
        or payload.get("organizationId")
        or payload.get("organizationID")
    )


def _resolve_user_id(payload: dict[str, Any]) -> str | None:
    return payload.get("user_id") or payload.get("userId") or payload.get("userID")


def _resolve_user_email(payload: dict[str, Any]) -> str | None:
    public = payload.get("public_user_data") or {}
    return (
        public.get("identifier")
        or public.get("email_address")
        or public.get("emailAddress")
        or payload.get("email_address")
        or payload.get("emailAddress")
    )


def _resolve_user_name(payload: dict[str, Any]) -> str | None:
    public = payload.get("public_user_data") or {}
    full = public.get("full_name") or public.get("fullName")
    if full:
        return full
    first = public.get("first_name") or public.get("firstName")
    last = public.get("last_name") or public.get("lastName")
    if first and last:
        return f"{first} {last}"
    return first or last


async def _upsert_org(
    session, clerk_org_id: str, name: str | None, slug: str | None
) -> OrganizationModel:
    result = await session.execute(
        select(OrganizationModel)
        .where(OrganizationModel.clerk_org_id == clerk_org_id)
        .where(OrganizationModel.is_active == True)  # noqa: E712
    )
    org = result.scalar_one_or_none()

    if org:
        if name and org.name != name:
            org.name = name
        if slug:
            candidate = _slugify(slug)
            if candidate and org.slug != candidate:
                org.slug = await _ensure_unique_org_slug(session, candidate)
        return org

    base_slug = _slugify(slug or name or f"org-{clerk_org_id}")
    base_slug = await _ensure_unique_org_slug(session, base_slug)
    org = OrganizationModel(
        id=generate_id(),
        name=name or "Organization",
        slug=base_slug,
        clerk_org_id=clerk_org_id,
    )
    session.add(org)
    await session.flush()
    return org


async def _upsert_user(
    session,
    org: OrganizationModel,
    clerk_user_id: str,
    email: str | None,
    name: str | None,
    role: UserRole,
) -> UserModel:
    result = await session.execute(
        select(UserModel)
        .where(UserModel.clerk_user_id == clerk_user_id)
        .where(UserModel.org_id == org.id)
        .where(UserModel.is_active == True)  # noqa: E712
    )
    user = result.scalar_one_or_none()
    if user:
        if name and not user.name:
            user.name = name
        if email and user.email != email:
            user.email = email
        if role and user.role != role and user.role != UserRole.OWNER:
            user.role = role
        return user

    safe_email = email
    if safe_email:
        existing = await session.execute(
            select(UserModel)
            .where(UserModel.org_id == org.id)
            .where(UserModel.email == safe_email)
            .where(UserModel.is_active == True)  # noqa: E712
        )
        if existing.scalar_one_or_none():
            safe_email = None

    user = UserModel(
        id=generate_id(),
        org_id=org.id,
        clerk_user_id=clerk_user_id,
        email=safe_email or f"{clerk_user_id}@clerk.user",
        name=name,
        role=role,
    )
    session.add(user)
    await session.flush()
    return user


def _verify_clerk_webhook(payload: bytes, headers: dict[str, Any]) -> dict[str, Any]:
    if not CLERK_WEBHOOK_SECRET:
        raise HTTPException(
            status_code=500, detail="CLERK_WEBHOOK_SECRET not configured"
        )

    try:
        wh = Webhook(CLERK_WEBHOOK_SECRET)
        event = wh.verify(payload, headers)
    except WebhookVerificationError:
        raise HTTPException(status_code=400, detail="Invalid webhook signature")

    if isinstance(event, str):
        return json.loads(event)
    return event


@router.post("/clerk")
async def handle_clerk_webhook(request: Request) -> dict[str, str]:
    payload = await request.body()
    event = _verify_clerk_webhook(payload, dict(request.headers))

    event_type = event.get("type")
    data = event.get("data") or {}

    async with get_session() as session:
        if event_type in {"organization.created", "organization.updated"}:
            clerk_org_id = data.get("id")
            if not clerk_org_id:
                raise HTTPException(status_code=400, detail="Missing organization id")
            await _upsert_org(
                session,
                clerk_org_id=clerk_org_id,
                name=data.get("name"),
                slug=data.get("slug"),
            )
            await session.commit()
            return {"status": "ok"}

        if event_type == "organizationMembership.created":
            clerk_org_id = _resolve_org_id(data)
            clerk_user_id = _resolve_user_id(data)
            if not clerk_org_id or not clerk_user_id:
                raise HTTPException(
                    status_code=400, detail="Missing organization or user id"
                )

            org = await _upsert_org(
                session,
                clerk_org_id=clerk_org_id,
                name=(data.get("organization") or {}).get("name"),
                slug=(data.get("organization") or {}).get("slug"),
            )
            await _upsert_user(
                session,
                org=org,
                clerk_user_id=clerk_user_id,
                email=_resolve_user_email(data),
                name=_resolve_user_name(data),
                role=_map_role(data.get("role")),
            )
            await session.commit()
            return {"status": "ok"}

        if event_type == "organizationMembership.deleted":
            clerk_org_id = _resolve_org_id(data)
            clerk_user_id = _resolve_user_id(data)
            if not clerk_org_id or not clerk_user_id:
                raise HTTPException(
                    status_code=400, detail="Missing organization or user id"
                )

            org = await _upsert_org(
                session,
                clerk_org_id=clerk_org_id,
                name=(data.get("organization") or {}).get("name"),
                slug=(data.get("organization") or {}).get("slug"),
            )
            result = await session.execute(
                select(UserModel)
                .where(UserModel.org_id == org.id)
                .where(UserModel.clerk_user_id == clerk_user_id)
                .where(UserModel.is_active == True)  # noqa: E712
            )
            user = result.scalar_one_or_none()
            if user:
                user.is_active = False
                await session.commit()
            return {"status": "ok"}

    logger.info("Unhandled Clerk webhook type: %s", event_type)
    return {"status": "ignored"}
