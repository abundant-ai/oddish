"""Suite for the github_id linkage gate.

An experiments-repo GitHub Action checks that github.actor is a Clerk-linked
Oddish user (via GET /github/linkage) before pushing tasks; the server-side gate
at /tasks/sweep re-checks the same linkage and 403s an unlinked github_id before
any rows are written. Owner resolves on the same strict predicate. No OIDC;
direct API-key bypass accepted; fail-open on Clerk outage. The experiments-repo
Action itself is not in this repo, so its contract tests are skipped.
"""

from __future__ import annotations

import importlib.util
import os
import uuid
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient

from fastapi import HTTPException
from sqlalchemy import func, select, text

from api.app import create_app
from api.routers.task_submission import (
    _lookup_user_by_github_id,
    _lookup_user_by_github_username,
    require_connected_github_user,
    resolve_experiment_owner_user_id,
)
from models import APIKeyScope, OrganizationModel, SubmissionIdempotency, UserModel
from oddish.core.api_keys import create_api_key
from oddish.core.idempotency import (
    STATUS_COMPLETED,
    STATUS_IN_PROGRESS,
    SWEEP_ROUTE,
    compute_request_hash,
    hash_idempotency_key,
)
from oddish.db import TrialModel, get_session, utcnow
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


async def _seed_tasks_key(org_id: str) -> tuple[str, str]:
    """Create an org-scoped TASKS API key; return (key_id, raw_token)."""
    model, raw = create_api_key(org_id=org_id, name="lg-sweep", scope=APIKeyScope.TASKS)
    async with get_session() as session:
        session.add(model)
    return model.id, raw


async def _seed_task(org_id: str) -> str:
    """Insert a bare append-target task in ``org_id``; return its id.

    The task has no trials yet, so an ``append_to_task`` sweep runs in append
    mode and auto-creates an experiment for it. Cleaned up by ``_purge_tasks``.
    """
    task_id = f"task_lg_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        await session.execute(
            text(
                "insert into tasks "
                '(id,name,org_id,"user",priority,status,task_path,tags,'
                "effective_tag_ids,current_version_tag_ids,"
                "run_analysis,run_probe,created_at,updated_at) "
                "values (:id,:id,:org,'u','LOW','COMPLETED','p','{}'::jsonb,"
                "'{}'::text[],'{}'::text[],false,false,now(),now())"
            ),
            {"id": task_id, "org": org_id},
        )
    return task_id


async def _purge_tasks(task_ids: list[str]) -> None:
    from oddish.db import TaskModel

    async with get_session() as session:
        await session.execute(
            text("delete from worker_jobs where subject_id = any(:t)"), {"t": task_ids}
        )
        await session.execute(
            TrialModel.__table__.delete().where(TrialModel.task_id.in_(task_ids))
        )
        await session.execute(
            text("delete from experiments where org_id in "
                 "(select org_id from tasks where id = any(:t))"),
            {"t": task_ids},
        )
        await session.execute(
            TaskModel.__table__.delete().where(TaskModel.id.in_(task_ids))
        )


async def _trial_count(task_id: str) -> int:
    async with get_session() as session:
        return await session.scalar(
            select(func.count())
            .select_from(TrialModel)
            .where(TrialModel.task_id == task_id)
        )


def _body_request_hash(body: dict) -> str:
    """The route fingerprints the RAW submission the client posted; mirror it by
    validating the body into the schema and hashing that, so a seeded record's
    request_hash matches a real retry of the same body."""
    return compute_request_hash(TaskSweepSubmission.model_validate(body))


async def _seed_idempotency(
    org_id: str,
    raw_key: str,
    *,
    status: str,
    request_hash: str,
    response_json: dict | None = None,
    expired: bool = False,
) -> None:
    now = utcnow()
    async with get_session() as session:
        session.add(
            SubmissionIdempotency(
                org_id=org_id,
                route=SWEEP_ROUTE,
                key_hash=hash_idempotency_key(raw_key),
                request_hash=request_hash,
                status=status,
                response_json=response_json,
                expires_at=(
                    now - timedelta(seconds=1) if expired else now + timedelta(hours=24)
                ),
            )
        )


async def _purge_idempotency(org_id: str) -> None:
    async with get_session() as session:
        await session.execute(
            SubmissionIdempotency.__table__.delete().where(
                SubmissionIdempotency.org_id == org_id
            )
        )


async def _deactivate_and_release_github_id(user: UserModel) -> None:
    """Reproduce the Bugbot precondition: the linked user leaves the org after
    the first sweep — deactivated and its github_id released."""
    async with get_session() as session:
        row = await session.get(UserModel, user.id)
        row.is_active = False
        row.github_id = None


def _sweep_body(task_id: str, *, github_id: str | None = None) -> dict:
    body: dict = {
        "task_id": task_id,
        "append_to_task": True,
        "configs": [
            {
                "agent": "claude-code",
                "model": "anthropic/claude-sonnet-4-6",
                "n_trials": 1,
            }
        ],
    }
    if github_id is not None:
        body["github_id"] = github_id
    return body


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
# 3. Linkage endpoint — GET /github/linkage
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
async def test_endpoint_inactive_user_excluded(client, org_with_users):
    """Plain read: an inactive user holding the handle is not a match (active-only)."""
    org_id, add = org_with_users
    await add("ghost", active=False)
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "ghost")
        assert resp.status_code == 200 and resp.json()["linked"] is False
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
# 4. A0 schema fix + preview fingerprint
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
# 5. Optional github_id correctness path
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
async def test_strict_blank_id_normalized_to_absent_falls_back_to_handle(org_with_users):
    """Strict resolve (e): a blank / whitespace github_id is normalized to None by
    the schema (not a truthy supplied id), so resolution behaves exactly like "no
    id sent" and falls back to the exact-one handle lookup."""
    org_id, add = org_with_users
    alice = await add("alice")
    assert await _resolve(org_id, "alice", github_id="") == alice.id
    assert await _resolve(org_id, "alice", github_id="   ") == alice.id


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


# ===========================================================================
# 8. F2 — server-side 403 gate (require_connected_github_user)
# ===========================================================================


@requires_db
@pytest.mark.asyncio
async def test_gate_unlinked_github_id_raises_403(org_with_users):
    """A truthy github_id resolving to no active org user raises 403."""
    org_id, add = org_with_users
    await add("alice")  # linked by handle, but no github_id set
    async with get_session() as session:
        with pytest.raises(HTTPException) as excinfo:
            await require_connected_github_user(
                session, _submission("alice", github_id="gid_unlinked"), _auth(org_id)
            )
    assert excinfo.value.status_code == 403
    assert "gid_unlinked" in str(excinfo.value.detail)
    assert "oddish.app" in str(excinfo.value.detail)


@requires_db
@pytest.mark.asyncio
async def test_gate_linked_github_id_returns_user(org_with_users):
    """A truthy github_id with an exact id match returns that user (no raise)."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    async with get_session() as session:
        user = await require_connected_github_user(
            session, _submission("wrong_handle", github_id="gid_alice"), _auth(org_id)
        )
    assert user is not None and user.id == alice.id


@requires_db
@pytest.mark.asyncio
async def test_gate_no_github_id_is_noop(org_with_users):
    """No github_id supplied → gate is a no-op returning None (never raises),
    even when the handle matches nobody."""
    org_id, _add = org_with_users
    async with get_session() as session:
        assert (
            await require_connected_github_user(
                session, _submission("whoever"), _auth(org_id)
            )
            is None
        )


@requires_db
@pytest.mark.asyncio
async def test_gate_empty_github_id_is_noop(org_with_users):
    """A blank github_id is normalized to None by the schema, so the gate treats
    it as absent (no-op) rather than a supplied-but-unresolvable id."""
    org_id, add = org_with_users
    await add("alice")
    submission = _submission("alice", github_id="   ")
    assert submission.github_id is None
    async with get_session() as session:
        assert (
            await require_connected_github_user(session, submission, _auth(org_id))
            is None
        )


def test_blank_github_id_normalizes_to_none():
    """The schema strips blank / whitespace github_id to None so downstream
    attribution, the linkage gate, and the idempotency hash stay consistent."""
    assert _submission("alice", github_id="").github_id is None
    assert _submission("alice", github_id="   ").github_id is None
    assert _submission("alice", github_id=" gid_alice ").github_id == "gid_alice"
    assert _submission("alice", github_id=None).github_id is None


@requires_db
@pytest.mark.asyncio
async def test_sweep_route_unlinked_github_id_403_and_zero_rows(client, org_with_users):
    """/tasks/sweep with an unlinked github_id → 403 AND zero trial rows created."""
    org_id, add = org_with_users
    await add("alice")  # exists, but carries no github_id
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    try:
        resp = await client.post(
            "/tasks/sweep",
            json=_sweep_body(task_id, github_id="gid_unlinked"),
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 403
        assert "gid_unlinked" in resp.json()["detail"]
        assert await _trial_count(task_id) == 0
    finally:
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_sweep_route_linked_github_id_succeeds(client, org_with_users):
    """/tasks/sweep with a linked github_id → 200 with trials created (the gate
    passes the resolved user through instead of raising)."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    try:
        resp = await client.post(
            "/tasks/sweep",
            json=_sweep_body(task_id, github_id="gid_alice"),
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["trials_count"] == 1
        assert await _trial_count(task_id) == 1
    finally:
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_gate_resolved_user_is_reused_for_owner_stamping(org_with_users):
    """The gate's resolved user is threaded into owner resolution: passing it as
    ``connected_user`` yields that user id WITHOUT a second lookup (the submission
    here carries an id that would MISS on a fresh resolve, proving reuse)."""
    org_id, add = org_with_users
    alice = await add("alice")
    async with get_session() as session:
        owner = await resolve_experiment_owner_user_id(
            session,
            _submission("alice", github_id="gid_nonexistent"),
            _auth(org_id),
            connected_user=alice,
        )
    assert owner == alice.id


@requires_db
@pytest.mark.asyncio
async def test_sweep_route_no_github_id_unchanged(client, org_with_users):
    """/tasks/sweep with NO github_id → behavior unchanged (200, trials created)."""
    org_id, _add = org_with_users
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    try:
        resp = await client.post(
            "/tasks/sweep",
            json=_sweep_body(task_id),
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["trials_count"] == 1
        assert await _trial_count(task_id) == 1
    finally:
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_batch_route_gates_each_submission(client, org_with_users):
    """/tasks/sweep/batch gates every submission: the unlinked item is a per-item
    403 with zero rows, while the linked sibling still succeeds."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    key_id, raw = await _seed_tasks_key(org_id)
    linked_task = await _seed_task(org_id)
    unlinked_task = await _seed_task(org_id)
    try:
        resp = await client.post(
            "/tasks/sweep/batch",
            json={
                "submissions": [
                    _sweep_body(linked_task, github_id="gid_alice"),
                    _sweep_body(unlinked_task, github_id="gid_unlinked"),
                ]
            },
            headers={"Authorization": f"Bearer {raw}"},
        )
        assert resp.status_code == 207  # mixed: one ok, one gated
        results = {r["index"]: r for r in resp.json()["results"]}
        assert results[0]["success"] is True
        assert results[1]["success"] is False
        assert results[1]["status_code"] == 403
        assert "gid_unlinked" in results[1]["error"]
        # The linked item committed its trial; the gated item wrote nothing.
        assert await _trial_count(linked_task) == 1
        assert await _trial_count(unlinked_task) == 0
    finally:
        await _purge_tasks([linked_task, unlinked_task])
        await _purge(api_key_ids=[key_id])


# ===========================================================================
# 9. F8 — a COMPLETED idempotency replay must bypass the linkage gate
# ===========================================================================
# The gate runs in the route BEFORE create_task_sweep_core (where the reserve /
# replay lives), so a faithful retry of an already-completed sweep would be 403d
# if the linked user was deactivated / lost their github_id in between. A probe
# in the route replays a COMPLETED, hash-matched, unexpired record before the
# gate; every other case falls through to the unchanged gate-then-core path.


@requires_db
@pytest.mark.asyncio
async def test_f8_completed_replay_bypasses_gate_after_user_deactivated(
    client, org_with_users
):
    """Bugbot scenario: sweep created with a linked github_id + Idempotency-Key;
    the linked user is then deactivated and its github_id released; the SAME
    key+body retried → 200 with the ORIGINAL stored response (not 403), and no
    duplicate trials."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    body = _sweep_body(task_id, github_id="gid_alice")
    headers = {"Authorization": f"Bearer {raw}", "Idempotency-Key": "f8-key"}
    try:
        first = await client.post("/tasks/sweep", json=body, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["trials_count"] == 1
        original = first.json()
        assert await _trial_count(task_id) == 1

        # The linked user leaves the org between attempts: a fresh gate would 403.
        await _deactivate_and_release_github_id(alice)

        retry = await client.post("/tasks/sweep", json=body, headers=headers)
        assert retry.status_code == 200, retry.text
        # Replays the ORIGINAL response verbatim — same task + trial ids.
        assert retry.json()["new_trial_ids"] == original["new_trial_ids"]
        assert retry.json()["id"] == original["id"]
        # And crucially no duplicate trials were created.
        assert await _trial_count(task_id) == 1
    finally:
        await _purge_idempotency(org_id)
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_f8_fresh_key_unlinked_still_403_and_zero_rows(client, org_with_users):
    """A FRESH Idempotency-Key with an unlinked github_id has no completed record
    to replay, so the probe returns None and the gate still 403s with zero rows
    (and no idempotency record is left behind, since the gate precedes reserve)."""
    org_id, add = org_with_users
    await add("alice")  # exists, but carries no github_id
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    try:
        resp = await client.post(
            "/tasks/sweep",
            json=_sweep_body(task_id, github_id="gid_unlinked"),
            headers={"Authorization": f"Bearer {raw}", "Idempotency-Key": "fresh-key"},
        )
        assert resp.status_code == 403
        assert "gid_unlinked" in resp.json()["detail"]
        assert await _trial_count(task_id) == 0
        async with get_session() as session:
            left = await session.scalar(
                select(func.count())
                .select_from(SubmissionIdempotency)
                .where(SubmissionIdempotency.org_id == org_id)
            )
        assert left == 0
    finally:
        await _purge_idempotency(org_id)
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_f8_completed_record_with_mismatched_hash_is_not_replayed(
    client, org_with_users
):
    """A COMPLETED record whose request_hash does NOT match the retry body must
    NOT be replayed (predicate parity with reserve_idempotency_slot). With an
    unlinked github_id it falls through to the gate → 403 (asserted deliberately),
    proving the probe never bypasses the gate on a hash mismatch."""
    org_id, add = org_with_users
    await add("alice")  # no github_id → gate would 403
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    body = _sweep_body(task_id, github_id="gid_unlinked")
    try:
        # Same key, but a request_hash for a DIFFERENT body -> not a faithful retry.
        await _seed_idempotency(
            org_id,
            "mismatch-key",
            status=STATUS_COMPLETED,
            request_hash="deadbeef" * 8,
            response_json={"id": "should-not-be-returned", "new_trial_ids": []},
        )
        resp = await client.post(
            "/tasks/sweep",
            json=body,
            headers={
                "Authorization": f"Bearer {raw}",
                "Idempotency-Key": "mismatch-key",
            },
        )
        assert resp.status_code == 403
        assert "gid_unlinked" in resp.json()["detail"]
        assert await _trial_count(task_id) == 0
    finally:
        await _purge_idempotency(org_id)
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_f8_in_progress_record_does_not_replay_early(client, org_with_users):
    """An IN_PROGRESS record (even hash-matched) must not replay in the probe; it
    falls through to the gate. With an unlinked github_id that means a 403
    (asserted deliberately) rather than an early 200 replay."""
    org_id, add = org_with_users
    await add("alice")  # no github_id → gate would 403
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    body = _sweep_body(task_id, github_id="gid_unlinked")
    try:
        await _seed_idempotency(
            org_id,
            "inflight-key",
            status=STATUS_IN_PROGRESS,
            request_hash=_body_request_hash(body),
            response_json=None,
        )
        resp = await client.post(
            "/tasks/sweep",
            json=body,
            headers={
                "Authorization": f"Bearer {raw}",
                "Idempotency-Key": "inflight-key",
            },
        )
        assert resp.status_code == 403
        assert "gid_unlinked" in resp.json()["detail"]
        assert await _trial_count(task_id) == 0
    finally:
        await _purge_idempotency(org_id)
        await _purge_tasks([task_id])
        await _purge(api_key_ids=[key_id])
