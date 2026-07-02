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

import asyncio
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
from api.routers.task_submission import (
    _lookup_user_by_github_id,
    _lookup_user_by_github_username,
    resolve_experiment_owner_user_id,
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
    # resolve_experiment_owner_user_id reads org_id / user_id / api_key_id / api_key.
    return SimpleNamespace(org_id=org_id, user_id=user_id, api_key_id=None, api_key=None)


def _submission(
    github_username: str | None, *, github_id: str | None = None
) -> TaskSweepSubmission:
    return TaskSweepSubmission(
        task_id="task_lg",
        configs=[
            AgentModelPair(
                agent="claude-code", model="anthropic/claude-sonnet-4-6", n_trials=1
            )
        ],
        user=None,
        github_username=github_username,
        github_id=github_id,
    )


async def _set_github_id(user: UserModel, github_id: str) -> None:
    """Persist a github_id on an already-created fixture user (the shared fixture
    leaves github_id None). Re-selects the row in a fresh session and mutates."""
    async with get_session() as session:
        row = await session.get(UserModel, user.id)
        row.github_id = github_id
    user.github_id = github_id


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


async def _resolve(
    org_id: str, handle: str | None, *, github_id: str | None = None
) -> str | None:
    async with get_session() as session:
        return await resolve_experiment_owner_user_id(
            session, _submission(handle, github_id=github_id), _auth(org_id)
        )


async def _linkage(
    client, raw_key: str, handle: str | None = None, *, actor_id: str | None = None
):
    params: dict[str, str] = {}
    if handle is not None:
        params["handle"] = handle
    if actor_id is not None:
        params["actor_id"] = actor_id
    return await client.get(
        "/github/linkage",
        params=params,
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
    from auth.provisioning import ClerkGithubIdentity

    async def _clerk_returns_new_handle(_clerk_user_id: str) -> ClerkGithubIdentity:
        return ClerkGithubIdentity(username="new_handle", email=None, github_id=None)

    monkeypatch.setattr(gh, "_fetch_clerk_identity", _clerk_returns_new_handle)

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
async def test_case17a_stored_match_short_circuits_clerk(client, org_with_users, monkeypatch):
    """Case 17 (fail-open, part a): a stored handle that already matches the actor
    resolves on the FIRST predicate pass, so the refresh never runs and Clerk is
    never consulted — a Clerk outage therefore cannot flip a real link to unlinked.
    Asserts the fetch seam is NOT called (the original test only implied this)."""
    import api.routers.github_linkage as gh

    calls: list[str] = []

    async def _clerk_down(clerk_user_id: str):
        calls.append(clerk_user_id)
        raise httpx.HTTPError("clerk unreachable")

    monkeypatch.setattr(gh, "_fetch_clerk_identity", _clerk_down)

    org_id, add = org_with_users
    alice = await add("alice")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "alice")
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True and body["user_id"] == alice.id  # stale, not unlinked
        assert calls == []  # short-circuited: Clerk was never consulted
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_case17b_refresh_raise_is_fail_open_not_500(client, org_with_users, monkeypatch):
    """Case 17 (fail-open, part b): on a MISS the refresh actually runs; if the
    Clerk fetch raises a NON-httpx error (e.g. a malformed-200 JSONDecodeError —
    the class the lessons doc warns escapes an httpx-only catch), the endpoint must
    still answer 200 (linked:false), NOT 500. This is the coverage the handle-match
    case can't provide: it forces the refresh path (called["n"] proves the raising
    fetch ran). The ValueError is caught by the per-user `except Exception` inside
    _refresh_org_github_identities; the endpoint's outer `except Exception` is a
    second backstop. So this pins end-to-end fail-open (a non-httpx raise -> 200),
    which breaks only if BOTH catches are narrowed off `except Exception`."""
    import api.routers.github_linkage as gh

    called = {"n": 0}

    async def _clerk_boom(clerk_user_id: str):
        called["n"] += 1
        raise ValueError("malformed clerk 200")  # non-httpx: escapes a narrow catch

    monkeypatch.setattr(gh, "_fetch_clerk_identity", _clerk_boom)

    org_id, add = org_with_users
    await add("alice")  # a stored member, so the refresh has someone to fetch
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "ghost_actor")  # unstored -> first pass misses
        assert resp.status_code == 200  # fail-open, not a 500
        assert resp.json()["linked"] is False
        assert called["n"] >= 1  # the refresh path really executed the raising fetch
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


def test_user_model_has_github_id():
    assert "github_id" in UserModel.__table__.c


def test_user_model_github_id_org_scoped_unique_and_indexed():
    """G1: github_id is org-scoped unique (NOT global) and indexed for lookup."""
    cols = {"org_id", "github_id"}
    assert any(
        isinstance(c, sa.UniqueConstraint) and {col.name for col in c.columns} == cols
        for c in UserModel.__table__.constraints
    ), "missing UniqueConstraint over (org_id, github_id)"
    assert any(
        {col.name for col in idx.columns} == cols for idx in UserModel.__table__.indexes
    ), "missing index over (org_id, github_id)"


def test_submission_carries_github_id():
    assert "github_id" in TaskSweepSubmission.model_fields


def test_submission_github_id_round_trips_through_serialization():
    """G2 transport: github_id survives model_dump → model_validate intact."""
    submission = _submission("octocat")
    submission.github_id = "123456"
    restored = TaskSweepSubmission.model_validate(submission.model_dump())
    assert restored.github_id == "123456"


def test_submission_github_id_defaults_to_none():
    """Back-compat: github_id is optional; omitting it leaves it None."""
    assert _submission("octocat").github_id is None


# ===========================================================================
# 5b. G4 — github_id lookup precedence + endpoint actor_id
# ===========================================================================


@requires_db
@pytest.mark.asyncio
async def test_lookup_by_github_id_exact_one(org_with_users):
    """G4: an active user carrying github_id resolves by that immutable id."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    async with get_session() as session:
        found = await _lookup_user_by_github_id(
            session, github_id="gid_alice", org_id=org_id
        )
        assert found is not None and found.id == alice.id


@requires_db
@pytest.mark.asyncio
async def test_lookup_by_github_id_org_scoped(org_with_users):
    """G4: a github_id set only in another org must not resolve here (org-unique
    constraint is per-org; lookups always filter org_id)."""
    org_id, add = org_with_users
    other = f"org_other_{uuid.uuid4().hex[:8]}"
    bob = await add("bob", in_org=other)
    await _set_github_id(bob, "gid_bob")
    async with get_session() as session:
        assert (
            await _lookup_user_by_github_id(session, github_id="gid_bob", org_id=org_id)
            is None
        )


@requires_db
@pytest.mark.asyncio
async def test_lookup_by_github_id_active_only(org_with_users):
    """G4: an inactive user holding the github_id is not a match (active-only)."""
    org_id, add = org_with_users
    ghost = await add("ghost", active=False)
    await _set_github_id(ghost, "gid_ghost")
    async with get_session() as session:
        assert (
            await _lookup_user_by_github_id(
                session, github_id="gid_ghost", org_id=org_id
            )
            is None
        )


@requires_db
@pytest.mark.asyncio
async def test_lookup_by_github_id_empty_is_none(org_with_users):
    """G4: empty / whitespace github_id treated as no match (never a bare query)."""
    org_id, _add = org_with_users
    async with get_session() as session:
        assert (
            await _lookup_user_by_github_id(session, github_id="   ", org_id=org_id)
            is None
        )


@requires_db
@pytest.mark.asyncio
async def test_precedence_github_id_beats_stale_handle(org_with_users):
    """G4 precedence: a user with github_id=X and a STALE stored handle; a
    submission carrying github_id=X and a DIFFERENT new handle resolves to that
    user BY ID (immutable id beats the mutable handle)."""
    org_id, add = org_with_users
    renamed = await add("old_handle")
    await _set_github_id(renamed, "gid_renamed")
    # Submission carries the new handle (not stored anywhere) + the immutable id.
    assert (
        await _resolve(org_id, "brand_new_handle", github_id="gid_renamed")
        == renamed.id
    )


@requires_db
@pytest.mark.asyncio
async def test_strict_supplied_id_unmatched_does_not_fall_back_to_handle(org_with_users):
    """Strict resolve: a supplied github_id with no id match resolves to None even
    when the handle matches a user — the id-only predicate is the linkage gate."""
    org_id, add = org_with_users
    await add("alice")
    assert await _resolve(org_id, "alice", github_id="gid_nobody") is None


@requires_db
@pytest.mark.asyncio
async def test_strict_supplied_id_linked_resolves_by_id(org_with_users):
    """Strict resolve (a): a supplied github_id with an exact id match resolves to
    that user, ignoring any handle."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    assert await _resolve(org_id, "wrong_handle", github_id="gid_alice") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_strict_no_id_resolves_by_handle(org_with_users):
    """Strict resolve (c): with no github_id supplied, resolution uses the exact-one
    handle lookup as before."""
    org_id, add = org_with_users
    alice = await add("alice")
    assert await _resolve(org_id, "alice") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_strict_no_id_duplicated_handle_resolves_to_none(org_with_users):
    """Strict resolve (d): no github_id, two users share the handle → None."""
    org_id, add = org_with_users
    await add("twin")
    await add("twin")
    assert await _resolve(org_id, "twin") is None


@requires_db
@pytest.mark.asyncio
async def test_strict_empty_string_id_treated_as_absent(org_with_users):
    """Strict resolve (e): an empty-string github_id is treated as absent, so
    resolution falls through to the handle lookup."""
    org_id, add = org_with_users
    alice = await add("alice")
    assert await _resolve(org_id, "alice", github_id="") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_parity_endpoint_and_sweep_resolve_same_user(client, org_with_users):
    """INV2 parity: for the SAME input (github_id=X), the endpoint (?actor_id=X)
    and resolve_experiment_owner_user_id (submission github_id=X) resolve to the
    SAME user via the shared _resolve_connected_user predicate."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_parity")
    key_id, raw = await _seed_key(org_id)
    try:
        sweep_owner = await _resolve(org_id, None, github_id="gid_parity")
        resp = await _linkage(client, raw, actor_id="gid_parity")
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True
        assert body["user_id"] == sweep_owner == alice.id
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_actor_id_linked(client, org_with_users):
    """G4 endpoint: ?actor_id=<github_id> with an exact id match → linked."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_actor")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, actor_id="gid_actor")
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True and body["user_id"] == alice.id
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_actor_id_unlinked(client, org_with_users):
    """G4 endpoint: ?actor_id with no id match and no handle → unlinked."""
    org_id, add = org_with_users
    await add("alice")  # has no github_id
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, actor_id="gid_missing")
        assert resp.status_code == 200 and resp.json()["linked"] is False
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_actor_id_beats_stale_handle(client, org_with_users):
    """G4 endpoint precedence: ?actor_id matches by id even when the caller also
    passes a stale/wrong handle (id wins)."""
    org_id, add = org_with_users
    renamed = await add("old_handle")
    await _set_github_id(renamed, "gid_ep")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await client.get(
            "/github/linkage",
            params={"handle": "wrong_handle", "actor_id": "gid_ep"},
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True and body["user_id"] == renamed.id
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_back_compat_handle_only_submission_unchanged(org_with_users):
    """Back-compat: a handle-only submission (no github_id) resolves exactly as
    before (owner = the exact-one active handle match)."""
    org_id, add = org_with_users
    alice = await add("alice")
    assert await _resolve(org_id, "alice") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_back_compat_handle_only_endpoint_unchanged(client, org_with_users):
    """Back-compat: a handle-only ?handle= request (no actor_id) resolves exactly
    as before."""
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
async def test_endpoint_refresh_fetches_clerk_concurrently(
    client, org_with_users, monkeypatch
):
    """PERF (round-2 finding): on a miss the per-member Clerk fan-out must run
    CONCURRENTLY (semaphore-bounded), not one sequential GET at a time — and it
    runs OUTSIDE the DB transaction. Patch the fetch seam to record max in-flight;
    a sequential loop would cap it at 1."""
    import api.routers.github_linkage as gh
    from auth.provisioning import ClerkGithubIdentity

    inflight = {"cur": 0, "max": 0}

    async def _slow_fetch(_clerk_user_id: str) -> ClerkGithubIdentity:
        inflight["cur"] += 1
        inflight["max"] = max(inflight["max"], inflight["cur"])
        try:
            await asyncio.sleep(0.05)
            return ClerkGithubIdentity(username=None, email=None, github_id=None)
        finally:
            inflight["cur"] -= 1

    monkeypatch.setattr(gh, "_fetch_clerk_identity", _slow_fetch)

    org_id, add = org_with_users
    for i in range(5):
        await add(f"member{i}")
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "ghost")  # miss -> refresh scans all 5
        assert resp.status_code == 200 and resp.json()["linked"] is False
        assert inflight["max"] >= 2  # concurrent, not sequential
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_actor_id_refresh_backfills_github_id(
    client, org_with_users, monkeypatch
):
    """G4 refresh-on-miss: an active user whose github_id is un-backfilled (None
    in DB) but whose Clerk identity carries it must link by ?actor_id after the
    endpoint's dedicated refresh backfills github_id from Clerk and re-matches by
    id. Refresh stays fail-open + dedicated (M2)."""
    import api.routers.github_linkage as gh
    from auth.provisioning import ClerkGithubIdentity

    org_id, add = org_with_users
    user = await add("carol")  # github_id stays None in DB

    async def _clerk_id(_clerk_user_id: str) -> ClerkGithubIdentity:
        return ClerkGithubIdentity(username=None, email=None, github_id="gid_backfilled")

    monkeypatch.setattr(gh, "_fetch_clerk_identity", _clerk_id)

    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, actor_id="gid_backfilled")
        assert resp.status_code == 200
        body = resp.json()
        assert body["linked"] is True and body["user_id"] == user.id
    finally:
        await _purge(api_key_ids=[key_id])


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
