from fastapi import HTTPException

from auth.permissions import allowed_api_key_scopes, can_create_api_keys
from api.routers.task_submission import (
    _should_auto_publish,
    require_experiment_publish_scope,
)
from auth.types import AuthContext, AuthMethod
from models import APIKeyScope, UserRole
from oddish.schemas import AgentModelPair, TaskSweepSubmission


def _clerk_auth(
    *,
    role: UserRole,
    org_slug: str = "customer",
    clerk_org_id: str | None = None,
    email: str = "member@example.com",
) -> AuthContext:
    return AuthContext(
        method=AuthMethod.CLERK_JWT,
        org_id="org_1",
        org_slug=org_slug,
        user_id="user_1",
        user_email=email,
        user_role=role,
    )


def test_org_member_can_create_limited_api_keys():
    auth = _clerk_auth(role=UserRole.MEMBER, email="contractor@example.com")

    assert can_create_api_keys(auth) is True
    assert allowed_api_key_scopes(auth) == [APIKeyScope.TASKS, APIKeyScope.READ]


def test_org_admin_can_create_full_api_keys():
    auth = _clerk_auth(role=UserRole.ADMIN, email="admin@example.com")

    assert can_create_api_keys(auth) is True
    assert allowed_api_key_scopes(auth) == [
        APIKeyScope.FULL,
        APIKeyScope.TASKS,
        APIKeyScope.READ,
    ]


def test_api_key_creation_is_org_agnostic():
    # No org-slug allowlist: members of any org qualify, not just Abundant.
    for org_slug in ("abundant", "customer", "acme-labs"):
        auth = _clerk_auth(role=UserRole.MEMBER, org_slug=org_slug)
        assert can_create_api_keys(auth) is True


def test_api_key_auth_cannot_create_more_api_keys():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        org_slug="abundant",
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.TASKS,
    )

    assert can_create_api_keys(auth) is False
    assert allowed_api_key_scopes(auth) == []


def test_member_created_task_key_is_blocked_from_restricted_task_operations():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="user_1",
        user_role=UserRole.MEMBER,
        api_key_created_by_role=UserRole.MEMBER.value,
        scope=APIKeyScope.TASKS,
    )

    # Default TASKS-scope checks include medium-tier actions such as cancellation.
    auth.require_scope(APIKeyScope.TASKS)

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
        assert "Member-created API keys" in exc.detail
    else:
        raise AssertionError("member-created task key should be restricted")


def test_admin_created_task_key_can_use_restricted_task_operations():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
        api_key_created_by_role=UserRole.ADMIN.value,
        scope=APIKeyScope.TASKS,
    )

    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)


def test_task_key_with_deleted_creator_is_restricted():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="deleted_user_1",
        user_role=None,
        api_key_created_by_role=UserRole.MEMBER.value,
        scope=APIKeyScope.TASKS,
    )

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("task key with deleted creator should be restricted")


def test_task_key_with_unknown_creator_role_is_restricted():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id=None,
        user_role=None,
        api_key_created_by_role=None,
        scope=APIKeyScope.TASKS,
    )

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("task key with unknown creator role should be restricted")


def test_member_created_task_key_stays_restricted_after_creator_promotion():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="user_1",
        user_role=UserRole.ADMIN,
        api_key_created_by_role=UserRole.MEMBER.value,
        scope=APIKeyScope.TASKS,
    )

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("promoted creator should not expand a tasks key")


def test_cached_admin_role_does_not_expand_member_created_task_key():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="user_1",
        user_role=UserRole.ADMIN,
        api_key_created_by_role=UserRole.MEMBER.value,
        scope=APIKeyScope.TASKS,
    )

    try:
        auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("cached creator role should not authorize the key")


def test_member_created_task_key_cannot_publish_experiment():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="user_1",
        user_role=UserRole.MEMBER,
        api_key_created_by_role=UserRole.MEMBER.value,
        scope=APIKeyScope.TASKS,
    )

    try:
        require_experiment_publish_scope(auth)
    except HTTPException as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("member-created task key should not publish")


def test_admin_created_task_key_can_publish_experiment():
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
        api_key_created_by_role=UserRole.ADMIN.value,
        scope=APIKeyScope.TASKS,
    )

    require_experiment_publish_scope(auth)


# --- auto-publish default: either github identity counts -----------------------


def _publish_probe(monkeypatch):
    """(published, auth, run) — run(submission) drives maybe_publish_experiment
    with an admin-created org API key and records published experiments."""
    import asyncio
    from types import SimpleNamespace

    import api.routers.task_submission as task_submission

    published: list[object] = []

    async def record_publish(_session, experiment):
        published.append(experiment)

    monkeypatch.setattr(task_submission, "ensure_experiment_public", record_publish)
    auth = AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        user_id="admin_1",
        user_role=UserRole.ADMIN,
        api_key_id="key_1",
        api_key_created_by_role=UserRole.ADMIN.value,
        scope=APIKeyScope.TASKS,
    )
    task = SimpleNamespace(experiments=[SimpleNamespace(id="exp-1")])

    class _FakeAwaitableAttrs:
        # Mirrors SQLAlchemy's ``awaitable_attrs``, which
        # maybe_publish_experiment uses to load ``task.experiments``.
        def __getattr__(self, name):
            async def _get():
                return getattr(task, name)

            return _get()

    task.awaitable_attrs = _FakeAwaitableAttrs()

    def run(submission):
        asyncio.run(
            task_submission.maybe_publish_experiment(object(), task, submission, auth)
        )

    return published, run


def test_github_id_only_submission_auto_publishes(monkeypatch):
    from types import SimpleNamespace

    published, run = _publish_probe(monkeypatch)
    run(
        SimpleNamespace(
            publish_experiment=None, github_username=None, github_id="12345"
        )
    )
    assert len(published) == 1


def test_explicit_publish_false_still_skips_for_github_id(monkeypatch):
    from types import SimpleNamespace

    published, run = _publish_probe(monkeypatch)
    run(
        SimpleNamespace(
            publish_experiment=False, github_username=None, github_id="12345"
        )
    )
    assert published == []


def test_handle_only_submission_auto_publishes_unchanged(monkeypatch):
    from types import SimpleNamespace

    published, run = _publish_probe(monkeypatch)
    run(
        SimpleNamespace(
            publish_experiment=None, github_username="octocat", github_id=None
        )
    )
    assert len(published) == 1


def test_no_github_identity_does_not_auto_publish(monkeypatch):
    from types import SimpleNamespace

    published, run = _publish_probe(monkeypatch)
    run(
        SimpleNamespace(publish_experiment=None, github_username=None, github_id=None)
    )
    assert published == []


def _publish_submission(**overrides) -> TaskSweepSubmission:
    data = dict(
        task_id="task_pub",
        configs=[
            AgentModelPair(
                agent="claude-code", model="anthropic/claude-sonnet-4-6", n_trials=1
            )
        ],
        user=None,
    )
    data.update(overrides)
    return TaskSweepSubmission(**data)


def _api_key_auth() -> AuthContext:
    return AuthContext(
        method=AuthMethod.API_KEY,
        org_id="org_1",
        api_key_id="key_1",
        user_role=UserRole.MEMBER,
        scope=APIKeyScope.TASKS,
    )


def test_auto_publish_defaults_on_for_github_id_only():
    # A CI run passing --github-id alone (no handle) must auto-publish like a
    # handle-based run, since attribution/linkage now key off github_id.
    assert _should_auto_publish(_publish_submission(github_id="583231"), _api_key_auth())


def test_auto_publish_defaults_on_for_github_username_only():
    assert _should_auto_publish(
        _publish_submission(github_username="octocat"), _api_key_auth()
    )


def test_auto_publish_defaults_off_without_github_or_api_key():
    assert not _should_auto_publish(_publish_submission(), _api_key_auth())
    assert not _should_auto_publish(
        _publish_submission(github_id="583231"),
        _clerk_auth(role=UserRole.MEMBER),  # no api_key_id
    )


def test_auto_publish_explicit_flag_overrides_default():
    auth = _api_key_auth()
    assert not _should_auto_publish(
        _publish_submission(github_id="583231", publish_experiment=False), auth
    )
    assert _should_auto_publish(
        _publish_submission(publish_experiment=True), auth
    )
