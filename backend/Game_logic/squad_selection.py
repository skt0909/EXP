"""
squad_selection.py — FastAPI endpoint for submitting/replacing a user's
15-player squad for a season.

user_squads / squad_players have no formation, budget, or club-limit
constraints at the DB level (see the DDL) -- those rules are enforced
here in application code. squad_players.player_id is the RAW FPL id
(matches starting_xi's convention); ml.players is only consulted to
validate picks and price them via cost_start, never to translate ids
before insert.


One squad per (user_id, season), enforced by user_squads' unique
constraint. Resubmission always overwrites -- no lock, no separate
update endpoint -- via an atomic INSERT ... ON CONFLICT upsert, so two
concurrent submissions for the same user/season can't race past a
pre-check.
"""

import logging
from collections import Counter

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Game_logic.db_utils import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

SQUAD_SIZE = 15
POSITION_REQUIREMENTS = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
BUDGET_CAP = 1000  # tenths of £m, e.g. 1000 = £100.0m -- matches ml.players.cost_start's scale
MAX_PER_CLUB = 3

router = APIRouter()

PLAYERS_LOOKUP_QUERY = text(
    """
    SELECT fpl_id, web_name, position, team_id, cost_start
    FROM ml.players
    WHERE season = :season AND fpl_id = ANY(:player_ids)
    """
)

UPSERT_USER_SQUAD_STMT = text(
    """
    INSERT INTO user_squads (user_id, season, budget_remaining, updated_at)
    VALUES (:user_id, :season, :budget_remaining, now())
    ON CONFLICT (user_id, season) DO UPDATE SET
        budget_remaining = EXCLUDED.budget_remaining,
        updated_at = now()
    RETURNING id
    """
)

DELETE_SQUAD_PLAYERS_STMT = text("DELETE FROM squad_players WHERE user_squad_id = :user_squad_id")

INSERT_SQUAD_PLAYER_STMT = text(
    """
    INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active)
    VALUES (:user_squad_id, :player_id, :purchase_price, NULL, TRUE)
    """
)


class SquadSelectionRequest(BaseModel):
    user_id: int
    season: str
    player_ids: list[int]  # exactly 15 raw FPL ids


class SquadPlayerOut(BaseModel):
    player_id: int
    web_name: str
    position: str
    team_id: int
    purchase_price: int


class SquadSelectionResponse(BaseModel):
    squad_id: int
    user_id: int
    season: str
    budget_remaining: int
    players: list[SquadPlayerOut]


def _validate_squad(req: SquadSelectionRequest, found: dict) -> tuple[list, list[str]]:
    """Collect every validation failure instead of stopping at the first.

    Returns (resolved_players, errors). resolved_players is the de-duped,
    found subset of req.player_ids -- used both for error-message context
    and, when errors is empty, as the exact roster to persist.
    """
    errors: list[str] = []

    if len(req.player_ids) != SQUAD_SIZE:
        errors.append(f"squad must contain exactly {SQUAD_SIZE} players, got {len(req.player_ids)}")

    seen: set[int] = set()
    duplicates = sorted({pid for pid in req.player_ids if pid in seen or seen.add(pid)})
    if duplicates:
        errors.append(f"duplicate player_id(s): {duplicates}")

    unique_ids = list(dict.fromkeys(req.player_ids))
    missing = sorted(pid for pid in unique_ids if pid not in found)
    if missing:
        errors.append(f"player_id(s) not found in ml.players for season {req.season}: {missing}")

    resolved_players = [found[pid] for pid in unique_ids if pid in found]

    position_counts = Counter(p.position for p in resolved_players)
    for position, required in POSITION_REQUIREMENTS.items():
        actual = position_counts.get(position, 0)
        if actual != required:
            errors.append(f"expected {required} {position}, got {actual}")

    total_cost = sum(p.cost_start for p in resolved_players)
    if total_cost > BUDGET_CAP:
        errors.append(f"squad cost {total_cost} exceeds budget cap {BUDGET_CAP}")

    club_counts = Counter(p.team_id for p in resolved_players)
    over_cap = {team_id: n for team_id, n in club_counts.items() if n > MAX_PER_CLUB}
    if over_cap:
        errors.append(f"max {MAX_PER_CLUB} players per club exceeded for team_id(s): {over_cap}")

    return resolved_players, errors


@router.post("/squad/select", response_model=SquadSelectionResponse)
def select_squad(req: SquadSelectionRequest) -> SquadSelectionResponse:
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(
            PLAYERS_LOOKUP_QUERY,
            {"season": req.season, "player_ids": list(dict.fromkeys(req.player_ids))},
        ).all()
    found = {row.fpl_id: row for row in rows}

    resolved_players, errors = _validate_squad(req, found)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    total_cost = sum(p.cost_start for p in resolved_players)
    budget_remaining = BUDGET_CAP - total_cost

    try:
        with engine.begin() as conn:
            squad_id = conn.execute(
                UPSERT_USER_SQUAD_STMT,
                {"user_id": req.user_id, "season": req.season, "budget_remaining": budget_remaining},
            ).scalar()

            conn.execute(DELETE_SQUAD_PLAYERS_STMT, {"user_squad_id": squad_id})

            conn.execute(
                INSERT_SQUAD_PLAYER_STMT,
                [
                    {"user_squad_id": squad_id, "player_id": p.fpl_id, "purchase_price": p.cost_start}
                    for p in resolved_players
                ],
            )
    except SQLAlchemyError as e:
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return SquadSelectionResponse(
        squad_id=squad_id,
        user_id=req.user_id,
        season=req.season,
        budget_remaining=budget_remaining,
        players=[
            SquadPlayerOut(
                player_id=p.fpl_id,
                web_name=p.web_name,
                position=p.position,
                team_id=p.team_id,
                purchase_price=p.cost_start,
            )
            for p in resolved_players
        ],
    )
