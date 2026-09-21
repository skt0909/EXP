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
from pydantic import BaseModel, Field
from sqlalchemy import text

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
# Shared with scoring.py rather than redeclared here -- this module used to
# keep its own copies, and the two had to agree or the multiplier shown
# beside a score would contradict the score itself.
from Shared.deadlines import resolve_gameweek_deadline
# Phase 4b: per-player points are computed at request time by the TACTICAL
# engine, for THIS manager and gameweek. The classic scoring.py is no longer
# imported at all -- resolve_autosubs went with it, because the engine already
# reports which players were covered and which were replaced.
from Results.scoring_job import ruleset_first_gameweek, score_manager
from Results.tactical_scoring import (
    general_points_breakdown,
    tactical_points_breakdown,
)

logger = logging.getLogger(__name__)

router = APIRouter()


# LEFT JOIN semantics not needed -- user_id is queried directly against
# users' own PK, so a nonexistent user_id just yields no row (username/
# team_name come back None below), same forgiving behavior as every
# other "doesn't exist yet" case in this endpoint.
USER_QUERY = text("SELECT username, team_name FROM users WHERE id = :user_id")

GW_SELECTION_QUERY = text(
    "SELECT id AS gw_selection_id, tactic FROM gw_selections "
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
    SELECT sx.position_slot, sx.is_bonus,
           mp.fpl_id AS player_id, mp.web_name, mp.position, t.short_name AS club
    FROM starting_xi sx
    JOIN ml.players mp ON mp.fpl_id = sx.player_id AND mp.season = :season
    JOIN ml.teams t ON t.id = mp.team_id
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
    "SELECT raw_points, final_points, tactical_points, sub_bonus, total_points, season_total "
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
    # NULLABLE since 4b: an unscored gameweek returns null rather than 0, so a
    # client cannot render "0 points" for a gameweek that will never be scored.
    #
    # DEPRECATED (F1). This key used to be FPL's own total_points and is now
    # the engine's GENERAL points -- same name, different definition. It is
    # kept so the current frontend keeps rendering; use `general_points`
    # instead. Removed in the frontend phase.
    points: int | None
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
    # --- added in Phase 4b, all additive ---
    # One of the six engine roles: starter, swapped_out, swapped_in,
    # auto_sub_cover, auto_sub_replaced, bench_unused.
    role: str = "starter"
    is_bonus: bool = False
    general_points: int | None = 0
    tactical_points: int | None = 0
    # [{"rule": "goals", "points": 6}, ...] -- which rules produced the two
    # numbers above, summed across the player's fixtures this gameweek.
    general_breakdown: list = Field(default_factory=list)
    tactical_breakdown: list = Field(default_factory=list)
    # Whether this player's General Points reached the manager's total. False
    # for a replaced starter and for an unused bench player.
    counted: bool = False


class SwapLine(BaseModel):
    """One executed Tactical swap, as the dashboard shows it."""
    player_out_id: int
    player_in_id: int
    general_out: int
    general_in: int
    sub_bonus: int


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
    gw_points: int | None
    gw_average: float | None
    season_total: int
    # Points breakdown. has_score is False when scoring.py hasn't run for this
    # (user, season, gameweek) yet -- the other four are 0 in that case, and a
    # dashboard showing "Raw 0 / Final 0" for an unplayed gameweek would read as
    # a real score rather than "not scored yet", so the UI needs the flag.
    has_score: bool
    raw_points: int | None
    captain_bonus: int
    transfer_hits: int
    hit_deductions: int
    final_total: int | None
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
    # --- added in Phase 4b, all additive ---
    tactic: str | None
    general_points: int | None
    tactical_points: int | None
    sub_bonus: int | None
    total: int | None
    # "committed" when the figures come from gw_scores, "live" when they are
    # the engine's, computed just now because the job has not run yet, and
    # None when the gameweek is not scored at all (F2).
    score_source: str | None
    swaps: list[SwapLine] = Field(default_factory=list)
    # True while ANY fixture in this gameweek is not finished. Points and
    # creativity are only final at full-time, so a client must be able to say
    # "so far" rather than presenting a settled score.
    provisional: bool = False
    # False when this gameweek is not scored under these rules at all -- before
    # the ruleset epoch, or no selection submitted. `scored_reason` says which.
    scored: bool = True
    scored_reason: str | None = None
    team_value: float
    bank: float
    lineup: Lineup
    bench: list[PlayerLine]


def _empty_lineup() -> Lineup:
    return Lineup(GK=[], DEF=[], MID=[], FWD=[])


def _merge_breakdown(parts):
    """Sum a player's per-fixture breakdown rows into one list per rule.

    A double gameweek produces two "goals" rows; a manager wants one line
    saying 2 goals, not two lines saying one each. Order is preserved so the
    rules read in the order they are applied."""
    merged, order = {}, []
    for part in parts:
        rule = part["rule"]
        if rule not in merged:
            merged[rule] = 0
            order.append(rule)
        merged[rule] += part["points"]
    return [{"rule": r, "points": merged[r]} for r in order if merged[r]]


def _resolve_live_status(row) -> str:
    """'final' only when every fixture is finished -- a gameweek with one match
    still to play is 'live', not final, so partial points aren't presented as a
    settled score. No fixtures ingested yet reads as 'upcoming'."""
    if row is None or row.total == 0:
        return "upcoming"
    if row.finished_count == row.total:
        return "final"
    return "live" if row.started_count > 0 else "upcoming"


# _autosub_player_ids was removed in Phase 4b. It delegated to the classic
# scoring.py's resolve_autosubs, which this module no longer imports: the
# tactical engine already reports who covered whom (role 'auto_sub_cover',
# with covers_player_id) and who was replaced ('auto_sub_replaced'), so a
# second implementation of the rules is no longer needed to draw the badge.


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
        # Chips are gone. The key stays in the response so the current frontend
        # keeps rendering; it is always None now, and the frontend phase
        # removes it. Same for captain_multiplier below.
        chip_used = None
        # An INTEGER, always 1, never null (F6). A client doing
        # points * captain_multiplier keeps working and gets the right answer;
        # null would break that arithmetic. Captaincy is removed, and 1 is what
        # "no multiplier" means.
        captain_multiplier = 1
        tactic = selection_row.tactic if has_lineup else None

        lineup = _empty_lineup()
        bench: list[PlayerLine] = []
        swap_lines: list[SwapLine] = []
        general_total = tactical_total = sub_bonus_total = engine_total = 0

        if has_lineup:
            rows = conn.execute(
                STARTING_XI_ROWS_QUERY,
                {"gw_selection_id": selection_row.gw_selection_id, "season": season},
            ).all()

            # THE change: per-player points now come from the tactical engine,
            # scored for THIS manager and gameweek, rather than from
            # ml.player_gw_stats.total_points. Two managers holding the same
            # player can now legitimately see different numbers, because a
            # Bonus Player's tactical points depend on the manager's tactic.
            score, stats_by_player, positions = score_manager(
                conn, season, gameweek, selection_row.gw_selection_id, tactic
            )
            by_player = {p.player_id: p for p in score.players}

            general_total = score.raw_points
            tactical_total = score.tactical_points
            sub_bonus_total = score.sub_bonus
            engine_total = score.total

            swap_lines = [
                SwapLine(
                    player_out_id=s.player_out_id, player_in_id=s.player_in_id,
                    general_out=s.general_out, general_in=s.general_in,
                    sub_bonus=s.sub_bonus,
                )
                for s in score.swaps
            ]

            for r in rows:
                p = by_player.get(r.player_id)
                fixtures = stats_by_player.get(r.player_id, [])
                position = positions.get(r.player_id, r.position)

                gen_parts, tac_parts = [], []
                for fixture in fixtures:
                    gen_parts.extend(general_points_breakdown(fixture, position))
                    if p is not None and p.is_bonus and p.tactical_points:
                        tac_parts.extend(
                            tactical_points_breakdown(fixture, position, tactic)
                        )

                line = PlayerLine(
                    player_id=r.player_id,
                    name=r.web_name,
                    position=r.position,
                    club=r.club,
                    # `points` keeps its meaning for the current frontend: the
                    # player's own points this gameweek. It is now GENERAL
                    # points from the engine rather than FPL's total_points.
                    points=p.general_points if p is not None else 0,
                    # Captaincy is gone; both stay present and always False.
                    is_captain=False,
                    is_vice_captain=False,
                    is_autosubbed_in=(p is not None and p.role == "auto_sub_cover"),
                    is_autosubbed_out=(p is not None and p.role == "auto_sub_replaced"),
                    role=p.role if p is not None else "starter",
                    is_bonus=bool(p.is_bonus) if p is not None else False,
                    general_points=p.general_points if p is not None else 0,
                    tactical_points=p.tactical_points if p is not None else 0,
                    general_breakdown=_merge_breakdown(gen_parts),
                    tactical_breakdown=_merge_breakdown(tac_parts),
                    counted=bool(p.counted) if p is not None else False,
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
        raw_points = gw_score_row.raw_points if has_score else general_total
        # Captaincy is gone, so there is no captain bonus. The key stays at 0
        # for the current frontend; tactical_points and sub_bonus below are
        # what actually sit between raw and final now.
        captain_bonus = 0
        # Hits are gone. Both keys stay at 0.
        transfer_hits = 0
        hit_deductions = 0
        # gw_scores.total_points when the job has run, the engine's own total
        # otherwise -- so a gameweek being scored right now still shows a
        # coherent bottom line instead of 0.
        final_total = gw_score_row.total_points if has_score else engine_total

        fixture_status = conn.execute(
            GW_FIXTURE_STATUS_QUERY, {"season": season, "gameweek": gameweek}
        ).first()
        live_status = _resolve_live_status(fixture_status)
        # PROVISIONAL while any fixture is unfinished. The source is
        # ml.fixtures.finished, via GW_FIXTURE_STATUS_QUERY's
        # "COUNT(*) FILTER (WHERE finished) AS finished_count". Points and
        # creativity only settle at full-time, so anything short of every
        # fixture finished is a running figure, not a result.
        provisional = bool(
            fixture_status is not None
            and fixture_status.total > 0
            and fixture_status.finished_count < fixture_status.total
        )

        # SCORED: is this gameweek scored under these rules at all?
        scored, scored_reason = True, None
        epoch_first_gw = ruleset_first_gameweek(conn, season)
        if gameweek < epoch_first_gw:
            scored = False
            scored_reason = (
                f"gameweek {gameweek} precedes the first gameweek played under "
                f"these rules ({epoch_first_gw}), so it is not scored"
            )
        elif not has_lineup:
            # Today's behaviour is preserved exactly: has_lineup is
            # "selection_row is not None" and an unstarted state is not an
            # error -- the same stance GET /squad and GET /gw_selection take.
            scored = False
            scored_reason = "no selection was submitted for this gameweek"

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
        # team_value_available now means exactly one thing: a
        # user_gameweek_finance row exists for THIS user and gameweek. It used
        # to be True for any non-final gameweek, which made it True for a fresh
        # user with no squad at all -- reporting 0.0 as a real figure.
        #
        # WHICH VALUES are shown is a separate question and is UNCHANGED: a
        # gameweek still in progress reports today's live squad state, because
        # that IS its state; a finished one reports the frozen snapshot, and
        # zeroes if it was never scored. Today's budget has nothing to do with
        # what a manager's finances were three gameweeks ago.
        finance_row = conn.execute(
            GAMEWEEK_FINANCE_SNAPSHOT_QUERY,
            {"user_id": user_id, "season": season, "gameweek": gameweek},
        ).first()
        team_value_available = finance_row is not None

        if live_status == "final":
            bank = finance_row.bank / 10 if finance_row is not None else 0.0
            team_value = finance_row.team_value / 10 if finance_row is not None else 0.0
        else:
            squad_row = conn.execute(TEAM_VALUE_AND_BANK_QUERY, {"user_id": user_id, "season": season}).first()
            bank = squad_row.budget_remaining / 10 if squad_row is not None else 0.0
            team_value = squad_row.team_value_tenths / 10 if squad_row is not None else 0.0

    # F2: say where the numbers came from, so "raw_points" is never ambiguous
    # between a committed figure and one computed a moment ago.
    score_source = "committed" if has_score else "live"

    # F3: a gameweek that is not scored returns NULL for every points field
    # rather than 0. Showing 0 invites a client to render it as a real score
    # for a gameweek that will never be scored. The lineup STRUCTURE stays --
    # names, positions, clubs, roles -- so the team still draws.
    if not scored:
        score_source = None
        general_total = tactical_total = sub_bonus_total = None
        raw_points = final_total = gw_points = None
        for group in (lineup.GK, lineup.DEF, lineup.MID, lineup.FWD, bench):
            for line in group:
                line.points = None
                line.general_points = None
                line.tactical_points = None
                line.general_breakdown = []
                line.tactical_breakdown = []

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
        # --- added in Phase 4b ---
        tactic=tactic,
        general_points=general_total,
        tactical_points=tactical_total,
        sub_bonus=sub_bonus_total,
        total=final_total,
        score_source=score_source,
        swaps=swap_lines,
        provisional=provisional,
        scored=scored,
        scored_reason=scored_reason,
    )
