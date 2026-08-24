from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sqlalchemy.dialects import postgresql

import dashboard_attribution
from api.routers import dashboard as dashboard_router
from auth import APIKeyScope, AuthContext, AuthMethod, require_auth
from dashboard_attribution import (
    AttributionProfile,
    _baseline_profile,
    _db_cache_profile,
    _memory_get,
    _memory_set,
    _persist_profile,
    _row_has_strong_attribution_match,
    _schedule_profile_refresh,
    invalidate_attribution_cache,
    resolve_partial_member_ids,
    resolve_search_authors,
)
from models import UserModel, UserRole


def _user(**overrides) -> UserModel:
    base = {
        "id": "user_1",
        "org_id": "org_1",
        "email": "pratty@abundant.ai",
        "github_username": "praxs",
        "clerk_user_id": "user_clerk",
        "role": "member",
        "is_active": True,
    }
    base.update(overrides)
    return UserModel(**base)


def test_baseline_profile_includes_registered_identity() -> None:
    profile = _baseline_profile(
        _user(),
        blocked_handles=set(),
        blocked_emails=set(),
    )
    assert profile.github_handles == ("praxs",)
    assert "pratty@abundant.ai" in profile.legacy_emails


def test_baseline_profile_blocks_other_member_handles() -> None:
    profile = _baseline_profile(
        _user(github_username="praxs"),
        blocked_handles={"skylark"},
        blocked_emails=set(),
    )
    assert profile.github_handles == ("praxs",)


@pytest.mark.asyncio
async def test_persist_profile_ignores_github_id_checked_at() -> None:
    """The checked-absent marker lives in its own column now, so a profile
    rewrite needs no key-preservation logic: the cache is replaced wholesale
    and github_id_checked_at is untouched."""
    checked_at = datetime(2024, 1, 1, tzinfo=timezone.utc)
    user = _user(
        id="user_persist",
        attribution_cache={"github_handles": [], "legacy_emails": []},
        github_id_checked_at=checked_at,
    )
    await _persist_profile(
        None,
        user,
        AttributionProfile(github_handles=("praxs",), legacy_emails=()),
    )
    assert user.github_id_checked_at == checked_at
    assert user.attribution_cache["github_handles"] == ["praxs"]
    assert "github_id_checked" not in user.attribution_cache
    invalidate_attribution_cache(org_id="org_1", user_id="user_persist")


def test_memory_cache_round_trip() -> None:
    profile = AttributionProfile(
        github_handles=("praxs", "dot-agi"),
        legacy_emails=("ps4534@nyu.edu",),
    )
    _memory_set("org_1", "user_1", profile)
    assert _memory_get("org_1", "user_1") == profile
    invalidate_attribution_cache(org_id="org_1", user_id="user_1")
    assert _memory_get("org_1", "user_1") is None


def test_row_strong_match_rejects_foreign_ci_github_tag() -> None:
    seen_handles = {"praxs"}
    seen_emails = {"pratty@abundant.ai"}
    assert not _row_has_strong_attribution_match(
        "skylark",
        "skylark@example.com",
        seen_handles=seen_handles,
        seen_emails=seen_emails,
    )
    assert not _row_has_strong_attribution_match(
        "skylark",
        "skylark",
        seen_handles=seen_handles,
        seen_emails=seen_emails,
        clerk_email="pratty@abundant.ai",
    )


def test_row_strong_match_accepts_clerk_email_sweep_rows() -> None:
    assert _row_has_strong_attribution_match(
        None,
        "pratty@abundant.ai",
        seen_handles=set(),
        seen_emails=set(),
        clerk_email="pratty@abundant.ai",
    )


def test_row_strong_match_accepts_self_attributed_alias_chain() -> None:
    seen_handles = {"praxs"}
    seen_emails = {"pratty@abundant.ai", "ps4534@nyu.edu"}
    assert _row_has_strong_attribution_match(
        "dot-agi",
        "ps4534@nyu.edu",
        seen_handles=seen_handles,
        seen_emails=seen_emails,
    )


def test_db_cache_fresh_reads_persisted_profile() -> None:
    user = _user(
        attribution_cache={
            "github_handles": ["praxs"],
            "legacy_emails": ["ps4534@nyu.edu"],
            "refreshed_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    profile, fresh = _db_cache_profile(user)
    assert fresh is True
    assert profile is not None
    assert profile.github_handles == ("praxs",)
    assert profile.legacy_emails == ("ps4534@nyu.edu",)


def _cache_dict(age_seconds: int) -> dict:
    refreshed = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return {
        "github_handles": ["praxs"],
        "legacy_emails": ["pratty@abundant.ai"],
        "refreshed_at": refreshed.isoformat(),
    }


def test_db_cache_profile_fresh() -> None:
    profile, fresh = _db_cache_profile(_user(attribution_cache=_cache_dict(60)))
    assert fresh is True
    assert profile is not None and profile.github_handles == ("praxs",)


def test_db_cache_profile_stale_still_returns_profile() -> None:
    profile, fresh = _db_cache_profile(
        _user(attribution_cache=_cache_dict(48 * 60 * 60))
    )
    assert fresh is False
    assert profile is not None and profile.github_handles == ("praxs",)


def test_db_cache_profile_absent() -> None:
    profile, fresh = _db_cache_profile(_user(attribution_cache=None))
    assert profile is None
    assert fresh is False


def test_clerk_is_not_called_from_dashboard_path() -> None:
    # The synchronous Clerk fetch was removed; the module must not even
    # import it any more.
    assert not hasattr(dashboard_attribution, "fetch_github_identity_from_clerk")


def test_schedule_profile_refresh_deduplicates(monkeypatch) -> None:
    created: list[object] = []

    class _FakeTask:
        def add_done_callback(self, _cb) -> None:
            return None

    def _fake_create_task(coro):
        created.append(coro)
        coro.close()
        return _FakeTask()

    monkeypatch.setattr(dashboard_attribution.asyncio, "create_task", _fake_create_task)
    dashboard_attribution._refresh_in_flight.discard("org_1:user_1")
    _schedule_profile_refresh(org_id="org_1", user_id="user_1")
    _schedule_profile_refresh(org_id="org_1", user_id="user_1")
    assert len(created) == 1
    dashboard_attribution._refresh_in_flight.discard("org_1:user_1")


# ---------------------------------------------------------------------------
# Search-author resolver (github:/author:/user: qualifiers)
# ---------------------------------------------------------------------------


class _FakeScalars:
    def __init__(self, users: list[UserModel]) -> None:
        self._users = users

    def all(self) -> list[UserModel]:
        return list(self._users)


class _FakeResult:
    def __init__(self, users: list[UserModel]) -> None:
        self._users = users

    def scalars(self) -> _FakeScalars:
        return _FakeScalars(self._users)


class _CapturingSession:
    """Minimal AsyncSession stand-in: returns canned users, records statements.

    The WHERE clause isn't evaluated (that needs a real DB); these tests
    cover the function contract (plural return, dedup, normalization) and
    compile the captured statement to assert its SQL shape.
    """

    def __init__(self, users: list[UserModel]) -> None:
        self._users = users
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return _FakeResult(self._users)


def _compiled(statement) -> str:  # noqa: ANN001
    return str(
        statement.compile(
            dialect=postgresql.dialect(),  # type: ignore[misc]
            compile_kwargs={"literal_binds": True},
        )
    ).lower()


@pytest.mark.asyncio
async def test_plural_github_lookup_unions_collisions_case_insensitively() -> None:
    from api.routers.task_submission import lookup_users_by_github_username

    u1 = _user(id="user_a", github_username="skylark")
    u2 = _user(id="user_b", github_username="skylark")
    session = _CapturingSession([u1, u2])

    result = await lookup_users_by_github_username(
        session,  # type: ignore[arg-type]
        github_username="@Skylark",
        org_id="org_1",
    )

    # scalars().all() (not scalar_one_or_none) -> collisions union, no raise.
    assert [u.id for u in result] == ["user_a", "user_b"]
    sql = _compiled(session.statements[0])
    assert "lower(users.github_username) = 'skylark'" in sql  # @-stripped + lowered
    assert "users.org_id = 'org_1'" in sql
    assert "users.is_active = true" in sql


@pytest.mark.asyncio
async def test_plural_github_lookup_empty_token_returns_empty() -> None:
    from api.routers.task_submission import lookup_users_by_github_username

    session = _CapturingSession([])
    assert (
        await lookup_users_by_github_username(
            session,  # type: ignore[arg-type]
            github_username="  @  ",
            org_id="org_1",
        )
        == []
    )
    assert session.statements == []  # short-circuits before querying


@pytest.mark.asyncio
async def test_match_authors_for_token_matches_email_or_name() -> None:
    user = _user(id="user_d", email="ada@x.com", name="Ada L", github_username="ada")
    session = _CapturingSession([user])

    result = await dashboard_attribution._match_authors_for_token(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        token="Ada L",
    )

    assert [u.id for u in result] == ["user_d"]  # deduped by id across both queries
    sql = " ".join(_compiled(s) for s in session.statements)
    assert "users.email ilike '%%ada l%%'" in sql
    assert "users.name ilike '%%ada l%%'" in sql
    assert "escape" in sql


class _IdRowsResult:
    def __init__(self, rows: list[tuple[str, str]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, str]]:
        return self._rows


class _CapturingIdSession:
    def __init__(
        self,
        user_ids: list[str],
        *,
        token: str = "kyl",
        rows: list[tuple[str, str]] | None = None,
    ) -> None:
        self._rows = rows or [(token, user_id) for user_id in user_ids]
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return _IdRowsResult(self._rows)


@pytest.mark.asyncio
async def test_partial_member_ids_resolve_canonical_name() -> None:
    session = _CapturingIdSession(["user_kyle"])

    resolved = await resolve_partial_member_ids(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        tokens=("Kyl",),
    )

    assert resolved == {"kyl": ("user_kyle",)}
    sql = _compiled(session.statements[0])
    assert "('kyl', '%%kyl%%', '%%kyl%%')" in sql
    assert "users.name ilike member_search_tokens.name_pattern" in sql
    assert "users.github_username ilike member_search_tokens.github_pattern" in sql
    assert "escape" in sql
    assert "users.email" not in sql


@pytest.mark.asyncio
async def test_partial_member_ids_normalize_prefixed_github_handle() -> None:
    session = _CapturingIdSession(["user_kyle"], token="@kyl")

    resolved = await resolve_partial_member_ids(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        tokens=("@Kyl",),
    )

    assert resolved == {"@kyl": ("user_kyle",)}
    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert "%@Kyl%" in compiled.params.values()  # display-name pattern stays literal
    assert "%Kyl%" in compiled.params.values()  # stored handles omit leading @


@pytest.mark.asyncio
async def test_partial_member_ids_batch_tokens_in_one_query() -> None:
    session = _CapturingIdSession(
        [],
        rows=[
            ("ada", "user_ada"),
            ("kyl", "user_kyle"),
            ("kyl", "user_kylie"),
        ],
    )
    resolved = await resolve_partial_member_ids(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        tokens=("Kyl", "Ada", "kyl"),
    )

    assert resolved == {
        "kyl": ("user_kyle", "user_kylie"),
        "ada": ("user_ada",),
    }
    assert len(session.statements) == 1


@pytest.mark.asyncio
async def test_partial_member_ids_are_scoped_to_one_organization() -> None:
    session = _CapturingIdSession([])
    await resolve_partial_member_ids(
        session,  # type: ignore[arg-type]
        org_id="org_current",
        tokens=("kyl",),
    )

    assert "users.org_id = 'org_current'" in _compiled(session.statements[0])


@pytest.mark.asyncio
async def test_partial_member_ids_exclude_inactive_users() -> None:
    session = _CapturingIdSession([])
    await resolve_partial_member_ids(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        tokens=("kyl",),
    )

    assert "users.is_active = true" in _compiled(session.statements[0])


@pytest.mark.asyncio
async def test_partial_member_ids_escape_literal_wildcards() -> None:
    session = _CapturingIdSession([])
    await resolve_partial_member_ids(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        tokens=("100%_done",),
    )

    compiled = session.statements[0].compile(dialect=postgresql.dialect())
    assert r"%100\%\_done%" in compiled.params.values()


@pytest.mark.asyncio
async def test_explicit_author_token_uses_partial_canonical_name() -> None:
    user = _user(id="user_kyle", name="Kyle Smith", github_username="kyle")
    session = _CapturingSession([user])

    result = await dashboard_attribution._match_authors_for_token(
        session,  # type: ignore[arg-type]
        org_id="org_1",
        token="kyl",
    )

    assert [matched.id for matched in result] == ["user_kyle"]
    sql = " ".join(_compiled(statement) for statement in session.statements)
    assert "users.name ilike '%%kyl%%'" in sql
    assert "escape" in sql


class _PeopleRows:
    def all(self) -> list[tuple[str, str | None, str | None]]:
        return [
            ("user_kyle", "Kyle", "kyle"),
            ("user_handle", None, "handle-only"),
        ]


class _PeopleSession:
    def __init__(self) -> None:
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001
        self.statements.append(statement)
        return _PeopleRows()


@pytest.mark.asyncio
async def test_people_search_is_member_visible_and_never_serializes_email(
    monkeypatch,
) -> None:
    session = _PeopleSession()

    @asynccontextmanager
    async def fake_get_session():
        yield session

    monkeypatch.setattr(dashboard_router, "get_session", fake_get_session)
    app = FastAPI()
    app.include_router(dashboard_router.router)
    app.dependency_overrides[require_auth] = lambda: AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_current",
        user_id="ordinary_member",
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.READ,
    )

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/people/search?q=@kyl&limit=999")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "id": "user_kyle",
                "display_name": "Kyle",
                "github_username": "kyle",
            },
            {
                "id": "user_handle",
                "display_name": "@handle-only",
                "github_username": "handle-only",
            },
        ]
    }
    assert "email" not in response.text
    assert "@example.com" not in response.text

    sql = _compiled(session.statements[0])
    assert "users.org_id = 'org_current'" in sql
    assert "users.is_active is true" in sql
    assert "users.name ilike '%%@kyl%%'" in sql
    assert "users.github_username ilike '%%kyl%%'" in sql
    assert "escape" in sql
    assert "users.id = '@kyl'" in sql
    assert "limit 25" in sql
    assert "users.email" not in sql


@pytest.mark.asyncio
async def test_resolve_search_authors_unions_handle_collision(monkeypatch) -> None:
    u1 = _user(id="user_a", github_username="skylark")
    u2 = _user(id="user_b", github_username="skylark")

    async def _fake_match(session, *, org_id, token):  # noqa: ANN001
        return [u1, u2] if token.lstrip("@").lower() == "skylark" else []

    monkeypatch.setattr(dashboard_attribution, "_match_authors_for_token", _fake_match)
    _memory_set(
        "org_1",
        "user_a",
        AttributionProfile(github_handles=("skylark",), legacy_emails=("a@x.com",)),
    )
    _memory_set(
        "org_1",
        "user_b",
        AttributionProfile(github_handles=("skylark", "sky2"), legacy_emails=()),
    )
    try:
        user_ids, handles, emails = await resolve_search_authors(
            None, org_id="org_1", tokens=["@Skylark"]
        )
    finally:
        invalidate_attribution_cache(org_id="org_1", user_id="user_a")
        invalidate_attribution_cache(org_id="org_1", user_id="user_b")

    assert set(user_ids) == {"user_a", "user_b"}
    assert "sky2" in handles
    assert "a@x.com" in emails
    # Literal "@Skylark" + both profiles' "skylark" collapse to one (case-insensitive).
    assert [h.lower() for h in handles].count("skylark") == 1


@pytest.mark.asyncio
async def test_resolve_search_authors_email_alias(monkeypatch) -> None:
    user = _user(id="user_c", email="ada@x.com", github_username="ada")

    async def _fake_match(session, *, org_id, token):  # noqa: ANN001
        return [user] if token.lower() == "ada@x.com" else []

    monkeypatch.setattr(dashboard_attribution, "_match_authors_for_token", _fake_match)
    _memory_set(
        "org_1",
        "user_c",
        AttributionProfile(github_handles=("ada",), legacy_emails=("ada@x.com",)),
    )
    try:
        user_ids, handles, emails = await resolve_search_authors(
            None, org_id="org_1", tokens=["ada@x.com"]
        )
    finally:
        invalidate_attribution_cache(org_id="org_1", user_id="user_c")

    assert user_ids == ("user_c",)
    assert "ada@x.com" in emails
    assert "ada" in handles
    # An email-shaped literal must not be treated as a github handle.
    assert "ada@x.com" not in handles


@pytest.mark.asyncio
async def test_resolve_search_authors_unknown_token_has_no_users(monkeypatch) -> None:
    async def _fake_match(session, *, org_id, token):  # noqa: ANN001
        return []

    monkeypatch.setattr(dashboard_attribution, "_match_authors_for_token", _fake_match)

    user_ids, handles, emails = await resolve_search_authors(
        None, org_id="org_1", tokens=["ghost"]
    )

    assert user_ids == ()
    # Literal handle is still carried so an unattributed-but-tagged task can match;
    # with no such task this resolves to an empty result set.
    assert handles == ("ghost",)
    assert emails == ()


@pytest.mark.asyncio
async def test_resolve_search_authors_ignores_blank_tokens(monkeypatch) -> None:
    calls: list[str] = []

    async def _fake_match(session, *, org_id, token):  # noqa: ANN001
        calls.append(token)
        return []

    monkeypatch.setattr(dashboard_attribution, "_match_authors_for_token", _fake_match)

    user_ids, handles, emails = await resolve_search_authors(
        None,
        org_id="org_1",
        tokens=["", "  ", None],  # type: ignore[list-item]
    )

    assert (user_ids, handles, emails) == ((), (), ())
    assert calls == []  # blank tokens never hit the DB
