from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

import dashboard_attribution
from dashboard_attribution import (
    AttributionProfile,
    _baseline_profile,
    _db_cache_profile,
    _memory_get,
    _memory_set,
    _row_has_strong_attribution_match,
    _schedule_profile_refresh,
    invalidate_attribution_cache,
    resolve_search_authors,
)
from models import UserModel


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
    from api.routers.tasks import _lookup_users_by_github_username

    u1 = _user(id="user_a", github_username="skylark")
    u2 = _user(id="user_b", github_username="skylark")
    session = _CapturingSession([u1, u2])

    result = await _lookup_users_by_github_username(
        session, github_username="@Skylark", org_id="org_1"
    )

    # scalars().all() (not scalar_one_or_none) -> collisions union, no raise.
    assert [u.id for u in result] == ["user_a", "user_b"]
    sql = _compiled(session.statements[0])
    assert "lower(users.github_username) = 'skylark'" in sql  # @-stripped + lowered
    assert "users.org_id = 'org_1'" in sql
    assert "users.is_active = true" in sql


@pytest.mark.asyncio
async def test_plural_github_lookup_empty_token_returns_empty() -> None:
    from api.routers.tasks import _lookup_users_by_github_username

    session = _CapturingSession([])
    assert await _lookup_users_by_github_username(
        session, github_username="  @  ", org_id="org_1"
    ) == []
    assert session.statements == []  # short-circuits before querying


@pytest.mark.asyncio
async def test_match_authors_for_token_matches_email_or_name() -> None:
    user = _user(id="user_d", email="ada@x.com", name="Ada L", github_username="ada")
    session = _CapturingSession([user])

    result = await dashboard_attribution._match_authors_for_token(
        session, org_id="org_1", token="Ada L"
    )

    assert [u.id for u in result] == ["user_d"]  # deduped by id across both queries
    sql = " ".join(_compiled(s) for s in session.statements)
    assert "lower(users.email) = 'ada l'" in sql
    assert "lower(users.name) = 'ada l'" in sql


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
        None, org_id="org_1", tokens=["", "  ", None]  # type: ignore[list-item]
    )

    assert (user_ids, handles, emails) == ((), (), ())
    assert calls == []  # blank tokens never hit the DB
