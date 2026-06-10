from __future__ import annotations

from dashboard_attribution import (
    AttributionProfile,
    _baseline_profile,
    _db_cache_fresh,
    _memory_get,
    _memory_set,
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


def test_baseline_profile_includes_clerk_and_github_email() -> None:
    profile = _baseline_profile(
        _user(),
        blocked_handles=set(),
        blocked_emails=set(),
        github_email="ps4534@nyu.edu",
    )
    assert profile.github_handles == ("praxs",)
    assert "pratty@abundant.ai" in profile.legacy_emails
    assert "ps4534@nyu.edu" in profile.legacy_emails


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


def test_db_cache_fresh_reads_persisted_profile() -> None:
    user = _user(
        attribution_cache={
            "github_handles": ["praxs"],
            "legacy_emails": ["ps4534@nyu.edu"],
            "refreshed_at": "2026-06-10T00:00:00+00:00",
        }
    )
    profile = _db_cache_fresh(user)
    assert profile is not None
    assert profile.github_handles == ("praxs",)
    assert profile.legacy_emails == ("ps4534@nyu.edu",)
