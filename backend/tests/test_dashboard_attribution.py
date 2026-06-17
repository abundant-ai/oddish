from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
