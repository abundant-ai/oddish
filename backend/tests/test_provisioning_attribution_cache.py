from __future__ import annotations

from auth.provisioning import _seed_attribution_cache_from_github
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


def test_seed_attribution_cache_merges_without_dropping_aliases() -> None:
    user = _user(
        attribution_cache={
            "github_handles": ["praxs", "dot-agi"],
            "legacy_emails": ["ps4534@nyu.edu"],
            "refreshed_at": "2026-01-01T00:00:00+00:00",
        }
    )
    _seed_attribution_cache_from_github(
        user,
        github_username="praxs",
        github_email="ps4534@nyu.edu",
    )
    assert user.attribution_cache is not None
    assert set(user.attribution_cache["github_handles"]) == {"praxs", "dot-agi"}
    assert "ps4534@nyu.edu" in user.attribution_cache["legacy_emails"]
    assert "pratty@abundant.ai" in user.attribution_cache["legacy_emails"]
