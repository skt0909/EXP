"""
dream11.py — FastAPI endpoints for Dream11-style single-match contests:
create, join, submit an 11-player team, plus the five reads the frontend
builds its screens from (my contests, contest detail, priced player pool,
leaderboard, my team).

The reads return 404 for a missing contest, matching leagues.py's GET
/leagues/{league_id}/table, while the writes keep returning 422 with a
collected detail list -- there, a bad contest_id is one validation
failure among several reported in one pass, not a missing resource.

GET .../team recomputes each player's points with dream11_scoring's
calculate_dream11_points rather than reading contest_members.total_points,
so a live team's per-player breakdown always sums to the total shown
beside it; the stored checkpoint value is returned alongside it as
contest_total_points. That import is the only coupling between this
module and dream11_scoring, and it exists specifically so the point
weightings have exactly one definition in the codebase.

AUTHENTICATION. Every route on this router requires a bearer token --
the dependency is declared once on the APIRouter itself
(dependencies=[Depends(get_current_user)]) rather than endpoint by
endpoint, so a route added later is authenticated by construction and
cannot be forgotten. That is the whole point of putting it there: this
module previously authenticated exactly one of its nine endpoints
(get_user_team) and took a caller-supplied user_id everywhere else,
which meant any unauthenticated caller could create, join or submit a
team AS ANY USER simply by naming their id.

Identity now comes from the token on EVERY endpoint but one: create_contest,
join_contest, submit_team, get_user_contests, get_fixture_contests,
get_contest and get_contest_leaderboard all use current_user.id, and the
user_id field is gone from their request models and signatures. (The last
two took an optional user_id that only decorated the response with that
user's membership flags -- never dangerous, since those fields expose
nothing the leaderboard does not already return to every member, but the
only remaining place a client could name an identity, and therefore the
only place a later reader would have to stop and ask whether it was
deliberate.) get_contest_pool never took one: its response is identical
for every caller.

Pydantic and FastAPI both ignore unknown fields, so a stale client that
still sends user_id keeps working and the value is simply inert -- the
same cutover shape the classic endpoints used.

get_user_team is the sole exception, and it is the one place here where a
user_id parameter is correct: it names WHOSE team to fetch, not who is
asking, and it is already checked against current_user.id (see its
docstring -- an opponent's XI is readable only after the contest locks,
and only by a fellow member). The rule that separates it from the seven
above: a user_id parameter is legitimate only where it selects data
belonging to someone OTHER than the caller, and only behind an explicit
authorization check.

PLAYER ID CONVENTION -- READ THIS BEFORE TOUCHING ANY QUERY HERE.

This project uses TWO different meanings of "player id" against the same
shared ml.players table, and Dream11 is the odd one out:

  * CLASSIC FPL tables store the RAW fpl_id, never translated:
    squad_players.player_id, starting_xi.player_id,
    transfers.player_in_id/player_out_id.

  * DREAM11 tables store ml.players.id, the INTERNAL SERIAL:
    dream11.player_prices.player_id, dream11.team_players.player_id.
    So does ml.player_gw_stats.player_id, which is why the scoring and
    rolling-form joins here line up without translation.

Unifying the two is a deliberate open decision, NOT an oversight to fix
in passing -- it would require migrating live data on one side or the
other. Until that is decided, the split is load-bearing and the rules
below are what keep it safe.

THE TRANSLATION BOUNDARY is exactly one query: PLAYERS_LOOKUP_QUERY
below, which selects `p.id AS internal_id` keyed by `p.fpl_id`. Every
request that carries player ids goes through it, and nothing else
converts between the two spaces. Concretely:

  * fpl_id space -- everything a client sends or receives:
    SubmitTeamRequest.player_ids/captain_id/vice_captain_id, and every
    *Out model's player_id field.
  * internal-id space -- everything that reaches storage: both dream11
    tables, POOL_QUERY's output, PRICE_INPUT_QUERY's input, and every
    join to ml.player_gw_stats.

captain_id/vice_captain_id are deliberately never translated: they are
only ever compared against req.player_ids in fpl space and stored as
booleans, so they never reach a query as ids at all.

The translation is only well-defined WITHIN A SEASON. ml.players holds
one row per (fpl_id, season) -- enforced by uq_players_fpl_season -- so
the same player has a different internal id each season. Dropping the
season filter from PLAYERS_LOOKUP_QUERY would not error; it would
silently resolve an fpl_id to whichever season's row sorted last. Always
pass the contest fixture's season.

Nothing in the codebase currently joins or compares a player between
Dream11 and classic FPL data -- fixtures.py's contest counts and
dream11_locking.py's contest locking both key on fixture_id/user_id only. Any
future feature that does cross that line is the place this split becomes
a real bug, so translate explicitly rather than assuming.

Pool/pricing (create_contest): the full player pool for a fixture is
ALL ml.players rows for both teams in it (not just a confirmed XI --
Dream11 lets you pick from the whole squad). Prices are computed once,
from pts_rolling_5gw computed over ml.player_gw_stats (the mean of each
player's last <=5 completed gameweeks before the fixture's gameweek --
see PRICE_INPUT_QUERY for why this is NOT read from the
ml.player_gw_features table any more), min-max normalized WITHIN this
pool only, to
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

That scheduling now runs as a FastAPI BackgroundTask
(_schedule_scoring_polls), i.e. after the response is sent, rather than
inline. Measured against a live uvicorn with Redis stopped: inline, the
POST hung ~100s and then poisoned the Celery app instance for the whole
process; with Worker/celery_app.py's bounded retry policy that dropped
to ~16s, still far too slow to make a user wait for work whose outcome
they never see. Off the request path it is 0s to the client, and the
retry policy just bounds how long the background thread spends failing.
"""

import logging
import random
import string
from collections import Counter
from datetime import datetime, timedelta

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from Data.auth import CurrentUser, get_current_user
from Shared.db_utils import get_engine
from Game_logic.dream11_scoring import (
    CAPTAIN_MULTIPLIER,
    VICE_CAPTAIN_MULTIPLIER,
    calculate_dream11_points,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DREAM11_TEAM_SIZE = 11
DEFAULT_MAX_MEMBERS = 50
# 2-50, matching the "How many friends can join (2-50)" stepper on the
# create-a-contest screen. The floor was previously 1, which let you create a
# contest only you could ever be in -- the creator auto-joins, so a 1-member
# contest is full the moment it exists. There was no ceiling at all.
MIN_MAX_MEMBERS = 2
MAX_MAX_MEMBERS = 50
CODE_LENGTH = 7
MAX_CODE_ATTEMPTS = 5

PRICE_FLOOR = 6.0
PRICE_CEILING = 11.0
PRICE_RANGE = PRICE_CEILING - PRICE_FLOOR
DREAM11_BUDGET_CAP = 100.0
# Both clubs in a fixture field 30-odd players, so without this a team could be
# one club's entire XI. Matches real Dream11's per-side cap.
MAX_PLAYERS_PER_CLUB = 7

# enforce_contest_lock_fn's exact RAISE EXCEPTION text (dream11.teams
# BEFORE INSERT trigger). See module docstring.
LOCK_ERROR_SUBSTRING = "Contest is locked"

# dream11.teams' UNIQUE (contest_id, user_id) constraint -- one team per
# user per contest. Postgres puts the constraint name in the error detail.
DUPLICATE_TEAM_CONSTRAINT = "uq_d11_teams_contest_user"

# Router-level auth: applies to EVERY route below, including any added
# later. See the AUTHENTICATION section of the module docstring.
router = APIRouter(dependencies=[Depends(get_current_user)])

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

# Mean total_points over each player's last <=5 completed gameweeks strictly
# before this fixture's gameweek -- i.e. pts_rolling_5gw.
#
# Computed here from raw ml.player_gw_stats rather than read from
# ml.player_gw_features, because NOTHING IN THIS CODEBASE EVER WRITES TO
# ml.player_gw_features -- verified by grepping every writer: the only
# INSERTs are in Tests/test_dream11.py, which seeded it by hand and so hid
# the gap. Feature_engineering/feature_builder.py abandoned that table for
# the same reason and computes its own rolling means from player_gw_stats
# (see its module docstring); this query deliberately matches its
# semantics -- last N completed gameweeks, mean over however many exist,
# and NO row at all for a player with zero prior games, which
# _compute_prices already reads as "no data -> PRICE_FLOOR".
#
# Left as-is, every pool player priced at the 6.0 floor forever, making an
# 11-player team cost 66 against a 100 cap -- the credit budget could never
# bind and the whole pricing dimension of the game was inert. Confirmed
# against the dev database, where ml.player_gw_features has 0 rows while
# ml.player_gw_stats has 2847.
PRICE_INPUT_QUERY = text(
    """
    SELECT player_id, AVG(total_points)::float8 AS pts_rolling_5gw
    FROM (
        SELECT player_id, total_points,
               ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY gameweek DESC) AS recency
        FROM ml.player_gw_stats
        WHERE player_id = ANY(:player_ids) AND season = :season AND gameweek < :gameweek
    ) recent
    WHERE recency <= 5
    GROUP BY player_id
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
    "INSERT INTO dream11.player_prices (contest_id, player_id, credit_price, rolling_points) "
    "VALUES (:contest_id, :player_id, :credit_price, :rolling_points)"
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

CONTEST_SEASON_AND_GW_QUERY = text(
    "SELECT c.id AS contest_id, c.is_locked, c.finalized_at, c.fixture_id, f.season, f.gameweek "
    "FROM dream11.contests c JOIN ml.fixtures f ON f.id = c.fixture_id WHERE c.id = :contest_id"
)

# LEFT JOIN, not JOIN: ml.players.team_id is nullable, and a player with no
# club must still resolve here so they fail on the real reason rather than
# silently reading as "not found in ml.players".
PLAYERS_LOOKUP_QUERY = text(
    """
    SELECT p.fpl_id, p.id AS internal_id, p.position, p.team_id, t.short_name AS club
    FROM ml.players p
    LEFT JOIN ml.teams t ON t.id = p.team_id
    WHERE p.season = :season AND p.fpl_id = ANY(:player_ids)
    """
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

# ------------------------------------------------------------------ read queries
#
# The shared contest-summary projection. :user_id is always the CALLER
# (current_user.id) -- there is no anonymous or view-as-someone-else case
# left on this router. It stays LEFT JOINed rather than filtered because a
# non-member must still get the contest row back, with the user_* fields at
# their empty defaults: that is the "look at this contest before joining"
# case, which is now about a caller who has not joined rather than a caller
# who did not identify themselves.
_CONTEST_SUMMARY_SELECT = """
    SELECT c.id AS contest_id, c.code, c.name, c.max_members, c.is_locked,
           c.created_by, c.created_at, c.finalized_at,
           f.id AS fixture_id, f.season, f.gameweek, f.kickoff_time, f.finished,
           h.short_name AS home_team, a.short_name AS away_team,
           (SELECT COUNT(*) FROM dream11.contest_members m WHERE m.contest_id = c.id) AS member_count,
           cm.user_id IS NOT NULL AS user_is_member,
           cm.total_points AS user_total_points,
           cm.rank AS user_rank,
           EXISTS (
               SELECT 1 FROM dream11.teams t WHERE t.contest_id = c.id AND t.user_id = :user_id
           ) AS user_has_team
    FROM dream11.contests c
    JOIN ml.fixtures f ON f.id = c.fixture_id
    JOIN ml.teams h ON h.id = f.home_team_id
    JOIN ml.teams a ON a.id = f.away_team_id
    LEFT JOIN dream11.contest_members cm ON cm.contest_id = c.id AND cm.user_id = :user_id
"""

# Contests this user is a MEMBER of -- the inner EXISTS, not the LEFT JOIN,
# is what scopes the list (the LEFT JOIN is only carrying user_* fields).
USER_CONTESTS_QUERY = text(
    _CONTEST_SUMMARY_SELECT
    + """
    WHERE EXISTS (
        SELECT 1 FROM dream11.contest_members m2 WHERE m2.contest_id = c.id AND m2.user_id = :user_id
    )
    ORDER BY f.kickoff_time DESC NULLS LAST, c.id DESC
    """
)

CONTEST_DETAIL_QUERY = text(_CONTEST_SUMMARY_SELECT + " WHERE c.id = :contest_id")

# This user's contests on ONE fixture -- the match-detail view of
# USER_CONTESTS_QUERY above, and scoped identically (membership via EXISTS).
#
# Deliberately NOT "every contest on this fixture": a contest is joined by its
# code, so listing contests the caller isn't in would either be useless (no
# code, no way to join) or a hole (code included, anyone walks into a private
# contest). Browsing contests you don't belong to needs a public/private flag
# on dream11.contests, which the schema doesn't have.
FIXTURE_CONTESTS_QUERY = text(
    _CONTEST_SUMMARY_SELECT
    + """
    WHERE c.fixture_id = :fixture_id
      AND EXISTS (
          SELECT 1 FROM dream11.contest_members m2 WHERE m2.contest_id = c.id AND m2.user_id = :user_id
      )
    ORDER BY c.created_at DESC NULLS LAST, c.id DESC
    """
)

# is_home_team lets the frontend group the pool into the two real sides
# without a second round-trip for the fixture's team ids.
CONTEST_POOL_QUERY = text(
    """
    SELECT p.fpl_id, p.web_name, p.position, p.status,
           t.short_name AS club,
           (p.team_id = f.home_team_id) AS is_home_team,
           pp.credit_price, pp.rolling_points
    FROM dream11.player_prices pp
    JOIN dream11.contests c ON c.id = pp.contest_id
    JOIN ml.fixtures f ON f.id = c.fixture_id
    JOIN ml.players p ON p.id = pp.player_id
    JOIN ml.teams t ON t.id = p.team_id
    WHERE pp.contest_id = :contest_id
    ORDER BY pp.credit_price DESC, p.web_name
    """
)

# rank = 0 is the "not scored yet" default (see the contest_members DDL),
# so unranked members sort last rather than first. FALSE < TRUE in Postgres.
CONTEST_LEADERBOARD_QUERY = text(
    """
    SELECT cm.user_id, u.username, u.team_name, cm.total_points, cm.rank, cm.joined_at,
           EXISTS (
               SELECT 1 FROM dream11.teams t WHERE t.contest_id = cm.contest_id AND t.user_id = cm.user_id
           ) AS has_submitted_team
    FROM dream11.contest_members cm
    JOIN public.users u ON u.id = cm.user_id
    WHERE cm.contest_id = :contest_id
    ORDER BY (cm.rank = 0), cm.rank ASC, cm.total_points DESC, cm.joined_at ASC
    """
)

# Mirrors dream11_scoring.TEAM_PLAYERS_STATS_QUERY's COALESCE-to-0 stance:
# a player with no player_gw_stats row yet scores 0, which is a real
# pre-kickoff state, not an error. Adds the display columns (web_name,
# club, credit_price) the scoring path has no use for.
#
# It also mirrors that query's :fixture_id scoping, and has to: these two
# are the live and the scored view of the same eleven players, so a
# difference in which rows they match would put a different number on the
# team panel than on the leaderboard. See TEAM_PLAYERS_STATS_QUERY for
# why the gameweek alone is not enough in a double gameweek.
USER_TEAM_QUERY = text(
    """
    SELECT te.id AS team_id, te.submitted_at,
           p.fpl_id, p.web_name, p.position, t.short_name AS club,
           tp.is_captain, tp.is_vice_captain,
           pp.credit_price,
           COALESCE(pgs.minutes, 0) AS minutes,
           COALESCE(pgs.goals_scored, 0) AS goals_scored,
           COALESCE(pgs.assists, 0) AS assists,
           COALESCE(pgs.goals_conceded, 0) AS goals_conceded,
           COALESCE(pgs.saves, 0) AS saves,
           COALESCE(pgs.yellow_cards, 0) AS yellow_cards,
           COALESCE(pgs.red_cards, 0) AS red_cards,
           COALESCE(pgs.own_goals, 0) AS own_goals,
           COALESCE(pgs.penalties_saved, 0) AS penalties_saved,
           COALESCE(pgs.penalties_missed, 0) AS penalties_missed
    FROM dream11.teams te
    JOIN dream11.team_players tp ON tp.team_id = te.id
    JOIN ml.players p ON p.id = tp.player_id
    JOIN ml.teams t ON t.id = p.team_id
    LEFT JOIN dream11.player_prices pp
           ON pp.contest_id = te.contest_id AND pp.player_id = tp.player_id
    LEFT JOIN ml.player_gw_stats pgs
           ON pgs.player_id = tp.player_id AND pgs.season = :season
          AND pgs.gameweek = :gameweek AND pgs.fixture_id = :fixture_id
    WHERE te.contest_id = :contest_id AND te.user_id = :user_id
    ORDER BY CASE p.position WHEN 'GK' THEN 0 WHEN 'DEF' THEN 1 WHEN 'MID' THEN 2 ELSE 3 END,
             pp.credit_price DESC
    """
)

CONTEST_MEMBER_POINTS_QUERY = text(
    "SELECT total_points, rank FROM dream11.contest_members WHERE contest_id = :contest_id AND user_id = :user_id"
)

# USER_TEAM_QUERY's counterpart for a FINALIZED contest. The whole point
# is what it does NOT contain: no join to ml.player_gw_stats, and no raw
# stat columns for calculate_dream11_points to run on. A finished
# contest's result is frozen, so its per-player points come from
# dream11.team_players' stored final_points/final_minutes, written by the
# scoring pass that finalized it.
#
# Keeping this as a separate statement rather than adding a CASE to
# USER_TEAM_QUERY is deliberate: "the finalized read does not touch
# player_gw_stats" is a property you can verify by reading the SQL, and a
# single query with a conditional join would make it something you'd have
# to reason about instead.
FINALIZED_USER_TEAM_QUERY = text(
    """
    SELECT te.id AS team_id, te.submitted_at,
           p.fpl_id, p.web_name, p.position, t.short_name AS club,
           tp.is_captain, tp.is_vice_captain,
           pp.credit_price,
           COALESCE(tp.final_points, 0) AS points,
           COALESCE(tp.final_minutes, 0) AS minutes
    FROM dream11.teams te
    JOIN dream11.team_players tp ON tp.team_id = te.id
    JOIN ml.players p ON p.id = tp.player_id
    JOIN ml.teams t ON t.id = p.team_id
    LEFT JOIN dream11.player_prices pp
           ON pp.contest_id = te.contest_id AND pp.player_id = tp.player_id
    WHERE te.contest_id = :contest_id AND te.user_id = :user_id
    ORDER BY CASE p.position WHEN 'GK' THEN 0 WHEN 'DEF' THEN 1 WHEN 'MID' THEN 2 ELSE 3 END,
             pp.credit_price DESC
    """
)


def _generate_code() -> str:
    return "".join(random.choices(string.ascii_uppercase + string.digits, k=CODE_LENGTH))


def _schedule_scoring_polls(contest_id: int, fixture_id: int, kickoff_time) -> None:
    """Queue this contest's two scoring checkpoints. Runs as a FastAPI
    BackgroundTask -- i.e. AFTER the response has been sent -- because a
    broker outage makes .apply_async() take many seconds even with the
    bounded retry policy Worker/celery_app.py sets, and the caller is an
    HTTP request that has already committed the contest to the database.
    Nothing here can affect what the client received, so every failure is
    logged and swallowed rather than raised."""
    try:
        from Worker.tasks import poll_and_score_dream11  # local import -- see module docstring

        if kickoff_time is None:
            logger.warning(
                "create_contest: contest_id=%s fixture_id=%s has no kickoff_time yet -- skipping automatic poll scheduling",
                contest_id, fixture_id,
            )
            return

        poll_and_score_dream11.apply_async(
            args=[fixture_id, contest_id, "halftime"], eta=kickoff_time + timedelta(minutes=50)
        )
        poll_and_score_dream11.apply_async(
            args=[fixture_id, contest_id, "fulltime"], eta=kickoff_time + timedelta(minutes=115)
        )
    except Exception as e:
        # Best-effort: scheduling failure (e.g. broker unreachable) must
        # never undo an already-committed contest creation.
        logger.error(
            "create_contest: failed to schedule automatic polling for contest_id=%s: %s: %s",
            contest_id, type(e).__name__, e,
        )


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


def _is_duplicate_team(exc: SQLAlchemyError) -> bool:
    return isinstance(exc, IntegrityError) and DUPLICATE_TEAM_CONSTRAINT in str(exc)


# NOTE on all three request models below: user_id is deliberately ABSENT.
# The creator/joiner/submitter is the token's owner, never a body field.
# Pydantic ignores unknown fields, so a client still sending user_id gets
# the same behaviour it would if it had sent its own id -- the value is
# read by nothing.
class CreateContestRequest(BaseModel):
    fixture_id: int
    name: str
    max_members: int = DEFAULT_MAX_MEMBERS


class CreateContestResponse(BaseModel):
    contest_id: int
    code: str
    fixture_id: int
    name: str
    max_members: int
    pool_size: int


class JoinContestRequest(BaseModel):
    code: str


class JoinContestResponse(BaseModel):
    contest_id: int
    user_id: int
    name: str


class SubmitTeamRequest(BaseModel):
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


class ContestSummaryResponse(BaseModel):
    contest_id: int
    code: str
    name: str
    max_members: int
    member_count: int
    is_locked: bool
    # is_locked means "kicked off"; it never goes back to FALSE, so on its
    # own it cannot distinguish a match in progress from one that ended
    # months ago. is_finalized is the settled state -- the result is frozen
    # and will not change again.
    is_finalized: bool = False
    created_by: int
    created_at: datetime | None = None
    fixture_id: int
    season: str
    gameweek: int
    kickoff_time: datetime | None = None
    finished: bool
    home_team: str
    away_team: str
    # The rule strip on the match-detail screen reads "11 players · 100 credits
    # · Captain 2x, Vice 1.5x", so all four numbers ship with the contest
    # rather than being duplicated as UI constants that can drift from
    # dream11_scoring.py's actual weighting.
    budget_cap: float = DREAM11_BUDGET_CAP
    team_size: int = DREAM11_TEAM_SIZE
    max_players_per_club: int = MAX_PLAYERS_PER_CLUB
    captain_multiplier: float = CAPTAIN_MULTIPLIER
    vice_captain_multiplier: float = VICE_CAPTAIN_MULTIPLIER
    # All four describe the CALLER, and stay at their empty defaults when
    # the caller isn't a member of this contest.
    user_is_member: bool = False
    user_has_team: bool = False
    user_total_points: int = 0
    user_rank: int = 0


class PoolPlayerResponse(BaseModel):
    player_id: int  # fpl_id, matching every other endpoint's convention
    name: str
    position: str
    club: str
    is_home_team: bool
    credit_price: float
    status: str
    # Mean total_points over the player's last <=5 completed gameweeks
    # before this contest's fixture -- the same rolling average
    # credit_price was priced from, frozen at contest creation. None means
    # no prior data (the same case _compute_prices reads as "price at the
    # floor"), not zero -- a manager should be able to tell "never played"
    # apart from "played and scored nothing."
    rolling_points: float | None = None


class LeaderboardRowResponse(BaseModel):
    user_id: int
    username: str | None
    team_name: str | None
    total_points: int
    rank: int
    has_submitted_team: bool


class ContestLeaderboardResponse(BaseModel):
    contest: ContestSummaryResponse
    rows: list[LeaderboardRowResponse]


class UserTeamPlayerResponse(BaseModel):
    player_id: int
    name: str
    position: str
    club: str
    credit_price: float
    is_captain: bool
    is_vice_captain: bool
    minutes: int
    points: int  # this player's own Dream11 points, before any C/VC multiplier


class UserTeamResponse(BaseModel):
    team_id: int
    contest_id: int
    user_id: int
    submitted_at: datetime | None = None
    total_credit_cost: float
    players: list[UserTeamPlayerResponse]
    # TRUE once the match is over and the result has been frozen -- see
    # dream11_scoring.finalize_dream11_contest. Clients should render a
    # finalized team as a settled result rather than a live one; before
    # this field existed the UI had no way to tell a contest that ended
    # months ago from one that kicked off a minute ago.
    is_finalized: bool = False
    # Recomputed from current stats on every call while the contest is
    # live, so it always agrees with the per-player breakdown above. Once
    # is_finalized is TRUE this is the FROZEN total instead, identical to
    # contest_total_points and never recomputed again -- the field keeps
    # its name so the response shape does not change mid-contest.
    live_total_points: int
    captain_bonus: float
    vice_captain_bonus: float
    # What the last scoring checkpoint actually wrote to contest_members.
    # Equals live_total_points once a checkpoint has run and no stats have
    # changed since; 0 before the first checkpoint.
    contest_total_points: int
    contest_rank: int


def _contest_summary(row) -> ContestSummaryResponse:
    return ContestSummaryResponse(
        contest_id=row.contest_id,
        code=row.code,
        name=row.name,
        max_members=row.max_members,
        member_count=int(row.member_count or 0),
        is_locked=row.is_locked,
        created_by=row.created_by,
        is_finalized=row.finalized_at is not None,
        created_at=row.created_at,
        fixture_id=row.fixture_id,
        season=row.season,
        gameweek=row.gameweek,
        kickoff_time=row.kickoff_time,
        finished=row.finished,
        home_team=row.home_team,
        away_team=row.away_team,
        user_is_member=bool(row.user_is_member),
        user_has_team=bool(row.user_has_team),
        user_total_points=int(row.user_total_points or 0),
        user_rank=int(row.user_rank or 0),
    )


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
            "team_id": row.team_id,
            "club": row.club,
        }
        for pid, row in resolvable
        if row.internal_id in pool_prices
    ]

    # Real Dream11 caps how many players may come from one side, so a team
    # can't just be the stronger club's entire XI. Counted on team_id (always
    # present for pool players -- POOL_QUERY selects on it) and reported by
    # short_name, which is what the UI shows.
    club_counts = Counter(p["team_id"] for p in resolved_players if p["team_id"] is not None)
    club_names = {p["team_id"]: p["club"] for p in resolved_players}
    for team_id, count in sorted(club_counts.items(), key=lambda pair: -pair[1]):
        if count > MAX_PLAYERS_PER_CLUB:
            label = club_names.get(team_id) or f"team_id {team_id}"
            errors.append(
                f"at most {MAX_PLAYERS_PER_CLUB} players from one club, got {count} from {label}"
            )

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


# ------------------------------------------------------------------ reads
#
# These are GETs on resources, so a missing contest is a 404 here --
# matching leagues.py's GET /leagues/{league_id}/table, not the 422 the
# POST handlers below return (there a bad contest_id is one validation
# failure among several collected into one detail list).

@router.get("/dream11/contests", response_model=list[ContestSummaryResponse])
def get_user_contests(
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ContestSummaryResponse]:
    """The caller's own contests. Scoped by the token, never by a parameter --
    a user_id query param here would have let anyone enumerate any user's
    contests, codes included."""
    user_id = current_user.id
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(USER_CONTESTS_QUERY, {"user_id": user_id}).all()
    return [_contest_summary(row) for row in rows]


@router.get("/dream11/fixtures/{fixture_id}/contests", response_model=list[ContestSummaryResponse])
def get_fixture_contests(
    fixture_id: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[ContestSummaryResponse]:
    """The caller's contests on one fixture -- the match-detail counterpart to
    GET /dream11/contests, and scoped the same way. The membership scoping
    is not optional: without it this would be "every contest on this match",
    which isn't a thing the code-join model can safely expose. See
    FIXTURE_CONTESTS_QUERY."""
    user_id = current_user.id
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            FIXTURE_CONTESTS_QUERY, {"fixture_id": fixture_id, "user_id": user_id}
        ).all()
    return [_contest_summary(row) for row in rows]


@router.get("/dream11/contests/{contest_id}", response_model=ContestSummaryResponse)
def get_contest(
    contest_id: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> ContestSummaryResponse:
    """One contest, with the caller's own membership flags on it.

    The contest itself is the same for everyone; the user_* fields describe
    the CALLER, which is why they come from the token. They used to come
    from an optional user_id query param -- viewing these flags "as" another
    user was never a use case, only the last place on this router where
    identity could be named by the client."""
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(
            CONTEST_DETAIL_QUERY, {"contest_id": contest_id, "user_id": current_user.id}
        ).first()

    if row is None:
        raise HTTPException(status_code=404, detail="contest not found")
    return _contest_summary(row)


@router.get("/dream11/contests/{contest_id}/players", response_model=list[PoolPlayerResponse])
def get_contest_pool(contest_id: int) -> list[PoolPlayerResponse]:
    """This contest's full player pool with its frozen credit prices --
    everything the team builder needs in one call. Not gated on membership
    or the lock: prices are fixed at creation and identical for everyone,
    so there is nothing here to leak or to go stale."""
    engine = get_engine()
    with engine.connect() as conn:
        exists = conn.execute(text("SELECT 1 FROM dream11.contests WHERE id = :cid"), {"cid": contest_id}).first()
        if exists is None:
            raise HTTPException(status_code=404, detail="contest not found")
        rows = conn.execute(CONTEST_POOL_QUERY, {"contest_id": contest_id}).all()

    return [
        PoolPlayerResponse(
            player_id=row.fpl_id,
            name=row.web_name,
            position=row.position,
            club=row.club,
            is_home_team=row.is_home_team,
            credit_price=float(row.credit_price),
            status=row.status,
            rolling_points=round(row.rolling_points, 1) if row.rolling_points is not None else None,
        )
        for row in rows
    ]


@router.get("/dream11/contests/{contest_id}/leaderboard", response_model=ContestLeaderboardResponse)
def get_contest_leaderboard(
    contest_id: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> ContestLeaderboardResponse:
    """Every member's standing, plus the contest summary carrying the
    CALLER's own membership flags -- same rule as get_contest above. The
    rows are the whole table either way; only the embedded summary is
    caller-specific."""
    engine = get_engine()
    with engine.connect() as conn:
        contest_row = conn.execute(
            CONTEST_DETAIL_QUERY, {"contest_id": contest_id, "user_id": current_user.id}
        ).first()
        if contest_row is None:
            raise HTTPException(status_code=404, detail="contest not found")
        rows = conn.execute(CONTEST_LEADERBOARD_QUERY, {"contest_id": contest_id}).all()

    return ContestLeaderboardResponse(
        contest=_contest_summary(contest_row),
        rows=[
            LeaderboardRowResponse(
                user_id=row.user_id,
                username=row.username,
                team_name=row.team_name,
                total_points=int(row.total_points or 0),
                rank=int(row.rank or 0),
                has_submitted_team=row.has_submitted_team,
            )
            for row in rows
        ],
    )


@router.get("/dream11/contests/{contest_id}/team", response_model=UserTeamResponse)
def get_user_team(
    contest_id: int,
    user_id: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> UserTeamResponse:
    """One user's submitted team, with each player's Dream11 points
    recomputed from current stats via the same calculate_dream11_points
    the scoring task uses -- so a live contest's per-player breakdown and
    its total are always internally consistent, and there is exactly one
    implementation of the point weightings in the codebase.

    Before kickoff every stat is 0 (no player_gw_stats rows), which comes
    back as a legitimate all-zero team rather than an error.

    OPPONENT VISIBILITY. user_id names WHOSE team to fetch; current_user is
    WHO is asking, and unlike user_id it cannot be spoofed. Your own team is
    always readable. Someone else's is readable only once the contest is
    locked (i.e. the fixture has kicked off and picks are final) and only by
    a fellow member -- before that, an opponent's XI and captain is exactly
    the information that decides the contest.

    This is the one endpoint on this router that still TAKES a user_id, and
    deliberately so: everywhere else that parameter named the caller's own
    data, so it was pure spoofing surface and has been removed in favour of
    current_user.id. Here it names a DIFFERENT user's data on purpose, which
    is exactly why the rule above needs a credential behind it -- without
    one a snooper would just pass the victim's id as their own.
    """
    engine = get_engine()
    with engine.connect() as conn:
        contest_row = conn.execute(CONTEST_SEASON_AND_GW_QUERY, {"contest_id": contest_id}).first()
        if contest_row is None:
            raise HTTPException(status_code=404, detail="contest not found")

        if user_id != current_user.id:
            if not contest_row.is_locked:
                raise HTTPException(
                    status_code=403,
                    detail="opponents' teams are hidden until the contest locks at kickoff",
                )
            viewer_is_member = conn.execute(
                IS_CONTEST_MEMBER_QUERY, {"contest_id": contest_id, "user_id": current_user.id}
            ).first()
            if viewer_is_member is None:
                raise HTTPException(
                    status_code=403,
                    detail="only contest members can view another member's team",
                )

        is_finalized = contest_row.finalized_at is not None

        if is_finalized:
            # Frozen result: read the stored breakdown, and do not go near
            # ml.player_gw_stats or calculate_dream11_points at all.
            rows = conn.execute(
                FINALIZED_USER_TEAM_QUERY, {"contest_id": contest_id, "user_id": user_id}
            ).all()
        else:
            rows = conn.execute(
                USER_TEAM_QUERY,
                {
                    "contest_id": contest_id,
                    "user_id": user_id,
                    "season": contest_row.season,
                    "gameweek": contest_row.gameweek,
                    "fixture_id": contest_row.fixture_id,
                },
            ).all()
        if not rows:
            raise HTTPException(status_code=404, detail="user has not submitted a team for this contest")

        member_row = conn.execute(
            CONTEST_MEMBER_POINTS_QUERY, {"contest_id": contest_id, "user_id": user_id}
        ).first()

    contest_total = int(member_row.total_points or 0) if member_row else 0

    if is_finalized:
        # Every number below comes from storage. The bonuses are derived
        # arithmetic over the stored per-player points rather than a
        # recompute from raw stats, so they stay consistent with the frozen
        # breakdown by construction -- and total_points is the stored team
        # total, NOT a re-sum, so it can never drift from the leaderboard.
        points_by_fpl_id = {row.fpl_id: int(row.points) for row in rows}
    else:
        points_by_fpl_id = {row.fpl_id: calculate_dream11_points(row, row.position) for row in rows}

    captain_row = next((r for r in rows if r.is_captain), None)
    vice_row = next((r for r in rows if r.is_vice_captain), None)
    captain_bonus = points_by_fpl_id[captain_row.fpl_id] * (CAPTAIN_MULTIPLIER - 1.0) if captain_row else 0.0
    vice_bonus = points_by_fpl_id[vice_row.fpl_id] * (VICE_CAPTAIN_MULTIPLIER - 1.0) if vice_row else 0.0

    return UserTeamResponse(
        team_id=rows[0].team_id,
        contest_id=contest_id,
        user_id=user_id,
        submitted_at=rows[0].submitted_at,
        # credit_price is LEFT JOINed, so a price row deleted out from under
        # a submitted team reads as 0.0 rather than crashing the response.
        total_credit_cost=sum(float(r.credit_price or 0) for r in rows),
        players=[
            UserTeamPlayerResponse(
                player_id=row.fpl_id,
                name=row.web_name,
                position=row.position,
                club=row.club,
                credit_price=float(row.credit_price or 0),
                is_captain=row.is_captain,
                is_vice_captain=row.is_vice_captain,
                minutes=row.minutes,
                points=points_by_fpl_id[row.fpl_id],
            )
            for row in rows
        ],
        is_finalized=is_finalized,
        live_total_points=(
            contest_total
            if is_finalized
            else round(sum(points_by_fpl_id.values()) + captain_bonus + vice_bonus)
        ),
        captain_bonus=captain_bonus,
        vice_captain_bonus=vice_bonus,
        contest_total_points=contest_total,
        contest_rank=int(member_row.rank or 0) if member_row else 0,
    )


# ------------------------------------------------------------------ writes

@router.post("/dream11/contests", response_model=CreateContestResponse)
def create_contest(
    req: CreateContestRequest,
    background_tasks: BackgroundTasks,
    current_user: CurrentUser = Depends(get_current_user),
) -> CreateContestResponse:
    user_id = current_user.id
    errors: list[str] = []
    if not (MIN_MAX_MEMBERS <= req.max_members <= MAX_MAX_MEMBERS):
        errors.append(
            f"max_members must be between {MIN_MAX_MEMBERS} and {MAX_MAX_MEMBERS}, got {req.max_members}"
        )

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
                        "created_by": user_id,
                        "max_members": req.max_members,
                    },
                ).scalar()
                conn.execute(
                    INSERT_PRICES_STMT,
                    [
                        {
                            "contest_id": contest_id,
                            "player_id": pid,
                            "credit_price": prices[pid],
                            # Frozen alongside credit_price, same "no prior
                            # data -> NULL" reading _compute_prices already
                            # uses for the floor price -- see migration
                            # c2f6a83e91d4.
                            "rolling_points": rolling_values.get(pid),
                        }
                        for pid in pool_internal_ids
                    ],
                )
                conn.execute(INSERT_CONTEST_MEMBER_STMT, {"contest_id": contest_id, "user_id": user_id})
            break
        except IntegrityError:
            logger.warning("create_contest: code collision on attempt %d/%d (%s), retrying", attempt, MAX_CODE_ATTEMPTS, code)
    else:
        logger.error("create_contest: exhausted %d code-generation attempts", MAX_CODE_ATTEMPTS)
        raise HTTPException(status_code=500, detail="Could not generate a unique contest code, please try again")

    background_tasks.add_task(_schedule_scoring_polls, contest_id, req.fixture_id, fixture_row.kickoff_time)

    return CreateContestResponse(
        contest_id=contest_id,
        code=code,
        fixture_id=req.fixture_id,
        name=req.name,
        max_members=req.max_members,
        pool_size=len(pool_internal_ids),
    )


@router.post("/dream11/contests/join", response_model=JoinContestResponse)
def join_contest(
    req: JoinContestRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> JoinContestResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        contest_row = conn.execute(CONTEST_BY_CODE_QUERY, {"code": req.code}).first()
        is_member = False
        member_count = 0
        if contest_row is not None:
            is_member = (
                conn.execute(IS_CONTEST_MEMBER_QUERY, {"contest_id": contest_row.contest_id, "user_id": user_id}).first()
                is not None
            )
            member_count = conn.execute(CONTEST_MEMBER_COUNT_QUERY, {"contest_id": contest_row.contest_id}).scalar()

    errors = _validate_join(contest_row, is_member, member_count)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    with engine.begin() as conn:
        conn.execute(INSERT_CONTEST_MEMBER_STMT, {"contest_id": contest_row.contest_id, "user_id": user_id})

    return JoinContestResponse(contest_id=contest_row.contest_id, user_id=user_id, name=contest_row.name)


@router.post("/dream11/contests/{contest_id}/team", response_model=SubmitTeamResponse)
def submit_team(
    contest_id: int,
    req: SubmitTeamRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> SubmitTeamResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        contest_season_row = conn.execute(CONTEST_SEASON_QUERY, {"contest_id": contest_id}).first()

    if contest_season_row is None:
        raise HTTPException(status_code=422, detail=[f"contest_id {contest_id} does not exist"])

    with engine.connect() as conn:
        is_member = (
            conn.execute(IS_CONTEST_MEMBER_QUERY, {"contest_id": contest_id, "user_id": user_id}).first() is not None
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
            team_id = conn.execute(INSERT_TEAM_STMT, {"contest_id": contest_id, "user_id": user_id}).scalar()
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
        if _is_duplicate_team(e):
            # uq_d11_teams_contest_user -- one team per user per contest, and
            # this endpoint is INSERT-only (no edit path). Without this branch
            # a second submission fell through to the generic 500 below, which
            # reads as a server fault rather than the ordinary user action it
            # is. Matched on the constraint NAME, not on message prose, so
            # unlike LOCK_ERROR_SUBSTRING above this can't break on rewording.
            raise HTTPException(
                status_code=422, detail=["user has already submitted a team for this contest"]
            ) from e
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return SubmitTeamResponse(
        team_id=team_id,
        contest_id=contest_id,
        user_id=user_id,
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
