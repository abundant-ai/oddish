"""Suite for the github_id linkage gate.

The endpoint and sweep route share the same org-scoped linkage predicate.
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
    """Deactivate a user and release its github_id between idempotent retries."""
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


# ---------------------------------------------------------------------------
# Owner resolution
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_exact_one_active_handle_resolves_owner(org_with_users):
    """Exactly one active matching handle resolves as the owner."""
    org_id, add = org_with_users
    alice = await add("alice")
    assert await _resolve(org_id, "alice") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_inactive_handle_user_not_resolved(org_with_users):
    """Inactive users are excluded from handle resolution."""
    org_id, add = org_with_users
    await add("ghost", active=False)
    assert await _resolve(org_id, "ghost") is None


@requires_db
@pytest.mark.asyncio
async def test_cross_org_handle_not_resolved(org_with_users):
    """A handle linked only in another org does not resolve."""
    org_id, add = org_with_users
    other = f"org_other_{uuid.uuid4().hex[:8]}"
    await add("bob", in_org=other)
    assert await _resolve(org_id, "bob") is None


@requires_db
@pytest.mark.asyncio
async def test_handle_lookup_normalizes_prefix_and_case(org_with_users):
    """Handle lookup ignores @ prefixes and case."""
    org_id, add = org_with_users
    alice = await add("OctoCat")
    assert await _resolve(org_id, "@octocat") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_unlinked_handle_resolves_to_no_owner(org_with_users):
    """An unlinked handle resolves to no owner without raising."""
    org_id, _add = org_with_users
    assert await _resolve(org_id, "nobody-here") is None


@requires_db
@pytest.mark.asyncio
async def test_duplicate_handle_resolves_to_no_owner(org_with_users):
    """Duplicate active handles resolve to no owner without raising."""
    org_id, add = org_with_users
    await add("twin")
    await add("twin")
    assert await _resolve(org_id, "twin") is None


# ---------------------------------------------------------------------------
# Linkage endpoint
# ---------------------------------------------------------------------------


@requires_db
@pytest.mark.asyncio
async def test_endpoint_links_exact_one_active_handle(client, org_with_users):
    """The endpoint links exactly one active matching handle."""
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
    """Unknown handles return linked=false."""
    org_id, _add = org_with_users
    key_id, raw = await _seed_key(org_id)
    try:
        resp = await _linkage(client, raw, "ghost")
        assert resp.status_code == 200 and resp.json()["linked"] is False
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_blank_actor_id_falls_back_to_handle(client, org_with_users):
    """A blank actor_id query param is absent, so handle lookup still applies."""
    org_id, add = org_with_users
    alice = await add("alice")
    key_id, raw = await _seed_key(org_id)
    try:
        for blank in ("", "   "):
            resp = await _linkage(client, raw, "alice", actor_id=blank)
            assert resp.status_code == 200
            body = resp.json()
            assert body["linked"] is True and body["user_id"] == alice.id
    finally:
        await _purge(api_key_ids=[key_id])


@requires_db
@pytest.mark.asyncio
async def test_endpoint_ambiguous_not_linked_not_500(client, org_with_users):
    """Duplicate active handles return linked=false without raising."""
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
async def test_endpoint_requires_auth_and_accepts_read_scope(client, org_with_users):
    """Unauthenticated requests fail and READ-scoped org keys succeed."""
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
async def test_endpoint_handle_lookup_is_org_scoped(client, org_with_users):
    """A key from another org sees a foreign handle as unlinked."""
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


# ---------------------------------------------------------------------------
# Schema and preview fingerprint
# ---------------------------------------------------------------------------


def test_clerk_user_id_is_not_globally_unique():
    """The model must not declare a global clerk_user_id unique constraint."""
    assert UserModel.__table__.c.clerk_user_id.unique is not True


def test_preview_fingerprint_covers_unique_constraint_change():
    """Unique constraint changes must alter the preview schema fingerprint."""
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


# ---------------------------------------------------------------------------
# github_id lookup and precedence
# ---------------------------------------------------------------------------


def test_user_model_github_id_org_scoped_unique_and_indexed():
    """github_id is org-scoped unique and indexed for lookup."""
    cols = {"org_id", "github_id"}
    assert any(
        isinstance(c, sa.UniqueConstraint) and {col.name for col in c.columns} == cols
        for c in UserModel.__table__.constraints
    ), "missing UniqueConstraint over (org_id, github_id)"
    assert any(
        {col.name for col in idx.columns} == cols for idx in UserModel.__table__.indexes
    ), "missing index over (org_id, github_id)"


def test_submission_github_id_round_trips_through_serialization():
    """github_id survives model_dump and model_validate."""
    submission = _submission("octocat")
    submission.github_id = "123456"
    restored = TaskSweepSubmission.model_validate(submission.model_dump())
    assert restored.github_id == "123456"


def test_submission_github_id_defaults_to_none():
    """Omitting github_id leaves it as None."""
    assert _submission("octocat").github_id is None


@requires_db
@pytest.mark.asyncio
async def test_lookup_by_github_id_exact_one(org_with_users):
    """An active user carrying github_id resolves by id."""
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
    """github_id lookup is scoped to the current org."""
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
    """Inactive users are excluded from github_id lookup."""
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
async def test_precedence_github_id_beats_stale_handle(org_with_users):
    """github_id takes precedence over a stale or changed handle."""
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
    when the handle matches a user."""
    org_id, add = org_with_users
    await add("alice")
    assert await _resolve(org_id, "alice", github_id="gid_nobody") is None


@requires_db
@pytest.mark.asyncio
async def test_strict_supplied_id_linked_resolves_by_id(org_with_users):
    """A supplied github_id with an exact id match resolves by id."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    assert await _resolve(org_id, "wrong_handle", github_id="gid_alice") == alice.id


@requires_db
@pytest.mark.asyncio
async def test_parity_endpoint_and_sweep_resolve_same_user(client, org_with_users):
    """The endpoint and sweep owner path resolve the same github_id to one user."""
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
    """The endpoint links an actor_id with an exact github_id match."""
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
    """The endpoint returns unlinked for an unmatched actor_id."""
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
    """The endpoint prefers actor_id over a stale or wrong handle."""
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


# ---------------------------------------------------------------------------
# Server-side sweep gate
# ---------------------------------------------------------------------------


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
async def test_gate_403_log_records_authenticating_principal(org_with_users, caplog):
    """The 403 log line names the authenticating principal."""
    import logging

    org_id, add = org_with_users
    await add("alice")  # no github_id set → gate rejects
    auth = SimpleNamespace(
        org_id=org_id,
        user_id="user_ci_bot",
        api_key_id="apikey_ci123",
        api_key=SimpleNamespace(name="ci-pipeline-key"),
    )
    with caplog.at_level(logging.INFO, logger="api.routers.task_submission"):
        async with get_session() as session:
            with pytest.raises(HTTPException):
                await require_connected_github_user(
                    session, _submission("alice", github_id="gid_unlinked"), auth
                )
    rejections = [r for r in caplog.records if "linkage gate rejected" in r.message]
    assert rejections, "expected a linkage-gate rejection log record"
    rendered = rejections[0].getMessage()
    assert "apikey_ci123" in rendered
    assert "ci-pipeline-key" in rendered
    assert "user_ci_bot" in rendered


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
    """/tasks/sweep with a linked github_id creates trials."""
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
    """The gate's resolved user is reused for owner resolution."""
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
    """/tasks/sweep/batch gates each submission independently."""
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


# ---------------------------------------------------------------------------
# Idempotency replay before gate
# ---------------------------------------------------------------------------
# The gate runs in the route BEFORE create_task_sweep_core (where the reserve /
# replay lives), so a faithful retry of an already-completed sweep would be 403d
# if the linked user was deactivated / lost their github_id in between. A probe
# in the route replays a COMPLETED, hash-matched, unexpired record before the
# gate; every other case falls through to the unchanged gate-then-core path.


@requires_db
@pytest.mark.asyncio
async def test_completed_replay_bypasses_gate_after_user_deactivated(
    client, org_with_users
):
    """A completed replay returns the stored response before rechecking linkage."""
    org_id, add = org_with_users
    alice = await add("alice")
    await _set_github_id(alice, "gid_alice")
    key_id, raw = await _seed_tasks_key(org_id)
    task_id = await _seed_task(org_id)
    body = _sweep_body(task_id, github_id="gid_alice")
    headers = {"Authorization": f"Bearer {raw}", "Idempotency-Key": "replay-key"}
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
async def test_fresh_key_unlinked_still_403_and_zero_rows(client, org_with_users):
    """A fresh Idempotency-Key has no replay and still runs the gate."""
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
async def test_completed_record_with_mismatched_hash_is_not_replayed(
    client, org_with_users
):
    """A completed record with a mismatched request hash is not replayed."""
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
async def test_in_progress_record_does_not_replay_early(client, org_with_users):
    """An in-progress record does not replay before completion."""
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
