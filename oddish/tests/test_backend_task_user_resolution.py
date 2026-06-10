"""Unit tests for the author-resolution helpers in ``backend.api.routers.tasks``.

Importing the router module is heavy because it pulls in Clerk auth,
Modal config, and the rest of the backend. We mirror the same source-extraction
trick used in ``test_backend_task_file_etag``: parse the helper functions out
of the router module source and ``exec`` them into a throwaway namespace where
we substitute ``AsyncSession``, ``AuthContext``, ``APIKeyModel``, and
``UserModel`` with simple stand-ins so we can drive the resolution logic
without spinning up the full backend.
"""

from __future__ import annotations

import asyncio
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select  # noqa: F401 — injected into exec namespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "oddish" / "src"))


class _AttrStub:
    def __eq__(self, _other: object) -> _AttrStub:
        return self


class _UserStub:
    github_username = _AttrStub()
    org_id = _AttrStub()
    is_active = _AttrStub()

    def __init__(
        self,
        *,
        id: str = "user-1",
        github_username: str | None = None,
        name: str | None = None,
        email: str | None = None,
        org_id: str | None = None,
        is_active: bool = True,
    ) -> None:
        self.id = id
        self.github_username = github_username
        self.name = name
        self.email = email
        self.org_id = org_id
        self.is_active = is_active


@dataclass
class _APIKeyStub:
    id: str = "key-1"
    name: str | None = None
    created_by_user_id: str | None = None


@dataclass
class _AuthStub:
    api_key_id: str | None = None
    api_key: _APIKeyStub | None = None
    user: _UserStub | None = None
    user_id: str | None = None
    org_id: str = "org-1"


@dataclass
class _SessionStub:
    objects: dict[tuple[type, str], Any] = field(default_factory=dict)

    async def get(self, model: type, key: str) -> Any:
        return self.objects.get((model, key))

    async def execute(self, _stmt: Any) -> Any:
        class _Result:
            def scalar_one_or_none(self) -> None:
                return None

        return _Result()


def _load_helpers() -> dict[str, Any]:
    """Extract the resolution helpers from the router source.

    We slice out ``_resolve_actor_user`` and ``_resolve_actor_user_string``
    from the ``backend/api/routers/tasks.py`` source and ``exec`` them with
    the model/auth names rebound to our stubs. This keeps the test decoupled
    from FastAPI/Clerk/Modal imports.
    """
    router_path = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "api"
        / "routers"
        / "tasks.py"
    )
    source = router_path.read_text(encoding="utf-8")

    def _slice(needle: str) -> str:
        start = source.index(needle)
        tail = source[start:]
        end = tail.index("\n\n\n")
        return tail[:end]

    snippet = "\n\n\n".join(
        _slice(needle)
        for needle in (
            "async def _resolve_actor_user(",
            "async def _resolve_actor_user_string(",
            "async def _lookup_user_by_github_username(",
            "async def _resolve_created_by_user_id(",
            "async def _resolve_experiment_owner_user_id(",
        )
    )

    # Strip type annotations that reference symbols we don't import here.
    snippet = re.sub(r":\s*AsyncSession", ": object", snippet)
    snippet = re.sub(r":\s*AuthContext", ": object", snippet)

    class _SelectStmtStub:
        def where(self, *_args: Any, **_kwargs: Any) -> _SelectStmtStub:
            return self

    def _select_stub(*_entities: Any, **_kwargs: Any) -> _SelectStmtStub:
        return _SelectStmtStub()

    namespace: dict[str, Any] = {
        "AsyncSession": object,
        "AuthContext": object,
        "APIKeyModel": _APIKeyStub,
        "UserModel": _UserStub,
        "select": _select_stub,
    }
    exec(compile(snippet, str(router_path), "exec"), namespace)
    return namespace


_HELPERS = _load_helpers()
_resolve_actor_user = _HELPERS["_resolve_actor_user"]
_resolve_actor_user_string = _HELPERS["_resolve_actor_user_string"]
_resolve_created_by_user_id = _HELPERS["_resolve_created_by_user_id"]
_resolve_experiment_owner_user_id = _HELPERS["_resolve_experiment_owner_user_id"]


def _run(coro):
    return asyncio.run(coro)


def test_explicit_user_wins():
    auth = _AuthStub()
    session = _SessionStub()
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user="alice", explicit_github_username=None
        )
    )
    assert result == "alice"


def test_explicit_github_username_used_when_no_user():
    auth = _AuthStub()
    session = _SessionStub()
    result = _run(
        _resolve_actor_user_string(
            session,
            auth,
            explicit_user=None,
            explicit_github_username="alice-gh",
        )
    )
    assert result == "alice-gh"


def test_api_key_resolves_to_linked_user_email():
    user = _UserStub(id="u1", github_username="alice-gh", name="Alice", email="alice@example.com")
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    # When UserModel is present, only email is used — never github_username/name.
    assert result == "alice@example.com"


def test_clerk_jwt_uses_auth_user_email():
    user = _UserStub(id="u1", github_username="bob", name="Bob", email="bob@example.com")
    auth = _AuthStub(user=user, user_id="u1")
    session = _SessionStub()  # no DB load needed
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "bob@example.com"


def test_clerk_jwt_cache_hit_lazy_loads_user():
    """Auth dependency strips the ORM user object on cache hits — verify we
    re-fetch via session.get(UserModel, auth.user_id) so the cached request
    still resolves to the real Clerk identity instead of falling back to
    "unknown"."""
    user = _UserStub(id="u1", github_username=None, name=None, email="cached@example.com")
    auth = _AuthStub(user_id="u1")  # no .user (cache-hit shape)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "cached@example.com"


def test_service_account_api_key_falls_back_to_key_name():
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id=None)
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub()
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "ci-bot"


def test_no_actor_falls_back_to_unknown():
    auth = _AuthStub()  # no api key, no user
    session = _SessionStub()
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "unknown"


def test_actor_with_empty_email_falls_back_to_api_key_name():
    user = _UserStub(id="u1", github_username="alice-gh", name="Alice", email="")
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "ci-bot"


def test_resolve_actor_user_prefers_auth_user_over_lookup():
    """If auth.user is set, it wins — no DB hit needed."""
    direct_user = _UserStub(id="u-direct", email="direct@example.com")
    auth = _AuthStub(api_key_id="k1", api_key=_APIKeyStub(id="k1"), user=direct_user)
    session = _SessionStub()  # session.get would return None — proves we don't call it
    actor = _run(_resolve_actor_user(session, auth))
    assert actor is direct_user


def test_resolve_actor_user_lazy_loads_via_user_id_on_cache_hit():
    """Clerk JWT cache hits drop the ORM user; we must lazy-load via user_id."""
    user = _UserStub(id="u1", email="cached@example.com")
    auth = _AuthStub(user_id="u1")  # no .user
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    actor = _run(_resolve_actor_user(session, auth))
    assert actor is user


def test_resolve_actor_user_returns_none_when_unauthenticated():
    auth = _AuthStub()
    session = _SessionStub()
    actor = _run(_resolve_actor_user(session, auth))
    assert actor is None


@dataclass
class _SubmissionStub:
    github_username: str | None = None


def test_created_by_user_id_prefers_api_key_owner():
    user = _UserStub(id="u1", email="alice@example.com")
    api_key = _APIKeyStub(id="k1", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key, user_id=None)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    submission = _SubmissionStub()
    result = _run(
        _resolve_created_by_user_id(session, submission, auth)
    )
    assert result == "u1"


def test_created_by_user_id_resolves_github_username_to_user():
    user = _UserStub(id="u2", github_username="octocat")
    auth = _AuthStub(user_id="u-other", org_id="org-1")
    session = _SessionStub()

    async def _fake_execute(stmt):
        class _Result:
            def scalar_one_or_none(self):
                return user

        return _Result()

    session.execute = _fake_execute  # type: ignore[attr-defined]
    submission = _SubmissionStub(github_username="octocat")
    result = _run(
        _resolve_created_by_user_id(session, submission, auth)
    )
    assert result == "u2"


def test_created_by_user_id_falls_back_to_auth_user_id():
    auth = _AuthStub(user_id="u-clerk", org_id="org-1")
    session = _SessionStub()
    submission = _SubmissionStub(github_username=None)
    result = _run(
        _resolve_created_by_user_id(session, submission, auth)
    )
    assert result == "u-clerk"


def test_experiment_owner_prefers_github_user_over_api_key_owner():
    api_user = _UserStub(id="u-ci", github_username=None)
    gh_user = _UserStub(id="u-gh", github_username="praxs")
    api_key = _APIKeyStub(id="k1", created_by_user_id="u-ci")
    auth = _AuthStub(api_key_id="k1", api_key=api_key, org_id="org-1")
    session = _SessionStub(objects={(_APIKeyStub, "k1"): api_key})

    async def _fake_execute(stmt):
        class _Result:
            def scalar_one_or_none(self):
                return gh_user

        return _Result()

    session.execute = _fake_execute  # type: ignore[attr-defined]
    submission = _SubmissionStub(github_username="praxs")

    created_by = _run(_resolve_created_by_user_id(session, submission, auth))
    owner = _run(_resolve_experiment_owner_user_id(session, submission, auth))

    assert created_by == "u-ci"
    assert owner == "u-gh"


def test_experiment_owner_falls_back_to_api_key_when_no_github_user():
    api_user = _UserStub(id="u-ci")
    api_key = _APIKeyStub(id="k1", created_by_user_id="u-ci")
    auth = _AuthStub(api_key_id="k1", api_key=api_key, org_id="org-1")
    session = _SessionStub(objects={(_APIKeyStub, "k1"): api_key})
    submission = _SubmissionStub(github_username="unknown-gh")

    owner = _run(_resolve_experiment_owner_user_id(session, submission, auth))
    assert owner == "u-ci"
