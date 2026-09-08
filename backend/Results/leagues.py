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
pure function in Results/standings.py, wrapped as a Celery task in
Worker/tasks.py, mirroring how scoring.py/score_gameweek relates to
Worker/tasks.py's compute_gw_scores.
"""

import logging
import random
import string
from types import SimpleNamespace

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user

# The H2H match-point values, from the pure rules module -- the same constants
# standings.py used to produce the rows this file serves, so the endpoint and
# the computation behind it cannot disagree about what a win is worth.
from Shared.rules import BYE_POINTS, DRAW_POINTS, LOSS_POINTS, WIN_POINTS

# Shared with GET /team rather than restated. "Is this gameweek finished?" has
# exactly one correct answer and both screens must give it -- a second copy of
# this query is how the two would drift into disagreeing about whether a score
# is settled. Intra-package import (Results -> Results), so it adds no edge to
# the package graph.
from Results.team_dashboard import GW_FIXTURE_STATUS_QUERY, _resolve_live_status

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

USER_LEAGUES_QUERY = text(
    """
    SELECT
        ml.id AS league_id,
        ml.code,
        ml.name,
        ml.season,
        ml.league_type,
        ml.scoring_type,
        ml.max_members,
        COUNT(all_members.user_id) AS member_count,
        lm.rank AS user_rank,
        lm.season_points AS user_season_points,
        lm.last_gw_points AS user_last_gw_points
    FROM mini_leagues ml
    JOIN league_members lm ON lm.league_id = ml.id
    LEFT JOIN league_members all_members ON all_members.league_id = ml.id
    WHERE lm.user_id = :user_id
      AND ml.season = :season
    GROUP BY
        ml.id,
        ml.code,
        ml.name,
        ml.season,
        ml.league_type,
        ml.scoring_type,
        ml.max_members,
        lm.rank,
        lm.season_points,
        lm.last_gw_points
    ORDER BY ml.id DESC
    """
)

LEAGUE_META_QUERY = text(
    """
    SELECT id AS league_id, code, name, season, league_type, scoring_type, max_members
    FROM mini_leagues
    WHERE id = :league_id
    """
)

LEAGUE_TABLE_QUERY = text(
    """
    SELECT
        lm.user_id,
        COALESCE(u.team_name, u.username, CONCAT('User ', lm.user_id)) AS team_name,
        u.username,
        lm.season_points,
        lm.rank,
        lm.last_gw_points
    FROM league_members lm
    LEFT JOIN users u ON u.id = lm.user_id
    WHERE lm.league_id = :league_id
    ORDER BY
        CASE WHEN lm.rank IS NULL OR lm.rank = 0 THEN 1 ELSE 0 END,
        lm.rank ASC,
        lm.season_points DESC,
        lm.last_gw_points DESC,
        lm.user_id ASC
    """
)


def _generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=CODE_LENGTH))


class CreateLeagueRequest(BaseModel):
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
    code: str


class JoinLeagueResponse(BaseModel):
    league_id: int
    user_id: int
    name: str


class LeagueSummaryResponse(BaseModel):
    league_id: int
    code: str
    name: str
    season: str
    league_type: str
    scoring_type: str
    max_members: int
    member_count: int
    user_rank: int
    user_season_points: int
    user_last_gw_points: int


class LeagueTableRowResponse(BaseModel):
    user_id: int
    team_name: str
    username: str | None
    season_points: int
    rank: int
    last_gw_points: int


class LeagueTableResponse(BaseModel):
    league: LeagueSummaryResponse
    rows: list[LeagueTableRowResponse]

# --- head-to-head matchups ---------------------------------------------
#
# READ-ONLY. Everything below serves what standings.py already computed and
# computes no H2H rule of its own.
#
# THREE STATES, not two. The schema makes user_id_2, points_1, points_2 and
# result all nullable, so a row is one of:
#   * scheduled but unplayed -- result IS NULL. The round-robin is generated
#     for the whole season upfront, so future gameweeks already exist as rows.
#   * a bye -- user_id_2 IS NULL, result = 'bye'
#   * played -- result IN ('win_1', 'win_2', 'draw')
# The response distinguishes all three rather than collapsing them.

H2H_GAMEWEEK_FIXTURES_QUERY = text(
    """
    SELECT
        f.id AS fixture_id,
        f.gameweek,
        f.user_id_1,
        COALESCE(u1.team_name, u1.username, CONCAT('User ', f.user_id_1)) AS team_name_1,
        u1.username AS username_1,
        f.points_1,
        f.user_id_2,
        COALESCE(u2.team_name, u2.username, CONCAT('User ', f.user_id_2)) AS team_name_2,
        u2.username AS username_2,
        f.points_2,
        f.result
    FROM league_h2h_fixtures f
    LEFT JOIN users u1 ON u1.id = f.user_id_1
    LEFT JOIN users u2 ON u2.id = f.user_id_2
    WHERE f.league_id = :league_id AND f.season = :season AND f.gameweek = :gameweek
    ORDER BY f.id
    """
)

# Season-to-date W/D/L per member. LEFT JOIN out from league_members so a
# member who has not yet appeared in a completed fixture still gets a zero row
# instead of vanishing from the table.
#
# `f.result IS NOT NULL` is what excludes the future fixtures the schedule
# generator already created -- without it every member would read as having
# lost every gameweek still to come.
H2H_SEASON_RECORDS_QUERY = text(
    """
    SELECT
        lm.user_id,
        COALESCE(u.team_name, u.username, CONCAT('User ', lm.user_id)) AS team_name,
        u.username,
        COUNT(*) FILTER (WHERE f.result = 'bye') AS byes,
        COUNT(*) FILTER (
            WHERE (f.user_id_1 = lm.user_id AND f.result = 'win_1')
               OR (f.user_id_2 = lm.user_id AND f.result = 'win_2')
        ) AS wins,
        COUNT(*) FILTER (WHERE f.result = 'draw') AS draws,
        COUNT(*) FILTER (
            WHERE (f.user_id_1 = lm.user_id AND f.result = 'win_2')
               OR (f.user_id_2 = lm.user_id AND f.result = 'win_1')
        ) AS losses
    FROM league_members lm
    LEFT JOIN users u ON u.id = lm.user_id
    LEFT JOIN league_h2h_fixtures f
           ON f.league_id = lm.league_id
          AND f.season = :season
          AND f.result IS NOT NULL
          AND (f.user_id_1 = lm.user_id OR f.user_id_2 = lm.user_id)
    WHERE lm.league_id = :league_id
    GROUP BY lm.user_id, u.team_name, u.username
    ORDER BY lm.user_id
    """
)


class H2HSideResponse(BaseModel):
    user_id: int
    team_name: str
    username: str | None = None
    # None until standings have processed this gameweek. Deliberately distinct
    # from 0, which is a real score -- see the endpoint docstring on why a 0
    # can also mean "not scored yet" mid-gameweek.
    points: int | None = None
    is_current_user: bool = False


class H2HFixtureResponse(BaseModel):
    fixture_id: int
    gameweek: int
    # side_1/side_2 rather than home/away: H2H has no venue, and these map
    # straight onto the schema's user_id_1/user_id_2. Their order is the
    # round-robin generator's, not a seeding, and carries no meaning.
    side_1: H2HSideResponse
    side_2: H2HSideResponse | None = None  # None on a bye
    result: str | None = None  # win_1 | win_2 | draw | bye | None (unplayed)
    is_bye: bool = False
    # Saves every client re-deriving "did I win?" from win_1/win_2 against
    # whichever side it happens to be -- a decode that is easy to get
    # backwards. win | loss | draw | bye | None (not the caller's fixture, or
    # not yet played).
    outcome_for_current_user: str | None = None


class H2HRecordResponse(BaseModel):
    user_id: int
    team_name: str
    username: str | None = None
    wins: int = 0
    draws: int = 0
    losses: int = 0
    byes: int = 0
    match_points: int = 0


class H2HGameweekResponse(BaseModel):
    league_id: int
    league_name: str
    season: str
    gameweek: int
    # upcoming | live | final -- the same definition GET /team uses.
    status: str
    # True when results exist but the gameweek is not over yet.
    is_provisional: bool
    fixtures: list[H2HFixtureResponse]
    your_fixture: H2HFixtureResponse | None = None
    records: list[H2HRecordResponse]



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


def _league_summary(row, member_count: int | None = None) -> LeagueSummaryResponse:
    return LeagueSummaryResponse(
        league_id=row.league_id,
        code=row.code,
        name=row.name,
        season=row.season,
        league_type=row.league_type,
        scoring_type=row.scoring_type,
        max_members=row.max_members,
        member_count=int(member_count if member_count is not None else row.member_count),
        user_rank=int(getattr(row, "user_rank", 0) or 0),
        user_season_points=int(getattr(row, "user_season_points", 0) or 0),
        user_last_gw_points=int(getattr(row, "user_last_gw_points", 0) or 0),
    )


@router.get("/leagues", response_model=list[LeagueSummaryResponse])
def get_user_leagues(
    season: str,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[LeagueSummaryResponse]:
    user_id = current_user.id
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(USER_LEAGUES_QUERY, {"user_id": user_id, "season": season}).all()

    return [_league_summary(row) for row in rows]


@router.get("/leagues/{league_id}/table", response_model=LeagueTableResponse)
def get_league_table(
    league_id: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> LeagueTableResponse:
    user_id = current_user.id
    engine = get_engine()
    with engine.connect() as conn:
        league_row = conn.execute(LEAGUE_META_QUERY, {"league_id": league_id}).first()
        if league_row is None:
            raise HTTPException(status_code=404, detail="league not found")

        member_count = conn.execute(MEMBER_COUNT_QUERY, {"league_id": league_id}).scalar()
        # Unconditional now: the viewer is whoever the token says, so there
        # is no longer an anonymous case. member_row stays None when the
        # viewer simply is not a member of this league.
        member_row = conn.execute(
            text(
                """
                SELECT rank AS user_rank, season_points AS user_season_points, last_gw_points AS user_last_gw_points
                FROM league_members
                WHERE league_id = :league_id AND user_id = :user_id
                """
            ),
            {"league_id": league_id, "user_id": user_id},
        ).first()
        table_rows = conn.execute(LEAGUE_TABLE_QUERY, {"league_id": league_id}).all()

    summary_source = member_row or league_row
    summary_values = {
        **league_row._mapping,
        "member_count": int(member_count),
        "user_rank": int(getattr(summary_source, "user_rank", 0) or 0),
        "user_season_points": int(getattr(summary_source, "user_season_points", 0) or 0),
        "user_last_gw_points": int(getattr(summary_source, "user_last_gw_points", 0) or 0),
    }

    return LeagueTableResponse(
        league=_league_summary(SimpleNamespace(**summary_values)),
        rows=[
            LeagueTableRowResponse(
                user_id=row.user_id,
                team_name=row.team_name,
                username=row.username,
                season_points=int(row.season_points or 0),
                rank=int(row.rank or 0),
                last_gw_points=int(row.last_gw_points or 0),
            )
            for row in table_rows
        ],
    )


def _side(user_id, team_name, username, points, current_user_id) -> H2HSideResponse:
    return H2HSideResponse(
        user_id=user_id,
        team_name=team_name,
        username=username,
        points=None if points is None else int(points),
        is_current_user=user_id == current_user_id,
    )


def _outcome_for(row, current_user_id: int | None) -> str | None:
    """Translate the stored result into the caller's own point of view.

    None when the caller is not in this fixture, or when it has not been
    played yet -- both genuinely mean "no outcome to show", and the client
    can tell them apart from is_current_user / result being None.
    """
    if row.result is None:
        return None
    if row.result == "bye":
        return "bye" if row.user_id_1 == current_user_id else None
    if row.user_id_1 == current_user_id:
        return {"win_1": "win", "win_2": "loss", "draw": "draw"}[row.result]
    if row.user_id_2 == current_user_id:
        return {"win_1": "loss", "win_2": "win", "draw": "draw"}[row.result]
    return None


@router.get("/leagues/{league_id}/h2h", response_model=H2HGameweekResponse)
def get_h2h_gameweek(
    league_id: int,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> H2HGameweekResponse:
    """One gameweek's head-to-head matchups for a league, plus season records.

    Returns EVERY fixture in the gameweek rather than only the caller's, with
    the caller's also lifted out as `your_fixture`. A league results screen
    needs all of them and it is one query either way; the reverse -- serving
    only the caller and adding a second endpoint for the rest -- would cost a
    round trip per view.

    Read-only. Results come from league_h2h_fixtures exactly as
    standings.py wrote them; nothing here recomputes a match.

    PROVISIONAL RESULTS. compute_league_standings runs repeatedly while a
    gameweek is in its active window, so a result can exist before the
    gameweek is over, and standings.py treats a member with no gw_scores row
    yet as 0. That means a mid-gameweek 40-0 may simply mean the opponent has
    not been scored yet, not that they blanked. `status` and `is_provisional`
    exist so a client can render that as in-progress instead of as a settled
    scoreline. `status` uses the same fixture-state definition as GET /team.

    Classic leagues are a 422, not an empty list: asking for the H2H view of a
    league that has none is a mistake in the caller worth naming, and an empty
    fixtures array would read as "no matches this week".
    """
    engine = get_engine()
    user_id = current_user.id

    with engine.connect() as conn:
        league_row = conn.execute(LEAGUE_META_QUERY, {"league_id": league_id}).first()
        if league_row is None:
            raise HTTPException(status_code=404, detail="league not found")
        if league_row.scoring_type != "head_to_head":
            raise HTTPException(
                status_code=422,
                detail=[
                    f"league {league_id} has scoring_type "
                    f"{league_row.scoring_type!r}, not 'head_to_head' -- "
                    "it has no head-to-head fixtures"
                ],
            )

        season = league_row.season
        key = {"league_id": league_id, "season": season}
        fixture_rows = conn.execute(
            H2H_GAMEWEEK_FIXTURES_QUERY, {**key, "gameweek": gameweek}
        ).all()
        record_rows = conn.execute(H2H_SEASON_RECORDS_QUERY, key).all()
        status_row = conn.execute(
            GW_FIXTURE_STATUS_QUERY, {"season": season, "gameweek": gameweek}
        ).first()

    status = _resolve_live_status(status_row)

    fixtures = []
    for row in fixture_rows:
        is_bye = row.user_id_2 is None
        fixtures.append(
            H2HFixtureResponse(
                fixture_id=row.fixture_id,
                gameweek=row.gameweek,
                side_1=_side(row.user_id_1, row.team_name_1, row.username_1, row.points_1, user_id),
                side_2=(
                    None
                    if is_bye
                    else _side(row.user_id_2, row.team_name_2, row.username_2, row.points_2, user_id)
                ),
                result=row.result,
                is_bye=is_bye,
                outcome_for_current_user=_outcome_for(row, user_id),
            )
        )

    your_fixture = next(
        (
            f
            for f in fixtures
            if f.side_1.is_current_user or (f.side_2 is not None and f.side_2.is_current_user)
        ),
        None,
    )

    records = [
        H2HRecordResponse(
            user_id=r.user_id,
            team_name=r.team_name,
            username=r.username,
            wins=int(r.wins or 0),
            draws=int(r.draws or 0),
            losses=int(r.losses or 0),
            byes=int(r.byes or 0),
            match_points=(
                int(r.wins or 0) * WIN_POINTS
                + int(r.draws or 0) * DRAW_POINTS
                + int(r.losses or 0) * LOSS_POINTS
                + int(r.byes or 0) * BYE_POINTS
            ),
        )
        for r in record_rows
    ]

    return H2HGameweekResponse(
        league_id=league_id,
        league_name=league_row.name,
        season=season,
        gameweek=gameweek,
        status=status,
        # A result that exists but is not settled. An unplayed gameweek is not
        # provisional -- it has nothing to be provisional about.
        is_provisional=status != "final" and any(f.result is not None for f in fixtures),
        fixtures=fixtures,
        your_fixture=your_fixture,
        records=records,
    )


@router.post("/leagues", response_model=CreateLeagueResponse)
def create_league(
    req: CreateLeagueRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> CreateLeagueResponse:
    user_id = current_user.id
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
                        "created_by": user_id,
                        "season": req.season,
                        "league_type": req.league_type,
                        "scoring_type": req.scoring_type,
                        "max_members": req.max_members,
                    },
                ).scalar()
                conn.execute(INSERT_MEMBER_STMT, {"league_id": league_id, "user_id": user_id})
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
def join_league(
    req: JoinLeagueRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> JoinLeagueResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        league_row = conn.execute(LEAGUE_BY_CODE_QUERY, {"code": req.code}).first()
        is_member = False
        member_count = 0
        if league_row is not None:
            is_member = (
                conn.execute(IS_MEMBER_QUERY, {"league_id": league_row.league_id, "user_id": user_id}).first()
                is not None
            )
            member_count = conn.execute(MEMBER_COUNT_QUERY, {"league_id": league_row.league_id}).scalar()

    errors = _validate_join(league_row, is_member, member_count)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    with engine.begin() as conn:
        conn.execute(INSERT_MEMBER_STMT, {"league_id": league_row.league_id, "user_id": user_id})

    return JoinLeagueResponse(league_id=league_row.league_id, user_id=user_id, name=league_row.name)
