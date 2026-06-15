from __future__ import annotations

import logging

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from dashboard_attribution import AttributionProfile, _baseline_profile
from models import UserModel
from oddish.core.dashboard import (
    EXPERIMENTS_UNATTRIBUTED_OWNER,
    build_primary_task_author_match,
    first_live_task_id_for_experiment,
    normalize_github_handle,
)
from oddish.db import ExperimentModel, TaskModel, get_session

logger = logging.getLogger(__name__)

# Per-user page size per sweep tick: keeps a single tick bounded on the first
# historical pass; the sweep runs every few minutes so large orgs converge
# across a handful of ticks.
_BACKFILL_BATCH_PER_USER = 500
_SENTINEL_BATCH = 500
# Leave very fresh rows alone so the submit-path stamp wins the race.
_SENTINEL_MIN_AGE_SQL = text("NOW() - INTERVAL '1 hour'")


def profile_from_user_row(
    user: UserModel,
    *,
    blocked_handles: set[str],
    blocked_emails: set[str],
) -> AttributionProfile:
    """Best profile derivable without any network or task scans.

    Persisted attribution cache (any age — learned aliases stay valid) merged
    with the always-current baseline user fields, re-filtered against the
    current org roster's blocked identities.
    """
    baseline = _baseline_profile(
        user, blocked_handles=blocked_handles, blocked_emails=blocked_emails
    )
    cached = AttributionProfile.from_dict(
        user.attribution_cache if isinstance(user.attribution_cache, dict) else None
    )
    if cached is None:
        return baseline

    handles = list(baseline.github_handles)
    emails = list(baseline.legacy_emails)
    seen_handles = {handle.lower() for handle in handles}
    seen_emails = {email.lower() for email in emails}
    for handle in cached.github_handles:
        normalized = normalize_github_handle(handle)
        if not normalized:
            continue
        key = normalized.lower()
        if key in seen_handles or key in blocked_handles:
            continue
        seen_handles.add(key)
        handles.append(normalized)
    for email in cached.legacy_emails:
        value = (email or "").strip()
        if not value or "@" not in value:
            continue
        key = value.lower()
        if key in seen_emails or key in blocked_emails:
            continue
        seen_emails.add(key)
        emails.append(value)
    return AttributionProfile(
        github_handles=tuple(handles), legacy_emails=tuple(emails)
    )


def _claim_candidates_select(
    *,
    org_id: str,
    user_id: str,
    profile: AttributionProfile,
    include_sentinel: bool,
    batch: int,
):
    """SELECT ids of unowned experiments whose primary task matches this user,
    reusing the exact legacy Mine-filter precedence.

    Kept as a standalone SELECT (ids fetched, then a separate guarded UPDATE)
    instead of ``UPDATE ... WHERE id IN (SELECT ...)`` so the correlated
    ``first_live_task_id_for_experiment`` subquery unambiguously binds to this
    SELECT's ``experiments`` reference — never to an UPDATE target of the same
    table.
    """
    owner_states = [ExperimentModel.owner_user_id.is_(None)]
    if include_sentinel:
        owner_states.append(
            ExperimentModel.owner_user_id == EXPERIMENTS_UNATTRIBUTED_OWNER
        )

    primary_task_id = first_live_task_id_for_experiment()
    author_match = build_primary_task_author_match(
        user_id,
        list(profile.github_handles),
        experiments_author_emails=profile.legacy_emails,
    )
    primary_owner_exists = (
        select(1)
        .select_from(TaskModel)
        .where(TaskModel.id == primary_task_id)
        .where(TaskModel.org_id == org_id)
        .where(author_match)
    )
    return (
        select(ExperimentModel.id)
        .where(ExperimentModel.org_id == org_id)
        .where(ExperimentModel.deleted_at.is_(None))
        .where(or_(*owner_states))
        .where(primary_owner_exists.exists())
        .limit(batch)
    )


def _claim_update_statement(*, experiment_ids: list[str], user_id: str):
    """Stamp owner on already-selected ids, re-checking the unowned guard so a
    concurrent submit-path stamp or another sweep container wins harmlessly."""
    return (
        update(ExperimentModel)
        .where(ExperimentModel.id.in_(experiment_ids))
        .where(
            or_(
                ExperimentModel.owner_user_id.is_(None),
                ExperimentModel.owner_user_id == EXPERIMENTS_UNATTRIBUTED_OWNER,
            )
        )
        .values(owner_user_id=user_id)
        .execution_options(synchronize_session=False)
    )


async def _claim_for_user(
    session: AsyncSession,
    *,
    org_id: str,
    user_id: str,
    profile: AttributionProfile,
    include_sentinel: bool,
    batch: int,
) -> int:
    rows = await session.execute(
        _claim_candidates_select(
            org_id=org_id,
            user_id=user_id,
            profile=profile,
            include_sentinel=include_sentinel,
            batch=batch,
        )
    )
    experiment_ids = [str(experiment_id) for (experiment_id,) in rows]
    if not experiment_ids:
        return 0
    result = await session.execute(
        _claim_update_statement(experiment_ids=experiment_ids, user_id=user_id)
    )
    return int(result.rowcount or 0)  # type: ignore[attr-defined]


def _sentinel_update_statement(*, org_id: str, batch: int):
    candidate_ids = (
        select(ExperimentModel.id)
        .where(ExperimentModel.org_id == org_id)
        .where(ExperimentModel.deleted_at.is_(None))
        .where(ExperimentModel.owner_user_id.is_(None))
        .where(ExperimentModel.created_at < _SENTINEL_MIN_AGE_SQL)
        .limit(batch)
    )
    return (
        update(ExperimentModel)
        .where(ExperimentModel.id.in_(candidate_ids.scalar_subquery()))
        .values(owner_user_id=EXPERIMENTS_UNATTRIBUTED_OWNER)
        .execution_options(synchronize_session=False)
    )


async def _org_ids_with_unowned_experiments(
    session: AsyncSession, *, include_sentinel: bool = False
) -> list[str]:
    owner_states = [ExperimentModel.owner_user_id.is_(None)]
    if include_sentinel:
        owner_states.append(
            ExperimentModel.owner_user_id == EXPERIMENTS_UNATTRIBUTED_OWNER
        )
    rows = await session.execute(
        select(ExperimentModel.org_id)
        .where(or_(*owner_states))
        .where(ExperimentModel.deleted_at.is_(None))
        .where(ExperimentModel.org_id.isnot(None))
        .distinct()
    )
    return [org_id for (org_id,) in rows]


async def _claim_for_org(
    session: AsyncSession, org_id: str, *, batch: int, include_sentinel: bool = False
) -> int:
    """Claim unowned experiments per member, deterministically and conservatively.

    Two-phase: build every member's profile first (registered fields +
    cached aliases), then claim with each user blocked from identities that
    appear in ANY other member's profile — so overlapping or stale cached
    aliases drop out instead of letting iteration order decide ownership.
    ``ORDER BY users.id`` keeps the remaining tie-break deterministic.
    """
    users = (
        (
            await session.execute(
                select(UserModel)
                .where(UserModel.org_id == org_id)
                .where(UserModel.is_active == True)  # noqa: E712
                .order_by(UserModel.id.asc())
            )
        )
        .scalars()
        .all()
    )
    if not users:
        return 0

    # Phase 1: full profile (registered + cached aliases) per user, unblocked.
    raw_profiles = {
        user.id: profile_from_user_row(
            user, blocked_handles=set(), blocked_emails=set()
        )
        for user in users
    }

    # Phase 2: block any identity claimed by another member's full profile.
    claimed = 0
    for user in users:
        blocked_handles = {
            handle.lower()
            for uid, profile in raw_profiles.items()
            if uid != user.id
            for handle in profile.github_handles
        }
        blocked_emails = {
            email.lower()
            for uid, profile in raw_profiles.items()
            if uid != user.id
            for email in profile.legacy_emails
        }
        profile = profile_from_user_row(
            user, blocked_handles=blocked_handles, blocked_emails=blocked_emails
        )
        claimed += await _claim_for_user(
            session,
            org_id=org_id,
            user_id=user.id,
            profile=profile,
            include_sentinel=include_sentinel,
            batch=batch,
        )
    return claimed


async def reclaim_experiments_for_user(
    session: AsyncSession,
    *,
    user: UserModel,
    profile: AttributionProfile,
    batch: int = _BACKFILL_BATCH_PER_USER,
) -> int:
    """Re-run the claim for one user, including sentinel rows.

    Called when attribution discovery learns new identities for the user, so
    previously-unattributable experiments get corrected without rescanning on
    every sweep tick. Applies the same cross-member identity blocking as the
    sweep's two-phase claim: any handle/email present in another active
    member's full profile (registered fields + cached aliases) is dropped
    before claiming, so this on-demand path can't out-claim the sweep.
    """
    if not user.org_id:
        return 0

    others = (
        (
            await session.execute(
                select(UserModel)
                .where(UserModel.org_id == user.org_id)
                .where(UserModel.id != user.id)
                .where(UserModel.is_active == True)  # noqa: E712
            )
        )
        .scalars()
        .all()
    )
    blocked_handles: set[str] = set()
    blocked_emails: set[str] = set()
    for other in others:
        other_profile = profile_from_user_row(
            other, blocked_handles=set(), blocked_emails=set()
        )
        blocked_handles.update(h.lower() for h in other_profile.github_handles)
        blocked_emails.update(e.lower() for e in other_profile.legacy_emails)

    filtered = AttributionProfile(
        github_handles=tuple(
            h for h in profile.github_handles if h.lower() not in blocked_handles
        ),
        legacy_emails=tuple(
            e for e in profile.legacy_emails if e.lower() not in blocked_emails
        ),
    )
    return await _claim_for_user(
        session,
        org_id=user.org_id,
        user_id=user.id,
        profile=filtered,
        include_sentinel=True,
        batch=batch,
    )


# Every Nth sweep pass the claim also revisits sentinel rows, so experiments
# whose true owner never opens the dashboard (no on-demand reclaim trigger)
# still get corrected ~hourly without paying the sentinel re-scan every tick.
# Module-level counter: best-effort across warm Modal containers, harmless if
# containers recycle (worst case the periodic pass runs more or less often).
_RECLAIM_EVERY_N_PASSES = 20
_pass_counter = 0


async def backfill_experiment_owners(
    *,
    claim_batch: int = _BACKFILL_BATCH_PER_USER,
    sentinel_batch: int = _SENTINEL_BATCH,
) -> dict[str, int]:
    """One sweep pass: claim unowned experiments per org member, then stamp
    leftovers with the unattributed sentinel so orgs converge to zero NULL
    owners (which flips the dashboard Mine filter onto the indexed path)."""
    global _pass_counter
    _pass_counter += 1
    include_sentinel = _pass_counter % _RECLAIM_EVERY_N_PASSES == 1

    claimed = 0
    sentineled = 0
    async with get_session() as session:
        org_ids = await _org_ids_with_unowned_experiments(
            session, include_sentinel=include_sentinel
        )
    # One session/transaction per org keeps the first historical pass from
    # holding row locks across the whole fleet in a single long transaction
    # (mirrors why the cleanup sweep keeps its steps bounded).
    for org_id in org_ids:
        async with get_session() as session:
            org_claimed = await _claim_for_org(
                session, org_id, batch=claim_batch, include_sentinel=include_sentinel
            )
            claimed += org_claimed
            # Sentinel ONLY once claims have converged for this org (a pass
            # that claims zero rows proves no current profile matches the
            # remaining NULLs). Stamping while claims are still draining the
            # per-user batch cap would hide claimable experiments from Mine
            # until a later reclaim pass.
            if org_claimed == 0:
                result = await session.execute(
                    _sentinel_update_statement(org_id=org_id, batch=sentinel_batch)
                )
                sentineled += int(result.rowcount or 0)  # type: ignore[attr-defined]
    if claimed or sentineled:
        logger.info(
            "experiments owner backfill: claimed=%s unattributed=%s",
            claimed,
            sentineled,
        )
    return {
        "experiments_owner_claimed": claimed,
        "experiments_owner_unattributed": sentineled,
    }
