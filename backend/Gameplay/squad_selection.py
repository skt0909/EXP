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

GET /squad is read-only, backing the Transfers page's "current squad"
list (transfers.py has no way to look this up itself -- it only reads
squad state to validate a submitted batch). Unlike the POST response's
raw-tenths ints (matching squad_players/user_squads' native scale),
this returns decimal £m for price/budget_remaining -- matching
players.py's convention instead, since GET /squad and GET /players are
consumed together by the same page and should share units.
"""

import logging
from collections import Counter

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
from Shared.rules import (
    BUDGET_CAP,
    MAX_PER_CLUB,
    POSITION_REQUIREMENTS,
    SQUAD_SIZE,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


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

CURRENT_SQUAD_QUERY = text(
    """
    SELECT sp.player_id AS fpl_id, sp.purchase_price, mp.web_name, mp.position,
           t.short_name AS club, COALESCE(pgs.total_points, 0) AS points,
           fx.first_kickoff
    FROM squad_players sp
    JOIN user_squads us ON us.id = sp.user_squad_id
    JOIN ml.players mp ON mp.fpl_id = sp.player_id AND mp.season = us.season
    JOIN ml.teams t ON t.id = mp.team_id
    LEFT JOIN ml.player_gw_stats pgs
        ON pgs.player_id = mp.id
       AND pgs.season = us.season
       AND pgs.gameweek = :gameweek
    -- The player's earliest kickoff this gameweek (NULL on a blank
    -- gameweek). Exposed so the client can auto-arrange the XI/bench by
    -- kickoff order -- Gameplay/selection_rules.py's Tactical Swap timing
    -- rule (incoming's first kickoff strictly after outgoing's last fixture
    -- ends) is otherwise easy to violate by picking an arbitrary pair.
    LEFT JOIN LATERAL (
        SELECT MIN(f.kickoff_time) AS first_kickoff
        FROM ml.fixtures f
        WHERE f.season = us.season AND f.gameweek = :gameweek
          AND (f.home_team_id = mp.team_id OR f.away_team_id = mp.team_id)
    ) fx ON TRUE
    WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE
    ORDER BY mp.position, mp.web_name
    """
)

USER_SQUAD_BUDGET_QUERY = text(
    "SELECT budget_remaining FROM user_squads WHERE user_id = :user_id AND season = :season"
)


class SquadSelectionRequest(BaseModel):
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


class CurrentSquadPlayerOut(BaseModel):
    player_id: int
    name: str
    position: str
    club: str
    price: float
    points: int = 0
    # This player's earliest kickoff in the requested gameweek, or null on a
    # blank gameweek or when `gameweek` wasn't passed. ISO 8601 with the
    # database's own timezone -- never re-derived client-side.
    first_kickoff: datetime | None = None


class CurrentSquadResponse(BaseModel):
    user_id: int
    season: str
    budget_remaining: float
    players: list[CurrentSquadPlayerOut]


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


@router.get("/squad", response_model=CurrentSquadResponse)
def get_current_squad(
    season: str,
    gameweek: int | None = None,
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentSquadResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        budget_row = conn.execute(USER_SQUAD_BUDGET_QUERY, {"user_id": user_id, "season": season}).first()
        rows = conn.execute(
            CURRENT_SQUAD_QUERY,
            {"user_id": user_id, "season": season, "gameweek": gameweek},
        ).all()

    return CurrentSquadResponse(
        user_id=user_id,
        season=season,
        budget_remaining=(budget_row.budget_remaining / 10) if budget_row is not None else 0.0,
        players=[
            CurrentSquadPlayerOut(
                player_id=row.fpl_id,
                name=row.web_name,
                position=row.position,
                club=row.club,
                price=row.purchase_price / 10,
                points=row.points,
                first_kickoff=row.first_kickoff,
            )
            for row in rows
        ],
    )


@router.post("/squad/select", response_model=SquadSelectionResponse)
def select_squad(
    req: SquadSelectionRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> SquadSelectionResponse:
    user_id = current_user.id
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
                {"user_id": user_id, "season": req.season, "budget_remaining": budget_remaining},
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
        user_id=user_id,
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
