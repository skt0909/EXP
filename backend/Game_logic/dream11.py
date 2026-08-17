"""
dream11.py — FastAPI endpoints for Dream11-style single-match contests:
create, join, submit an 11-player team.

SCHEMA NOTE (surprise, confirmed with the user before writing this):
dream11.player_prices.player_id and dream11.team_players.player_id are
FKs to ml.players.id (the internal serial id) -- NOT fpl_id, the
opposite of squad_players/starting_xi/transfers' convention everywhere
else in this project. The HTTP API here still accepts raw fpl_ids from
the client (player_ids, captain_id, vice_captain_id), consistent with
every other endpoint, but internally resolves fpl_id -> ml.players.id
before any INSERT into player_prices/team_players, since the FK
requires it.

Pool/pricing (create_contest): the full player pool for a fixture is
ALL ml.players rows for both teams in it (not just a confirmed XI --
Dream11 lets you pick from the whole squad). Prices are computed once,
from ml.player_gw_features.pts_rolling_5gw (most recent row before the
fixture's gameweek), min-max normalized WITHIN this pool only, to
[6.0, 11.0], rounded to the nearest 0.5. A player with no rolling
history yet (NaN) is floored to 6.0 directly, excluded from the
min/max computation. If the non-NaN subset has fewer than 2 distinct
values (empty, or everyone identical -- including the trivial
single-data-point case), EVERY pool player gets 6.0, avoiding a
divide-by-zero and an arbitrary single-winner price. Prices are
inserted once and never touched again -- dream11.player_prices has its
own enforce_price_immutability trigger (BEFORE UPDATE) enforcing this
at the DB level too.

Locking: dream11.contests.is_locked gates joining (checked here,
application-side, same as leagues.py's capacity check) AND team
submission (enforced by enforce_contest_lock_fn, a BEFORE INSERT
trigger on dream11.teams -- NOT team_players -- checking
NEW.contest_id's is_locked flag). Since the trigger fires on the
teams-row insert specifically, inserting that row first naturally
gates the whole submission before any team_players rows are attempted.
Exact RAISE EXCEPTION text: 'Contest is locked (fixture has kicked
off): contest_id=%' -- matching on "Contest is locked" is inherently
fragile if that message is ever reworded, same caveat as
starting_xi.py's LOCK_ERROR_SUBSTRING.

Automatic scoring scheduling (create_contest): two one-off Celery
tasks (poll_and_score_dream11, defined in Worker/tasks.py) are
scheduled via .apply_async(eta=...) relative to the fixture's
kickoff_time. The import of that task is done LOCALLY inside
create_contest, not at module level -- no existing Game_logic module
imports anything from Worker (confirmed by reading all of them before
writing this file; the dependency direction has only ever gone
Worker -> Game_logic), and a module-level import here would mean every
FastAPI boot (main.py mounting this router) transitively drags in
Celery, the Redis broker config, and the whole ML pipeline
(feature_builder/predictor/tier_builder) just to mount an HTTP router
that only ever calls .apply_async(). Scheduling itself is best-effort:
wrapped in its own try/except so a broker being unreachable (e.g. in
tests, where Redis isn't running) never fails contest creation, which
has already committed to the DB by that point.
"""

import logging
import random
import string
from collections import Counter
from datetime import timedelta

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from Game_logic.db_utils import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DREAM11_TEAM_SIZE = 11
DEFAULT_MAX_MEMBERS = 50
CODE_LENGTH = 7
MAX_CODE_ATTEMPTS = 5

PRICE_FLOOR = 6.0
PRICE_CEILING = 11.0
PRICE_RANGE = PRICE_CEILING - PRICE_FLOOR
DREAM11_BUDGET_CAP = 100.0

# enforce_contest_lock_fn's exact RAISE EXCEPTION text (dream11.teams
# BEFORE INSERT trigger). See module docstring.
LOCK_ERROR_SUBSTRING = "Contest is locked"

router = APIRouter()

FIXTURE_QUERY = text(
    "SELECT id, season, gameweek, home_team_id, away_team_id, kickoff_time FROM ml.fixtures WHERE id = :fixture_id"
)

POOL_QUERY = text(
    """
    SELECT id AS internal_id, fpl_id, position, team_id
    FROM ml.players
    WHERE season = :season AND (team_id = :home_team_id OR team_id = :away_team_id)
    """
)

# Most recent pts_rolling_5gw strictly before this fixture's gameweek, per player.
PRICE_INPUT_QUERY = text(
    """
    SELECT DISTINCT ON (player_id) player_id, pts_rolling_5gw
    FROM ml.player_gw_features
    WHERE player_id = ANY(:player_ids) AND season = :season AND gameweek < :gameweek
    ORDER BY player_id, gameweek DESC
    """
)

INSERT_CONTEST_STMT = text(
    """
    INSERT INTO dream11.contests (fixture_id, name, code, created_by, max_members)
    VALUES (:fixture_id, :name, :code, :created_by, :max_members)
    RETURNING id
    """
)

INSERT_PRICES_STMT = text(
    "INSERT INTO dream11.player_prices (contest_id, player_id, credit_price) VALUES (:contest_id, :player_id, :credit_price)"
)

INSERT_CONTEST_MEMBER_STMT = text(
    "INSERT INTO dream11.contest_members (contest_id, user_id, total_points, rank) VALUES (:contest_id, :user_id, 0, 0)"
)

CONTEST_BY_CODE_QUERY = text(
    "SELECT id AS contest_id, name, max_members, is_locked FROM dream11.contests WHERE code = :code"
)

IS_CONTEST_MEMBER_QUERY = text(
    "SELECT 1 FROM dream11.contest_members WHERE contest_id = :contest_id AND user_id = :user_id"
)

CONTEST_MEMBER_COUNT_QUERY = text("SELECT COUNT(*) FROM dream11.contest_members WHERE contest_id = :contest_id")

CONTEST_SEASON_QUERY = text(
    "SELECT c.id AS contest_id, f.season FROM dream11.contests c JOIN ml.fixtures f ON f.id = c.fixture_id WHERE c.id = :contest_id"
)

PLAYERS_LOOKUP_QUERY = text(
    "SELECT fpl_id, id AS internal_id, position FROM ml.players WHERE season = :season AND fpl_id = ANY(:player_ids)"
)

CONTEST_PRICES_QUERY = text(
    "SELECT player_id AS internal_id, credit_price FROM dream11.player_prices WHERE contest_id = :contest_id"
)

INSERT_TEAM_STMT = text("INSERT INTO dream11.teams (contest_id, user_id) VALUES (:contest_id, :user_id) RETURNING id")

INSERT_TEAM_PLAYER_STMT = text(
    """
    INSERT INTO dream11.team_players (team_id, player_id, is_captain, is_vice_captain)
    VALUES (:team_id, :player_id, :is_captain, :is_vice_captain)
    """
)


def _generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=CODE_LENGTH))


def _compute_prices(pool_internal_ids: list[int], rolling_values: dict[int, float | None]) -> dict[int, float]:
    have_data = {pid: v for pid, v in rolling_values.items() if v is not None}

    if len(set(have_data.values())) <= 1:
        # Empty (nobody has rolling history yet), or every data point identical
        # (including the trivial single-player-has-data case) -- floor
        # everyone rather than divide by zero or crown an arbitrary winner.
        return {pid: PRICE_FLOOR for pid in pool_internal_ids}

    pool_min = min(have_data.values())
    pool_max = max(have_data.values())

    prices = {}
    for pid in pool_internal_ids:
        v = rolling_values.get(pid)
        if v is None:
            prices[pid] = PRICE_FLOOR
        else:
            raw = PRICE_FLOOR + (v - pool_min) / (pool_max - pool_min) * PRICE_RANGE
            prices[pid] = round(raw * 2) / 2
    return prices


def _is_lock_violation(exc: SQLAlchemyError) -> bool:
    return LOCK_ERROR_SUBSTRING in str(exc)


class CreateContestRequest(BaseModel):
    fixture_id: int
    name: str
    user_id: int
    max_members: int = DEFAULT_MAX_MEMBERS


class CreateContestResponse(BaseModel):
    contest_id: int
    code: str
    fixture_id: int
    name: str
    max_members: int
    pool_size: int


class JoinContestRequest(BaseModel):
    user_id: int
    code: str


class JoinContestResponse(BaseModel):
    contest_id: int
    user_id: int
    name: str


class SubmitTeamRequest(BaseModel):
    user_id: int
    player_ids: list[int]  # exactly 11 raw FPL ids
    captain_id: int
    vice_captain_id: int


class TeamPlayerOut(BaseModel):
    player_id: int
    position: str
    credit_price: float
    is_captain: bool
    is_vice_captain: bool


class SubmitTeamResponse(BaseModel):
    team_id: int
    contest_id: int
    user_id: int
    captain_id: int
    vice_captain_id: int
    total_credit_cost: float
    players: list[TeamPlayerOut]


def _validate_join(contest_row, is_member: bool, member_count: int) -> list[str]:
    errors: list[str] = []
    if contest_row is None:
        errors.append("code does not match any existing contest")
        return errors

    if contest_row.is_locked:
        errors.append("contest is locked and can no longer be joined")
    if is_member:
        errors.append("user is already a member of this contest")
    if member_count >= contest_row.max_members:
        errors.append(f"contest is at capacity ({contest_row.max_members} members)")

    return errors


def _validate_team(
    req: SubmitTeamRequest,
    is_member: bool,
    players_by_fpl_id: dict,
    pool_prices: dict[int, float],
) -> tuple[list[dict], list[str]]:
    """Collect every validation failure instead of stopping at the first.
    Returns (resolved_players, errors)."""
    errors: list[str] = []

    if len(req.player_ids) != DREAM11_TEAM_SIZE:
        errors.append(f"team must contain exactly {DREAM11_TEAM_SIZE} players, got {len(req.player_ids)}")

    seen: set[int] = set()
    duplicates = sorted({pid for pid in req.player_ids if pid in seen or seen.add(pid)})
    if duplicates:
        errors.append(f"duplicate player_id(s): {duplicates}")

    if not is_member:
        errors.append("user must join this contest before submitting a team")

    unique_ids = list(dict.fromkeys(req.player_ids))

    # Distinct from "not in this contest's pool" below: this player_id
    # doesn't resolve in ml.players at all for this fixture's season.
    unresolved = sorted(pid for pid in unique_ids if pid not in players_by_fpl_id)
    if unresolved:
        errors.append(f"player_id(s) not found in ml.players for this fixture's season: {unresolved}")

    resolvable = [(pid, players_by_fpl_id[pid]) for pid in unique_ids if pid in players_by_fpl_id]

    # Distinct from "unresolved" above: this player_id IS a real player,
    # just not one of the two teams playing in this contest's fixture.
    not_in_pool = sorted(pid for pid, row in resolvable if row.internal_id not in pool_prices)
    if not_in_pool:
        errors.append(f"player_id(s) not part of this contest's player pool: {not_in_pool}")

    resolved_players = [
        {
            "fpl_id": pid,
            "internal_id": row.internal_id,
            "position": row.position,
            "credit_price": pool_prices[row.internal_id],
        }
        for pid, row in resolvable
        if row.internal_id in pool_prices
    ]

    position_counts = Counter(p["position"] for p in resolved_players)
    gk_count = position_counts.get("GK", 0)
    if gk_count != 1:
        errors.append(f"expected exactly 1 GK, got {gk_count}")

    def_count = position_counts.get("DEF", 0)
    if not (3 <= def_count <= 5):
        errors.append(f"DEF count must be between 3 and 5, got {def_count}")

    mid_count = position_counts.get("MID", 0)
    if not (3 <= mid_count <= 5):
        errors.append(f"MID count must be between 3 and 5, got {mid_count}")

    fwd_count = position_counts.get("FWD", 0)
    if not (1 <= fwd_count <= 3):
        errors.append(f"FWD count must be between 1 and 3, got {fwd_count}")

    outfield_total = def_count + mid_count + fwd_count
    if outfield_total != 10:
        errors.append(f"outfield players (DEF+MID+FWD) must sum to 10, got {outfield_total}")

    total_cost = sum(p["credit_price"] for p in resolved_players)
    if total_cost > DREAM11_BUDGET_CAP:
        errors.append(f"total credit cost {total_cost} exceeds budget cap {DREAM11_BUDGET_CAP}")

    if req.captain_id == req.vice_captain_id:
        errors.append("captain_id and vice_captain_id must be different players")
    if req.captain_id not in req.player_ids:
        errors.append(f"captain_id {req.captain_id} is not in the submitted team")
    if req.vice_captain_id not in req.player_ids:
        errors.append(f"vice_captain_id {req.vice_captain_id} is not in the submitted team")

    return resolved_players, errors


@router.post("/dream11/contests", response_model=CreateContestResponse)
def create_contest(req: CreateContestRequest) -> CreateContestResponse:
    errors: list[str] = []
    if req.max_members < 1:
        errors.append(f"max_members must be at least 1, got {req.max_members}")

    engine = get_engine()
    with engine.connect() as conn:
        fixture_row = conn.execute(FIXTURE_QUERY, {"fixture_id": req.fixture_id}).first()

    if fixture_row is None:
        errors.append(f"fixture_id {req.fixture_id} does not exist")
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    with engine.connect() as conn:
        pool_rows = conn.execute(
            POOL_QUERY,
            {"season": fixture_row.season, "home_team_id": fixture_row.home_team_id, "away_team_id": fixture_row.away_team_id},
        ).all()

    if not pool_rows:
        raise HTTPException(status_code=422, detail=[f"no players found for fixture_id {req.fixture_id}'s teams"])

    pool_internal_ids = [p.internal_id for p in pool_rows]
    with engine.connect() as conn:
        rolling_rows = conn.execute(
            PRICE_INPUT_QUERY,
            {"player_ids": pool_internal_ids, "season": fixture_row.season, "gameweek": fixture_row.gameweek},
        ).all()
    rolling_values = {r.player_id: (float(r.pts_rolling_5gw) if r.pts_rolling_5gw is not None else None) for r in rolling_rows}
    prices = _compute_prices(pool_internal_ids, {pid: rolling_values.get(pid) for pid in pool_internal_ids})

    contest_id = None
    code = None
    for attempt in range(1, MAX_CODE_ATTEMPTS + 1):
        code = _generate_code()
        try:
            with engine.begin() as conn:
                contest_id = conn.execute(
                    INSERT_CONTEST_STMT,
                    {
                        "fixture_id": req.fixture_id,
                        "name": req.name,
                        "code": code,
                        "created_by": req.user_id,
                        "max_members": req.max_members,
                    },
                ).scalar()
                conn.execute(
                    INSERT_PRICES_STMT,
                    [
                        {"contest_id": contest_id, "player_id": pid, "credit_price": prices[pid]}
                        for pid in pool_internal_ids
                    ],
                )
                conn.execute(INSERT_CONTEST_MEMBER_STMT, {"contest_id": contest_id, "user_id": req.user_id})
            break
        except IntegrityError:
            logger.warning("create_contest: code collision on attempt %d/%d (%s), retrying", attempt, MAX_CODE_ATTEMPTS, code)
    else:
        logger.error("create_contest: exhausted %d code-generation attempts", MAX_CODE_ATTEMPTS)
        raise HTTPException(status_code=500, detail="Could not generate a unique contest code, please try again")

    try:
        from Worker.tasks import poll_and_score_dream11  # local import -- see module docstring

        if fixture_row.kickoff_time is not None:
            poll_and_score_dream11.apply_async(
                args=[req.fixture_id, contest_id, "halftime"], eta=fixture_row.kickoff_time + timedelta(minutes=50)
            )
            poll_and_score_dream11.apply_async(
                args=[req.fixture_id, contest_id, "fulltime"], eta=fixture_row.kickoff_time + timedelta(minutes=115)
            )
        else:
            logger.warning(
                "create_contest: contest_id=%s fixture_id=%s has no kickoff_time yet -- skipping automatic poll scheduling",
                contest_id, req.fixture_id,
            )
    except Exception as e:
        # Best-effort: scheduling failure (e.g. broker unreachable) must
        # never undo an already-committed contest creation.
        logger.error("create_contest: failed to schedule automatic polling for contest_id=%s: %s: %s", contest_id, type(e).__name__, e)

    return CreateContestResponse(
        contest_id=contest_id,
        code=code,
        fixture_id=req.fixture_id,
        name=req.name,
        max_members=req.max_members,
        pool_size=len(pool_internal_ids),
    )


@router.post("/dream11/contests/join", response_model=JoinContestResponse)
def join_contest(req: JoinContestRequest) -> JoinContestResponse:
    engine = get_engine()

    with engine.connect() as conn:
        contest_row = conn.execute(CONTEST_BY_CODE_QUERY, {"code": req.code}).first()
        is_member = False
        member_count = 0
        if contest_row is not None:
            is_member = (
                conn.execute(IS_CONTEST_MEMBER_QUERY, {"contest_id": contest_row.contest_id, "user_id": req.user_id}).first()
                is not None
            )
            member_count = conn.execute(CONTEST_MEMBER_COUNT_QUERY, {"contest_id": contest_row.contest_id}).scalar()

    errors = _validate_join(contest_row, is_member, member_count)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    with engine.begin() as conn:
        conn.execute(INSERT_CONTEST_MEMBER_STMT, {"contest_id": contest_row.contest_id, "user_id": req.user_id})

    return JoinContestResponse(contest_id=contest_row.contest_id, user_id=req.user_id, name=contest_row.name)


@router.post("/dream11/contests/{contest_id}/team", response_model=SubmitTeamResponse)
def submit_team(contest_id: int, req: SubmitTeamRequest) -> SubmitTeamResponse:
    engine = get_engine()

    with engine.connect() as conn:
        contest_season_row = conn.execute(CONTEST_SEASON_QUERY, {"contest_id": contest_id}).first()

    if contest_season_row is None:
        raise HTTPException(status_code=422, detail=[f"contest_id {contest_id} does not exist"])

    with engine.connect() as conn:
        is_member = (
            conn.execute(IS_CONTEST_MEMBER_QUERY, {"contest_id": contest_id, "user_id": req.user_id}).first() is not None
        )
        unique_ids = list(dict.fromkeys(req.player_ids))
        players_by_fpl_id = {
            row.fpl_id: row
            for row in conn.execute(PLAYERS_LOOKUP_QUERY, {"season": contest_season_row.season, "player_ids": unique_ids})
        }
        pool_prices = {
            row.internal_id: float(row.credit_price)
            for row in conn.execute(CONTEST_PRICES_QUERY, {"contest_id": contest_id})
        }

    resolved_players, errors = _validate_team(req, is_member, players_by_fpl_id, pool_prices)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    try:
        with engine.begin() as conn:
            team_id = conn.execute(INSERT_TEAM_STMT, {"contest_id": contest_id, "user_id": req.user_id}).scalar()
            conn.execute(
                INSERT_TEAM_PLAYER_STMT,
                [
                    {
                        "team_id": team_id,
                        "player_id": p["internal_id"],
                        "is_captain": p["fpl_id"] == req.captain_id,
                        "is_vice_captain": p["fpl_id"] == req.vice_captain_id,
                    }
                    for p in resolved_players
                ],
            )
    except SQLAlchemyError as e:
        if _is_lock_violation(e):
            raise HTTPException(status_code=422, detail="Contest is locked, team can no longer be submitted") from e
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return SubmitTeamResponse(
        team_id=team_id,
        contest_id=contest_id,
        user_id=req.user_id,
        captain_id=req.captain_id,
        vice_captain_id=req.vice_captain_id,
        total_credit_cost=sum(p["credit_price"] for p in resolved_players),
        players=[
            TeamPlayerOut(
                player_id=p["fpl_id"],
                position=p["position"],
                credit_price=p["credit_price"],
                is_captain=p["fpl_id"] == req.captain_id,
                is_vice_captain=p["fpl_id"] == req.vice_captain_id,
            )
            for p in resolved_players
        ],
    )
