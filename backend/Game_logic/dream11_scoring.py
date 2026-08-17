"""
dream11_scoring.py — pure Dream11 contest scoring:
dream11.team_players + ml.player_gw_stats -> dream11.contest_members.

Points are NOT ml.player_gw_stats.total_points (that's FPL's own
scoring) -- Dream11 uses its own distinct point-weighting, computed
fresh from raw stat columns via calculate_dream11_points(). The
weighting table is a module-level constant specifically so it's easy
to find and adjust later.

clean_sheets is deliberately NEVER read from ml.player_gw_stats
directly -- that column is precomputed using FPL's 60-minute rule.
Clean sheet status is recomputed here from goals_conceded + minutes
against CLEAN_SHEET_MINUTES_THRESHOLD (54, Dream11's real threshold),
so FPL's and Dream11's clean-sheet definitions can never get crossed.

Captain 2x / vice-captain 1.5x: unlike Game_logic/scoring.py's FPL
captain/vice rule (vice is a FALLBACK -- only kicks in if captain has
0 minutes), Dream11's captain and vice bonuses are both unconditional
and simultaneous, applied to two different players regardless of how
either performed. raw_points already counts everyone (including
captain/vice) once; the multiplier is layered on as an ADDITIVE bonus
on top, not a re-multiplication:
    final_points = raw_points + captain_points*(2.0-1.0) + vice_points*(1.5-1.0)
A captain/vice with 0 points naturally contributes a 0 bonus -- no
special-casing needed, and critically that does NOT change the other
role's bonus (no fallback coupling between the two, unlike FPL).

score_dream11_contest(engine, contest_id) processes every
dream11.teams row for this contest, each isolated in its own
try/except (one broken team can't abort the batch) -- same philosophy
as scoring.py/standings.py. contest_members has no immutability
trigger, so re-scoring is a plain UPDATE with a freshly recomputed
value each time -- naturally idempotent, no upsert-vs-insert
complexity needed (unlike leaderboard_snapshots elsewhere), since a
Dream11 contest is scoped to a single fixture, not a season with prior
gameweeks to sum.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

DREAM11_TEAM_SIZE = 11

GOAL_POINTS = {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
ASSIST_POINTS = 3
CLEAN_SHEET_MINUTES_THRESHOLD = 54  # Dream11's real threshold, NOT FPL's 60
CLEAN_SHEET_POINTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
GOALS_CONCEDED_PENALTY = -1  # per 2 conceded, GK/DEF only
SAVES_POINTS = 1  # per 3 saves, GK only
YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3
OWN_GOAL_POINTS = -2
PENALTY_SAVED_POINTS = 5
PENALTY_MISSED_POINTS = -2

CAPTAIN_MULTIPLIER = 2.0
VICE_CAPTAIN_MULTIPLIER = 1.5

CONTEST_FIXTURE_QUERY = text(
    "SELECT f.season, f.gameweek FROM dream11.contests c JOIN ml.fixtures f ON f.id = c.fixture_id WHERE c.id = :contest_id"
)

TEAMS_QUERY = text("SELECT id AS team_id, user_id FROM dream11.teams WHERE contest_id = :contest_id")

# minutes/stats default to 0 via COALESCE when no player_gw_stats row
# exists yet -- partial/live scoring is allowed by design, not an error,
# same philosophy as scoring.py's STARTING_XI_STATS_QUERY.
TEAM_PLAYERS_STATS_QUERY = text(
    """
    SELECT tp.player_id, tp.is_captain, tp.is_vice_captain,
           mp.position,
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
    FROM dream11.team_players tp
    JOIN ml.players mp ON mp.id = tp.player_id
    LEFT JOIN ml.player_gw_stats pgs ON pgs.player_id = tp.player_id AND pgs.season = :season AND pgs.gameweek = :gameweek
    WHERE tp.team_id = :team_id
    """
)

UPDATE_CONTEST_MEMBER_POINTS_STMT = text(
    "UPDATE dream11.contest_members SET total_points = :total_points WHERE contest_id = :contest_id AND user_id = :user_id"
)

UPDATE_CONTEST_MEMBER_RANK_STMT = text(
    "UPDATE dream11.contest_members SET rank = :rank WHERE contest_id = :contest_id AND user_id = :user_id"
)


def calculate_dream11_points(stats_row, position: str) -> int:
    goal_pts = stats_row.goals_scored * GOAL_POINTS[position]
    assist_pts = stats_row.assists * ASSIST_POINTS

    clean_sheet = stats_row.goals_conceded == 0 and stats_row.minutes >= CLEAN_SHEET_MINUTES_THRESHOLD
    cs_pts = CLEAN_SHEET_POINTS[position] if clean_sheet else 0

    conceded_pts = (stats_row.goals_conceded // 2) * GOALS_CONCEDED_PENALTY if position in ("GK", "DEF") else 0
    saves_pts = (stats_row.saves // 3) * SAVES_POINTS if position == "GK" else 0

    card_pts = stats_row.yellow_cards * YELLOW_CARD_POINTS + stats_row.red_cards * RED_CARD_POINTS
    other_pts = (
        stats_row.own_goals * OWN_GOAL_POINTS
        + stats_row.penalties_saved * PENALTY_SAVED_POINTS
        + stats_row.penalties_missed * PENALTY_MISSED_POINTS
    )

    return goal_pts + assist_pts + cs_pts + conceded_pts + saves_pts + card_pts + other_pts


def _rank_teams(scored: list[tuple[int, int]]) -> dict[int, int]:
    """scored: [(user_id, points), ...]. Standard competition ranking:
    ties share a rank, next distinct rank skips accordingly."""
    ranked = sorted(scored, key=lambda pair: -pair[1])
    ranks: dict[int, int] = {}
    prev_points = None
    prev_rank = 0
    for i, (user_id, points) in enumerate(ranked, start=1):
        rank = i if points != prev_points else prev_rank
        ranks[user_id] = rank
        prev_points = points
        prev_rank = rank
    return ranks


def _score_one_team(engine, contest_id: int, team_id: int, user_id: int, season: str, gameweek: int) -> int:
    with engine.connect() as conn:
        rows = conn.execute(TEAM_PLAYERS_STATS_QUERY, {"team_id": team_id, "season": season, "gameweek": gameweek}).all()

    if len(rows) != DREAM11_TEAM_SIZE:
        raise ValueError(f"expected {DREAM11_TEAM_SIZE} team_players rows for team_id={team_id}, got {len(rows)}")

    per_player_points = {r.player_id: calculate_dream11_points(r, r.position) for r in rows}
    raw_points = sum(per_player_points.values())

    captain_row = next((r for r in rows if r.is_captain), None)
    vice_row = next((r for r in rows if r.is_vice_captain), None)

    captain_bonus = per_player_points[captain_row.player_id] * (CAPTAIN_MULTIPLIER - 1.0) if captain_row is not None else 0
    vice_bonus = per_player_points[vice_row.player_id] * (VICE_CAPTAIN_MULTIPLIER - 1.0) if vice_row is not None else 0

    final_points = round(raw_points + captain_bonus + vice_bonus)

    with engine.begin() as conn:
        conn.execute(UPDATE_CONTEST_MEMBER_POINTS_STMT, {"contest_id": contest_id, "user_id": user_id, "total_points": final_points})

    return final_points


def score_dream11_contest(engine, contest_id: int) -> dict:
    """Score every dream11.teams row for this contest.

    Returns {"scored": [user_id, ...], "failed": [(user_id, error_message), ...]}.
    """
    with engine.connect() as conn:
        contest_fixture = conn.execute(CONTEST_FIXTURE_QUERY, {"contest_id": contest_id}).first()
        teams = conn.execute(TEAMS_QUERY, {"contest_id": contest_id}).all()

    if contest_fixture is None:
        raise ValueError(f"contest_id {contest_id} does not exist")

    scored = []
    failed = []
    team_points: dict[int, int] = {}
    for t in teams:
        try:
            points = _score_one_team(engine, contest_id, t.team_id, t.user_id, contest_fixture.season, contest_fixture.gameweek)
            team_points[t.user_id] = points
            scored.append(t.user_id)
        except Exception as e:
            logger.error(
                "score_dream11_contest: failed to score user_id=%s (contest_id=%s): %s: %s",
                t.user_id, contest_id, type(e).__name__, e,
            )
            failed.append((t.user_id, str(e)))

    ranks = _rank_teams(list(team_points.items()))
    with engine.begin() as conn:
        for user_id, rank in ranks.items():
            conn.execute(UPDATE_CONTEST_MEMBER_RANK_STMT, {"contest_id": contest_id, "user_id": user_id, "rank": rank})

    return {"scored": scored, "failed": failed}
