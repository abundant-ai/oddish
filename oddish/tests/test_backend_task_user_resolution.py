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

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "oddish" / "src"))


@dataclass
class _UserStub:
    id: str = "user-1"
    github_username: str | None = None
    name: str | None = None
    email: str | None = None


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
        )
    )

    # Strip type annotations that reference symbols we don't import here.
    snippet = re.sub(r":\s*AsyncSession", ": object", snippet)
    snippet = re.sub(r":\s*AuthContext", ": object", snippet)

    namespace: dict[str, Any] = {
        "AsyncSession": object,
        "AuthContext": object,
        "APIKeyModel": _APIKeyStub,
        "UserModel": _UserStub,
    }
    exec(compile(snippet, str(router_path), "exec"), namespace)
    return namespace


_HELPERS = _load_helpers()
_resolve_actor_user = _HELPERS["_resolve_actor_user"]
_resolve_actor_user_string = _HELPERS["_resolve_actor_user_string"]


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


def test_api_key_resolves_to_linked_user_github_username():
    user = _UserStub(id="u1", github_username="alice-gh", name="Alice", email="a@x")
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "alice-gh"


def test_api_key_falls_back_to_name_when_no_github_username():
    user = _UserStub(id="u1", github_username=None, name="Alice", email="a@x")
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "Alice"


def test_api_key_falls_back_to_email_when_no_github_username_or_name():
    user = _UserStub(id="u1", github_username=None, name=None, email="alice@example.com")
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "alice@example.com"


def test_clerk_jwt_uses_auth_user_directly():
    user = _UserStub(id="u1", github_username="bob", name=None, email="bob@x")
    auth = _AuthStub(user=user, user_id="u1")
    session = _SessionStub()  # no DB load needed
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "bob"


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


def test_actor_with_only_empty_strings_falls_back_to_api_key_name():
    user = _UserStub(id="u1", github_username="", name="", email="")
    api_key = _APIKeyStub(id="k1", name="ci-bot", created_by_user_id="u1")
    auth = _AuthStub(api_key_id="k1", api_key=api_key)
    session = _SessionStub(objects={(_UserStub, "u1"): user})
    result = _run(
        _resolve_actor_user_string(
            session, auth, explicit_user=None, explicit_github_username=None
        )
    )
    assert result == "ci-bot"


def test_resolve_actor_user_prefers_auth_user_over_api_key_lookup():
    """If both auth.user and auth.api_key are set, auth.user wins (no DB hit)."""
    direct_user = _UserStub(id="u-direct", github_username="direct")
    auth = _AuthStub(api_key_id="k1", api_key=_APIKeyStub(id="k1"), user=direct_user)
    session = _SessionStub()  # session.get would return None — this proves we don't call it
    actor = _run(_resolve_actor_user(session, auth))
    assert actor is direct_user


def test_resolve_actor_user_returns_none_when_unauthenticated():
    auth = _AuthStub()
    session = _SessionStub()
    actor = _run(_resolve_actor_user(session, auth))
    assert actor is None
