"""
players.py — FastAPI endpoint for listing all players for a season.

Read-only, no pagination -- a season's player list is a few hundred rows,
small enough to return in one response and let the client filter/search
locally (matches the Squad Selection page's search/filter UI).

cost_start is stored x10-scaled in ml.players (matches squad_selection.py's
BUDGET_CAP convention); this endpoint applies the API-boundary conversion
back to a real decimal (e.g. 85 -> 8.5) so the frontend never has to know
about the x10 scale.

status is returned but not used to filter or block anything here -- same
"no availability rule at this layer" stance as squad_selection.py -- purely
so the frontend can show an injured/doubtful badge if it wants to.

season_points sums total_points across every ingested gameweek this season
(independent of :gameweek, always computed) -- Squad Selection reads this
to show "how has this player done all season", separately from `points`,
which stays exactly what it always was: one specific gameweek's score, the
number Transfers relies on to compare targets by recent form. A row only
ever exists in ml.player_gw_stats once that gameweek has actually been
polled, so summing every row already ingested is summing every gameweek
that's actually finished -- no separate "is it complete" check needed.

next_opponent/next_opponent_is_home are optional (default None) -- for the
Transfers page's player browsing, resolved per-team as the earliest
unfinished fixture in ml.fixtures for that team. If a team has no
unfinished fixture yet (e.g. next gameweek's fixtures haven't been
ingested), both fields stay None rather than erroring -- an incomplete
fixture calendar is an expected, temporary state, not a bug.
"""

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy import text

from Shared.db_utils import get_engine

router = APIRouter()

PLAYERS_QUERY = text(
    """
    SELECT p.fpl_id, p.web_name, p.position, p.team_id, t.short_name,
           COALESCE(p.now_cost, p.cost_start) AS price,
           p.status, COALESCE(pgs.total_points, 0) AS points,
           COALESCE(season_totals.season_points, 0) AS season_points
    FROM ml.players p
    JOIN ml.teams t ON t.id = p.team_id
    LEFT JOIN ml.player_gw_stats pgs
        ON pgs.player_id = p.id
       AND pgs.season = p.season
       AND pgs.gameweek = :gameweek
    LEFT JOIN (
        SELECT player_id, SUM(total_points) AS season_points
        FROM ml.player_gw_stats
        WHERE season = :season
        GROUP BY player_id
    ) season_totals ON season_totals.player_id = p.id
    WHERE p.season = :season
    ORDER BY p.fpl_id
    """
)

# One row per team_id -- DISTINCT ON picks the earliest-kickoff unfinished
# fixture whether that team is home or away, unioning both sides first so
# "earliest" is computed across both roles together, not per-role.
NEXT_FIXTURE_QUERY = text(
    """
    SELECT DISTINCT ON (team_id) team_id, opponent_short_name, is_home
    FROM (
        SELECT f.home_team_id AS team_id, away.short_name AS opponent_short_name,
               TRUE AS is_home, f.kickoff_time
        FROM ml.fixtures f
        JOIN ml.teams away ON away.id = f.away_team_id
        WHERE f.season = :season AND f.finished = FALSE
        UNION ALL
        SELECT f.away_team_id AS team_id, home.short_name AS opponent_short_name,
               FALSE AS is_home, f.kickoff_time
        FROM ml.fixtures f
        JOIN ml.teams home ON home.id = f.home_team_id
        WHERE f.season = :season AND f.finished = FALSE
    ) upcoming
    ORDER BY team_id, kickoff_time ASC
    """
)


class PlayerOut(BaseModel):
    id: int
    name: str
    position: str
    club: str
    price: float
    status: str
    points: int = 0
    season_points: int = 0
    next_opponent: str | None = None
    next_opponent_is_home: bool | None = None


@router.get("/players", response_model=list[PlayerOut])
def get_players(season: str, gameweek: int | None = None) -> list[PlayerOut]:
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(PLAYERS_QUERY, {"season": season, "gameweek": gameweek}).all()
        next_fixture_rows = conn.execute(NEXT_FIXTURE_QUERY, {"season": season}).all()

    next_fixture_by_team = {
        row.team_id: (row.opponent_short_name, row.is_home) for row in next_fixture_rows
    }

    result = []
    for row in rows:
        opponent, is_home = next_fixture_by_team.get(row.team_id, (None, None))
        result.append(
            PlayerOut(
                id=row.fpl_id,
                name=row.web_name,
                position=row.position,
                club=row.short_name,
                price=row.price / 10,
                status=row.status,
                points=row.points,
                season_points=row.season_points,
                next_opponent=opponent,
                next_opponent_is_home=is_home,
            )
        )
    return result
