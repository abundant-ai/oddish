"""Server-authoritative casino game engines.

Every outcome is rolled here with a system RNG and returned as a payout
``multiplier`` (total return vs wager: 0 = lost, 1 = push, 2 = 1:1 win) plus a
``detail`` dict the frontend replays as an animation. Engines are pure given
``rng`` so tests inject determinism. The caller (orgs router) owns wager
validation, the ledger row, and quota math.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal

_rng = secrets.SystemRandom()

CENT = Decimal("0.01")


class GameError(ValueError):
    """Invalid game parameters (rendered as HTTP 400)."""


@dataclass
class GamePlay:
    multiplier: Decimal
    detail: dict


# --- plinko ---------------------------------------------------------------

PLINKO_ROWS = 12
PLINKO_TABLES = {
    "low": ["10", "3", "1.6", "1.4", "1.1", "1", "0.5", "1", "1.1", "1.4", "1.6", "3", "10"],
    "medium": ["33", "11", "4", "2", "1.1", "0.6", "0.3", "0.6", "1.1", "2", "4", "11", "33"],
    "high": ["170", "24", "8.1", "2", "0.7", "0.2", "0.2", "0.2", "0.7", "2", "8.1", "24", "170"],
}


def play_plinko(risk: str, rng=_rng) -> GamePlay:
    table = PLINKO_TABLES.get(risk)
    if table is None:
        raise GameError(f"Unknown plinko risk: {risk}")
    path = "".join(rng.choice("LR") for _ in range(PLINKO_ROWS))
    bucket = path.count("R")
    return GamePlay(
        Decimal(table[bucket]),
        {"risk": risk, "path": path, "bucket": bucket, "buckets": table},
    )


# --- slots ----------------------------------------------------------------

SLOT_STRIP = ["7"] * 1 + ["BAR"] * 2 + ["BELL"] * 3 + ["CHERRY"] * 4 + ["LEMON"] * 6
SLOT_TRIPLES = {
    "7": Decimal("100"),
    "BAR": Decimal("25"),
    "BELL": Decimal("12"),
    "CHERRY": Decimal("8"),
    "LEMON": Decimal("4"),
}


def slot_multiplier(reels: list[str]) -> Decimal:
    counts = {s: reels.count(s) for s in set(reels)}
    if len(counts) == 1:
        return SLOT_TRIPLES[reels[0]]
    if counts.get("7", 0) == 2:
        return Decimal("2")
    if counts.get("CHERRY", 0) == 2:
        return Decimal("1.5")
    if counts.get("BELL", 0) == 2:
        return Decimal("1")
    if counts.get("7", 0) == 1:
        return Decimal("1")
    return Decimal("0")


def play_slots(rng=_rng) -> GamePlay:
    reels = [rng.choice(SLOT_STRIP) for _ in range(3)]
    return GamePlay(slot_multiplier(reels), {"reels": reels})


# --- rocket (crash) --------------------------------------------------------

ROCKET_MIN_TARGET = Decimal("1.01")
ROCKET_MAX_TARGET = Decimal("100")
ROCKET_MAX_CRASH = Decimal("1000")


def play_rocket(target: Decimal | None, rng=_rng) -> GamePlay:
    if target is None or not ROCKET_MIN_TARGET <= target <= ROCKET_MAX_TARGET:
        raise GameError("Rocket target must be between 1.01x and 100x")
    # P(crash >= t) = 0.99/t, so EV = 0.99 for every target.
    u = rng.random()
    raw = Decimal("0.99") / Decimal(str(max(1e-12, 1.0 - u)))
    crash = max(Decimal("1"), min(ROCKET_MAX_CRASH, raw.quantize(CENT, ROUND_HALF_UP)))
    won = crash >= target
    return GamePlay(
        target if won else Decimal("0"),
        {"target": str(target), "crash": str(crash)},
    )


# --- sling (rung ladder) ----------------------------------------------------

SLING_SURVIVAL = Decimal("0.75")
SLING_MAX_RUNG = 10


def sling_multiplier(rung: int) -> Decimal:
    return (Decimal("0.98") / SLING_SURVIVAL**rung).quantize(CENT, ROUND_HALF_UP)


def play_sling(rung: int | None, rng=_rng) -> GamePlay:
    if rung is None or not 1 <= rung <= SLING_MAX_RUNG:
        raise GameError(f"Sling rung must be between 1 and {SLING_MAX_RUNG}")
    fell_at = None
    for step in range(1, rung + 1):
        if Decimal(str(rng.random())) >= SLING_SURVIVAL:
            fell_at = step
            break
    target = sling_multiplier(rung)
    return GamePlay(
        Decimal("0") if fell_at else target,
        {"rung": rung, "fell_at": fell_at, "target_multiplier": str(target)},
    )


# --- baccarat ---------------------------------------------------------------

RANKS = ["A", "2", "3", "4", "5", "6", "7", "8", "9", "10", "J", "Q", "K"]
SUITS = ["S", "H", "D", "C"]
BACCARAT_PAYOUTS = {
    "player": Decimal("2"),
    "banker": Decimal("1.95"),
    "tie": Decimal("9"),
}


def card_baccarat_value(card: str) -> int:
    rank = card[:-1]
    if rank == "A":
        return 1
    if rank in ("10", "J", "Q", "K"):
        return 0
    return int(rank)


def _baccarat_total(cards: list[str]) -> int:
    return sum(card_baccarat_value(c) for c in cards) % 10


def play_baccarat(bet: str, rng=_rng) -> GamePlay:
    if bet not in BACCARAT_PAYOUTS:
        raise GameError(f"Unknown baccarat bet: {bet}")
    shoe = [r + s for r in RANKS for s in SUITS] * 8
    rng.shuffle(shoe)
    player = [shoe.pop(), shoe.pop()]
    banker = [shoe.pop(), shoe.pop()]
    if _baccarat_total(player) < 8 and _baccarat_total(banker) < 8:
        third_value = None
        if _baccarat_total(player) <= 5:
            player.append(shoe.pop())
            third_value = card_baccarat_value(player[2])
        bt = _baccarat_total(banker)
        draws = (
            bt <= 5
            if third_value is None
            else (
                bt <= 2
                or (bt == 3 and third_value != 8)
                or (bt == 4 and 2 <= third_value <= 7)
                or (bt == 5 and 4 <= third_value <= 7)
                or (bt == 6 and 6 <= third_value <= 7)
            )
        )
        if draws:
            banker.append(shoe.pop())
    pt, bt = _baccarat_total(player), _baccarat_total(banker)
    outcome = "tie" if pt == bt else "player" if pt > bt else "banker"
    if bet == outcome:
        multiplier = BACCARAT_PAYOUTS[bet]
    elif outcome == "tie":
        multiplier = Decimal("1")
    else:
        multiplier = Decimal("0")
    return GamePlay(
        multiplier,
        {
            "bet": bet,
            "player": player,
            "banker": banker,
            "player_total": pt,
            "banker_total": bt,
            "outcome": outcome,
        },
    )


# --- blackjack (session-based) ----------------------------------------------

def _deck(rng=_rng) -> list[str]:
    deck = [r + s for r in RANKS for s in SUITS]
    rng.shuffle(deck)
    return deck


def hand_total(cards: list[str]) -> tuple[int, bool]:
    total, aces = 0, 0
    for card in cards:
        rank = card[:-1]
        if rank == "A":
            aces += 1
            total += 11
        elif rank in ("10", "J", "Q", "K"):
            total += 10
        else:
            total += int(rank)
    while total > 21 and aces:
        total -= 10
        aces -= 1
    return total, aces > 0


def blackjack_deal(rng=_rng) -> dict:
    deck = _deck(rng)
    return {
        "deck": deck[4:],
        "player": deck[:2],
        "dealer": deck[2:4],
        "doubled": False,
    }


def _is_blackjack(cards: list[str]) -> bool:
    return len(cards) == 2 and hand_total(cards)[0] == 21


def blackjack_initial_settle(state: dict) -> Decimal | None:
    player_bj, dealer_bj = _is_blackjack(state["player"]), _is_blackjack(state["dealer"])
    if player_bj and dealer_bj:
        return Decimal("1")
    if player_bj:
        return Decimal("2.5")
    if dealer_bj:
        return Decimal("0")
    return None


def _dealer_out(state: dict) -> Decimal:
    while hand_total(state["dealer"])[0] < 17:
        state["dealer"].append(state["deck"].pop(0))
    player_total = hand_total(state["player"])[0]
    dealer_total = hand_total(state["dealer"])[0]
    if dealer_total > 21 or player_total > dealer_total:
        return Decimal("2")
    if player_total == dealer_total:
        return Decimal("1")
    return Decimal("0")


def blackjack_act(state: dict, action: str) -> Decimal | None:
    """Mutates state; returns the settle multiplier (on total riding wager) or
    None while the hand is still the player's turn."""
    if action == "hit":
        state["player"].append(state["deck"].pop(0))
        if hand_total(state["player"])[0] > 21:
            return Decimal("0")
        return None
    if action == "double":
        if len(state["player"]) != 2:
            raise GameError("Double is only allowed on the first two cards")
        state["doubled"] = True
        state["player"].append(state["deck"].pop(0))
        if hand_total(state["player"])[0] > 21:
            return Decimal("0")
        return _dealer_out(state)
    if action == "stand":
        return _dealer_out(state)
    raise GameError(f"Unknown blackjack action: {action}")


# --- dispatcher --------------------------------------------------------------

SINGLE_ROUND_GAMES = ("coinflip", "plinko", "slots", "baccarat", "rocket", "sling")


def play(game: str, *, risk: str, bet: str, target: Decimal | None, rung: int | None, rng=_rng) -> GamePlay:
    if game == "plinko":
        return play_plinko(risk, rng)
    if game == "slots":
        return play_slots(rng)
    if game == "baccarat":
        return play_baccarat(bet, rng)
    if game == "rocket":
        return play_rocket(target, rng)
    if game == "sling":
        return play_sling(rung, rng)
    raise GameError(f"Unknown game: {game}")
