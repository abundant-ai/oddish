from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import pytest_asyncio

import auth.provisioning as prov
from auth.provisioning import (
    GITHUB_ID_RECHECK_TTL,
    ClerkGithubIdentity,
    _github_account_from_clerk_payload,
    _refresh_user_github_identity,
    _set_github_id_if_absent,
    fetch_github_identity_from_clerk,
    get_or_create_user_in_org,
)
from models import OrganizationModel, UserModel, UserRole
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


def _payload(**account_overrides) -> dict:
    account = {
        "provider": "oauth_github",
        "username": "octocat",
        "email_address": "octo@example.com",
        "provider_user_id": "583231",
    }
    account.update(account_overrides)
    return {"external_accounts": [account]}


def test_payload_parse_extracts_github_id() -> None:
    identity = _github_account_from_clerk_payload(_payload())
    assert identity.username == "octocat"
    assert identity.email == "octo@example.com"
    assert identity.github_id == "583231"


def test_payload_parse_github_id_missing_is_none() -> None:
    identity = _github_account_from_clerk_payload(_payload(provider_user_id=None))
    assert identity.username == "octocat"
    assert identity.github_id is None


def test_payload_parse_no_github_account() -> None:
    identity = _github_account_from_clerk_payload({"external_accounts": []})
    assert identity == ClerkGithubIdentity(None, None, None)


def _mock_clerk_http(monkeypatch, handler) -> None:
    real_client = httpx.AsyncClient

    def _factory(*_args, **kwargs):
        kwargs.pop("timeout", None)
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_client(**kwargs)

    monkeypatch.setattr(prov.httpx, "AsyncClient", _factory)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["no_secret", "http_500", "transport_error"])
async def test_fetch_returns_none_for_nondefinitive_errors(monkeypatch, mode) -> None:
    """Non-definitive Clerk failures return None so callers retry later."""
    if mode == "no_secret":
        monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "")
    else:
        monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")
        if mode == "http_500":
            _mock_clerk_http(monkeypatch, lambda _req: httpx.Response(500))
        else:
            def _boom(_req):
                raise httpx.ConnectError("no route")

            _mock_clerk_http(monkeypatch, _boom)
    assert await fetch_github_identity_from_clerk("clerk_1") is None


@pytest.mark.asyncio
async def test_fetch_404_is_definitive_absent(monkeypatch) -> None:
    """Real error contract: a Clerk 404 means the user is gone — a DEFINITIVE
    no-github answer (empty identity that stamps the marker), not the None
    couldn't-verify sentinel that would re-fetch forever."""
    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")
    _mock_clerk_http(monkeypatch, lambda _req: httpx.Response(404))
    identity = await fetch_github_identity_from_clerk("clerk_gone")
    assert identity == ClerkGithubIdentity(None, None, None)


@pytest.mark.asyncio
async def test_fetch_success_no_github_is_definitive_absent(monkeypatch) -> None:
    """Real success-with-no-github answer is a definitive ClerkGithubIdentity of
    all-None — distinct from the None couldn't-verify sentinel — so the marker
    gets stamped."""
    monkeypatch.setattr(prov, "CLERK_SECRET_KEY", "sk_test")
    _mock_clerk_http(
        monkeypatch,
        lambda _req: httpx.Response(200, json={"external_accounts": []}),
    )
    identity = await fetch_github_identity_from_clerk("clerk_1")
    assert identity == ClerkGithubIdentity(None, None, None)


def _mock_clerk(monkeypatch, identity: ClerkGithubIdentity | None) -> None:
    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return identity

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)


def _user(**overrides) -> UserModel:
    base = {
        "id": f"user_{uuid.uuid4().hex[:8]}",
        "org_id": "org_1",
        "email": "u@example.com",
        "github_username": None,
        "github_id": None,
        "clerk_user_id": "clerk_1",
        "role": UserRole.MEMBER,
        "is_active": True,
    }
    base.update(overrides)
    return UserModel(**base)


@pytest_asyncio.fixture
async def org_id():
    value = f"org_gid_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        session.add(OrganizationModel(id=value, name=value, slug=value))
    try:
        yield value
    finally:
        async with get_session() as session:
            await session.execute(
                UserModel.__table__.delete()
                .where(UserModel.org_id == value)
                .execution_options(include_deleted=True)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == value
                )
            )


@pytest.mark.asyncio
async def test_refresh_sets_github_id_when_missing(monkeypatch) -> None:
    user = _user()
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="octocat", email="o@e.com", github_id="42"),
    )
    await _refresh_user_github_identity(user)
    assert user.github_id == "42"
    assert user.github_username == "octocat"


@pytest.mark.asyncio
async def test_refresh_does_not_overwrite_differing_github_id(monkeypatch) -> None:
    user = _user(github_id="original", github_username="octocat")
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="octocat", email=None, github_id="different"),
    )
    await _refresh_user_github_identity(user)
    assert user.github_id == "original"


@pytest.mark.asyncio
async def test_refresh_clears_stale_id_on_definitive_no_github(monkeypatch) -> None:
    """A user with a github_id but no username re-fetches; a definitive no-github
    answer (Clerk unlinked) drops the stale id and stamps the checked marker."""
    _mock_clerk(monkeypatch, ClerkGithubIdentity(None, None, None))
    user = _user(github_id="stale", github_username=None)
    await _refresh_user_github_identity(user)
    assert user.github_id is None
    assert isinstance(user.attribution_cache.get("github_id_checked"), str)


@pytest.mark.asyncio
async def test_refresh_relinks_changed_id_when_username_absent(monkeypatch) -> None:
    """A user with a github_id but no username re-fetches; a different Clerk id
    (relinked account) replaces the stale one via reconcile."""
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="octocat", email=None, github_id="new"),
    )
    user = _user(github_id="old", github_username=None)
    await _refresh_user_github_identity(user)
    assert user.github_id == "new"
    assert user.github_username == "octocat"


@pytest.mark.asyncio
async def test_refresh_no_clerk_call_once_github_id_known(monkeypatch) -> None:
    """Hot-path guard: a user with a github_username AND a known github_id (either
    the column is set or the checked marker is stamped) must NOT trigger a Clerk
    GET on refresh."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="999")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username="octocat",
        github_id="already-set",
        attribution_cache={"refreshed_at": "2020-01-01T00:00:00+00:00"},
    )
    await _refresh_user_github_identity(user)
    assert called is False


@pytest.mark.asyncio
async def test_refresh_fills_github_id_for_cached_pre_deploy_user(monkeypatch) -> None:
    """Login-fill regression: a pre-deploy user with a cached github_username and
    refreshed_at but NULL github_id must still get github_id filled on the next
    refresh (the old early-return starved these users forever)."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="777")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username="octocat",
        github_id=None,
        attribution_cache={
            "github_handles": ["octocat"],
            "legacy_emails": [],
            "refreshed_at": "2020-01-01T00:00:00+00:00",
        },
    )
    await _refresh_user_github_identity(user)
    assert called is True
    assert user.github_id == "777"


@pytest.mark.asyncio
async def test_refresh_stamps_marker_and_skips_second_fetch(monkeypatch) -> None:
    """A fresh checked marker short-circuits later refreshes."""
    calls = 0

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal calls
        calls += 1
        return ClerkGithubIdentity(None, None, None)

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(github_username="octocat", github_id=None)
    await _refresh_user_github_identity(user)
    assert calls == 1
    assert isinstance(user.attribution_cache.get("github_id_checked"), str)
    await _refresh_user_github_identity(user)
    assert calls == 1  # marker short-circuits the second refresh
    assert user.github_id is None

    handleless = _user(
        github_username=None,
        github_id=None,
        attribution_cache={"github_id_checked": datetime.now(timezone.utc).isoformat()},
    )
    await _refresh_user_github_identity(handleless)
    assert calls == 1
    assert handleless.github_id is None


def _stale_marker() -> str:
    stale = datetime.now(timezone.utc) - GITHUB_ID_RECHECK_TTL - timedelta(minutes=5)
    return stale.isoformat()


@pytest.mark.asyncio
async def test_refresh_stale_marker_refetches_and_claims_github_id(monkeypatch) -> None:
    """A stale checked marker refetches and claims a newly linked github_id."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="linked")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username="octocat",
        github_id=None,
        attribution_cache={"github_id_checked": _stale_marker()},
    )
    await _refresh_user_github_identity(user)
    assert called is True
    assert user.github_id == "linked"


@pytest.mark.asyncio
async def test_refresh_stale_marker_still_no_github_restamps(monkeypatch) -> None:
    """A stale marker + still-no-github answer re-fetches and re-stamps the marker
    with a fresh timestamp (so the TTL window restarts)."""
    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return ClerkGithubIdentity(None, None, None)

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    old = _stale_marker()
    user = _user(
        github_username="octocat",
        github_id=None,
        attribution_cache={"github_id_checked": old},
    )
    await _refresh_user_github_identity(user)
    assert user.github_id is None
    new = user.attribution_cache.get("github_id_checked")
    assert isinstance(new, str) and new > old
    # The fresh restamp short-circuits a subsequent refresh.
    assert prov._marker_is_fresh(new)


@pytest.mark.asyncio
async def test_refresh_none_answer_stamps_nothing_then_fills_later(monkeypatch) -> None:
    """A None (Clerk error / secret unset) answer stamps nothing and is retried;
    a later definitive fetch fills the id."""
    result: ClerkGithubIdentity | None = None

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return result

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(github_username="octocat", github_id=None)
    await _refresh_user_github_identity(user)
    cache = user.attribution_cache if isinstance(user.attribution_cache, dict) else {}
    assert "github_id_checked" not in cache
    assert user.github_id is None

    result = ClerkGithubIdentity(username="octocat", email=None, github_id="888")
    await _refresh_user_github_identity(user)
    assert user.github_id == "888"


@pytest.mark.asyncio
async def test_refresh_username_no_id_stamps_nothing_then_fills_later(monkeypatch) -> None:
    """A partial Clerk answer (username present, id absent) is NOT a definitive
    no-github: it stamps nothing and stays eligible; a later fetch that reports
    the id fills github_id."""
    result = ClerkGithubIdentity(username="octocat", email=None, github_id=None)

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        return result

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(github_username=None, github_id=None)
    await _refresh_user_github_identity(user)
    cache = user.attribution_cache if isinstance(user.attribution_cache, dict) else {}
    assert "github_id_checked" not in cache
    assert user.github_id is None
    assert user.github_username == "octocat"

    result = ClerkGithubIdentity(username="octocat", email=None, github_id="888")
    await _refresh_user_github_identity(user)
    assert user.github_id == "888"


@pytest.mark.asyncio
async def test_refresh_handleless_stale_marker_refetches_and_claims(monkeypatch) -> None:
    """Handle-less user with a STALE marker re-fetches Clerk and claims a
    now-present github_id (self-heal once the TTL lapses)."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="linked")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username=None,
        github_id=None,
        attribution_cache={"github_id_checked": _stale_marker()},
    )
    await _refresh_user_github_identity(user)
    assert called is True
    assert user.github_id == "linked"
    assert user.github_username == "octocat"


@pytest.mark.asyncio
async def test_refresh_id_without_username_still_fetches(monkeypatch) -> None:
    """A user with a github_id but NO username must still fetch — the new
    handle-less skip only applies when there is also no id — so the fetch fills
    the missing username."""
    called = False

    async def _fetch(_clerk_user_id: str) -> ClerkGithubIdentity | None:
        nonlocal called
        called = True
        return ClerkGithubIdentity(username="octocat", email=None, github_id="already")

    monkeypatch.setattr(prov, "fetch_github_identity_from_clerk", _fetch)
    user = _user(
        github_username=None,
        github_id="already",
        attribution_cache={"github_id_checked": datetime.now(timezone.utc).isoformat()},
    )
    await _refresh_user_github_identity(user)
    assert called is True
    assert user.github_username == "octocat"


@requires_db
@pytest.mark.asyncio
async def test_provisioning_sets_github_id_on_new_user(monkeypatch, org_id) -> None:
    clerk_user_id = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="newby", email="n@e.com", github_id="999"),
    )
    async with get_session() as session:
        org = await session.get(OrganizationModel, org_id)
        user = await get_or_create_user_in_org(
            session, clerk_user_id, org, "n@e.com", "member", UserRole.MEMBER
        )
        assert user.github_id == "999"


@requires_db
@pytest.mark.asyncio
async def test_provisioning_github_id_collision_does_not_crash(
    monkeypatch, org_id
) -> None:
    """Two users in the same org would share a github_id (uq_users_org_github_id).
    Provisioning must stay fail-open: skip the colliding set, never raise."""
    existing_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide"),
    )
    async with get_session() as session:
        session.add(
            UserModel(
                id=f"user_{uuid.uuid4().hex[:8]}",
                org_id=org_id,
                email="existing@e.com",
                github_id="collide",
                clerk_user_id=existing_clerk,
                role=UserRole.MEMBER,
                is_active=True,
            )
        )
        await session.flush()

    async with get_session() as session:
        org = await session.get(OrganizationModel, org_id)
        user = await get_or_create_user_in_org(
            session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
        )
        assert user.github_id is None


@requires_db
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "holder_overrides",
    [
        {"is_active": False, "deleted_at": datetime(2020, 1, 1, tzinfo=timezone.utc)},
        {"is_active": False},
    ],
)
async def test_provisioning_relink_releases_inactive_holder(
    monkeypatch, org_id, holder_overrides
) -> None:
    """Inactive holder rows release github_id so a rejoining user can claim it."""
    old_id = f"user_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="relink-id"),
    )
    async with get_session() as session:
        session.add(
            UserModel(
                id=old_id,
                org_id=org_id,
                email="holder@e.com",
                github_id="relink-id",
                clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                role=UserRole.MEMBER,
                **holder_overrides,
            )
        )
        await session.flush()

    async with get_session() as session:
        org = await session.get(OrganizationModel, org_id)
        user = await get_or_create_user_in_org(
            session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
        )
        assert user.github_id == "relink-id"
        released = await session.execute(
            UserModel.__table__.select()
            .where(UserModel.id == old_id)
            .execution_options(include_deleted=True)
        )
        assert released.mappings().one()["github_id"] is None


@requires_db
@pytest.mark.asyncio
async def test_provisioning_github_id_active_clash_still_skips(
    monkeypatch, org_id
) -> None:
    """An ACTIVE (not soft-deleted, is_active) clash row keeps its github_id and
    the new user is skipped — two live users must never share a github_id."""
    holder_id = f"user_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide-live"),
    )
    async with get_session() as session:
        session.add(
            UserModel(
                id=holder_id,
                org_id=org_id,
                email="live@e.com",
                github_id="collide-live",
                clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                role=UserRole.MEMBER,
                is_active=True,
            )
        )
        await session.flush()

    async with get_session() as session:
        org = await session.get(OrganizationModel, org_id)
        user = await get_or_create_user_in_org(
            session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
        )
        assert user.github_id is None
        holder = await session.get(UserModel, holder_id)
        assert holder.github_id == "collide-live"


@requires_db
@pytest.mark.asyncio
async def test_provisioning_active_clash_does_not_stamp_marker(
    monkeypatch, org_id
) -> None:
    """An active id clash is not stamped as a definitive no-github answer."""
    holder_id = f"user_{uuid.uuid4().hex[:8]}"
    new_clerk = f"clerk_{uuid.uuid4().hex[:8]}"
    _mock_clerk(
        monkeypatch,
        ClerkGithubIdentity(username="dup", email="d@e.com", github_id="collide-x"),
    )
    async with get_session() as session:
        session.add(
            UserModel(
                id=holder_id,
                org_id=org_id,
                email="holder@e.com",
                github_id="collide-x",
                clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                role=UserRole.MEMBER,
                is_active=True,
            )
        )
        await session.flush()

    async with get_session() as session:
        org = await session.get(OrganizationModel, org_id)
        user = await get_or_create_user_in_org(
            session, new_clerk, org, "new@e.com", "member", UserRole.MEMBER
        )
        assert user.github_id is None
        cache = user.attribution_cache or {}
        assert "github_id_checked" not in cache


@requires_db
@pytest.mark.asyncio
async def test_set_github_id_savepoint_swallows_concurrent_race(org_id) -> None:
    """Genuine concurrent race: two separate sessions claim the same github_id for
    two users in one org at once. Exactly one wins uq_users_org_github_id; the
    loser's claim trips the constraint at the SAVEPOINT flush, which must swallow
    the IntegrityError, leave that user's github_id None, and keep its transaction
    committable — neither claim may raise."""
    a_id = f"user_{uuid.uuid4().hex[:8]}"
    b_id = f"user_{uuid.uuid4().hex[:8]}"
    async with get_session() as session:
        for uid, email in ((a_id, "a@e.com"), (b_id, "b@e.com")):
            session.add(
                UserModel(
                    id=uid,
                    org_id=org_id,
                    email=email,
                    github_id=None,
                    clerk_user_id=f"clerk_{uuid.uuid4().hex[:8]}",
                    role=UserRole.MEMBER,
                    is_active=True,
                )
            )

    async def _claim(uid: str) -> str | None:
        async with get_session() as session:
            user = await session.get(UserModel, uid)
            await _set_github_id_if_absent(session, user, "raced")
            return user.github_id

    results = await asyncio.wait_for(
        asyncio.gather(_claim(a_id), _claim(b_id)), timeout=15
    )
    # Exactly one wins the id; the loser is swallowed to None, no exception.
    assert sorted(results, key=lambda v: v or "") == [None, "raced"]

    async with get_session() as session:
        a = await session.get(UserModel, a_id)
        b = await session.get(UserModel, b_id)
        holders = [u.github_id for u in (a, b) if u.github_id == "raced"]
        assert len(holders) == 1  # persisted exactly once
