from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext
from models import APIKeyModel, UserModel
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER
from oddish.core.sharing.helpers import ensure_experiment_public
from oddish.db import ExperimentModel, TaskModel
from oddish.schemas import TaskSweepSubmission


def apply_github_attribution(submission: TaskSweepSubmission) -> None:
    if submission.github_username:
        submission.tags = submission.tags or {}
        submission.tags.setdefault("github_username", submission.github_username)


async def _resolve_actor_user(
    session: AsyncSession,
    auth: AuthContext,
) -> UserModel | None:
    """Return the UserModel of the authenticating principal, or None.

    The auth dependency caches lightweight identity tuples; on cache hits the
    ORM user / api_key objects are stripped and only the IDs are available, so
    load via ``session.get`` when needed.
    """
    if auth.user is not None:
        return auth.user
    if auth.user_id:
        user = await session.get(UserModel, auth.user_id)
        if user is not None:
            return user
    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return await session.get(UserModel, api_key.created_by_user_id)
    return None


async def resolve_actor_user_string(
    session: AsyncSession,
    auth: AuthContext,
    explicit_user: str | None,
    explicit_github_username: str | None,
) -> str:
    """Resolve a non-empty author string from the authenticated actor."""
    if explicit_user:
        return explicit_user
    if explicit_github_username:
        return explicit_github_username

    actor = await _resolve_actor_user(session, auth)
    if actor and actor.email:
        return actor.email

    if auth.api_key_id:
        api_key = auth.api_key or await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.name:
            return api_key.name

    return "unknown"


async def resolve_submission_identity(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    """Fill submission.user and submission.github_username from the actor.

    ``github_username`` is only auto-filled from ``UserModel.github_username`` so
    the dashboard's ``source: "github"`` attribution stays meaningful. Mutates
    ``submission`` in place.
    """
    if not submission.github_username:
        actor = await _resolve_actor_user(session, auth)
        if actor and actor.github_username:
            submission.github_username = actor.github_username

    submission.user = await resolve_actor_user_string(
        session,
        auth,
        explicit_user=submission.user,
        explicit_github_username=submission.github_username,
    )


async def _lookup_user_by_github_username(
    session: AsyncSession,
    *,
    github_username: str,
    org_id: str,
) -> UserModel | None:
    normalized = (github_username or "").strip().lstrip("@")
    if not normalized:
        return None
    user_result = await session.execute(
        select(UserModel).where(
            func.lower(UserModel.github_username) == normalized.lower(),
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
        )
    )
    return user_result.scalar_one_or_none()


async def lookup_users_by_github_username(
    session: AsyncSession,
    *,
    github_username: str,
    org_id: str,
) -> list[UserModel]:
    """Return all active org users for a GitHub handle.

    Two active members can share a GitHub handle, so search filters must union
    all matches rather than assume a single owner.
    """
    normalized = (github_username or "").strip().lstrip("@")
    if not normalized:
        return []
    result = await session.execute(
        select(UserModel).where(
            func.lower(UserModel.github_username) == normalized.lower(),
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
        )
    )
    return list(result.scalars().all())


async def resolve_created_by_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> str | None:
    """Who submitted the task. API key owner wins for CI/service accounts."""
    if auth.api_key_id:
        api_key = auth.api_key
        if api_key is None:
            api_key = await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return api_key.created_by_user_id

    if submission.github_username:
        user = await _lookup_user_by_github_username(
            session,
            github_username=submission.github_username,
            org_id=auth.org_id,
        )
        if user:
            return user.id

    if auth.user_id:
        return auth.user_id

    return None


async def resolve_experiment_owner_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> str | None:
    """Primary experiment owner for dashboard Mine. GitHub author beats submitter."""
    if submission.github_username:
        user = await _lookup_user_by_github_username(
            session,
            github_username=submission.github_username,
            org_id=auth.org_id,
        )
        if user:
            return user.id
        # Explicit --github-user with no linked org member: leave owner unset so
        # the legacy primary-task Mine filter can match the github tag.
        return None

    if auth.user_id:
        return auth.user_id

    if auth.api_key_id:
        api_key = auth.api_key
        if api_key is None:
            api_key = await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return api_key.created_by_user_id

    return None


def stamp_experiment_owner(
    experiment: ExperimentModel | None,
    owner_user_id: str | None,
    *,
    claim_unowned: bool = True,
) -> None:
    """Stamp the dashboard Mine owner on an experiment."""
    if experiment is None or not owner_user_id:
        return
    claimable = (
        (None, EXPERIMENTS_UNATTRIBUTED_OWNER)
        if claim_unowned
        else (EXPERIMENTS_UNATTRIBUTED_OWNER,)
    )
    if experiment.owner_user_id in claimable:
        experiment.owner_user_id = owner_user_id


def require_experiment_publish_scope(auth: AuthContext) -> None:
    auth.require_scope(APIKeyScope.TASKS, allow_member_created_task_key=False)


async def maybe_publish_experiment(
    session: AsyncSession,
    task: TaskModel,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    should_publish = submission.publish_experiment
    if should_publish is None:
        should_publish = bool(submission.github_username and auth.api_key_id)
    if not should_publish:
        return

    require_experiment_publish_scope(auth)
    experiments = list(task.experiments or [])
    for experiment in experiments:
        await ensure_experiment_public(session, experiment)
