"""
transfers.py — FastAPI endpoint for submitting a batch of transfers
(player swaps) for a user's squad in a given gameweek.

transfers is APPEND-ONLY (enforce_transfers_immutability_fn blocks any
UPDATE/DELETE) -- unlike squad_selection.py/starting_xi.py, there is no
upsert/delete-reinsert resubmission pattern here. Every valid call
permanently records new transfer rows; nothing here can undo a prior
submission. Submitting the exact same transfer twice is not deduplicated
-- that's a deliberate choice (transfers are real historical events;
idempotency protection, if ever needed, belongs on the client/API-key
layer, not here).

Sell price is always purchase_price (no profit/loss tracking, a
deliberate simplification) -- selling a player credits back exactly what
was paid for them, no more, no less. That value becomes both the
outgoing squad_players row's sell_price and the transfer row's
price_out.

Free transfers are flat 1/gameweek and do not bank: the first free slot
still available this gameweek (1 minus transfers already recorded with
is_free = TRUE for this user/season/gameweek) goes to the first
transfer(s) in submission order; the rest are paid (is_free = FALSE, no
cap on how many). If gw_selections.chip_used for this gameweek is
'wildcard' or 'free_hit', every transfer in the batch is free instead,
uncapped.

transfers has no FK/trigger tie to gw_selections, so unlike
starting_xi.py (which lets enforce_selection_lock_fn's trigger catch a
locked gameweek), this module explicitly pre-checks
gw_selections.is_locked for (user_id, season, gameweek) before writing.
No gw_selections row for that gameweek at all means unlocked (deadline
hasn't passed / selection was never submitted).

A transfer must swap like-for-like position (player_in.position ==
player_out.position) to keep the squad's 2 GK/5 DEF/5 MID/3 FWD shape
valid without re-running full squad validation, and the max-3-per-club
cap (shared from squad_selection.py) is re-checked against the *entire*
resulting squad, not just the transferred players, since one swap can
tip an unrelated club over the cap via cumulative batch effects.
"""

import logging
from collections import Counter

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Game_logic.db_utils import get_engine
from Game_logic.squad_selection import MAX_PER_CLUB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

FREE_TRANSFERS_PER_GAMEWEEK = 1
FREE_CHIPS = {"wildcard", "free_hit"}  # active this gameweek -> every transfer is free, uncapped

router = APIRouter()

USER_SQUAD_QUERY = text(
    "SELECT id AS user_squad_id, budget_remaining, total_transfers FROM user_squads "
    "WHERE user_id = :user_id AND season = :season"
)

ACTIVE_SQUAD_QUERY = text(
    """
    SELECT sp.id AS squad_player_id, sp.player_id, sp.purchase_price
    FROM squad_players sp
    JOIN user_squads us ON us.id = sp.user_squad_id
    WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE
    """
)

PLAYERS_LOOKUP_QUERY = text(
    "SELECT fpl_id, position, team_id, cost_start FROM ml.players "
    "WHERE season = :season AND fpl_id = ANY(:player_ids)"
)

GW_SELECTION_QUERY = text(
    "SELECT is_locked, chip_used FROM gw_selections "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek"
)

FREE_TRANSFERS_USED_QUERY = text(
    "SELECT COUNT(*) FROM transfers "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek AND is_free = TRUE"
)

DEACTIVATE_SQUAD_PLAYER_STMT = text(
    "UPDATE squad_players SET is_active = FALSE, sell_price = :sell_price WHERE id = :squad_player_id"
)

INSERT_SQUAD_PLAYER_STMT = text(
    """
    INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active)
    VALUES (:user_squad_id, :player_id, :purchase_price, NULL, TRUE)
    """
)

INSERT_TRANSFER_STMT = text(
    """
    INSERT INTO transfers (user_id, season, gameweek, player_in_id, player_out_id, price_in, price_out, is_free, transferred_at)
    VALUES (:user_id, :season, :gameweek, :player_in_id, :player_out_id, :price_in, :price_out, :is_free, now())
    """
)

UPDATE_USER_SQUAD_STMT = text(
    "UPDATE user_squads SET budget_remaining = :budget_remaining, total_transfers = :total_transfers, "
    "updated_at = now() WHERE id = :user_squad_id"
)


class TransferPair(BaseModel):
    player_out_id: int
    player_in_id: int


class TransfersRequest(BaseModel):
    user_id: int
    season: str
    gameweek: int
    transfers: list[TransferPair]  # at least 1


class TransferOut(BaseModel):
    player_out_id: int
    player_in_id: int
    price_out: int
    price_in: int
    is_free: bool


class TransfersResponse(BaseModel):
    user_id: int
    season: str
    gameweek: int
    budget_remaining: int
    total_transfers: int
    transfers: list[TransferOut]


def _validate_transfers(
    req: TransfersRequest,
    user_squad_row,
    active_squad: dict[int, object],
    players: dict[int, object],
    gw_selection_row,
    free_used_count: int,
) -> tuple[list[str], int]:
    """Collect every validation failure instead of stopping at the first.

    Returns (errors, budget_remaining_after) -- the latter is only
    meaningful when errors is empty.
    """
    errors: list[str] = []

    if user_squad_row is None:
        errors.append(f"no squad found for user_id {req.user_id}, season {req.season} -- submit a squad first")

    if not req.transfers:
        errors.append("at least one transfer is required")

    if gw_selection_row is not None and gw_selection_row.is_locked:
        errors.append(f"gameweek {req.gameweek} is locked and can no longer be modified")

    out_ids = [t.player_out_id for t in req.transfers]
    in_ids = [t.player_in_id for t in req.transfers]

    same_player_pairs = [t for t in req.transfers if t.player_out_id == t.player_in_id]
    if same_player_pairs:
        errors.append(f"player_out_id and player_in_id must differ: {[t.player_out_id for t in same_player_pairs]}")

    seen_out: set[int] = set()
    dup_out = sorted({pid for pid in out_ids if pid in seen_out or seen_out.add(pid)})
    if dup_out:
        errors.append(f"duplicate player_out_id(s) in the same batch: {dup_out}")

    seen_in: set[int] = set()
    dup_in = sorted({pid for pid in in_ids if pid in seen_in or seen_in.add(pid)})
    if dup_in:
        errors.append(f"duplicate player_in_id(s) in the same batch: {dup_in}")

    overlap = sorted(set(out_ids) & set(in_ids))
    if overlap:
        errors.append(f"player_id(s) cannot appear as both an in and an out in the same batch: {overlap}")

    not_owned = sorted(pid for pid in set(out_ids) if pid not in active_squad)
    if not_owned:
        errors.append(f"player_out_id(s) not in user's active squad for season {req.season}: {not_owned}")

    already_owned = sorted(pid for pid in set(in_ids) if pid in active_squad)
    if already_owned:
        errors.append(f"player_in_id(s) already in user's active squad: {already_owned}")

    unresolved_in = sorted(pid for pid in set(in_ids) if pid not in players)
    if unresolved_in:
        errors.append(f"player_in_id(s) not found in ml.players for season {req.season}: {unresolved_in}")

    # Position match, only checkable for pairs where both sides resolved
    # against ml.players (lookup_ids always includes every out_id/in_id
    # regardless of ownership validity, so this doesn't depend on the
    # not_owned/already_owned checks above having passed).
    mismatched = []
    for t in req.transfers:
        out_player = players.get(t.player_out_id)
        in_player = players.get(t.player_in_id)
        if out_player is None or in_player is None:
            continue
        if out_player.position != in_player.position:
            mismatched.append(
                f"{t.player_out_id} ({out_player.position}) -> {t.player_in_id} ({in_player.position})"
            )
    if mismatched:
        errors.append(f"transfer(s) must swap the same position: {mismatched}")

    # Whole-squad club-cap re-check: start from the current active squad's
    # club counts, then simulate every out/in in the batch, regardless of
    # whether any single pair looks fine in isolation.
    club_counts = Counter(
        players[pid].team_id for pid in active_squad if pid in players
    )
    for t in req.transfers:
        out_info = players.get(t.player_out_id)
        in_info = players.get(t.player_in_id)
        if out_info is not None:
            club_counts[out_info.team_id] -= 1
        if in_info is not None:
            club_counts[in_info.team_id] += 1
    over_cap = {team_id: n for team_id, n in club_counts.items() if n > MAX_PER_CLUB}
    if over_cap:
        errors.append(f"max {MAX_PER_CLUB} players per club exceeded after transfer(s) for team_id(s): {over_cap}")

    # Always computed, regardless of other errors already found -- gating
    # this behind "no errors yet" would silently skip the budget check
    # whenever any other validation failure exists, contradicting
    # "collect every error, not just the first." Safe to compute
    # unconditionally: both sums only include pids that actually resolved.
    budget_remaining_after = user_squad_row.budget_remaining if user_squad_row is not None else None
    if user_squad_row is not None:
        total_price_out = sum(active_squad[pid].purchase_price for pid in out_ids if pid in active_squad)
        total_price_in = sum(players[pid].cost_start for pid in in_ids if pid in players)
        budget_remaining_after = user_squad_row.budget_remaining + total_price_out - total_price_in
        if budget_remaining_after < 0:
            errors.append(
                f"insufficient budget: transfer(s) would leave budget_remaining at {budget_remaining_after}"
            )

    return errors, budget_remaining_after


@router.post("/transfers", response_model=TransfersResponse)
def submit_transfers(req: TransfersRequest) -> TransfersResponse:
    engine = get_engine()

    out_ids = [t.player_out_id for t in req.transfers]
    in_ids = [t.player_in_id for t in req.transfers]
    lookup_ids = list(dict.fromkeys(out_ids + in_ids))

    with engine.connect() as conn:
        user_squad_row = conn.execute(
            USER_SQUAD_QUERY, {"user_id": req.user_id, "season": req.season}
        ).first()

        active_squad = {
            row.player_id: row
            for row in conn.execute(ACTIVE_SQUAD_QUERY, {"user_id": req.user_id, "season": req.season})
        }

        all_relevant_ids = list(dict.fromkeys(list(active_squad.keys()) + lookup_ids))
        players = {
            row.fpl_id: row
            for row in conn.execute(PLAYERS_LOOKUP_QUERY, {"season": req.season, "player_ids": all_relevant_ids})
        }

        gw_selection_row = conn.execute(
            GW_SELECTION_QUERY, {"user_id": req.user_id, "season": req.season, "gameweek": req.gameweek}
        ).first()

        free_used_count = conn.execute(
            FREE_TRANSFERS_USED_QUERY, {"user_id": req.user_id, "season": req.season, "gameweek": req.gameweek}
        ).scalar()

    errors, budget_remaining_after = _validate_transfers(
        req, user_squad_row, active_squad, players, gw_selection_row, free_used_count
    )
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    chip_active = gw_selection_row is not None and gw_selection_row.chip_used in FREE_CHIPS
    free_slots_left = 0 if chip_active else max(0, FREE_TRANSFERS_PER_GAMEWEEK - free_used_count)

    transfer_plan = []
    for i, t in enumerate(req.transfers):
        out_row = active_squad[t.player_out_id]
        in_row = players[t.player_in_id]
        is_free = chip_active or i < free_slots_left
        transfer_plan.append(
            {
                "player_out_id": t.player_out_id,
                "player_in_id": t.player_in_id,
                "squad_player_id": out_row.squad_player_id,
                "price_out": out_row.purchase_price,
                "price_in": in_row.cost_start,
                "is_free": is_free,
            }
        )

    total_transfers_after = user_squad_row.total_transfers + len(req.transfers)

    try:
        with engine.begin() as conn:
            for plan in transfer_plan:
                conn.execute(
                    DEACTIVATE_SQUAD_PLAYER_STMT,
                    {"squad_player_id": plan["squad_player_id"], "sell_price": plan["price_out"]},
                )
                conn.execute(
                    INSERT_SQUAD_PLAYER_STMT,
                    {
                        "user_squad_id": user_squad_row.user_squad_id,
                        "player_id": plan["player_in_id"],
                        "purchase_price": plan["price_in"],
                    },
                )
                conn.execute(
                    INSERT_TRANSFER_STMT,
                    {
                        "user_id": req.user_id,
                        "season": req.season,
                        "gameweek": req.gameweek,
                        "player_in_id": plan["player_in_id"],
                        "player_out_id": plan["player_out_id"],
                        "price_in": plan["price_in"],
                        "price_out": plan["price_out"],
                        "is_free": plan["is_free"],
                    },
                )

            conn.execute(
                UPDATE_USER_SQUAD_STMT,
                {
                    "user_squad_id": user_squad_row.user_squad_id,
                    "budget_remaining": budget_remaining_after,
                    "total_transfers": total_transfers_after,
                },
            )
    except SQLAlchemyError as e:
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return TransfersResponse(
        user_id=req.user_id,
        season=req.season,
        gameweek=req.gameweek,
        budget_remaining=budget_remaining_after,
        total_transfers=total_transfers_after,
        transfers=[
            TransferOut(
                player_out_id=plan["player_out_id"],
                player_in_id=plan["player_in_id"],
                price_out=plan["price_out"],
                price_in=plan["price_in"],
                is_free=plan["is_free"],
            )
            for plan in transfer_plan
        ],
    )
