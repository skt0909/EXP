"""
fixtures.py — FastAPI endpoint for listing a season's matches.

Fills a gap the frontend had no way around: nothing in the API exposed
ml.fixtures at all, so a client could never discover a fixture_id --
which POST /dream11/contests requires to create a contest. GET /players
leaked a little of it (next_opponent per team) but there was no way to
list matches, their kickoff times, or which of them already have
contests running.

Read-only, no pagination -- a Premier League season is 380 fixtures,
and a single gameweek is 10, so the client filters locally the same way
players.py expects it to.

The contest_count / user_* columns are Dream11 data living on an
otherwise game-agnostic endpoint. That's deliberate: the Contests-mode
match list renders "in 2 contests" and a "Your Performance -- Rank 3rd
of 10 -- 42 pts" line beside every fixture, and serving those here costs
indexed subqueries (idx_d11_contests_fixture) against one extra
round-trip per fixture from the client. contest_count is a plain total
-- notably NOT the contests' join codes, which would let anyone into a
private contest.

home_score/away_score come straight from ml.fixtures and are NULL until
the match has a result. There is deliberately no match-minute field:
nothing in the schema stores a clock, so the "68'" on the live match
card has no source yet.

upcoming_only is opt-in rather than the default. Defaulting it to TRUE
reads as the safer choice but makes the endpoint return nothing at all
against a database whose fixtures are all in the past (which is exactly
the state of the dev database, every ingested fixture being GW1-4 of
2025-26), so the natural first call would look broken rather than
empty-by-design.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from Shared.db_utils import get_engine
from Shared.deadlines import _DEADLINE_EXPR
from Data.auth import CurrentUser, get_current_user

router = APIRouter()

# How long after kickoff a match is still treated as in-play. 90 minutes plus
# halftime plus stoppage plus a margin.
#
# This exists because ml.fixtures.finished cannot be trusted to flip promptly.
# FPL's own feed distinguishes `finished` from `finished_provisional` -- a match
# that has ended but whose bonus points aren't confirmed is finished_provisional
# =True, finished=False, and can sit that way for a while -- and this project
# doesn't ingest that column at all (Data/fpl_ingest.py's
# ingest_fixtures refuses a gameweek until its fixtures report finished). Left
# to `finished` alone, a match that ended an hour ago would still be advertised
# as Live. Past this window the result is treated as final regardless of the
# flag: a fixture that kicked off well over two hours ago is not still being
# played, whatever the bonus-point state says.
LIVE_WINDOW_MINUTES = 130

STATUS_UPCOMING = "upcoming"
STATUS_LIVE = "live"
STATUS_COMPLETED = "completed"

# Casts are explicit because psycopg2 sends a bare NULL with no type for an
# unset optional param, and Postgres can't infer one for `:gameweek IS NULL`.
FIXTURES_QUERY = text(
    """
    SELECT f.id AS fixture_id, f.season, f.gameweek, f.kickoff_time, f.finished,
           f.home_score, f.away_score,
           (f.kickoff_time IS NOT NULL AND f.kickoff_time <= NOW()) AS has_started,
           -- Derived here rather than left to each client: the live-window rule
           -- below is a judgement call about untrustworthy data (see
           -- LIVE_WINDOW_MINUTES), and it should be made once, server-side,
           -- against the database clock.
           CASE
               WHEN f.finished THEN 'completed'
               WHEN f.kickoff_time IS NULL OR f.kickoff_time > NOW() THEN 'upcoming'
               WHEN f.kickoff_time + (CAST(:live_window_minutes AS INTEGER) * INTERVAL '1 minute') <= NOW()
                   THEN 'completed'
               ELSE 'live'
           END AS status,
           h.id AS home_team_id, h.name AS home_team_name, h.short_name AS home_team,
           a.id AS away_team_id, a.name AS away_team_name, a.short_name AS away_team,
           (SELECT COUNT(*) FROM dream11.contests c WHERE c.fixture_id = f.id) AS contest_count,
           (SELECT COUNT(*)
              FROM dream11.contests c2
              JOIN dream11.contest_members m ON m.contest_id = c2.id
             WHERE c2.fixture_id = f.id AND m.user_id = CAST(:user_id AS INTEGER)) AS user_contest_count,
           perf.contest_id AS user_contest_id,
           perf.total_points AS user_points,
           perf.rank AS user_rank,
           perf.contest_size AS user_contest_size
    FROM ml.fixtures f
    JOIN ml.teams h ON h.id = f.home_team_id
    JOIN ml.teams a ON a.id = f.away_team_id
    -- "Your Performance -- Rank 3rd of 10 -- 42 pts" is ONE line per match, but
    -- a user can hold teams in several contests on the same fixture. Pick their
    -- best-ranked one, newest as the tiebreak, and expose which contest it was
    -- so the card can link straight to it. rank = 0 means "not scored yet" (the
    -- contest_members default), so those sort last -- same convention as
    -- dream11.py's CONTEST_LEADERBOARD_QUERY.
    LEFT JOIN LATERAL (
        SELECT c3.id AS contest_id, m3.total_points, m3.rank,
               (SELECT COUNT(*) FROM dream11.contest_members m4 WHERE m4.contest_id = c3.id) AS contest_size
        FROM dream11.contests c3
        JOIN dream11.contest_members m3 ON m3.contest_id = c3.id
        WHERE c3.fixture_id = f.id AND m3.user_id = CAST(:user_id AS INTEGER)
        ORDER BY (m3.rank = 0), m3.rank ASC, c3.created_at DESC NULLS LAST
        LIMIT 1
    ) perf ON TRUE
    WHERE f.season = :season
      AND (CAST(:gameweek AS INTEGER) IS NULL OR f.gameweek = CAST(:gameweek AS INTEGER))
      AND (NOT CAST(:upcoming_only AS BOOLEAN)
           OR f.kickoff_time IS NULL OR f.kickoff_time > NOW())
    ORDER BY f.kickoff_time ASC NULLS LAST, f.id
    """
)


class FixtureResponse(BaseModel):
    fixture_id: int
    season: str
    gameweek: int
    kickoff_time: datetime | None = None
    finished: bool
    # Distinct from `finished`: a match that has kicked off but isn't over yet
    # is started-and-unfinished, and that's precisely the window in which
    # Dream11 contests are locked but still being scored.
    has_started: bool
    # "upcoming" | "live" | "completed" -- the Live Now / Upcoming / Completed
    # grouping on the match list, decided server-side. Not a plain restatement
    # of `finished`; see LIVE_WINDOW_MINUTES.
    status: str
    home_team_id: int
    home_team: str
    home_team_name: str
    away_team_id: int
    away_team: str
    away_team_name: str
    # None until the match has a result -- an unplayed fixture genuinely has no
    # score, which is different from 0-0.
    home_score: int | None = None
    away_score: int | None = None
    contest_count: int = 0
    # All of the user_* fields stay empty when the request carries no user_id.
    user_contest_count: int = 0
    # The user's best-ranked contest on this fixture, for the "Your Performance
    # -- Rank 3rd of 10 -- 42 pts" line. user_rank 0 means not scored yet.
    user_contest_id: int | None = None
    user_points: int = 0
    user_rank: int = 0
    user_contest_size: int = 0


@router.get("/fixtures", response_model=list[FixtureResponse])
def get_fixtures(
    season: str,
    gameweek: int | None = None,
    upcoming_only: bool = False,
    current_user: CurrentUser = Depends(get_current_user),
) -> list[FixtureResponse]:
    user_id = current_user.id
    engine = get_engine()
    with engine.connect() as conn:
        rows = conn.execute(
            FIXTURES_QUERY,
            {
                "season": season,
                "gameweek": gameweek,
                "upcoming_only": upcoming_only,
                "user_id": user_id,
                "live_window_minutes": LIVE_WINDOW_MINUTES,
            },
        ).all()

    return [
        FixtureResponse(
            fixture_id=row.fixture_id,
            season=row.season,
            gameweek=row.gameweek,
            kickoff_time=row.kickoff_time,
            finished=row.finished,
            has_started=row.has_started,
            status=row.status,
            home_team_id=row.home_team_id,
            home_team=row.home_team,
            home_team_name=row.home_team_name,
            away_team_id=row.away_team_id,
            away_team=row.away_team,
            away_team_name=row.away_team_name,
            home_score=row.home_score,
            away_score=row.away_score,
            contest_count=int(row.contest_count or 0),
            user_contest_count=int(row.user_contest_count or 0),
            user_contest_id=row.user_contest_id,
            user_points=int(row.user_points or 0),
            user_rank=int(row.user_rank or 0),
            user_contest_size=int(row.user_contest_size or 0),
        )
        for row in rows
    ]


# Reuses deadlines.py's exact deadline expression rather than restating "90
# minutes before kickoff" a third time -- see that module's docstring on why
# DEADLINE_OFFSET_MINUTES has exactly one source of truth.
#
# REAL SEASONS ONLY. Without the season filter this ranked deadlines across
# every season in ml.fixtures, and the simulation seasons (SIM38OK, SIM38TST,
# SIMSMOKE, SIMGWE2E) carry real timestamps -- so a simulation fixture could
# win and every page in the app would show a simulation gameweek. Proven by
# test_a_simulation_season_never_wins_the_current_gameweek, which asserted the
# broken behaviour before this line existed. Same pattern as the scoring job's
# REAL_SEASON_RE; the braces are doubled because this is an f-string.
#
# THE RULE: the current gameweek is the earliest one NOT YET SCORED.
#
# It used to be "the gameweek whose deadline is soonest in the future", which
# moves the instant a deadline passes -- so from Saturday 11:30 onward, while
# gw8 was still being played and scored, every page in the app already showed
# gw9. Proven by test_a_locked_but_unscored_gameweek_is_still_current, which
# asserted the broken answer (9) before this rewrite. Under the scored rule a
# gameweek stays current through kickoff, through the 90 minutes, and through
# the scoring run -- it stops being current only when the scoring job records
# that it finished (gameweeks.scored_at, added in b4e1f37c920d).
#
# "Not scored" is a LEFT JOIN with scored_at IS NULL, so no row and a row with
# a NULL scored_at read identically -- the latter means "known about, not
# finished" and must not be mistaken for done.
#
# LATEST SEASON ONLY. ml.fixtures holds years of history, and none of those
# gameweeks were ever marked scored, because scored_at did not exist when they
# were played. Ranking unscored gameweeks across all seasons would therefore
# hand back the first gameweek of the oldest season in the database, forever.
# max(season) is well defined here because the season filter below admits only
# 'YYYY-YY', where lexical and chronological order coincide.
#
# REAL SEASONS ONLY. Without this filter the query ranked across every season
# in ml.fixtures, and the simulation seasons (SIM38OK, SIM38TST, SIMSMOKE,
# SIMGWE2E) carry real timestamps -- so a simulation fixture could win and
# every page would show a simulation gameweek. Proven by
# test_a_simulation_season_never_wins_the_current_gameweek. Same pattern as the
# scoring job's REAL_SEASON_RE; braces are doubled because this is an f-string.
#
# The deadline expression reuses deadlines.py's rather than restating "90
# minutes before kickoff" a third time -- see that module's docstring on why
# DEADLINE_OFFSET_MINUTES has exactly one source of truth.
#
# THE TWO DEADLINE TIERS ARE KEPT, below the scored rule, as the fallback for
# the cases where it selects nothing:
#   * no fixtures ingested yet for any real season -- the season-start edge
#     case. Nothing has ever been scored and there is nothing to rank, so all
#     three tiers are empty and the endpoint answers found=False;
#   * every gameweek of the latest season is scored -- end of season. Tier 3
#     returns the most recent past gameweek, which renders read-only through
#     the same locked-gameweek path Starting XI and Transfers already have
#     rather than falsely inviting an edit. Returning nothing here would
#     strand every page that reads this endpoint.
CURRENT_GAMEWEEK_QUERY = text(
    f"""
    WITH gw_deadlines AS (
        SELECT season, gameweek, {_DEADLINE_EXPR} AS deadline
        FROM ml.fixtures
        WHERE season ~ '^[0-9]{{4}}-[0-9]{{2}}$'
        GROUP BY season, gameweek
    ),
    latest_season AS (
        SELECT max(season) AS season FROM gw_deadlines
    )
    (SELECT d.season, d.gameweek, d.deadline
       FROM gw_deadlines d
       JOIN latest_season l ON l.season = d.season
       LEFT JOIN gameweeks g
              ON g.season = d.season AND g.gameweek = d.gameweek
      WHERE g.scored_at IS NULL
      ORDER BY d.gameweek ASC LIMIT 1)
    UNION ALL
    (SELECT season, gameweek, deadline FROM gw_deadlines
     WHERE deadline > NOW()
     ORDER BY deadline ASC LIMIT 1)
    UNION ALL
    (SELECT season, gameweek, deadline FROM gw_deadlines
     ORDER BY deadline DESC LIMIT 1)
    LIMIT 1
    """
)


class CurrentGameweekResponse(BaseModel):
    found: bool
    season: str | None = None
    gameweek: int | None = None
    deadline: datetime | None = None


@router.get("/gameweeks/current", response_model=CurrentGameweekResponse)
def get_current_gameweek(current_user: CurrentUser = Depends(get_current_user)) -> CurrentGameweekResponse:
    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(CURRENT_GAMEWEEK_QUERY).first()

    # found=False rather than a 404 when ml.fixtures is empty -- same
    # "unstarted state is not an error" stance as GET /gw_selection's
    # has_selection and GET /squad's empty list.
    if row is None:
        return CurrentGameweekResponse(found=False)

    return CurrentGameweekResponse(
        found=True, season=row.season, gameweek=row.gameweek, deadline=row.deadline
    )
