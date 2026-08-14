"""
leagues.py — FastAPI endpoints for creating and joining mini-leagues.

POST /leagues validates league_type/scoring_type against the DDL's CHECK
values before ever hitting the DB, so a bad value surfaces as a normal
422 in the errors list rather than a raw constraint violation -- same
philosophy as every other Game_logic endpoint. The join code is
generated client-side (short uppercase alphanumeric) and retried on the
rare collision against mini_leagues.code's UNIQUE constraint, rather
than pre-checking for existence (avoids a TOCTOU race, same reasoning
as squad_selection.py's upsert-not-precheck choice for
(user_id, season)).

POST /leagues/join resolves a code to a league, then validates
membership/capacity, collecting every failure into one list -- same
pattern as every other Game_logic validation.

Standings computation (compute_league_standings) is NOT here -- it's a
pure function in Game_logic/standings.py, wrapped as a Celery task in
Worker/tasks.py, mirroring how scoring.py/score_gameweek relates to
Worker/tasks.py's compute_gw_scores.
"""

import logging
import random
import string

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from Game_logic.db_utils import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

LEAGUE_TYPES = {"public", "private"}
SCORING_TYPES = {"classic", "head_to_head"}
DEFAULT_MAX_MEMBERS = 50
CODE_LENGTH = 7
MAX_CODE_ATTEMPTS = 5

router = APIRouter()

INSERT_LEAGUE_STMT = text(
    """
    INSERT INTO mini_leagues (name, code, created_by, season, league_type, scoring_type, max_members)
    VALUES (:name, :code, :created_by, :season, :league_type, :scoring_type, :max_members)
    RETURNING id
    """
)

INSERT_MEMBER_STMT = text(
    """
    INSERT INTO league_members (league_id, user_id, season_points, rank, last_gw_points)
    VALUES (:league_id, :user_id, 0, 0, 0)
    """
)

LEAGUE_BY_CODE_QUERY = text("SELECT id AS league_id, name, max_members FROM mini_leagues WHERE code = :code")

IS_MEMBER_QUERY = text("SELECT 1 FROM league_members WHERE league_id = :league_id AND user_id = :user_id")

MEMBER_COUNT_QUERY = text("SELECT COUNT(*) FROM league_members WHERE league_id = :league_id")


def _generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=CODE_LENGTH))


class CreateLeagueRequest(BaseModel):
    user_id: int
    name: str
    season: str
    league_type: str
    scoring_type: str
    max_members: int = DEFAULT_MAX_MEMBERS


class CreateLeagueResponse(BaseModel):
    league_id: int
    code: str
    name: str
    season: str
    league_type: str
    scoring_type: str
    max_members: int


class JoinLeagueRequest(BaseModel):
    user_id: int
    code: str


class JoinLeagueResponse(BaseModel):
    league_id: int
    user_id: int
    name: str


def _validate_create(req: CreateLeagueRequest) -> list[str]:
    errors: list[str] = []
    if req.league_type not in LEAGUE_TYPES:
        errors.append(f"league_type must be one of {sorted(LEAGUE_TYPES)}, got {req.league_type!r}")
    if req.scoring_type not in SCORING_TYPES:
        errors.append(f"scoring_type must be one of {sorted(SCORING_TYPES)}, got {req.scoring_type!r}")
    if req.max_members < 1:
        errors.append(f"max_members must be at least 1, got {req.max_members}")
    return errors


def _validate_join(league_row, is_member: bool, member_count: int) -> list[str]:
    errors: list[str] = []
    if league_row is None:
        errors.append("code does not match any existing league")
        return errors  # nothing else is checkable without a resolved league

    if is_member:
        errors.append("user is already a member of this league")
    if member_count >= league_row.max_members:
        errors.append(f"league is at capacity ({league_row.max_members} members)")

    return errors


@router.post("/leagues", response_model=CreateLeagueResponse)
def create_league(req: CreateLeagueRequest) -> CreateLeagueResponse:
    errors = _validate_create(req)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    engine = get_engine()
    league_id = None
    code = None
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        code = _generate_code()
        try:
            with engine.begin() as conn:
                league_id = conn.execute(
                    INSERT_LEAGUE_STMT,
                    {
                        "name": req.name,
                        "code": code,
                        "created_by": req.user_id,
                        "season": req.season,
                        "league_type": req.league_type,
                        "scoring_type": req.scoring_type,
                        "max_members": req.max_members,
                    },
                ).scalar()
                conn.execute(INSERT_MEMBER_STMT, {"league_id": league_id, "user_id": req.user_id})
            break
        except IntegrityError:
            logger.warning("create_league: code collision on attempt %d/%d (%s), retrying", attempt, MAX_CODE_ATTEMPTS, code)
    else:
        logger.error("create_league: exhausted %d code-generation attempts", MAX_CODE_ATTEMPTS)
        raise HTTPException(status_code=500, detail="Could not generate a unique league code, please try again")

    return CreateLeagueResponse(
        league_id=league_id,
        code=code,
        name=req.name,
        season=req.season,
        league_type=req.league_type,
        scoring_type=req.scoring_type,
        max_members=req.max_members,
    )


@router.post("/leagues/join", response_model=JoinLeagueResponse)
def join_league(req: JoinLeagueRequest) -> JoinLeagueResponse:
    engine = get_engine()

    with engine.connect() as conn:
        league_row = conn.execute(LEAGUE_BY_CODE_QUERY, {"code": req.code}).first()
        is_member = False
        member_count = 0
        if league_row is not None:
            is_member = (
                conn.execute(IS_MEMBER_QUERY, {"league_id": league_row.league_id, "user_id": req.user_id}).first()
                is not None
            )
            member_count = conn.execute(MEMBER_COUNT_QUERY, {"league_id": league_row.league_id}).scalar()

    errors = _validate_join(league_row, is_member, member_count)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    with engine.begin() as conn:
        conn.execute(INSERT_MEMBER_STMT, {"league_id": league_row.league_id, "user_id": req.user_id})

    return JoinLeagueResponse(league_id=league_row.league_id, user_id=req.user_id, name=league_row.name)
