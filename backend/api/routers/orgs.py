from __future__ import annotations

import calendar
import logging
import os
import secrets
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import ProgrammingError

from api import casino_games
from api.schemas import (
    BlackjackRequest,
    BlackjackStateResponse,
    DuelAcceptResponse,
    DuelCreateRequest,
    DuelFight,
    DuelItem,
    DuelListResponse,
    DuelMember,
    InviteUserRequest,
    InviteUserResponse,
    OrganizationResponse,
    OrgQuotaResponse,
    OrgUsageResponse,
    QuotaBumpRequest,
    QuotaGambleItem,
    QuotaGambleListResponse,
    QuotaGambleRequest,
    QuotaGambleResponse,
    QuotaListResponse,
    QuotaMemberItem,
    QuotaUpdateRequest,
    QuotaUsageResponse,
    UserResponse,
    WrestleRequest,
    WrestleResponse,
)
from auth import (
    AuthContext,
    AuthMethod,
    require_admin,
    require_auth,
    require_can_manage_quotas,
)
from auth.verification import invalidate_cached_clerk_auth
from oddish.config import QuotaMode, settings
from models import (
    OrgQuotaModel,
    QuotaBumpModel,
    QuotaDuelModel,
    QuotaGambleModel,
    QuotaGambleSessionModel,
    QuotaModel,
    UserModel,
    UserRole,
    generate_id,
)
from oddish.core.quotas import (
    get_base_limit,
    get_effective_limit,
    get_effective_org_limit,
    inflight_reserved_usd,
    live_bump_total,
    live_bump_totals_by_user,
    org_inflight_reserved_usd,
    quota_duels_ready,
    quota_gambles_ready,
    quota_window_start,
    start_of_month_utc,
    start_of_today_utc,
    sum_cost_usd,
    sum_cost_usd_by_user,
    sum_gamble_net_usd,
    sum_org_cost_usd,
)
from oddish.core.tags.ownership_transfer import transfer_tag_ownership_to_admin
from oddish.db import get_session, utcnow

logger = logging.getLogger(__name__)

CLERK_SECRET_KEY = os.getenv("CLERK_SECRET_KEY", "")

router = APIRouter(tags=["Organization"])


# =============================================================================
# Organization Endpoints
# =============================================================================


@router.get("/org", response_model=OrganizationResponse)
async def get_organization(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> OrganizationResponse:
    """Get the current organization."""
    if auth.org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    return OrganizationResponse(
        id=auth.org.id,
        name=auth.org.name,
        slug=auth.org.slug,
        plan=auth.org.plan,
        created_at=auth.org.created_at.isoformat(),
    )


# =============================================================================
# User Management
# =============================================================================


def _clerk_invite_role(role: UserRole) -> str:
    if role == UserRole.MEMBER:
        return "org:member"
    return "org:admin"


async def _create_clerk_invitation(
    clerk_org_id: str,
    email: str,
    role: UserRole,
) -> dict:
    if not CLERK_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="CLERK_SECRET_KEY not configured",
        )

    url = f"https://api.clerk.com/v1/organizations/{clerk_org_id}/invitations"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}
    payload = {"email_address": email, "role": _clerk_invite_role(role)}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or "Failed to create Clerk invitation"
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"Failed to reach Clerk: {str(exc)}"
        )


@router.get("/users", response_model=list[UserResponse])
async def list_users(
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> list[UserResponse]:
    """List all users in the organization."""

    async with get_session() as session:
        result = await session.execute(
            select(UserModel)
            .where(UserModel.org_id == auth.org_id)
            .order_by(UserModel.created_at.desc())
        )
        users = result.scalars().all()

        return [
            UserResponse(
                id=u.id,
                email=u.email,
                name=u.name,
                github_username=u.github_username,
                github_id=u.github_id,
                role=u.role.value,
                org_id=u.org_id,
                created_at=u.created_at.isoformat(),
            )
            for u in users
        ]


@router.get("/quotas/me", response_model=QuotaUsageResponse)
async def get_my_quota_usage(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QuotaUsageResponse:
    used_usd = reserved = bump_usd = Decimal(0)
    base_limit_usd, bump_expires_at = settings.default_daily_quota_usd, None
    if auth.user_id:
        async with get_session() as session:
            used_usd, base_limit_usd, bump_usd, bump_expires_at = (
                await _read_member_quota_fields(session, auth.org_id, auth.user_id)
            )
            reserved = await inflight_reserved_usd(session, auth.org_id, auth.user_id)

    return QuotaUsageResponse(
        user_id=auth.user_id or "",
        limit_usd=float(base_limit_usd + bump_usd),
        used_usd=float(used_usd),
        reserved_usd=float(reserved),
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
        base_limit_usd=float(base_limit_usd),
        bump_usd=float(bump_usd),
        bump_expires_at=bump_expires_at.isoformat() if bump_expires_at else None,
    )


async def _get_member_or_404(session, org_id: str | None, user_id: str) -> UserModel:
    member = (
        await session.execute(
            select(UserModel).where(
                UserModel.id == user_id, UserModel.org_id == org_id
            )
        )
    ).scalar_one_or_none()
    if member is None:
        raise HTTPException(
            status_code=404, detail=f"User {user_id} not found in this org"
        )
    return member


async def _read_member_quota_fields(session, org_id: str | None, user_id: str):
    used_usd = await sum_cost_usd(session, org_id, user_id, quota_window_start())
    base_limit_usd = await get_base_limit(session, org_id, user_id)
    bump_usd, bump_expires_at = await _bump_total_or_zero(session, org_id, user_id)
    return used_usd, base_limit_usd, bump_usd, bump_expires_at


GAMBLE_SIDES = ("heads", "tails")

# Advisory-lock namespace for serializing per-user gamble transactions so
# concurrent bets cannot both validate against the same remaining-quota
# snapshot and collectively overspend.
_GAMBLE_LOCK_NAMESPACE = 0x0DD15


def _flip_coin() -> str:
    return secrets.choice(GAMBLE_SIDES)


def _require_gambler(auth: AuthContext) -> str:
    # Humans only: an org API key must not be able to gamble someone's quota.
    if auth.method != AuthMethod.CLERK_JWT or auth.user_id is None:
        raise HTTPException(
            status_code=403, detail="User auth required to gamble quota"
        )
    return auth.user_id


async def _require_gambles_table(session) -> None:
    if not await quota_gambles_ready(session):
        raise HTTPException(
            status_code=503,
            detail=(
                "Quota gambling is not available yet (schema is still "
                "migrating). Try again shortly."
            ),
        )


@router.post("/quotas/gamble", response_model=QuotaGambleResponse)
async def gamble_quota(
    payload: QuotaGambleRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QuotaGambleResponse:
    user_id = _require_gambler(auth)
    async with get_session() as session:
        await _require_gambles_table(session)
        # Serialize concurrent gambles for the same user: a transaction-scoped
        # advisory lock guarantees the read-validate-insert below runs to
        # completion before another bet reads the remaining-quota snapshot,
        # so two requests can't both pass validation and overspend.
        await session.execute(
            sa_text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": _GAMBLE_LOCK_NAMESPACE, "key": f"{auth.org_id}:{user_id}"},
        )
        limit = await get_effective_limit(session, auth.org_id, user_id)
        used = await sum_cost_usd(session, auth.org_id, user_id, quota_window_start())
        reserved = await inflight_reserved_usd(session, auth.org_id, user_id)
        remaining = limit - used - reserved
        if payload.wager_usd > remaining:
            raise HTTPException(
                status_code=400,
                detail=(
                    "Wager exceeds remaining quota "
                    f"(${max(remaining, Decimal(0)):.2f} left to gamble)"
                ),
            )
        if payload.game == "coinflip":
            result = _flip_coin()
            side: str | None = payload.side
            flip_result: str | None = result
            multiplier = Decimal("2") if result == payload.side else Decimal("0")
            detail: dict = {"side": payload.side, "result": result}
        else:
            side = flip_result = None
            try:
                outcome = casino_games.play(
                    payload.game,
                    risk=payload.risk,
                    bet=payload.bet,
                    target=payload.target,
                    rung=payload.rung,
                )
            except casino_games.GameError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            multiplier, detail = outcome.multiplier, outcome.detail
        won = multiplier > 1
        net = (payload.wager_usd * (multiplier - 1)).quantize(Decimal("0.0001"))
        session.add(
            QuotaGambleModel(
                org_id=auth.org_id,
                user_id=user_id,
                game=payload.game,
                wager_usd=payload.wager_usd,
                net_usd=net,
                multiplier=multiplier,
                detail=detail,
                won=won,
            )
        )
    return QuotaGambleResponse(
        game=payload.game,
        won=won,
        side=side,
        result=flip_result,
        multiplier=float(multiplier),
        wager_usd=float(payload.wager_usd),
        net_usd=float(net),
        limit_usd=float(max(Decimal(0), limit + net)),
        used_usd=float(used),
        reserved_usd=float(reserved),
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
        detail=detail,
    )


@router.get("/quotas/gambles", response_model=QuotaGambleListResponse)
async def list_my_gambles(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> QuotaGambleListResponse:
    user_id = _require_gambler(auth)
    window_start = quota_window_start()
    async with get_session() as session:
        await _require_gambles_table(session)
        rows = (
            (
                await session.execute(
                    select(QuotaGambleModel)
                    .where(
                        QuotaGambleModel.org_id == auth.org_id,
                        QuotaGambleModel.user_id == user_id,
                        QuotaGambleModel.deleted_at.is_(None),
                        QuotaGambleModel.created_at > window_start,
                    )
                    .order_by(QuotaGambleModel.created_at.desc())
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        net = await sum_gamble_net_usd(session, auth.org_id, user_id, window_start)
    return QuotaGambleListResponse(
        items=[
            QuotaGambleItem(
                id=gamble.id,
                game=gamble.game,
                wager_usd=float(gamble.wager_usd),
                net_usd=float(gamble.net_usd),
                multiplier=None if gamble.multiplier is None else float(gamble.multiplier),
                won=gamble.won,
                created_at=gamble.created_at.isoformat(),
                detail=gamble.detail,
            )
            for gamble in rows
        ],
        net_usd=float(net),
    )


def _blackjack_outcome(multiplier: Decimal) -> str:
    if multiplier == Decimal("0"):
        return "lose"
    if multiplier == Decimal("1"):
        return "push"
    if multiplier == Decimal("2"):
        return "win"
    return "blackjack"


def _blackjack_quota_fields(limit: Decimal, used, reserved) -> dict:
    return {
        "limit_usd": float(max(Decimal(0), limit)),
        "used_usd": float(used),
        "reserved_usd": float(reserved),
        "enforced": settings.quota_mode == QuotaMode.ENFORCE,
    }


@router.post("/quotas/gamble/blackjack", response_model=BlackjackStateResponse)
async def blackjack_quota(
    payload: BlackjackRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> BlackjackStateResponse:
    user_id = _require_gambler(auth)
    async with get_session() as session:
        await _require_gambles_table(session)
        # Same per-user advisory lock as single-round gambles: deals, actions
        # and other bets serialize so escrow/settle can't race quota reads.
        await session.execute(
            sa_text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": _GAMBLE_LOCK_NAMESPACE, "key": f"{auth.org_id}:{user_id}"},
        )
        limit = await get_effective_limit(session, auth.org_id, user_id)
        used = await sum_cost_usd(session, auth.org_id, user_id, quota_window_start())
        reserved = await inflight_reserved_usd(session, auth.org_id, user_id)
        remaining = limit - used - reserved

        if payload.action == "deal":
            wager = payload.wager_usd
            if wager is None:
                raise HTTPException(
                    status_code=400, detail="wager_usd is required to deal"
                )
            if wager > remaining:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "Wager exceeds remaining quota "
                        f"(${max(remaining, Decimal(0)):.2f} left to gamble)"
                    ),
                )
            state = casino_games.blackjack_deal()
            # Escrow the wager as a loss now so an abandoned hand can't be a
            # free look at the cards.
            gamble = QuotaGambleModel(
                org_id=auth.org_id,
                user_id=user_id,
                game="blackjack",
                wager_usd=wager,
                net_usd=-wager,
                multiplier=Decimal(0),
                detail={"status": "in_play"},
                won=False,
            )
            session.add(gamble)
            await session.flush()
            hand = QuotaGambleSessionModel(
                org_id=auth.org_id,
                user_id=user_id,
                game="blackjack",
                wager_usd=wager,
                status="active",
                state=state,
                gamble_id=gamble.id,
            )
            session.add(hand)
            await session.flush()
            settle = casino_games.blackjack_initial_settle(state)
            # `limit` was read before the escrow row existed.
            escrow_correction = Decimal(0)
        else:
            if not payload.session_id:
                raise HTTPException(status_code=400, detail="session_id is required")
            hand = (
                await session.execute(
                    select(QuotaGambleSessionModel).where(
                        QuotaGambleSessionModel.id == payload.session_id,
                        QuotaGambleSessionModel.org_id == auth.org_id,
                        QuotaGambleSessionModel.user_id == user_id,
                        QuotaGambleSessionModel.status == "active",
                        QuotaGambleSessionModel.deleted_at.is_(None),
                    )
                )
            ).scalar_one_or_none()
            if hand is None:
                raise HTTPException(
                    status_code=404, detail="No active blackjack hand with that id"
                )
            gamble = (
                await session.execute(
                    select(QuotaGambleModel).where(
                        QuotaGambleModel.id == hand.gamble_id
                    )
                )
            ).scalar_one()
            wager = hand.wager_usd
            state = dict(hand.state) if isinstance(hand.state, dict) else {}
            if any(k not in state for k in ("deck", "player", "dealer", "doubled")):
                raise HTTPException(
                    status_code=409,
                    detail="This hand's state is corrupted; deal a new hand",
                )
            if payload.action == "double" and wager > remaining:
                # The original wager is already escrowed into `remaining`;
                # doubling needs headroom for the second stake.
                raise HTTPException(
                    status_code=400, detail="Not enough quota left to double"
                )
            try:
                settle = casino_games.blackjack_act(state, payload.action)
            except casino_games.GameError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            hand.state = state
            # `limit` already includes the -wager escrow from the deal.
            escrow_correction = wager

        total_wager = wager * (2 if state["doubled"] else 1)
        player_total = casino_games.hand_total(state["player"])[0]

        if settle is None:
            return BlackjackStateResponse(
                session_id=hand.id,
                status="player_turn",
                player=state["player"],
                dealer=[state["dealer"][0], "??"],
                player_total=player_total,
                can_double=(
                    len(state["player"]) == 2
                    and remaining - (wager if payload.action == "deal" else 0)
                    >= wager
                ),
                wager_usd=float(total_wager),
                **_blackjack_quota_fields(
                    limit - (wager if payload.action == "deal" else Decimal(0)),
                    used,
                    reserved,
                ),
            )

        net = (total_wager * (settle - 1)).quantize(Decimal("0.0001"))
        outcome = _blackjack_outcome(settle)
        gamble.net_usd = net
        gamble.multiplier = settle
        gamble.won = net > 0
        gamble.detail = {
            "player": state["player"],
            "dealer": state["dealer"],
            "player_total": player_total,
            "dealer_total": casino_games.hand_total(state["dealer"])[0],
            "outcome": outcome,
            "doubled": state["doubled"],
        }
        gamble.updated_at = utcnow()
        hand.status = "settled"

        return BlackjackStateResponse(
            session_id=hand.id,
            status="settled",
            player=state["player"],
            dealer=state["dealer"],
            player_total=player_total,
            dealer_total=casino_games.hand_total(state["dealer"])[0],
            can_double=False,
            outcome=outcome,
            wager_usd=float(total_wager),
            net_usd=float(net),
            multiplier=float(settle),
            **_blackjack_quota_fields(
                limit + escrow_correction + net, used, reserved
            ),
        )


@router.post("/quotas/gamble/wrestle", response_model=WrestleResponse)
async def wrestle_bet(
    payload: WrestleRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> WrestleResponse:
    # LOCAL hot-seat match: the server can't referee a same-keyboard game, so
    # the win/loss is trust-the-client (deliberate, per the feature owner). No
    # escrow, no cap -- just double-or-nothing on the reported result.
    user_id = _require_gambler(auth)
    async with get_session() as session:
        await _require_gambles_table(session)
        await session.execute(
            text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": _GAMBLE_LOCK_NAMESPACE, "key": f"{auth.org_id}:{user_id}"},
        )
        limit = await get_effective_limit(session, auth.org_id, user_id)
        used = await sum_cost_usd(session, auth.org_id, user_id, quota_window_start())
        reserved = await inflight_reserved_usd(session, auth.org_id, user_id)
        if payload.wager_usd > limit - used - reserved:
            raise HTTPException(
                status_code=400, detail="Wager exceeds your remaining quota"
            )
        net = payload.wager_usd if payload.won else -payload.wager_usd
        session.add(
            QuotaGambleModel(
                org_id=auth.org_id,
                user_id=user_id,
                game="wrestle",
                wager_usd=payload.wager_usd,
                net_usd=net,
                multiplier=Decimal("2") if payload.won else Decimal("0"),
                detail={"outcome": "win" if payload.won else "lose"},
                won=payload.won,
            )
        )
    return WrestleResponse(
        won=payload.won,
        wager_usd=float(payload.wager_usd),
        net_usd=float(net),
        limit_usd=float(max(Decimal(0), limit + net)),
        used_usd=float(used),
        reserved_usd=float(reserved),
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
    )


DUEL_OPEN_LIMIT = 5


async def _lock_gamble_users(session, org_id, *user_ids: str) -> None:
    # Same lock keys as the single-user gamble path, taken in sorted order so
    # a duel settlement can't deadlock against solo bets or another duel.
    for user_id in sorted(set(user_ids)):
        await session.execute(
            sa_text("SELECT pg_advisory_xact_lock(:ns, hashtext(:key))"),
            {"ns": _GAMBLE_LOCK_NAMESPACE, "key": f"{org_id}:{user_id}"},
        )


def _pick_duel_winner(challenger_user_id: str, opponent_user_id: str) -> str:
    return secrets.choice((challenger_user_id, opponent_user_id))


async def _require_duels_table(session) -> None:
    await _require_gambles_table(session)
    if not await quota_duels_ready(session):
        raise HTTPException(
            status_code=503,
            detail=(
                "Duels are not available yet (schema is still migrating). "
                "Try again shortly."
            ),
        )


def _duel_member(user) -> DuelMember:
    return DuelMember(
        user_id=user.id,
        label=user.name or user.github_username or user.email,
    )


def _duel_member_or_ghost(user_id: str, users_by_id: dict) -> DuelMember:
    user = users_by_id.get(user_id)
    if user is None:
        return DuelMember(user_id=user_id, label="departed member")
    return _duel_member(user)


def _duel_fight(duel) -> DuelFight | None:
    if not isinstance(duel.fight, dict):
        return None
    seed = duel.fight.get("seed")
    winner_role = duel.fight.get("winner_role")
    if not isinstance(seed, int) or winner_role not in ("challenger", "opponent"):
        return None
    return DuelFight(seed=seed, winner_role=winner_role)


def _duel_item(duel, me: str, users_by_id: dict) -> DuelItem:
    role = "challenger" if duel.challenger_user_id == me else "opponent"
    settled = duel.status == "settled"
    i_won = (duel.winner_user_id == me) if settled else None
    wager = float(duel.wager_usd)
    return DuelItem(
        id=duel.id,
        status=duel.status,
        role=role,
        challenger=_duel_member_or_ghost(duel.challenger_user_id, users_by_id),
        opponent=_duel_member_or_ghost(duel.opponent_user_id, users_by_id),
        wager_usd=wager,
        winner_user_id=duel.winner_user_id,
        i_won=i_won,
        my_net_usd=(wager if i_won else -wager) if settled else None,
        fight=_duel_fight(duel) if settled else None,
        created_at=duel.created_at.isoformat(),
    )


async def _duel_users(session, org_id, *user_ids: str) -> dict:
    rows = (
        (
            await session.execute(
                select(UserModel).where(
                    UserModel.org_id == org_id, UserModel.id.in_(set(user_ids))
                )
            )
        )
        .scalars()
        .all()
    )
    return {u.id: u for u in rows}


@router.get("/quotas/duels", response_model=DuelListResponse)
async def list_my_duels(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DuelListResponse:
    me = _require_gambler(auth)
    window_start = quota_window_start()
    async with get_session() as session:
        await _require_duels_table(session)
        duels = (
            (
                await session.execute(
                    select(QuotaDuelModel)
                    .where(
                        QuotaDuelModel.org_id == auth.org_id,
                        QuotaDuelModel.deleted_at.is_(None),
                        (QuotaDuelModel.challenger_user_id == me)
                        | (QuotaDuelModel.opponent_user_id == me),
                        QuotaDuelModel.created_at > window_start,
                    )
                    .order_by(QuotaDuelModel.created_at.desc())
                    .limit(100)
                )
            )
            .scalars()
            .all()
        )
        roster = (
            (
                await session.execute(
                    select(UserModel)
                    .where(
                        UserModel.org_id == auth.org_id,
                        UserModel.is_active.is_(True),
                        UserModel.id != me,
                    )
                    .order_by(UserModel.created_at.asc())
                )
            )
            .scalars()
            .all()
        )
        user_ids = {d.challenger_user_id for d in duels} | {
            d.opponent_user_id for d in duels
        } | {me}
        users_by_id = await _duel_users(session, auth.org_id, *user_ids)
    items = [
        _duel_item(d, me, users_by_id)
        for d in duels
        if d.challenger_user_id in users_by_id and d.opponent_user_id in users_by_id
    ]
    return DuelListResponse(
        incoming=[
            i for i in items if i.status == "open" and i.role == "opponent"
        ],
        outgoing=[
            i for i in items if i.status == "open" and i.role == "challenger"
        ],
        recent=[i for i in items if i.status == "settled"],
        members=[_duel_member(u) for u in roster],
    )


@router.post("/quotas/duels", response_model=DuelItem)
async def create_duel(
    payload: DuelCreateRequest,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DuelItem:
    me = _require_gambler(auth)
    if payload.opponent_user_id == me:
        raise HTTPException(
            status_code=400, detail="You cannot wrestle yourself. Seek help."
        )
    async with get_session() as session:
        await _require_duels_table(session)
        opponent = (
            await session.execute(
                select(UserModel).where(
                    UserModel.id == payload.opponent_user_id,
                    UserModel.org_id == auth.org_id,
                    UserModel.is_active.is_(True),
                )
            )
        ).scalar_one_or_none()
        if opponent is None:
            raise HTTPException(status_code=404, detail="Opponent not found in this org")
        open_count = await session.scalar(
            select(func.count(QuotaDuelModel.id)).where(
                QuotaDuelModel.org_id == auth.org_id,
                QuotaDuelModel.challenger_user_id == me,
                QuotaDuelModel.status == "open",
                QuotaDuelModel.deleted_at.is_(None),
                QuotaDuelModel.created_at > quota_window_start(),
            )
        )
        if (open_count or 0) >= DUEL_OPEN_LIMIT:
            raise HTTPException(
                status_code=400,
                detail=f"You already have {DUEL_OPEN_LIMIT} open challenges. Settle some.",
            )
        limit = await get_effective_limit(session, auth.org_id, me)
        used = await sum_cost_usd(session, auth.org_id, me, quota_window_start())
        reserved = await inflight_reserved_usd(session, auth.org_id, me)
        if payload.wager_usd > limit - used - reserved:
            raise HTTPException(
                status_code=400, detail="Wager exceeds your remaining quota"
            )
        duel = QuotaDuelModel(
            org_id=auth.org_id,
            challenger_user_id=me,
            opponent_user_id=opponent.id,
            wager_usd=payload.wager_usd,
            status="open",
        )
        session.add(duel)
        await session.flush()
        users_by_id = await _duel_users(session, auth.org_id, me, opponent.id)
        return _duel_item(duel, me, users_by_id)


@router.post("/quotas/duels/{duel_id}/accept", response_model=DuelAcceptResponse)
async def accept_duel(
    duel_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DuelAcceptResponse:
    me = _require_gambler(auth)
    async with get_session() as session:
        await _require_duels_table(session)
        duel = (
            await session.execute(
                select(QuotaDuelModel).where(
                    QuotaDuelModel.id == duel_id,
                    QuotaDuelModel.org_id == auth.org_id,
                    QuotaDuelModel.opponent_user_id == me,
                    QuotaDuelModel.deleted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if duel is None:
            raise HTTPException(status_code=404, detail="No such challenge for you")
        await _lock_gamble_users(session, auth.org_id, duel.challenger_user_id, me)
        # CAS under the locks: a concurrent cancel/second-accept loses cleanly.
        claimed = await session.execute(
            QuotaDuelModel.__table__.update()
            .where(
                QuotaDuelModel.id == duel.id,
                QuotaDuelModel.status == "open",
            )
            .values(status="settled", updated_at=utcnow())
        )
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="This challenge is gone")
        if duel.created_at <= quota_window_start():
            raise HTTPException(
                status_code=409, detail="This challenge expired (24h). Rematch?"
            )
        window_start = quota_window_start()
        my_limit = await get_effective_limit(session, auth.org_id, me)
        my_used = await sum_cost_usd(session, auth.org_id, me, window_start)
        my_reserved = await inflight_reserved_usd(session, auth.org_id, me)
        if duel.wager_usd > my_limit - my_used - my_reserved:
            raise HTTPException(
                status_code=400, detail="You can't cover the stake right now"
            )
        rival = duel.challenger_user_id
        rival_remaining = (
            await get_effective_limit(session, auth.org_id, rival)
            - await sum_cost_usd(session, auth.org_id, rival, window_start)
            - await inflight_reserved_usd(session, auth.org_id, rival)
        )
        if duel.wager_usd > rival_remaining:
            raise HTTPException(
                status_code=409,
                detail="The challenger can no longer cover the stake",
            )
        users_by_id = await _duel_users(session, auth.org_id, rival, me)
        if rival not in users_by_id:
            raise HTTPException(
                status_code=409, detail="The challenger is no longer in this org"
            )
        winner = _pick_duel_winner(rival, me)
        fight = {
            "seed": secrets.randbelow(2**31),
            "winner_role": "challenger" if winner == rival else "opponent",
        }
        duel.status = "settled"
        duel.winner_user_id = winner
        duel.fight = fight
        duel.updated_at = utcnow()
        for user_id in (rival, me):
            won = user_id == winner
            other = users_by_id[me if user_id == rival else rival]
            session.add(
                QuotaGambleModel(
                    org_id=auth.org_id,
                    user_id=user_id,
                    game="wrestle",
                    wager_usd=duel.wager_usd,
                    net_usd=duel.wager_usd if won else -duel.wager_usd,
                    multiplier=Decimal("2") if won else Decimal("0"),
                    detail={
                        "duel_id": duel.id,
                        "opponent": _duel_member(other).label,
                        "seed": fight["seed"],
                    },
                    won=won,
                )
            )
        my_net = duel.wager_usd if winner == me else -duel.wager_usd
        item = _duel_item(duel, me, users_by_id)
    return DuelAcceptResponse(
        duel=item,
        limit_usd=float(max(Decimal(0), my_limit + my_net)),
        used_usd=float(my_used),
        reserved_usd=float(my_reserved),
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
    )


@router.post("/quotas/duels/{duel_id}/dismiss", response_model=DuelItem)
async def dismiss_duel(
    duel_id: str,
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> DuelItem:
    me = _require_gambler(auth)
    async with get_session() as session:
        await _require_duels_table(session)
        duel = (
            await session.execute(
                select(QuotaDuelModel).where(
                    QuotaDuelModel.id == duel_id,
                    QuotaDuelModel.org_id == auth.org_id,
                    QuotaDuelModel.deleted_at.is_(None),
                    (QuotaDuelModel.challenger_user_id == me)
                    | (QuotaDuelModel.opponent_user_id == me),
                )
            )
        ).scalar_one_or_none()
        if duel is None:
            raise HTTPException(status_code=404, detail="No such challenge for you")
        new_status = "cancelled" if duel.challenger_user_id == me else "declined"
        claimed = await session.execute(
            QuotaDuelModel.__table__.update()
            .where(
                QuotaDuelModel.id == duel.id,
                QuotaDuelModel.status == "open",
            )
            .values(status=new_status, updated_at=utcnow())
        )
        if claimed.rowcount != 1:
            raise HTTPException(status_code=409, detail="This challenge is gone")
        duel.status = new_status
        users_by_id = await _duel_users(
            session, auth.org_id, duel.challenger_user_id, duel.opponent_user_id
        )
        return _duel_item(duel, me, users_by_id)


def _quota_member_item(
    member, used_usd, base_limit_usd, bump_usd=Decimal(0), bump_expires_at=None
) -> QuotaMemberItem:
    return QuotaMemberItem(
        user_id=member.id,
        email=member.email,
        name=member.name,
        github_username=member.github_username,
        role=member.role.value,
        limit_usd=float(base_limit_usd + bump_usd),
        used_usd=float(used_usd),
        base_limit_usd=float(base_limit_usd),
        bump_usd=float(bump_usd),
        bump_expires_at=bump_expires_at.isoformat() if bump_expires_at else None,
    )


async def _member_quota_item(session, org_id: str | None, member) -> QuotaMemberItem:
    fields = await _read_member_quota_fields(session, org_id, member.id)
    return _quota_member_item(member, *fields)


def _as_float_or_none(value) -> float | None:
    return None if value is None else float(value)


def _is_undefined_table_error(exc: ProgrammingError) -> bool:
    error: BaseException | None = exc.orig if exc.orig is not None else exc
    seen: set[int] = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if (
            getattr(error, "sqlstate", None) == "42P01"
            or getattr(error, "pgcode", None) == "42P01"
            or type(error).__name__ == "UndefinedTableError"
        ):
            return True
        error = error.__cause__
    return False


async def _org_trial_usage(session, org_id) -> tuple[Decimal, Decimal]:
    used = await sum_org_cost_usd(session, org_id, start_of_month_utc())
    reserved = await org_inflight_reserved_usd(session, org_id)
    return used, reserved


async def _org_quota_fields(session, org_id) -> dict:
    effective_org_limit = await get_effective_org_limit(session, org_id)
    org_used, org_reserved = await _org_trial_usage(session, org_id)
    return {
        "org_limit_usd": _as_float_or_none(effective_org_limit),
        "org_used_usd": float(org_used),
        "org_reserved_usd": float(org_reserved),
        "org_default_limit_usd": _as_float_or_none(
            settings.default_org_monthly_quota_usd
        ),
    }


async def _org_quota_fields_no_cap_table(org_id) -> dict:
    # org_quotas is missing (deploy-before-migrate): the cap falls back to the
    # configured default, but month spend + in-flight reservation live on the
    # trials table and stay readable, so report the real usage, not zero.
    default = _as_float_or_none(settings.default_org_monthly_quota_usd)
    async with get_session() as session:
        org_used, org_reserved = await _org_trial_usage(session, org_id)
    return {
        "org_limit_usd": default,
        "org_used_usd": float(org_used),
        "org_reserved_usd": float(org_reserved),
        "org_default_limit_usd": default,
    }


async def _org_quota_fields_or_unavailable(org_id) -> dict:
    try:
        async with get_session() as session:
            return await _org_quota_fields(session, org_id)
    except ProgrammingError as exc:
        if not _is_undefined_table_error(exc):
            raise
        logger.warning(
            "GET /quotas org cap unavailable (org_quotas schema not "
            "migrated yet); degrading limit to default, usage still real",
            exc_info=True,
        )
        return await _org_quota_fields_no_cap_table(org_id)


async def _bump_totals_or_empty(org_id) -> dict:
    # quota_bumps can be missing in a deploy-before-migrate window; the boost
    # feature then reads as "no live boosts" until the migration lands, mirroring
    # the org-cap fallback above. Own session so a missing table cannot poison a
    # caller's transaction.
    try:
        async with get_session() as session:
            return await live_bump_totals_by_user(session, org_id)
    except ProgrammingError as exc:
        if not _is_undefined_table_error(exc):
            raise
        logger.warning(
            "boosts unavailable (quota_bumps schema not migrated yet); "
            "treating members as un-boosted",
            exc_info=True,
        )
        return {}


async def _bump_total_or_zero(
    session, org_id, user_id
) -> tuple[Decimal, datetime | None]:
    # Savepoint so a missing quota_bumps table (deploy-before-migrate) rolls back
    # just this read and leaves the caller's transaction usable -- while still
    # seeing the caller's own uncommitted writes, so POST/DELETE can build their
    # response from a re-read in the same transaction.
    try:
        async with session.begin_nested():
            return await live_bump_total(session, org_id, user_id)
    except ProgrammingError as exc:
        if not _is_undefined_table_error(exc):
            raise
        logger.warning(
            "boost lookup unavailable (quota_bumps schema not migrated yet); "
            "treating member as un-boosted",
            exc_info=True,
        )
        return Decimal(0), None


@router.get("/quotas", response_model=QuotaListResponse)
async def list_member_quotas(
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaListResponse:
    period_start = quota_window_start()
    default_limit_usd = settings.default_daily_quota_usd

    async with get_session() as session:
        members = (
            (
                await session.execute(
                    select(UserModel)
                    .where(UserModel.org_id == auth.org_id)
                    .order_by(UserModel.created_at.desc())
                )
            )
            .scalars()
            .all()
        )

        used_usd_by_user_id = await sum_cost_usd_by_user(
            session, auth.org_id, period_start
        )

        override_rows = await session.execute(
            select(QuotaModel.user_id, QuotaModel.limit_usd).where(
                QuotaModel.org_id == auth.org_id,
                QuotaModel.deleted_at.is_(None),
            )
        )
        override_limit_by_user_id = dict(override_rows.all())

    bump_totals_by_user_id = await _bump_totals_or_empty(auth.org_id)
    org_fields = await _org_quota_fields_or_unavailable(auth.org_id)

    return QuotaListResponse(
        members=[
            _quota_member_item(
                member,
                used_usd_by_user_id.get(member.id, Decimal(0)),
                override_limit_by_user_id.get(member.id, default_limit_usd),
                *bump_totals_by_user_id.get(member.id, (Decimal(0), None)),
            )
            for member in members
        ],
        **org_fields,
    )


def _org_usage_response(org_fields: dict, org_used_today: Decimal) -> OrgUsageResponse:
    now = start_of_today_utc()
    days_in_month = calendar.monthrange(now.year, now.month)[1]
    days_remaining = days_in_month - now.day + 1

    limit = org_fields["org_limit_usd"]
    used_month = Decimal(str(org_fields["org_used_usd"]))
    reserved = Decimal(str(org_fields["org_reserved_usd"]))
    daily_goal: float | None = None
    if limit is not None:
        remaining_at_day_start = max(
            Decimal(0),
            Decimal(str(limit)) - used_month + org_used_today - reserved,
        )
        daily_goal = float(remaining_at_day_start / Decimal(days_remaining))

    return OrgUsageResponse(
        org_limit_usd=limit,
        org_used_month_usd=float(used_month),
        org_reserved_usd=org_fields["org_reserved_usd"],
        org_used_today_usd=float(org_used_today),
        daily_goal_usd=daily_goal,
        days_remaining=days_remaining,
        enforced=settings.quota_mode == QuotaMode.ENFORCE,
    )


@router.get("/quotas/org", response_model=OrgUsageResponse)
async def get_org_quota_usage(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> OrgUsageResponse:
    if auth.org_id is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    try:
        async with get_session() as session:
            org_fields = await _org_quota_fields(session, auth.org_id)
            org_used_today = await sum_org_cost_usd(
                session, auth.org_id, start_of_today_utc()
            )
    except ProgrammingError as exc:
        if not _is_undefined_table_error(exc):
            raise
        logger.warning(
            "GET /quotas/org cap unavailable (org_quotas schema not migrated "
            "yet); degrading limit to default, usage still real",
            exc_info=True,
        )
        org_fields = await _org_quota_fields_no_cap_table(auth.org_id)
        async with get_session() as session:
            org_used_today = await sum_org_cost_usd(
                session, auth.org_id, start_of_today_utc()
            )

    return _org_usage_response(org_fields, org_used_today)


@router.put("/quotas/org", response_model=OrgQuotaResponse)
async def set_org_quota(
    payload: QuotaUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> OrgQuotaResponse:
    try:
        async with get_session() as session:
            if payload.limit_usd is None:
                await session.execute(
                    OrgQuotaModel.__table__.delete().where(
                        OrgQuotaModel.org_id == auth.org_id
                    )
                )
            else:
                await session.execute(
                    pg_insert(OrgQuotaModel)
                    .values(
                        id=generate_id(),
                        org_id=auth.org_id,
                        limit_usd=payload.limit_usd,
                        period_kind="monthly",
                    )
                    .on_conflict_do_update(
                        index_elements=["org_id"],
                        index_where=OrgQuotaModel.deleted_at.is_(None),
                        # Clear deleted_at too: get_effective_org_limit / the
                        # admin display only read live rows, so a PUT over a
                        # tombstoned override must revive it (the partial unique
                        # index only spans live rows).
                        set_={
                            "limit_usd": payload.limit_usd,
                            "period_kind": "monthly",
                            "updated_at": utcnow(),
                            "deleted_at": None,
                        },
                    )
                )

            org_fields = await _org_quota_fields(session, auth.org_id)
    except ProgrammingError as exc:
        if not _is_undefined_table_error(exc):
            raise
        raise HTTPException(
            status_code=503,
            detail=(
                "Org quotas are not available yet (schema is still migrating). "
                "Try again shortly."
            ),
        ) from exc

    return OrgQuotaResponse(**org_fields)


@router.put("/quotas/{user_id}", response_model=QuotaMemberItem)
async def set_member_quota(
    user_id: str,
    payload: QuotaUpdateRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = await _get_member_or_404(session, auth.org_id, user_id)

        if payload.limit_usd is None:
            await session.execute(
                QuotaModel.__table__.delete().where(
                    QuotaModel.org_id == auth.org_id,
                    QuotaModel.user_id == user_id,
                )
            )
        else:
            await session.execute(
                pg_insert(QuotaModel)
                .values(
                    id=generate_id(),
                    org_id=auth.org_id,
                    user_id=user_id,
                    limit_usd=payload.limit_usd,
                )
                .on_conflict_do_update(
                    index_elements=["org_id", "user_id"],
                    set_={
                        "limit_usd": payload.limit_usd,
                        "updated_at": utcnow(),
                        "deleted_at": None,
                    },
                )
            )

        return await _member_quota_item(session, auth.org_id, member)


@router.post("/quotas/{user_id}/bumps", response_model=QuotaMemberItem)
async def add_member_quota_bump(
    user_id: str,
    payload: QuotaBumpRequest,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = await _get_member_or_404(session, auth.org_id, user_id)

        db_now = await session.scalar(sa_text("SELECT NOW()"))
        session.add(
            QuotaBumpModel(
                id=generate_id(),
                org_id=auth.org_id,
                user_id=user_id,
                amount_usd=payload.amount_usd,
                expires_at=db_now + timedelta(hours=payload.duration_hours),
                reason=payload.reason,
                granted_by_user_id=auth.user_id,
            )
        )
        await session.flush()
        return await _member_quota_item(session, auth.org_id, member)


@router.delete("/quotas/{user_id}/bumps", response_model=QuotaMemberItem)
async def revoke_member_quota_bumps(
    user_id: str,
    auth: Annotated[AuthContext, Depends(require_can_manage_quotas)],
) -> QuotaMemberItem:
    async with get_session() as session:
        member = await _get_member_or_404(session, auth.org_id, user_id)

        await session.execute(
            update(QuotaBumpModel)
            .where(
                QuotaBumpModel.org_id == auth.org_id,
                QuotaBumpModel.user_id == user_id,
                QuotaBumpModel.revoked_at.is_(None),
                QuotaBumpModel.deleted_at.is_(None),
                QuotaBumpModel.expires_at > func.now(),
            )
            .values(revoked_at=utcnow(), updated_at=utcnow())
        )
        await session.flush()
        return await _member_quota_item(session, auth.org_id, member)


@router.post("/users", response_model=InviteUserResponse)
async def invite_user(
    request: InviteUserRequest,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> InviteUserResponse:
    """Invite a new user to the organization via Clerk."""

    # Validate role
    try:
        role = UserRole(request.role)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid role: {request.role}. Must be one of: admin, member",
        )

    if not auth.org or not auth.org.clerk_org_id:
        raise HTTPException(
            status_code=400,
            detail="Organization is not linked to Clerk",
        )

    invitation = await _create_clerk_invitation(
        auth.org.clerk_org_id, request.email, role
    )

    return InviteUserResponse(
        invitation_id=invitation.get("id", ""),
        email=invitation.get("email_address", request.email),
        role=invitation.get("role", _clerk_invite_role(role)),
        status=invitation.get("status", "pending"),
    )


async def _delete_clerk_user(clerk_user_id: str) -> None:
    """Delete the user in Clerk. A 404 means the Clerk user is already gone,
    which is fine — we still proceed with local cleanup."""
    if not CLERK_SECRET_KEY:
        raise HTTPException(
            status_code=500,
            detail="CLERK_SECRET_KEY not configured",
        )

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.delete(url, headers=headers)
            if response.status_code == 404:
                return
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = exc.response.text or "Failed to delete Clerk user"
        raise HTTPException(status_code=exc.response.status_code, detail=detail)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=503, detail=f"Failed to reach Clerk: {str(exc)}"
        )


async def _require_no_stranded_org(session, rows: list[UserModel]) -> None:
    """Reject self-deletion that would leave a *shared* org without any
    admin. Orgs where the deleter is the only active member (e.g. personal
    orgs) are exempt — otherwise personal-org users could never delete."""
    for row in rows:
        if row.role != UserRole.ADMIN:
            continue
        others = (
            await session.execute(
                select(UserModel.id, UserModel.role)
                .where(UserModel.org_id == row.org_id)
                .where(UserModel.id != row.id)
                .where(UserModel.is_active == True)  # noqa: E712
            )
        ).all()
        if others and not any(role == UserRole.ADMIN for _, role in others):
            raise HTTPException(
                status_code=400,
                detail=(
                    "You are the last admin of a workspace with other "
                    "members. Promote another admin before deleting your "
                    "account."
                ),
            )


async def _clerk_user_exists(clerk_user_id: str) -> bool | None:
    """Best-effort existence probe used when a Clerk delete errors.

    Returns True/False on a definitive answer, None when Clerk cannot be
    reached (or no secret is configured) — callers must treat None as
    "unknown", not as either definitive state.
    """
    if not CLERK_SECRET_KEY:
        return None

    url = f"https://api.clerk.com/v1/users/{clerk_user_id}"
    headers = {"Authorization": f"Bearer {CLERK_SECRET_KEY}"}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(url, headers=headers)
        if response.status_code == 404:
            return False
        response.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        logger.warning(
            "Clerk existence probe for %s failed (treating as unknown): %s",
            clerk_user_id,
            exc,
        )
        return None


@router.delete("/users/me")
async def delete_my_account(
    auth: Annotated[AuthContext, Depends(require_auth)],
) -> dict:
    """Delete the calling user's account.

    Soft-deletes the local user rows first (committed before Clerk is
    touched), then deletes the Clerk user. If the Clerk delete errors, the
    tombstones are rolled back only when Clerk confirms the account still
    exists; a confirmed-gone answer proceeds as success and an unknown answer
    keeps the tombstones and asks the user to retry. Every outcome is
    retryable and none leaves a Clerk-deleted identity active locally.
    Requires interactive Clerk auth — an API key must not be able to destroy
    the account that minted it.
    """
    if auth.method != AuthMethod.CLERK_JWT:
        raise HTTPException(
            status_code=403,
            detail="Account deletion requires signing in (API keys not allowed)",
        )

    tombstoned: list[tuple[str, str]] = []
    async with get_session() as session:
        result = await session.execute(
            select(UserModel).where(UserModel.id == auth.user_id)
        )
        user = result.scalar_one_or_none()
        if not user or not user.clerk_user_id:
            raise HTTPException(status_code=404, detail="User not found")
        clerk_user_id = user.clerk_user_id

        rows_result = await session.execute(
            select(UserModel)
            .where(UserModel.clerk_user_id == clerk_user_id)
            .where(UserModel.is_active == True)  # noqa: E712
        )
        rows = list(rows_result.scalars().all())
        await _require_no_stranded_org(session, rows)

        for row in rows:
            row.is_active = False
            row.deleted_at = utcnow()
            tombstoned.append((row.org_id, row.id))
        await session.commit()

    # Drop this container's cached auth contexts as soon as the rows are
    # tombstoned, so a cached JWT context can't keep acting as a locally
    # deactivated user while the Clerk call below is in flight.
    invalidate_cached_clerk_auth(clerk_user_id)

    original_row_ids = [row_id for _, row_id in tombstoned]
    try:
        await _delete_clerk_user(clerk_user_id)
    except Exception as delete_exc:
        logger.warning(
            "Account deletion: Clerk delete for %s failed: %s",
            clerk_user_id,
            delete_exc,
        )
        # Check whether the Clerk account actually survived: the delete may
        # have succeeded before a timeout/network error, or a parallel
        # DELETE /users/me may have won.
        clerk_alive = await _clerk_user_exists(clerk_user_id)
        if clerk_alive is False:
            logger.warning(
                "Clerk delete for %s errored but the user is already gone; "
                "treating as success",
                clerk_user_id,
            )
        else:
            # Confirmed alive OR unknown: restore the tombstones and surface
            # the real error. Keeping tombstones on "unknown" would brick the
            # account whenever Clerk is persistently unreachable or the
            # secret is misconfigured (both calls fail identically): locally
            # deactivated, Clerk sign-in still alive. The opposite mistake —
            # restoring rows for an identity whose deletion actually landed —
            # is recoverable: the sign-in is gone, no new JWT can use the
            # rows, and the ``user.deleted`` webhook re-tombstones them.
            #
            # A concurrent request with a still-valid JWT may have
            # JIT-provisioned fresh rows during the window — tombstone those
            # first so the restore can't leave duplicate active rows.
            async with get_session() as session:
                await session.execute(
                    update(UserModel)
                    .where(UserModel.clerk_user_id == clerk_user_id)
                    .where(UserModel.id.notin_(original_row_ids))
                    .where(UserModel.is_active == True)  # noqa: E712
                    .values(is_active=False, deleted_at=utcnow())
                )
                await session.execute(
                    update(UserModel)
                    .where(UserModel.id.in_(original_row_ids))
                    .values(is_active=True, deleted_at=None)
                    .execution_options(include_deleted=True)
                )
                await session.commit()
            # Contexts cached during the window may point at the
            # now-tombstoned duplicate rows; drop them so the next request
            # resolves the restored originals.
            invalidate_cached_clerk_auth(clerk_user_id)
            # Propagate the underlying failure so the dialog shows an
            # actionable reason instead of a generic retry message.
            if isinstance(delete_exc, HTTPException):
                raise delete_exc
            raise HTTPException(
                status_code=503,
                detail=f"Could not delete your sign-in account: {delete_exc}",
            ) from delete_exc

    # Point of no return: the Clerk account is gone. Nothing past here may
    # fail the request — the UI must not report failure for a deletion that
    # already happened. Sweep any rows a concurrent JIT provisioning revived
    # between the tombstone commit and Clerk deletion, then drop contexts
    # cached during that window. Other containers' caches age out within the
    # 60s TTL, Clerk revoked the user's sessions above, and the
    # ``user.deleted`` webhook is the cross-container safety net that
    # re-tombstones any later revival.
    try:
        async with get_session() as session:
            await session.execute(
                update(UserModel)
                .where(UserModel.clerk_user_id == clerk_user_id)
                .where(UserModel.is_active == True)  # noqa: E712
                .values(is_active=False, deleted_at=utcnow())
            )
            await session.commit()
    except Exception:
        logger.exception(
            "Account deletion: post-Clerk revived-row sweep failed for %s "
            "(user.deleted webhook is the safety net)",
            clerk_user_id,
        )
    invalidate_cached_clerk_auth(clerk_user_id)

    # Tag ownership transfer runs best-effort per org, after the point of no
    # return. A failure must not surface as a deletion error (the Clerk
    # account is already gone); the hourly ``sweep_orphaned_tag_owners``
    # sweep is the documented safety net for any tags this leaves orphaned.
    for org_id, user_row_id in tombstoned:
        try:
            async with get_session() as session:
                await transfer_tag_ownership_to_admin(
                    session, org_id=org_id, deactivated_user_id=user_row_id
                )
                await session.commit()
        except Exception:
            logger.exception(
                "Account deletion: tag ownership transfer failed for user %s "
                "in org %s (tags left for the orphaned-owner sweep)",
                user_row_id,
                org_id,
            )

    return {"status": "deleted", "clerk_user_id": clerk_user_id}


@router.delete("/users/{user_id}")
async def remove_user(
    user_id: str,
    auth: Annotated[AuthContext, Depends(require_admin)],
) -> dict:
    """Remove a user from the organization.

    Soft-deletes the row (stamps ``deleted_at`` and clears ``is_active``)
    so the session-level filter immediately hides the user from list /
    auth paths. ``is_active=False`` is preserved alongside the tombstone
    for any reader that hasn't migrated off the legacy flag.
    """

    async with get_session() as session:
        result = await session.execute(
            select(UserModel)
            .where(UserModel.id == user_id)
            .where(UserModel.org_id == auth.org_id)
        )
        user = result.scalar_one_or_none()

        if not user:
            raise HTTPException(status_code=404, detail="User not found")

        # Prevent removing the last admin. The admin count uses live rows
        # only -- the auto-filter already excludes soft-deleted users, so
        # the explicit ``is_active`` check just additionally ignores
        # deactivated-but-not-removed admins.
        if user.role == UserRole.ADMIN:
            admins = await session.execute(
                select(UserModel)
                .where(UserModel.org_id == auth.org_id)
                .where(UserModel.role == UserRole.ADMIN)
                .where(UserModel.is_active == True)  # noqa: E712
            )
            if len(list(admins.scalars().all())) <= 1:
                raise HTTPException(
                    status_code=400,
                    detail="Cannot remove the last admin of the organization",
                )

        user.is_active = False
        user.deleted_at = utcnow()
        await transfer_tag_ownership_to_admin(
            session, org_id=auth.org_id, deactivated_user_id=user_id
        )
        await session.commit()

        return {"status": "removed", "user_id": user_id}
