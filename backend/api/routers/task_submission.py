from __future__ import annotations

import logging

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from auth import APIKeyScope, AuthContext
from models import APIKeyModel, UserModel
from oddish.core.dashboard import EXPERIMENTS_UNATTRIBUTED_OWNER
from oddish.core.sharing.helpers import ensure_experiment_public
from oddish.db import ExperimentModel, TaskModel
from oddish.schemas import TaskSweepSubmission

logger = logging.getLogger(__name__)


def apply_github_attribution(submission: TaskSweepSubmission) -> None:
    if submission.github_username:
        submission.tags = submission.tags or {}
        submission.tags.setdefault("github_username", submission.github_username)
    if submission.github_id:
        submission.tags = submission.tags or {}
        submission.tags.setdefault("github_id", submission.github_id)


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
    users = await lookup_users_by_github_username(
        session, github_username=github_username, org_id=org_id
    )
    return users[0] if len(users) == 1 else None


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


async def _lookup_user_by_github_id(
    session: AsyncSession,
    *,
    github_id: str,
    org_id: str,
) -> UserModel | None:
    result = await session.execute(
        select(UserModel).where(
            UserModel.github_id == github_id,
            UserModel.org_id == org_id,
            UserModel.is_active == True,  # noqa: E712
        )
    )
    return result.scalars().first()


async def resolve_connected_user(
    session: AsyncSession,
    *,
    org_id: str,
    github_id: str | None,
    github_username: str | None,
) -> UserModel | None:
    # A blank id is an absent id on every transport (schema-normalized
    # submissions and raw query params alike).
    github_id = (github_id or "").strip() or None
    if github_id:
        return await _lookup_user_by_github_id(
            session, github_id=github_id, org_id=org_id
        )
    if github_username:
        return await _lookup_user_by_github_username(
            session, github_username=github_username, org_id=org_id
        )
    return None


async def require_connected_github_user(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> UserModel | None:
    """Gate a sweep on GitHub linkage, quota-independent.

    A submission carrying a truthy ``github_id`` must strict-resolve (id-only)
    to an active org user or the sweep is rejected with 403 before any rows are
    written. Returns the resolved user (reusable for owner / created_by
    stamping), or None when no ``github_id`` was supplied (gate is a no-op).

    Trust model: this gate is COOPERATIVE, not adversarial. It checks linkage
    ("does this id map to a connected org user?"), not ownership — any caller
    holding an org credential may omit ``github_id`` to skip the gate entirely,
    or pass any linked member's id and have attribution/ownership credit that
    member. Anti-spoofing is an explicit non-goal; do not build billing or
    quota enforcement that assumes a hostile client cannot choose whose id it
    sends.
    """
    if not (submission.github_id and submission.github_id.strip()):
        return None
    user = await _lookup_user_by_github_id(
        session, github_id=submission.github_id, org_id=auth.org_id
    )
    if user is None:
        api_key = auth.api_key
        logger.info(
            "linkage gate rejected github_id=%s org=%s api_key_id=%s "
            "api_key_name=%s user_id=%s",
            submission.github_id,
            auth.org_id,
            auth.api_key_id,
            api_key.name if api_key else None,
            auth.user_id,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                f"GitHub account {submission.github_id} is not connected to an "
                "oddish user in this org. Sign in at https://oddish.app and link "
                "your GitHub account, then rerun. If you linked your GitHub account "
                "recently, it can take up to an hour to sync."
            ),
        )
    return user


async def resolve_created_by_user_id(
    session: AsyncSession,
    submission: TaskSweepSubmission,
    auth: AuthContext,
    connected_user: UserModel | None = None,
) -> str | None:
    """Who submitted the task. API key owner wins for CI/service accounts."""
    if auth.api_key_id:
        api_key = auth.api_key
        if api_key is None:
            api_key = await session.get(APIKeyModel, auth.api_key_id)
        if api_key and api_key.created_by_user_id:
            return api_key.created_by_user_id

    if submission.github_id is not None or submission.github_username:
        user = connected_user or await resolve_connected_user(
            session,
            org_id=auth.org_id,
            github_id=submission.github_id,
            github_username=submission.github_username,
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
    connected_user: UserModel | None = None,
) -> str | None:
    """Primary experiment owner for dashboard Mine. GitHub author beats submitter."""
    if submission.github_id is not None or submission.github_username:
        user = connected_user or await resolve_connected_user(
            session,
            org_id=auth.org_id,
            github_id=submission.github_id,
            github_username=submission.github_username,
        )
        if user:
            return user.id
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


def _should_auto_publish(submission: TaskSweepSubmission, auth: AuthContext) -> bool:
    if submission.publish_experiment is not None:
        return submission.publish_experiment
    # Attribution and the linkage gate now key off github_id, so a CI run that
    # passes --github-id alone (no handle) must auto-publish like a handle-based
    # run did. github_id is schema-normalized (blank -> None).
    return bool(
        (submission.github_username or submission.github_id) and auth.api_key_id
    )


async def maybe_publish_experiment(
    session: AsyncSession,
    task: TaskModel,
    submission: TaskSweepSubmission,
    auth: AuthContext,
) -> None:
    if not _should_auto_publish(submission, auth):
        return

    require_experiment_publish_scope(auth)
    experiments = list(task.experiments or [])
    for experiment in experiments:
        await ensure_experiment_public(session, experiment)
