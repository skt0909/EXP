"""
team_dashboard.py — FastAPI read-only endpoint backing the frontend's
Squad/Dashboard page: GET /team?user_id=&season=&gameweek=.

Doubles as the app's "combined profile" view -- username/team_name are
included alongside the squad/score aggregation below rather than living
behind a separate endpoint, since this one already assembles everything
else a profile view would need per (user, season, gameweek). team_name
is a plain nullable users column with no write path yet (no user-
registration/profile-update endpoint exists in this app at all) -- None
here just means it was never set, same "not an error, unstarted state"
philosophy as has_lineup=False below.

Aggregates data that already exists across several tables/modules rather
than introducing any new scoring or ranking logic of its own:
- gw_scores (written by Results/scoring.py) for this gameweek's points,
  season_total, and the season_total-based rank/average computed here.
- starting_xi/gw_selections (written by Gameplay/starting_xi.py) for the
  picked lineup + bench, captain/vice-captain flags, and chip_used (to
  pick the captain multiplier -- 3x under triple_captain, 2x otherwise,
  same rule Results/scoring.py uses).
- user_squads/squad_players for team_value (sum of the active 15 squad's
  purchase_price) and bank (user_squads.budget_remaining).
- Shared.deadlines.resolve_gameweek_deadline for the deadline,
  reused rather than re-deriving MIN(kickoff_time) here.

"overall_rank" is this user's rank by season_total among every OTHER user
who also has a gw_scores row for the same (season, gameweek) -- i.e. rank
within this app's own user base, not a real/official FPL global rank
(this backend has no access to that). "gw_average" is the same-scoped
average of total_points across those same users. Both are None (not 0 or
a guess) when no gw_scores rows exist yet for that gameweek, since 0 would
misleadingly read as "you rank last."

If the user has no gw_selections/starting_xi rows for this gameweek
(hasn't set a lineup yet), has_lineup is False and starting_xi/bench come
back empty -- 200, not an error, same "not an error, just an unstarted
state" philosophy Context_assembler/main.py's /chat endpoint already uses
for the equivalent case.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
# Shared with scoring.py rather than redeclared here -- this module used to
# keep its own copies, and the two had to agree or the multiplier shown
# beside a score would contradict the score itself.
from Shared.rules import CAPTAIN_MULTIPLIER, TRIPLE_CAPTAIN_MULTIPLIER
from Shared.deadlines import resolve_gameweek_deadline
from Results.scoring import resolve_autosubs

logger = logging.getLogger(__name__)

router = APIRouter()


# LEFT JOIN semantics not needed -- user_id is queried directly against
# users' own PK, so a nonexistent user_id just yields no row (username/
# team_name come back None below), same forgiving behavior as every
# other "doesn't exist yet" case in this endpoint.
USER_QUERY = text("SELECT username, team_name FROM users WHERE id = :user_id")

GW_SELECTION_QUERY = text(
    "SELECT id AS gw_selection_id, chip_used FROM gw_selections "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek"
)

# total_points defaults to 0 via COALESCE when no player_gw_stats row
# exists yet (match not played/ingested) -- same partial/live-scoring
# convention Results/scoring.py uses for the same join.
#
# club (t.short_name) is joined in the same way Gameplay/squad_selection.py's
# own player-row query already does for GET /squad -- this query used to
# omit it entirely, which is why the Dashboard's pitch view rendered a
# fallback placeholder instead of a real jersey for every player: the
# frontend's PlayerJersey component resolves a club's jersey graphic from
# this exact short_name string (e.g. "ARS"), and had nothing to resolve
# against. No id-space translation is involved -- short_name is the same
# human-readable string in every mode's response (Dream11's queries select
# it the same way), never a raw numeric id.
STARTING_XI_ROWS_QUERY = text(
    """
    SELECT sx.position_slot, sx.is_captain, sx.is_vice_captain,
           mp.fpl_id AS player_id, mp.web_name, mp.position, t.short_name AS club,
           COALESCE(pgs.total_points, 0) AS points,
           COALESCE(pgs.minutes, 0) AS minutes
    FROM starting_xi sx
    JOIN ml.players mp ON mp.fpl_id = sx.player_id AND mp.season = :season
    JOIN ml.teams t ON t.id = mp.team_id
    LEFT JOIN ml.player_gw_stats pgs
        ON pgs.player_id = mp.id AND pgs.season = :season AND pgs.gameweek = :gameweek
    WHERE sx.gw_selection_id = :gw_selection_id
    ORDER BY sx.position_slot
    """
)

# raw_points/final_points/hit_deductions back the Dashboard's points-breakdown
# table. They are read straight from gw_scores (written by
# Results/scoring.py) rather than recomputed here -- scoring.py is the only
# place the captain rule lives, and raw_points there is deliberately the sum of
# the effective XI at each player's TRUE base value with the captain NOT
# doubled, so captain_bonus = final_points - raw_points cannot double-count.
GW_SCORE_QUERY = text(
    "SELECT raw_points, final_points, transfer_hits, hit_deductions, total_points, season_total "
    "FROM gw_scores WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek"
)

# Drives live_status. finished is the authoritative "match over" flag from
# Data; kickoff_time only tells us play should have started, which is
# why a gameweek with started-but-unfinished fixtures reads as "live" rather
# than "final".
GW_FIXTURE_STATUS_QUERY = text(
    """
    SELECT COUNT(*) AS total,
           COUNT(*) FILTER (WHERE finished) AS finished_count,
           COUNT(*) FILTER (WHERE kickoff_time IS NOT NULL AND kickoff_time <= now()) AS started_count
    FROM ml.fixtures
    WHERE season = :season AND gameweek = :gameweek
    """
)

GW_AVERAGE_QUERY = text(
    "SELECT AVG(total_points) FROM gw_scores WHERE season = :season AND gameweek = :gameweek"
)

# Ranked by season_total (this gameweek's cumulative total) among every
# user who has a gw_scores row for this exact gameweek -- see module
# docstring for why this is scoped to this app's users, not a real rank.
OVERALL_RANK_QUERY = text(
    """
    SELECT rank, total FROM (
        SELECT user_id,
               RANK() OVER (ORDER BY season_total DESC) AS rank,
               COUNT(*) OVER () AS total
        FROM gw_scores
        WHERE season = :season AND gameweek = :gameweek
    ) ranked
    WHERE user_id = :user_id
    """
)

TEAM_VALUE_AND_BANK_QUERY = text(
    """
    SELECT us.budget_remaining,
           COALESCE(SUM(sp.purchase_price) FILTER (WHERE sp.is_active), 0) AS team_value_tenths
    FROM user_squads us
    LEFT JOIN squad_players sp ON sp.user_squad_id = us.id
    WHERE us.user_id = :user_id AND us.season = :season
    GROUP BY us.id, us.budget_remaining
    """
)

# Results/scoring.py writes one of these rows at the same moment it writes
# gw_scores (same transaction) -- see that module's UPSERT_GW_FINANCE_STMT.
# Only ever consulted for a 'final' gameweek (see get_team_dashboard below):
# the live/current gameweek keeps reading TEAM_VALUE_AND_BANK_QUERY exactly
# as before, unchanged, since a not-yet-scored gameweek has no snapshot row
# to read anyway.
GAMEWEEK_FINANCE_SNAPSHOT_QUERY = text(
    "SELECT bank, team_value FROM user_gameweek_finance "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek"
)


class PlayerLine(BaseModel):
    player_id: int
    name: str
    position: str
    # Club short-name (e.g. "ARS") -- what the frontend's PlayerJersey
    # component resolves a real kit graphic from. Same field GET /squad's
    # CurrentSquadPlayerOut already carries for the same purpose.
    club: str
    points: int
    is_captain: bool
    is_vice_captain: bool
    # Autosub outcome for THIS gameweek, recomputed for display via
    # scoring.resolve_autosubs (the swaps aren't persisted anywhere -- scoring
    # derives them in memory and only stores the resulting totals). Reusing
    # that function rather than reimplementing the rules means the badge can't
    # disagree with the points it's explaining. Both stay False before any
    # match is played and under bench_boost, which skips autosub entirely.
    is_autosubbed_in: bool = False
    is_autosubbed_out: bool = False


class Lineup(BaseModel):
    GK: list[PlayerLine]
    DEF: list[PlayerLine]
    MID: list[PlayerLine]
    FWD: list[PlayerLine]


class TeamDashboardResponse(BaseModel):
    user_id: int
    username: str | None
    team_name: str | None
    season: str
    gameweek: int
    deadline: str | None
    has_lineup: bool
    chip_used: str | None
    captain_multiplier: int
    gw_points: int
    gw_average: float | None
    season_total: int
    # Points breakdown. has_score is False when scoring.py hasn't run for this
    # (user, season, gameweek) yet -- the other four are 0 in that case, and a
    # dashboard showing "Raw 0 / Final 0" for an unplayed gameweek would read as
    # a real score rather than "not scored yet", so the UI needs the flag.
    has_score: bool
    raw_points: int
    captain_bonus: int
    transfer_hits: int
    hit_deductions: int
    final_total: int
    # 'upcoming' | 'live' | 'final' -- whether this gameweek's fixtures are all
    # done, in progress, or not started. Points shown during 'live' are partial.
    live_status: str
    overall_rank: int | None
    overall_rank_total: int | None
    # False only for a 'final' gameweek with no user_gameweek_finance row --
    # bank/team_value are 0.0 in that case, not a real figure. Always True
    # for the live/current gameweek, which reads today's live squad state
    # exactly as it always has.
    team_value_available: bool
    team_value: float
    bank: float
    lineup: Lineup
    bench: list[PlayerLine]


def _empty_lineup() -> Lineup:
    return Lineup(GK=[], DEF=[], MID=[], FWD=[])


def _resolve_live_status(row) -> str:
    """'final' only when every fixture is finished -- a gameweek with one match
    still to play is 'live', not final, so partial points aren't presented as a
    settled score. No fixtures ingested yet reads as 'upcoming'."""
    if row is None or row.total == 0:
        return "upcoming"
    if row.finished_count == row.total:
        return "final"
    return "live" if row.started_count > 0 else "upcoming"


def _autosub_player_ids(rows, chip_used: str | None) -> tuple[set[int], set[int]]:
    """Returns (subbed_in_player_ids, subbed_out_player_ids).

    Delegates the actual rules to scoring.resolve_autosubs so there is exactly
    one implementation of them. Returns empty sets whenever autosub can't apply:
    bench_boost (all 15 count, so nothing is substituted), an incomplete squad,
    or a gameweek where nobody has played yet.
    """
    if chip_used == "bench_boost" or len(rows) != 15:
        return set(), set()

    starters = {r.position_slot: r for r in rows if r.position_slot <= 11}
    bench = {r.position_slot: r for r in rows if r.position_slot > 11}
    try:
        effective = resolve_autosubs(starters, bench)
    except StopIteration:
        # resolve_autosubs assumes exactly one GK on the pitch and one on the
        # bench. A squad that doesn't satisfy that is a scoring-side problem;
        # the dashboard should still render, just without autosub badges.
        logger.warning(
            "Could not resolve autosubs for display (no GK in starters or bench) -- "
            "rendering dashboard without autosub indicators."
        )
        return set(), set()

    effective_ids = {r.player_id for r in effective}
    starter_ids = {r.player_id for r in starters.values()}
    subbed_out = starter_ids - effective_ids
    subbed_in = effective_ids - starter_ids
    return subbed_in, subbed_out


@router.get("/team", response_model=TeamDashboardResponse)
def get_team_dashboard(
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> TeamDashboardResponse:
    user_id = current_user.id
    engine = get_engine()

    deadline = resolve_gameweek_deadline(engine, season, gameweek)

    with engine.connect() as conn:
        user_row = conn.execute(USER_QUERY, {"user_id": user_id}).first()
        username = user_row.username if user_row is not None else None
        team_name = user_row.team_name if user_row is not None else None

        selection_row = conn.execute(
            GW_SELECTION_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).first()

        has_lineup = selection_row is not None
        chip_used = selection_row.chip_used if has_lineup else None
        captain_multiplier = TRIPLE_CAPTAIN_MULTIPLIER if chip_used == "triple_captain" else CAPTAIN_MULTIPLIER

        lineup = _empty_lineup()
        bench: list[PlayerLine] = []
        if has_lineup:
            rows = conn.execute(
                STARTING_XI_ROWS_QUERY,
                {"gw_selection_id": selection_row.gw_selection_id, "season": season, "gameweek": gameweek},
            ).all()
            subbed_in, subbed_out = _autosub_player_ids(rows, chip_used)
            for r in rows:
                line = PlayerLine(
                    player_id=r.player_id,
                    name=r.web_name,
                    position=r.position,
                    club=r.club,
                    points=r.points,
                    is_captain=r.is_captain,
                    is_vice_captain=r.is_vice_captain,
                    is_autosubbed_in=r.player_id in subbed_in,
                    is_autosubbed_out=r.player_id in subbed_out,
                )
                if r.position_slot <= 11:
                    getattr(lineup, r.position).append(line)
                else:
                    bench.append(line)

        gw_score_row = conn.execute(
            GW_SCORE_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).first()
        gw_points = gw_score_row.total_points if gw_score_row is not None else 0
        season_total = gw_score_row.season_total if gw_score_row is not None else 0

        has_score = gw_score_row is not None
        raw_points = gw_score_row.raw_points if has_score else 0
        # The captain's extra only. scoring.py stores final_points = raw_points
        # + (multiplier - 1) * captain's base, so the difference IS the bonus --
        # deriving it this way makes it structurally impossible for the
        # breakdown to double-count the captain.
        captain_bonus = (gw_score_row.final_points - gw_score_row.raw_points) if has_score else 0
        transfer_hits = gw_score_row.transfer_hits if has_score else 0
        hit_deductions = gw_score_row.hit_deductions if has_score else 0
        # Equals raw_points + captain_bonus - hit_deductions by construction;
        # returned as gw_scores.total_points rather than re-added here so the
        # table's bottom line is the same number scoring.py committed.
        final_total = gw_score_row.total_points if has_score else 0

        live_status = _resolve_live_status(
            conn.execute(GW_FIXTURE_STATUS_QUERY, {"season": season, "gameweek": gameweek}).first()
        )

        gw_average_raw = conn.execute(GW_AVERAGE_QUERY, {"season": season, "gameweek": gameweek}).scalar()
        gw_average = round(float(gw_average_raw), 1) if gw_average_raw is not None else None

        rank_row = conn.execute(
            OVERALL_RANK_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).first()
        overall_rank = rank_row.rank if rank_row is not None else None
        overall_rank_total = rank_row.total if rank_row is not None else None

        # The live/current gameweek (anything not yet 'final') keeps reading
        # today's live squad state exactly as before -- unchanged behavior,
        # per spec. A 'final' gameweek instead reads the frozen snapshot
        # Results/scoring.py wrote at scoring time, since today's live
        # budget_remaining/purchase_price sum has nothing to do with what
        # this user's finances looked like back then. If a 'final' gameweek
        # was somehow never scored (no gw_selections row that week), there
        # is no snapshot to read -- reported as unavailable rather than
        # falling back to live figures, which would misrepresent today's
        # numbers as historical.
        if live_status == "final":
            finance_row = conn.execute(
                GAMEWEEK_FINANCE_SNAPSHOT_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
            ).first()
            team_value_available = finance_row is not None
            bank = finance_row.bank / 10 if finance_row is not None else 0.0
            team_value = finance_row.team_value / 10 if finance_row is not None else 0.0
        else:
            squad_row = conn.execute(TEAM_VALUE_AND_BANK_QUERY, {"user_id": user_id, "season": season}).first()
            team_value_available = True
            bank = squad_row.budget_remaining / 10 if squad_row is not None else 0.0
            team_value = squad_row.team_value_tenths / 10 if squad_row is not None else 0.0

    return TeamDashboardResponse(
        user_id=user_id,
        username=username,
        team_name=team_name,
        season=season,
        gameweek=gameweek,
        deadline=deadline.isoformat() if deadline is not None else None,
        has_lineup=has_lineup,
        chip_used=chip_used,
        captain_multiplier=captain_multiplier,
        has_score=has_score,
        raw_points=raw_points,
        captain_bonus=captain_bonus,
        transfer_hits=transfer_hits,
        hit_deductions=hit_deductions,
        final_total=final_total,
        live_status=live_status,
        gw_points=gw_points,
        gw_average=gw_average,
        season_total=season_total,
        overall_rank=overall_rank,
        overall_rank_total=overall_rank_total,
        team_value_available=team_value_available,
        team_value=team_value,
        bank=bank,
        lineup=lineup,
        bench=bench,
    )
