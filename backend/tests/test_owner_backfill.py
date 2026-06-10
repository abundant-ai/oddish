from __future__ import annotations

from dashboard_attribution import AttributionProfile
from dashboard_owner_backfill import (
    _claim_candidates_select,
    _claim_update_statement,
    _sentinel_update_statement,
    profile_from_user_row,
)
from models import UserModel
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER
from sqlalchemy.dialects import postgresql


def _compile(stmt) -> str:
    return str(
        stmt.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )


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


def test_profile_from_user_row_merges_cache_and_baseline() -> None:
    user = _user(
        attribution_cache={
            "github_handles": ["dot-agi"],
            "legacy_emails": ["ps4534@nyu.edu"],
            "refreshed_at": "2020-01-01T00:00:00+00:00",  # stale is fine here
        }
    )
    profile = profile_from_user_row(user, blocked_handles=set(), blocked_emails=set())
    assert set(profile.github_handles) == {"praxs", "dot-agi"}
    assert set(profile.legacy_emails) == {"pratty@abundant.ai", "ps4534@nyu.edu"}


def test_profile_from_user_row_refilters_blocked() -> None:
    user = _user(
        attribution_cache={
            "github_handles": ["skylark"],
            "legacy_emails": [],
            "refreshed_at": "2020-01-01T00:00:00+00:00",
        }
    )
    profile = profile_from_user_row(
        user, blocked_handles={"skylark"}, blocked_emails=set()
    )
    assert profile.github_handles == ("praxs",)


def test_claim_candidates_target_null_and_sentinel_owners() -> None:
    stmt = _claim_candidates_select(
        org_id="org_1",
        user_id="user_1",
        profile=AttributionProfile(
            github_handles=("praxs",), legacy_emails=("pratty@abundant.ai",)
        ),
        include_sentinel=True,
        batch=500,
    )
    sql = _compile(stmt)
    assert "owner_user_id IS NULL" in sql
    assert EXPERIMENTS_UNATTRIBUTED_OWNER in sql
    assert "org_id = 'org_1'" in sql
    assert "LIMIT 500" in sql
    assert "EXISTS" in sql


def test_claim_candidates_can_exclude_sentinel() -> None:
    stmt = _claim_candidates_select(
        org_id="org_1",
        user_id="user_1",
        profile=AttributionProfile(github_handles=("praxs",), legacy_emails=()),
        include_sentinel=False,
        batch=500,
    )
    assert EXPERIMENTS_UNATTRIBUTED_OWNER not in _compile(stmt)


def test_claim_update_recheck_guard_and_stamp() -> None:
    stmt = _claim_update_statement(
        experiment_ids=["exp_1", "exp_2"], user_id="user_1"
    )
    sql = _compile(stmt)
    assert "UPDATE experiments" in sql
    assert "owner_user_id IS NULL" in sql
    assert EXPERIMENTS_UNATTRIBUTED_OWNER in sql
    assert "'user_1'" in sql
    assert "exp_1" in sql and "exp_2" in sql


def test_sentinel_update_only_touches_stale_null_rows() -> None:
    sql = _compile(_sentinel_update_statement(org_id="org_1", batch=500))
    assert EXPERIMENTS_UNATTRIBUTED_OWNER in sql
    assert "owner_user_id IS NULL" in sql
    assert "INTERVAL '1 hour'" in sql or "interval '1 hour'" in sql.lower()
    assert "LIMIT 500" in sql


class _FakeScalars:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return _FakeScalars(self._rows)


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows

    async def execute(self, _stmt):
        return _FakeResult(self._rows)


def test_reclaim_filters_profile_against_other_members(monkeypatch) -> None:
    import asyncio

    import dashboard_owner_backfill as backfill

    captured: dict = {}

    async def _fake_claim(session, *, org_id, user_id, profile, include_sentinel, batch):
        captured["profile"] = profile
        captured["include_sentinel"] = include_sentinel
        return 0

    monkeypatch.setattr(backfill, "_claim_for_user", _fake_claim)

    other = _user(
        id="user_2",
        email="other@abundant.ai",
        github_username="skylark",
        attribution_cache={
            "github_handles": ["dot-agi"],  # stale alias overlapping user_1's
            "legacy_emails": [],
            "refreshed_at": "2020-01-01T00:00:00+00:00",
        },
    )
    me = _user()
    my_profile = AttributionProfile(
        github_handles=("praxs", "dot-agi"),
        legacy_emails=("pratty@abundant.ai", "other@abundant.ai"),
    )

    asyncio.run(
        backfill.reclaim_experiments_for_user(
            _FakeSession([other]), user=me, profile=my_profile
        )
    )

    assert captured["include_sentinel"] is True
    # Identities present in another member's full profile are dropped.
    assert captured["profile"].github_handles == ("praxs",)
    assert captured["profile"].legacy_emails == ("pratty@abundant.ai",)
