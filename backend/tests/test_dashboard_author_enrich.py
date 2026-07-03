"""Unit tests for the dashboard experiments-list author enrichment.

Covers ``api.routers.dashboard._enrich_experiment_authors``: the hosted layer
promotes canonical org-member labels into ``author`` (from the experiment's
``owner_user_id``) and ``last_runner`` (from the latest trial's
``billed_user_id``) through three tiers: id -> member name, id -> github
handle (name-less users from JWT provisioning), and raw-string match against
exactly one active org member's email/handle (legacy rows with NULL ids).
No database is touched -- a tiny fake session returns queued ``UserModel``
result sets in execute-call order (call 1 = active org users, call 2 = the
``include_deleted`` id lookup).
"""

from __future__ import annotations

import pytest

from api.routers.dashboard import _enrich_experiment_authors
from models import UserModel


def _user(**overrides) -> UserModel:
    base = {
        "id": "user_1",
        "org_id": "org_1",
        "email": "charles@abundant.ai",
        "github_username": "charlesyhuang",
        "clerk_user_id": "user_clerk",
        "role": "member",
        "is_active": True,
    }
    base.update(overrides)
    return UserModel(**base)


class _FakeScalars:
    def __init__(self, users: list[UserModel]) -> None:
        self._users = users

    def scalars(self):
        return iter(self._users)


class _FakeSession:
    """Returns queued result sets in ``execute``-call order (ignores the SQL).

    With ``org_id`` passed to the enrichment, call 1 is the active-org-users
    query and call 2 (if any) is the ``include_deleted`` lookup for referenced
    ids not found among the active users.
    """

    def __init__(self, result_sets: list[list[UserModel]]) -> None:
        self._result_sets = result_sets
        self.calls = 0

    async def execute(self, _stmt):
        idx = self.calls
        self.calls += 1
        users = self._result_sets[idx] if idx < len(self._result_sets) else []
        return _FakeScalars(users)


# ---------------------------------------------------------------------------
# Tier 1: id -> member name
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_promotes_member_name() -> None:
    original_row = {
        "id": "e1",
        "owner_user_id": "user_1",
        "author": {"name": "charlesyhuang", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession([[_user(id="user_1", name="Charles Huang")]])

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "Charles Huang",
        "source": "member",
    }
    # The original row object (shared with the core's experiments cache) must
    # NOT be mutated: enrichment replaces rows with shallow copies so the
    # cached github/api fallback survives for later requests in the TTL.
    assert original_row["author"] == {"name": "charlesyhuang", "source": "github"}
    assert dashboard["experiments"][0] is not original_row
    # The id was found among active org users -> no second (deleted-id) query.
    assert session.calls == 1


@pytest.mark.asyncio
async def test_enrich_resolves_deleted_owner_via_second_query() -> None:
    """An id absent from the active roster resolves via the include_deleted
    lookup (historical owners), costing exactly two queries."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": "user_gone",
                "author": {"name": "gone@abundant.ai", "source": "api"},
            }
        ]
    }
    session = _FakeSession(
        [
            [],  # no active org users
            [_user(id="user_gone", name="Gone Person", is_active=False)],
        ]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "Gone Person",
        "source": "member",
    }
    assert session.calls == 2


# ---------------------------------------------------------------------------
# Tier 2: id -> github handle (name-less users from JWT provisioning)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_id_falls_back_to_handle_when_no_name() -> None:
    """JWT provisioning creates users without a name; the id still
    canonicalizes to their handle."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": "user_1",
                "author": {"name": "charles@abundant.ai", "source": "api"},
            }
        ]
    }
    session = _FakeSession(
        [[_user(id="user_1", name=None, github_username="charlesyhuang")]]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "charlesyhuang",
        "source": "github",
    }


@pytest.mark.asyncio
async def test_enrich_leaves_author_when_no_name_or_handle() -> None:
    """A resolvable id whose user has neither name nor handle keeps the core
    value (an email is never promoted into the label)."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": "user_1",
                "author": {"name": "someone-else", "source": "api"},
            }
        ]
    }
    session = _FakeSession([[_user(id="user_1", name=None, github_username=None)]])

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "someone-else",
        "source": "api",
    }


# ---------------------------------------------------------------------------
# Tier 3: raw-string match against active org members (legacy NULL-id rows)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_string_matches_member_email() -> None:
    """A legacy row whose author is a member's email (here the ugly
    ``{clerk_id}@clerk.user`` provisioning sentinel, in mixed case) shows the
    member's name."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": None,
                "author": {"name": "User_2abcDEF@Clerk.User", "source": "api"},
            }
        ]
    }
    session = _FakeSession(
        [
            [
                _user(
                    id="user_1",
                    name="Charles Huang",
                    email="user_2abcdef@clerk.user",
                )
            ]
        ]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "Charles Huang",
        "source": "member",
    }


@pytest.mark.asyncio
async def test_enrich_string_matches_unique_handle() -> None:
    """``@Handle`` (any case, leading @) matching exactly one active member
    canonicalizes to that member's label."""
    original_row = {
        "id": "e1",
        "owner_user_id": None,
        "author": {"name": "@CharlesYHuang", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession(
        [[_user(id="user_1", name="Charles Huang", github_username="charlesyhuang")]]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "Charles Huang",
        "source": "member",
    }
    assert original_row["author"] == {"name": "@CharlesYHuang", "source": "github"}


@pytest.mark.asyncio
async def test_enrich_string_skips_ambiguous_handle() -> None:
    """A handle shared by two active members must not resolve (it could
    re-label the row to the wrong person)."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": None,
                "author": {"name": "sharedhandle", "source": "github"},
            }
        ]
    }
    session = _FakeSession(
        [
            [
                _user(
                    id="user_a",
                    name="Person A",
                    email="a@abundant.ai",
                    github_username="sharedhandle",
                ),
                _user(
                    id="user_b",
                    name="Person B",
                    email="b@abundant.ai",
                    github_username="sharedhandle",
                ),
            ]
        ]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["author"] == {
        "name": "sharedhandle",
        "source": "github",
    }


@pytest.mark.asyncio
async def test_enrich_string_matches_nobody_row_unmutated() -> None:
    original_row = {
        "id": "e1",
        "owner_user_id": None,
        "author": {"name": "someone@example.com", "source": "api"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession([[_user(id="user_1", name="Charles Huang")]])

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0] is original_row
    assert original_row["author"] == {"name": "someone@example.com", "source": "api"}


# ---------------------------------------------------------------------------
# last_runner: id tier + string tier + last_author mirror
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_overrides_last_runner_from_billed_user() -> None:
    """A resolvable ``last_runner_user_id`` replaces the task-based runner.

    Repro of the APPEND-to-shared-task bug: the task was created by Rishi, so
    the core's task-based ``last_runner`` says @RishiDesai, but the latest
    trial was billed to Charles -- the column must show Charles.
    """
    original_row = {
        "id": "e1",
        "owner_user_id": None,
        "last_runner_user_id": "user_charles",
        "author": {"name": "rishi-something", "source": "api"},
        "last_runner": {"name": "RishiDesai", "source": "github"},
        "last_author": {"name": "RishiDesai", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession([[_user(id="user_charles", name="Charles Huang")]])

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    row = dashboard["experiments"][0]
    expected = {"name": "Charles Huang", "source": "member"}
    assert row["last_runner"] == expected
    assert row["last_author"] == expected  # deprecated mirror stays in sync
    # Author string matched nobody, so the core value stands.
    assert row["author"] == {"name": "rishi-something", "source": "api"}
    # Cached row object untouched.
    assert original_row["last_runner"] == {"name": "RishiDesai", "source": "github"}
    assert row is not original_row


@pytest.mark.asyncio
async def test_enrich_last_runner_string_tier_and_mirror() -> None:
    """Legacy rows (NULL billed_user_id) resolve last_runner via the string
    tier, keeping ``last_author`` mirrored."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": None,
                "last_runner_user_id": None,
                "last_runner": {"name": "@RishiDesai", "source": "github"},
                "last_author": {"name": "@RishiDesai", "source": "github"},
            }
        ]
    }
    session = _FakeSession(
        [
            [
                _user(
                    id="user_rishi",
                    name="Rishi Desai",
                    email="rishi@abundant.ai",
                    github_username="rishidesai",
                )
            ]
        ]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    row = dashboard["experiments"][0]
    expected = {"name": "Rishi Desai", "source": "member"}
    assert row["last_runner"] == expected
    assert row["last_author"] == expected


@pytest.mark.asyncio
async def test_enrich_leaves_last_runner_when_unresolvable() -> None:
    """NULL id plus a string matching nobody keeps the task-based runner; a
    nameless+handleless user likewise."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": None,
                "last_runner_user_id": None,
                "last_runner": {"name": "unknown-stranger", "source": "api"},
            },
            {
                "id": "e2",
                "owner_user_id": None,
                "last_runner_user_id": "user_nameless",
                "last_runner": {"name": "another-stranger", "source": "api"},
            },
        ]
    }
    session = _FakeSession(
        [[_user(id="user_nameless", name=None, github_username=None)]]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert dashboard["experiments"][0]["last_runner"] == {
        "name": "unknown-stranger",
        "source": "api",
    }
    assert dashboard["experiments"][1]["last_runner"] == {
        "name": "another-stranger",
        "source": "api",
    }


@pytest.mark.asyncio
async def test_enrich_author_and_last_runner_different_members() -> None:
    """One row can get both overrides, each resolved to its own member."""
    original_row = {
        "id": "e1",
        "owner_user_id": "user_rishi",
        "last_runner_user_id": "user_charles",
        "author": {"name": "RishiDesai", "source": "github"},
        "last_runner": {"name": "RishiDesai", "source": "github"},
        "last_author": {"name": "RishiDesai", "source": "github"},
    }
    dashboard = {"experiments": [original_row]}
    session = _FakeSession(
        [
            [
                _user(
                    id="user_rishi",
                    name="Rishi Desai",
                    email="rishi@abundant.ai",
                    github_username="rishidesai",
                ),
                _user(id="user_charles", name="Charles Huang"),
            ]
        ]
    )

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    row = dashboard["experiments"][0]
    assert row["author"] == {"name": "Rishi Desai", "source": "member"}
    assert row["last_runner"] == {"name": "Charles Huang", "source": "member"}
    assert row["last_author"] == {"name": "Charles Huang", "source": "member"}
    # One shallow copy carries both overrides; the cached original is intact.
    assert original_row["author"] == {"name": "RishiDesai", "source": "github"}
    assert original_row["last_runner"] == {"name": "RishiDesai", "source": "github"}


# ---------------------------------------------------------------------------
# No-ops
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_enrich_no_experiments_is_noop() -> None:
    dashboard: dict = {"experiments": []}
    session = _FakeSession([[_user(id="user_1", name="Charles Huang")]])

    await _enrich_experiment_authors(session, dashboard, org_id="org_1")

    assert session.calls == 0
    assert dashboard == {"experiments": []}


@pytest.mark.asyncio
async def test_enrich_without_org_id_uses_id_tier_only() -> None:
    """No org context -> no active-roster query; ids still resolve through the
    single include_deleted lookup, strings are left alone."""
    dashboard = {
        "experiments": [
            {
                "id": "e1",
                "owner_user_id": "user_1",
                "author": {"name": "charlesyhuang", "source": "github"},
            },
            {
                "id": "e2",
                "owner_user_id": None,
                "author": {"name": "charles@abundant.ai", "source": "api"},
            },
        ]
    }
    session = _FakeSession([[_user(id="user_1", name="Charles Huang")]])

    await _enrich_experiment_authors(session, dashboard, org_id=None)

    assert session.calls == 1
    assert dashboard["experiments"][0]["author"] == {
        "name": "Charles Huang",
        "source": "member",
    }
    assert dashboard["experiments"][1]["author"] == {
        "name": "charles@abundant.ai",
        "source": "api",
    }
