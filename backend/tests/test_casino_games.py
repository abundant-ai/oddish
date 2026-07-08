"""Casino engines (pure math, seeded rng) + endpoint round-trips.

Engine determinism comes from random.Random(seed) or hand-rolled rng stubs
passed as ``rng=``; baccarat stacks the shoe via a shuffle() stub (cards pop
from the END of the shoe). Endpoint blackjack is made deterministic by
monkeypatching api.casino_games.blackjack_deal.
"""

from __future__ import annotations

import itertools
import math
import os
import random
import uuid
from decimal import ROUND_HALF_UP, Decimal

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from api import casino_games
from api.app import create_app
from auth import require_auth
from auth.types import AuthContext, AuthMethod
from models import (
    OrganizationModel,
    QuotaGambleModel,
    QuotaGambleSessionModel,
    QuotaModel,
    UserModel,
    UserRole,
)
from oddish.db import get_session

DB_URL = os.environ.get("ODDISH_DATABASE_URL")
requires_db = pytest.mark.skipif(not DB_URL, reason="ODDISH_DATABASE_URL not set")


# --- engine: slots ----------------------------------------------------------


def test_slots_ev_full_enumeration():
    strip = casino_games.SLOT_STRIP
    total = sum(
        (
            casino_games.slot_multiplier(list(reels))
            for reels in itertools.product(strip, repeat=3)
        ),
        Decimal(0),
    )
    ev = total / (len(strip) ** 3)
    assert Decimal("0.90") <= ev <= Decimal("1.02")


def test_play_slots_seeded_matches_paytable():
    rng = random.Random(7)
    play = casino_games.play_slots(rng)
    assert len(play.detail["reels"]) == 3
    assert play.multiplier == casino_games.slot_multiplier(play.detail["reels"])


# --- engine: plinko -----------------------------------------------------------


@pytest.mark.parametrize("risk", ["low", "medium", "high"])
def test_plinko_ev_binomial(risk):
    table = casino_games.PLINKO_TABLES[risk]
    ev = sum(
        Decimal(table[k]) * math.comb(casino_games.PLINKO_ROWS, k)
        for k in range(casino_games.PLINKO_ROWS + 1)
    ) / Decimal(2**casino_games.PLINKO_ROWS)
    assert Decimal("0.95") <= ev <= Decimal("1.01")


def test_play_plinko_seeded_path_consistent():
    rng = random.Random(42)
    play = casino_games.play_plinko("medium", rng)
    path = play.detail["path"]
    assert len(path) == casino_games.PLINKO_ROWS
    assert set(path) <= {"L", "R"}
    assert play.detail["bucket"] == path.count("R")
    assert play.multiplier == Decimal(
        casino_games.PLINKO_TABLES["medium"][play.detail["bucket"]]
    )


def test_play_plinko_unknown_risk():
    with pytest.raises(casino_games.GameError):
        casino_games.play_plinko("extreme", random.Random(1))


# --- engine: rocket -----------------------------------------------------------


def test_rocket_seeded_win_rate_near_theoretical():
    rng = random.Random(1234)
    target = Decimal("2.00")
    plays = [casino_games.play_rocket(target, rng) for _ in range(20000)]
    win_rate = sum(1 for p in plays if p.multiplier > 0) / len(plays)
    assert abs(win_rate - 0.495) < 0.02
    assert {p.multiplier for p in plays} <= {Decimal("0"), target}
    for play in plays[:200]:
        won = Decimal(play.detail["crash"]) >= target
        assert (play.multiplier == target) is won


@pytest.mark.parametrize("target", [None, Decimal("0.5"), Decimal("101")])
def test_rocket_invalid_target(target):
    with pytest.raises(casino_games.GameError):
        casino_games.play_rocket(target, random.Random(1))


# --- engine: sling --------------------------------------------------------------


class _FixedRandom:
    def __init__(self, values):
        self._values = iter(values)

    def random(self):
        return next(self._values)


def test_sling_multiplier_ladder():
    expected = ["1.31", "1.74", "2.32", "3.10", "4.13", "5.51", "7.34", "9.79", "13.05", "17.40"]
    for rung, value in enumerate(expected, start=1):
        assert casino_games.sling_multiplier(rung) == Decimal(value)
        assert casino_games.sling_multiplier(rung) == (
            Decimal("0.98") / Decimal("0.75") ** rung
        ).quantize(Decimal("0.01"), ROUND_HALF_UP)


def test_play_sling_survives_and_falls():
    survived = casino_games.play_sling(3, _FixedRandom([0.1, 0.2, 0.3]))
    assert survived.multiplier == Decimal("2.32")
    assert survived.detail["fell_at"] is None

    fell = casino_games.play_sling(3, _FixedRandom([0.1, 0.8]))
    assert fell.multiplier == Decimal("0")
    assert fell.detail["fell_at"] == 2


@pytest.mark.parametrize("rung", [None, 0, 11])
def test_sling_invalid_rung(rung):
    with pytest.raises(casino_games.GameError):
        casino_games.play_sling(rung, random.Random(1))


# --- engine: baccarat -------------------------------------------------------------


class _StackedShoe:
    """rng stub whose shuffle() plants ``deal_order`` at the END of the shoe,
    so pops deal exactly P1, P2, B1, B2, then any third cards in order."""

    def __init__(self, deal_order):
        self._deal = list(deal_order)

    def shuffle(self, shoe):
        rest = list(shoe)
        for card in self._deal:
            rest.remove(card)
        shoe[:] = rest + list(reversed(self._deal))


def test_baccarat_both_naturals_stand():
    play = casino_games.play_baccarat(
        "player", _StackedShoe(["9S", "KS", "8H", "QH"])
    )
    assert len(play.detail["player"]) == 2
    assert len(play.detail["banker"]) == 2
    assert play.detail["player_total"] == 9
    assert play.detail["banker_total"] == 8
    assert play.detail["outcome"] == "player"
    assert play.multiplier == Decimal("2")


def test_baccarat_player_draws_on_five_or_less():
    play = casino_games.play_baccarat(
        "player", _StackedShoe(["2S", "3S", "KH", "QH", "4S", "2H"])
    )
    assert play.detail["player"] == ["2S", "3S", "4S"]
    assert play.detail["banker"] == ["KH", "QH", "2H"]
    assert play.detail["outcome"] == "player"


def test_baccarat_banker_three_stands_on_player_third_eight():
    play = casino_games.play_baccarat(
        "banker", _StackedShoe(["2S", "2H", "3H", "KD", "8S"])
    )
    assert len(play.detail["player"]) == 3
    assert len(play.detail["banker"]) == 2
    assert play.detail["banker_total"] == 3
    assert play.detail["outcome"] == "banker"
    assert play.multiplier == Decimal("1.95")


def test_baccarat_banker_six_draws_on_player_third_seven():
    play = casino_games.play_baccarat(
        "banker", _StackedShoe(["2S", "3S", "6H", "QD", "7S", "2D"])
    )
    assert play.detail["player"] == ["2S", "3S", "7S"]
    assert play.detail["banker"] == ["6H", "QD", "2D"]


def test_baccarat_banker_six_stands_on_player_third_five():
    play = casino_games.play_baccarat(
        "banker", _StackedShoe(["2S", "3S", "6H", "QD", "5S"])
    )
    assert play.detail["player"] == ["2S", "3S", "5S"]
    assert play.detail["banker"] == ["6H", "QD"]
    assert play.detail["outcome"] == "banker"


def test_baccarat_tie_pushes_non_tie_bets():
    deal = ["9S", "KS", "9H", "QH"]
    for bet in ("player", "banker"):
        play = casino_games.play_baccarat(bet, _StackedShoe(deal))
        assert play.detail["outcome"] == "tie"
        assert play.multiplier == Decimal("1")
    tie = casino_games.play_baccarat("tie", _StackedShoe(deal))
    assert tie.multiplier == Decimal("9")


def test_baccarat_unknown_bet():
    with pytest.raises(casino_games.GameError):
        casino_games.play_baccarat("dragon", random.Random(1))


# --- engine: blackjack ----------------------------------------------------------


def _bj_state(player, dealer, deck=(), doubled=False):
    return {
        "deck": list(deck),
        "player": list(player),
        "dealer": list(dealer),
        "doubled": doubled,
    }


def test_blackjack_initial_settle_cases():
    push = _bj_state(["AS", "KS"], ["AH", "QH"])
    assert casino_games.blackjack_initial_settle(push) == Decimal("1")
    player_bj = _bj_state(["AS", "KS"], ["9H", "5H"])
    assert casino_games.blackjack_initial_settle(player_bj) == Decimal("2.5")
    dealer_bj = _bj_state(["9S", "5S"], ["AH", "KH"])
    assert casino_games.blackjack_initial_settle(dealer_bj) == Decimal("0")
    live = _bj_state(["9S", "5S"], ["9H", "5H"])
    assert casino_games.blackjack_initial_settle(live) is None


def test_blackjack_hit_bust_returns_zero():
    state = _bj_state(["KS", "QS"], ["9H", "5H"], deck=["5H"])
    assert casino_games.blackjack_act(state, "hit") == Decimal("0")
    assert state["player"] == ["KS", "QS", "5H"]


def test_blackjack_hit_under_21_stays_open():
    state = _bj_state(["2S", "3S"], ["9H", "5H"], deck=["5H"])
    assert casino_games.blackjack_act(state, "hit") is None


def test_blackjack_stand_dealer_draws_to_17():
    state = _bj_state(["KS", "9S"], ["2H", "3H"], deck=["5D", "7D", "KD"])
    assert casino_games.blackjack_act(state, "stand") == Decimal("2")
    assert state["dealer"] == ["2H", "3H", "5D", "7D"]
    assert casino_games.hand_total(state["dealer"])[0] >= 17


def test_blackjack_double_on_three_cards_rejected():
    state = _bj_state(["2S", "3S", "4S"], ["9H", "5H"], deck=["5H"])
    with pytest.raises(casino_games.GameError):
        casino_games.blackjack_act(state, "double")


def test_blackjack_dealer_bust_pays_two():
    state = _bj_state(["5H", "6H"], ["KS", "6S"], deck=["KH"])
    assert casino_games.blackjack_act(state, "stand") == Decimal("2")
    assert casino_games.hand_total(state["dealer"])[0] > 21


# --- dispatcher --------------------------------------------------------------


def test_dispatcher_routes_and_rejects_unknown():
    play = casino_games.play(
        "slots", risk="medium", bet="player", target=None, rung=None,
        rng=random.Random(3),
    )
    assert isinstance(play, casino_games.GamePlay)
    with pytest.raises(casino_games.GameError):
        casino_games.play(
            "roulette", risk="medium", bet="player", target=None, rung=None,
            rng=random.Random(3),
        )


# --- endpoints ----------------------------------------------------------------


def _user_auth(
    *,
    org_id: str,
    user_id: str | None,
    role: UserRole = UserRole.MEMBER,
    method: AuthMethod = AuthMethod.CLERK_JWT,
) -> AuthContext:
    return AuthContext(method=method, org_id=org_id, user_id=user_id, user_role=role)


@pytest_asyncio.fixture
async def org_with_player():
    org_id = f"org_csn_{uuid.uuid4().hex[:8]}"
    suffix = uuid.uuid4().hex[:8]
    player = UserModel(
        id=f"user_casino_{suffix}",
        org_id=org_id,
        email=f"casino_{suffix}@example.com",
        github_username="casino",
        clerk_user_id=f"clerk_{suffix}",
        role="member",
        is_active=True,
    )
    async with get_session() as session:
        session.add(OrganizationModel(id=org_id, name=org_id, slug=org_id))
        await session.flush()
        session.add(player)
    try:
        yield org_id, player
    finally:
        async with get_session() as session:
            await session.execute(
                QuotaGambleSessionModel.__table__.delete().where(
                    QuotaGambleSessionModel.org_id == org_id
                )
            )
            await session.execute(
                QuotaGambleModel.__table__.delete().where(
                    QuotaGambleModel.org_id == org_id
                )
            )
            await session.execute(
                QuotaModel.__table__.delete().where(QuotaModel.org_id == org_id)
            )
            await session.execute(
                UserModel.__table__.delete().where(UserModel.id == player.id)
            )
            await session.execute(
                OrganizationModel.__table__.delete().where(
                    OrganizationModel.id == org_id
                )
            )


def _member_client(app, org_id, user):
    app.dependency_overrides[require_auth] = lambda: _user_auth(
        org_id=org_id, user_id=user.id
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _set_override(org_id: str, user_id: str, limit: str) -> None:
    async with get_session() as session:
        session.add(
            QuotaModel(org_id=org_id, user_id=user_id, limit_usd=Decimal(limit))
        )


def _stacked_deal(player, dealer, deck):
    def deal(rng=None):
        return {
            "deck": list(deck),
            "player": list(player),
            "dealer": list(dealer),
            "doubled": False,
        }

    return deal


@requires_db
@pytest.mark.asyncio
async def test_plinko_endpoint_persists_game_and_detail(org_with_player):
    org_id, player = org_with_player
    await _set_override(org_id, player.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, player) as client:
            response = await client.post(
                "/quotas/gamble",
                json={"wager_usd": "1", "game": "plinko", "risk": "medium"},
            )
            assert response.status_code == 200
            body = response.json()
            assert body["game"] == "plinko"
            assert len(body["detail"]["path"]) == 12
            assert body["multiplier"] == pytest.approx(
                float(
                    Decimal(
                        casino_games.PLINKO_TABLES["medium"][body["detail"]["bucket"]]
                    )
                )
            )
            history = await client.get("/quotas/gambles")
    finally:
        app.dependency_overrides.clear()
    async with get_session() as session:
        rows = (
            (
                await session.execute(
                    select(QuotaGambleModel).where(QuotaGambleModel.org_id == org_id)
                )
            )
            .scalars()
            .all()
        )
    assert [row.game for row in rows] == ["plinko"]
    assert len(rows[0].detail["path"]) == 12
    item = history.json()["items"][0]
    assert item["game"] == "plinko"
    assert item["multiplier"] is not None
    assert len(item["detail"]["path"]) == 12
    assert item["detail"]["bucket"] == item["detail"]["path"].count("R")


@requires_db
@pytest.mark.asyncio
async def test_rocket_endpoint_missing_target_400(org_with_player):
    org_id, player = org_with_player
    await _set_override(org_id, player.id, "50")
    app = create_app()
    try:
        async with _member_client(app, org_id, player) as client:
            response = await client.post(
                "/quotas/gamble", json={"wager_usd": "1", "game": "rocket"}
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert "Rocket target" in response.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_blackjack_full_flow_escrow_and_settle(monkeypatch, org_with_player):
    org_id, player = org_with_player
    await _set_override(org_id, player.id, "50")
    monkeypatch.setattr(
        casino_games,
        "blackjack_deal",
        _stacked_deal(["10S", "9S"], ["10H", "6H"], ["KD", "2C", "3C"]),
    )
    app = create_app()
    try:
        async with _member_client(app, org_id, player) as client:
            deal = await client.post(
                "/quotas/gamble/blackjack",
                json={"action": "deal", "wager_usd": "10"},
            )
            assert deal.status_code == 200
            deal_body = deal.json()
            assert deal_body["status"] == "player_turn"
            assert deal_body["dealer"] == ["10H", "??"]
            assert deal_body["player"] == ["10S", "9S"]
            assert deal_body["can_double"] is True
            assert deal_body["limit_usd"] == pytest.approx(40.0)

            me_during = await client.get("/quotas/me")
            assert me_during.json()["limit_usd"] == pytest.approx(40.0)

            stand = await client.post(
                "/quotas/gamble/blackjack",
                json={"action": "stand", "session_id": deal_body["session_id"]},
            )
            assert stand.status_code == 200
            stand_body = stand.json()
            assert stand_body["status"] == "settled"
            assert stand_body["outcome"] in {"win", "lose", "push", "blackjack"}
            assert stand_body["outcome"] == "win"
            assert stand_body["multiplier"] == pytest.approx(2.0)
            assert stand_body["net_usd"] == pytest.approx(10.0)
            assert stand_body["limit_usd"] == pytest.approx(
                50.0 + stand_body["net_usd"]
            )

            me_after = await client.get("/quotas/me")
            assert me_after.json()["limit_usd"] == pytest.approx(60.0)

            stale = await client.post(
                "/quotas/gamble/blackjack",
                json={"action": "hit", "session_id": deal_body["session_id"]},
            )
            assert stale.status_code == 404
    finally:
        app.dependency_overrides.clear()


@requires_db
@pytest.mark.asyncio
async def test_blackjack_double_rejected_without_headroom(
    monkeypatch, org_with_player
):
    org_id, player = org_with_player
    await _set_override(org_id, player.id, "11")
    monkeypatch.setattr(
        casino_games,
        "blackjack_deal",
        _stacked_deal(["5S", "6S"], ["9H", "5H"], ["2C", "3C", "KD"]),
    )
    app = create_app()
    try:
        async with _member_client(app, org_id, player) as client:
            deal = await client.post(
                "/quotas/gamble/blackjack",
                json={"action": "deal", "wager_usd": "10"},
            )
            assert deal.status_code == 200
            deal_body = deal.json()
            assert deal_body["can_double"] is False

            double = await client.post(
                "/quotas/gamble/blackjack",
                json={"action": "double", "session_id": deal_body["session_id"]},
            )
    finally:
        app.dependency_overrides.clear()
    assert double.status_code == 400
    assert "double" in double.json()["detail"]


@requires_db
@pytest.mark.asyncio
async def test_blackjack_deal_without_wager_400(org_with_player):
    org_id, player = org_with_player
    app = create_app()
    try:
        async with _member_client(app, org_id, player) as client:
            response = await client.post(
                "/quotas/gamble/blackjack", json={"action": "deal"}
            )
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 400
    assert "wager_usd" in response.json()["detail"]
