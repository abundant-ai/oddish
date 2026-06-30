"""Stub suite for the account-merge in-Action linkage gate.

Design (account-merge-plan.md v7): an experiments-repo GitHub Action checks that
github.actor is a Clerk-linked Oddish user (via a NET-NEW GET /github/linkage)
before pushing tasks; owner resolves at /tasks/sweep on the same EXACTLY-ONE
predicate. No OIDC; direct API-key bypass accepted; fail-open on Clerk outage.

Markers:
  - runnable now: behaviour that already exists (org-scope, active-only, normalize).
  - xfail(strict): asserts target behaviour not yet built (endpoint, github_id, A0,
    constraint fingerprint) — flips to a hard failure when implemented, prompting
    marker removal.
  - skip: the experiments-repo Action is not in this repo.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from api.app import create_app
from api.routers.tasks import (
    _lookup_user_by_github_username,
    _resolve_experiment_owner_user_id,
)
from models import APIKeyScope, OrganizationModel, UserModel
from oddish.core.api_keys import create_api_key
from oddish.db import get_session
from oddish.schemas import AgentModelPair, TaskSweepSubmission

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _auth(org_id: str, *, user_id: str | None = None) -> SimpleNamespace:
    # _resolve_experiment_owner_user_id reads org_id / user_id / api_key_id / api_key.
    return SimpleNamespace(org_id=org_id, user_id=user_id, api_key_id=None, api_key=None)


def _submission(github_username: str | None) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="task_lg",
        configs=[
            AgentModelPair(
                agent="claude-code", model="anthropic/claude-sonnet-4-6", n_trials=1
            )
        ],
        user=None,
        github_username=github_username,
    )


def _new_user(org_id: str, handle: str, *, active: bool = True) -> UserModel:
    suffix = uuid.uuid4().hex[:8]
    return UserModel(
        id=f"user_{suffix}",
        org_id=org_id,
        email=f"{handle}_{suffix}@example.com",
        github_username=handle,
        clerk_user_id=f"clerk_{suffix}",
        role="member",
        is_active=active,
    )


async def _purge(
    *,
    org_ids: list[str] | None = None,
    user_ids: list[str] | None = None,
    api_key_ids: list[str] | None = None,
) -> None:
    from oddish.db.models import APIKeyModel

    async with get_session() as session:
        if user_ids:
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id.in_(user_ids))
            )
        if api_key_ids:
            await session.execute(
                APIKeyModel.__table__.delete().where(APIKeyModel.id.in_(api_key_ids))
            )
        if org_ids:
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id.in_(org_ids)
                )
            )


async def _seed_key(org_id: str) -> tuple[str, str]:
    """Create an org-scoped READ API key; return (key_id, raw_token)."""
    model, raw = create_api_key(org_id=org_id, name="lg", scope=APIKeyScope.READ)
    async with get_session() as session:
        session.add(model)
    return model.id, raw


@pytest_asyncio.fixture
async def org_with_users():
    """Factory fixture. Yields ``(org_id, add)`` where
    ``add(handle, active=True, in_org=None) -> UserModel`` persists a user and is
    auto-cleaned. Extra orgs created via ``in_org`` are tracked and purged too.
    """
    org_id = f"org_lg_{uuid.uuid4().hex[:8]}"
    user_ids: list[str] = []
    extra_orgs: list[str] = []

    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))

    async def add(handle: str, *, active: bool = True, in_org: str | None = None) -> UserModel:
        target_org = in_org or org_id
        user = _new_user(target_org, handle, active=active)
        async with get_session() as session:
            if in_org and in_org not in extra_orgs:
                session.add(OrganizationModel(id=in_org, name=in_org, slug=in_org))
                extra_orgs.append(in_org)
                await session.flush()
            session.add(user)
        user_ids.append(user.id)
        return user

    try:
        yield org_id, add
    finally:
        await _purge(org_ids=[org_id, *extra_orgs], user_ids=user_ids)


@pytest.fixture
def app():
    return create_app()


@pytest_asyncio.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


def _load_bootstrap():
    path = (
        Path(__file__).resolve().parents[2]
        / ".github/scripts/preview/bootstrap_preview_db.py"
    )
    spec = importlib.util.spec_from_file_location("bootstrap_preview_db", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _resolve(org_id: str, handle: str | None) -> str | None:
    async with get_session() as session:
        return await _resolve_experiment_owner_user_id(
            session, _submission(handle), _auth(org_id)
        )


async def _linkage(client, raw_key: str, handle: str):
    return await client.get(
        "/github/linkage",
        params={"handle": handle},
        headers={"Authorization": f"Bearer {raw_key}"},
    )


# ===========================================================================
# 1. Owner-resolution predicate — runnable now (current behaviour)
# ===========================================================================


@requires_db
@pytest.mark.asyncio
async def test_case1_exact_one_active_resolves_owner(org_with_users):
    """Case 1 (happy): exactly one active linked user → owner = that user.id."""
    org_id, add = org_with_users
    alice = await add("alice")
    assert await _resolve(org_id, "alice") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_case4_inactive_user_not_resolved(org_with_users):
    """Case 4: an inactive user with the handle is not a match (active-only)."""
    org_id, add = org_with_users
    await add("ghost", active=False)
    assert await _resolve(org_id, "ghost") is None


@requires_db
@pytest.mark.asyncio
async def test_case5_cross_org_handle_not_resolved(org_with_users):
    """Case 5: a handle linked only in another org must not resolve here."""
    org_id, add = org_with_users
    other = f"org_other_{uuid.uuid4().hex[:8]}"
    await add("bob", in_org=other)
    assert await _resolve(org_id, "bob") is None


@requires_db
@pytest.mark.asyncio
async def test_case8_9_handle_normalization(org_with_users):
    """Cases 8/9: @-prefix + case differences normalize to the same user."""
    org_id, add = org_with_users
    alice = await add("OctoCat")
    assert await _resolve(org_id, "@octocat") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_case21_bypass_unlinked_handle_no_enforcement(org_with_users):
    """Case 21 (policy guard): an unlinked handle resolves to None — NOT an error,
    NOT server-side enforcement. The Action gate, not /tasks/sweep, is the gate;
    the direct API-key path stays a graceful no-owner."""
    org_id, _add = org_with_users
    assert await _resolve(org_id, "nobody-here") is None


# ===========================================================================
# 2. Duplicate handle — the main correctness trap
# ===========================================================================


@requires_db
@pytest.mark.asyncio
async def test_case3_duplicate_handle_resolves_to_none(org_with_users):
    """Case 3 (unit): two active users share a handle → the exactly-one lookup
    returns None (not-connected) and never raises. Locks the fix at the helper
    level; test_case3_20 covers the same at the /tasks/sweep resolution level."""
    org_id, add = org_with_users
    await add("twin")
    await add("twin")
    async with get_session() as session:
        assert (
            await _lookup_user_by_github_username(
                session, github_username="twin", org_id=org_id
            )
            is None
        )


@requires_db
@pytest.mark.asyncio
async def test_case3_20_duplicate_handle_should_not_500(org_with_users):
    """Cases 3 + 20 (target): a duplicate handle must resolve to no owner
    (not-connected), never raise / 500 at /tasks/sweep."""
    org_id, add = org_with_users
    await add("twin")
    await add("twin")
    assert await _resolve(org_id, "twin") is None


# ===========================================================================
# 3. Linkage endpoint — xfail until GET /github/linkage exists
# ===========================================================================


@requires_db
@pytest.mark.asyncio
async def test_endpoint_linked_exact_one(client, org_with_users):
    """Case 1 (endpoint): exactly one active match → {linked: true, user_id}."""
    org_id, add = org_with_users
    alice = await add("alice")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "alice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True and body["user_id"] == alice.id
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_unlinked_returns_false(client, org_with_users):
    """Case 2 (endpoint): unknown handle → {linked: false}."""
    org_id, _add = org_with_users
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "ghost")
        assert resp.status_code == 200 and resp.json()["linked"] is False
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_ambiguous_not_linked_not_500(client, org_with_users):
    """Cases 3 + 22 (endpoint): two users share the handle → {linked: false},
    NOT a 500; the endpoint shares the exactly-one predicate with the sweep."""
    org_id, add = org_with_users
    await add("twin")
    await add("twin")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "twin")
        assert resp.status_code == 200 and resp.json()["linked"] is False
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_case16_endpoint_scope_and_auth(client, org_with_users):
    """Case 16: unauth → 401/403; an org API key with READ scope → 200."""
    org_id, add = org_with_users
    await add("alice")
    key_id, raw = await _seed_key(org_id)
    try:
        unauth = await client.get("/github/linkage", params={"handle": "alice"})
        assert unauth.status_code in (401, 403)
        assert (await _linkage(client, raw, "alice")).status_code == 200
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_case5_endpoint_org_scoped(client, org_with_users):
    """Case 5 (endpoint): org B's key sees a handle linked only in org A as unlinked."""
    _org_a, add = org_with_users
    await add("carol")
    org_b = f"org_b_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(OrganizationModel(id=org_b, name=org_b, slug=org_b))
    key_id, raw = await _seed_key(org_b)
    try:
        resp = await _linkage(client, raw, "carol")
        assert resp.status_code == 200 and resp.json()["linked"] is False
    finally:
        await _purge(org_ids=[org_b], api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_case10_endpoint_refreshes_stale_handle(client, org_with_users, monkeypatch):
    """Case 10: stored handle stale; actor uses the new handle; the endpoint must
    refresh from Clerk (its OWN path, not the early-returning
    ensure_user_github_identity / M2) and then match."""
    import api.routers.github_linkage as gh

    async def _clerk_returns_new_handle(_clerk_user_id: str) -> str:
        return "new_handle"

    monkeypatch.setattr(gh, "_fetch_clerk_github_username", _clerk_returns_new_handle)

    org_id, add = org_with_users
    user = await add("old_handle")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "new_handle")
        assert resp.status_code == 200 and resp.json()["user_id"] == user.id
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_case17_fail_open_uses_stale_db_on_clerk_outage(client, org_with_users, monkeypatch):
    """Case 17 (fail-open): when the Clerk refresh raises, the endpoint must answer
    from the stale DB value, NOT report unlinked. A stored handle that already
    matches the actor is the most fail-open answer — Clerk is never consulted —
    so a Clerk outage cannot flip a real link to unlinked."""
    import api.routers.github_linkage as gh

    async def _clerk_down(_clerk_user_id: str) -> str:
        raise httpx.HTTPError("clerk unreachable")

    monkeypatch.setattr(gh, "_fetch_clerk_github_username", _clerk_down)

    org_id, add = org_with_users
    alice = await add("alice")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "alice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True and body["user_id"] == alice.id  # stale, not unlinked
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_M2_refresh_not_early_returning_helper(client, org_with_users, monkeypatch):
    """M2: ensure_user_github_identity / _refresh_user_github_identity early-return
    when github_username is already set (provisioning.py:129-155). The endpoint must
    NOT reuse them for its refresh."""
    org_id, add = org_with_users
    await add("alice")
    key_id, raw = await _seed_key(org_id)
    import auth.provisioning as prov

    def _boom(*a, **k):
        raise AssertionError("linkage refresh must not reuse the early-returning helper")

    monkeypatch.setattr(prov, "ensure_user_github_identity", _boom, raising=False)
    try:
        assert (await _linkage(client, raw, "alice")).status_code == 200
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_M4_authorizes_by_org_scope_not_user_identity(client, org_with_users):
    """M4: API-key AuthContext has no user_id (auth/types.py:23-33). The endpoint
    must authorize by org+scope and never require a human identity from the key."""
    org_id, add = org_with_users
    await add("alice")
    key_id, raw = await _seed_key(org_id)
    try:
        assert (await _linkage(client, raw, "alice")).status_code == 200
    finally:
        await _purge(api_key_ids=[key_id])


# ===========================================================================
# 4. A0 schema fix + preview fingerprint — xfail until shipped
# ===========================================================================


def test_case23_clerk_user_id_not_globally_unique():
    """Case 23 (A0): the model must stop declaring a global unique on
    clerk_user_id so create_all-built previews match the migrated head."""
    assert UserModel.__table__.c.clerk_user_id.unique is not True


def test_M1_fingerprint_covers_unique_constraint_change():
    """M1: dropping a UNIQUE constraint must bust the preview trust marker, or a
    reused preview silently keeps the old constraint."""
    mod = _load_bootstrap()
    with_uc = sa.MetaData()
    sa.Table(
        "users", with_uc,
        sa.Column("id", sa.String), sa.Column("clerk_user_id", sa.String),
        sa.UniqueConstraint("clerk_user_id", name="uq_users_clerk"),
    )
    without_uc = sa.MetaData()
    sa.Table(
        "users", without_uc,
        sa.Column("id", sa.String), sa.Column("clerk_user_id", sa.String),
    )
    assert mod._fingerprint_metadata(with_uc) != mod._fingerprint_metadata(without_uc)


# ===========================================================================
# 5. Optional github_id correctness path — xfail until column/schema added
# ===========================================================================


@pytest.mark.xfail(strict=True, reason="github_id column not yet added (models.py)")
def test_user_model_has_github_id():
    assert "github_id" in UserModel.__table__.c


@pytest.mark.xfail(strict=True, reason="github_id not yet on TaskSweepSubmission (oddish/schemas.py:373)")
def test_submission_carries_github_id():
    assert "github_id" in TaskSweepSubmission.model_fields


# ===========================================================================
# 6. Owner-stamp None-safety (M3) — already covered
# ===========================================================================
# M3 (resolving to no owner must never CLEAR attribution) is guarded by
# tasks.py:350-351 and the existing tests/test_stamp_experiment_owner.py
# (test_ignores_missing_inputs); not duplicated here.


# ===========================================================================
# 7. Experiments-repo Action contract — skipped (Action not in this repo)
# ===========================================================================


@pytest.mark.skip(reason="experiments-repo Action not present in this repo (account-merge-plan.md:25-28)")
def test_case6_action_forces_github_user_to_actor():
    """Case 6 / biggest risk: the Action must submit --github-user=$GITHUB_ACTOR and
    forbid repo config / a CLI flag from overriding it (checked id == submitted id)."""


@pytest.mark.skip(reason="experiments-repo Action not present in this repo")
def test_case18_action_fail_open_when_endpoint_down():
    """Case 18: if the linkage endpoint is unreachable, the Action ALLOWS the push
    (fail-open) rather than blocking CI."""
