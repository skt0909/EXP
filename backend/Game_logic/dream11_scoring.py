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

Captain 2x / vice-captain 1.5x: unlike Results/scoring.py's FPL
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
as scoring.py/standings.py. Re-scoring is a plain UPDATE with a freshly
recomputed value each time -- naturally idempotent, no upsert-vs-insert
complexity needed (unlike leaderboard_snapshots elsewhere), since a
Dream11 contest is scoped to a single fixture, not a season with prior
gameweeks to sum.

FINALIZATION, and why re-scoring stops.

Re-scoring being cheap and idempotent was true only while the scoring
constants held still. They don't: changing ASSIST_POINTS from 3 to 20
meant a contest scored under the old value kept its stored total while
Game_logic/dream11.py's get_user_team recomputed a different one from
the same raw stats -- and the frontend shows the stored number on the
leaderboard and the recomputed number on the team panel, so one contest
had two totals on adjacent screens.

So a contest's result is now FROZEN once its match is over.
finalize_dream11_contest scores one final time and stamps
dream11.contests.finalized_at; after that score_dream11_contest refuses
to touch it, get_user_team reads the stored numbers instead of
recomputing, and enforce_contest_result_immutability blocks the UPDATE
at the database even if both of those are bypassed. A frozen result is
frozen under the rules that were in force when the match was played,
which is the only version of it that was ever true.

Classic FPL reaches the same place by a different route: gw_scores rows
simply fall out of refresh_active_gameweeks' 5-day window and are never
revisited, with rules_version recording which ruleset wrote each row.
Dream11 has no recurring window to fall out of, so it needs the explicit
flag instead of an implicit one.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

DREAM11_TEAM_SIZE = 11

GOAL_POINTS = {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
# Flat, every position -- real Dream11 does NOT weight assists by position
# the way it weights goals. 20, not FPL's 3: this is the one constant here
# whose value differs from FPL by more than a rounding, so an assist is
# worth more than any goal in this ruleset. That inversion is intentional
# and is what the number is checked against; do not "correct" it toward
# Results/scoring.py's 3, which is the separate classic-FPL rule.
ASSIST_POINTS = 20
CLEAN_SHEET_MINUTES_THRESHOLD = 54  # Dream11's real threshold, NOT FPL's 60
CLEAN_SHEET_POINTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}
GOALS_CONCEDED_PENALTY = -1  # per 2 conceded, GK/DEF only
SAVES_POINTS = 1  # per 3 saves, GK only
# The divisors above, named because GET /scoring-rules publishes them. Left as
# literals they were a comment rather than a value, and the endpoint would have
# had to retype them -- the exact drift the endpoint exists to prevent.
GOALS_CONCEDED_PER = 2
SAVES_PER = 3
YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3
OWN_GOAL_POINTS = -2
PENALTY_SAVED_POINTS = 5
PENALTY_MISSED_POINTS = -2

CAPTAIN_MULTIPLIER = 2.0
VICE_CAPTAIN_MULTIPLIER = 1.5

CONTEST_FIXTURE_QUERY = text(
    "SELECT c.fixture_id, f.season, f.gameweek, c.finalized_at "
    "FROM dream11.contests c JOIN ml.fixtures f ON f.id = c.fixture_id WHERE c.id = :contest_id"
)

# Finalization reads ml.fixtures.finished rather than trusting a clock
# offset -- see finalize_dream11_contest.
CONTEST_FINALIZATION_STATE_QUERY = text(
    "SELECT c.finalized_at, f.finished "
    "FROM dream11.contests c JOIN ml.fixtures f ON f.id = c.fixture_id WHERE c.id = :contest_id"
)

# Every contest whose match is over but whose result is not yet frozen.
# Backed by idx_d11_contests_unfinalized, and in steady state this returns
# nothing -- see find_contests_needing_finalization.
CONTESTS_NEEDING_FINALIZATION_QUERY = text(
    """
    SELECT c.id AS contest_id
    FROM dream11.contests c
    JOIN ml.fixtures f ON f.id = c.fixture_id
    WHERE c.finalized_at IS NULL
      AND f.finished = TRUE
    ORDER BY c.id
    """
)

# Also locks: finalization can now run within a minute of FPL marking the
# match finished (poll_due_fixtures' 'final' checkpoint), and a contest the
# 5-minute lock sweep hadn't reached yet would otherwise be finalized but
# still accept team edits (enforce_contest_lock checks is_locked only).
STAMP_CONTEST_FINALIZED_STMT = text(
    "UPDATE dream11.contests SET finalized_at = now(), is_locked = TRUE "
    "WHERE id = :contest_id AND finalized_at IS NULL"
)

TEAMS_QUERY = text("SELECT id AS team_id, user_id FROM dream11.teams WHERE contest_id = :contest_id")

# minutes/stats default to 0 via COALESCE when no player_gw_stats row
# exists yet -- partial/live scoring is allowed by design, not an error,
# same philosophy as scoring.py's STARTING_XI_STATS_QUERY.
#
# SCOPED TO :fixture_id, NOT JUST THE GAMEWEEK. A Dream11 contest is
# played on ONE match; a gameweek can contain two for the same club.
# ml.player_gw_stats is unique on
# (player_id, season, gameweek, COALESCE(fixture_id, -1)) -- note the
# fixture in that key -- so a player whose club plays twice in a gameweek
# legitimately has TWO rows, one per match. Matching on the gameweek
# alone pulled both, which meant an 11-player team came back as 12 rows
# and _score_one_team's size guard raised, failing the team outright.
# Since finalization refuses to freeze a contest that has any failed
# team, that turned a double gameweek into a contest stuck unfinalized
# forever, with the sweep retrying it on every pass.
#
# Adding fixture_id to the join makes at most one row matchable per
# player: the unique index above is exactly this tuple, so equality on a
# non-null fixture_id can select at most one of the rows for that player.
# A player with no row for THIS match (LEFT JOIN, all-NULL) COALESCEs to
# zeros, which is the correct reading of "did not feature in this match"
# and the same thing that happens before kickoff.
#
# Classic FPL's Results/scoring.py deliberately does NOT do this: an FPL
# gameweek score is the sum across every match a player played that week,
# so its gameweek-wide join is correct for it and must stay.
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
    LEFT JOIN ml.player_gw_stats pgs ON pgs.player_id = tp.player_id AND pgs.season = :season
                                    AND pgs.gameweek = :gameweek AND pgs.fixture_id = :fixture_id
    WHERE tp.team_id = :team_id
    """
)

UPDATE_CONTEST_MEMBER_POINTS_STMT = text(
    "UPDATE dream11.contest_members SET total_points = :total_points WHERE contest_id = :contest_id AND user_id = :user_id"
)

UPDATE_CONTEST_MEMBER_RANK_STMT = text(
    "UPDATE dream11.contest_members SET rank = :rank WHERE contest_id = :contest_id AND user_id = :user_id"
)

# The per-player breakdown, persisted alongside the team total on every
# scoring pass. Two reasons it has to be stored rather than recomputed:
# it must still be readable once the contest is finalized and
# ml.player_gw_stats is off-limits to that read path, and storing it
# beside the total is what keeps the breakdown summing to the total
# permanently rather than only for as long as the constants hold still.
UPDATE_TEAM_PLAYER_FINAL_STMT = text(
    "UPDATE dream11.team_players SET final_points = :final_points, final_minutes = :final_minutes "
    "WHERE team_id = :team_id AND player_id = :player_id"
)


def dream11_points_breakdown(stats_row, position: str) -> dict:
    """Every named component calculate_dream11_points sums into one number,
    kept alongside its own raw stat count so a UI can show both ("+2 goals"
    and "12 pts" rather than just the latter). calculate_dream11_points is
    now a one-line wrapper around this -- added for the points-breakdown
    bottom sheet, which needs the intermediate values this function was
    already computing internally; nothing here is a recompute of anything,
    just the same arithmetic returned instead of discarded.

    goals_conceded/saves are int-divided (// GOALS_CONCEDED_PER, // SAVES_PER)
    before their point value is looked up, same as calculate_dream11_points
    always did -- this returns the POINTS from that division, not the raw
    conceded/save count, which the caller gets separately as *_count.
    """
    clean_sheet = stats_row.goals_conceded == 0 and stats_row.minutes >= CLEAN_SHEET_MINUTES_THRESHOLD
    conceded_eligible = position in ("GK", "DEF")
    saves_eligible = position == "GK"

    goals = stats_row.goals_scored * GOAL_POINTS[position]
    assists = stats_row.assists * ASSIST_POINTS
    clean_sheet_pts = CLEAN_SHEET_POINTS[position] if clean_sheet else 0
    goals_conceded_pts = (
        (stats_row.goals_conceded // GOALS_CONCEDED_PER) * GOALS_CONCEDED_PENALTY if conceded_eligible else 0
    )
    saves_pts = (stats_row.saves // SAVES_PER) * SAVES_POINTS if saves_eligible else 0
    yellow_cards = stats_row.yellow_cards * YELLOW_CARD_POINTS
    red_cards = stats_row.red_cards * RED_CARD_POINTS
    own_goals = stats_row.own_goals * OWN_GOAL_POINTS
    penalties_saved = stats_row.penalties_saved * PENALTY_SAVED_POINTS
    penalties_missed = stats_row.penalties_missed * PENALTY_MISSED_POINTS

    total = (
        goals + assists + clean_sheet_pts + goals_conceded_pts + saves_pts
        + yellow_cards + red_cards + own_goals + penalties_saved + penalties_missed
    )

    return {
        "minutes": stats_row.minutes,
        "goals": goals,
        "goals_count": stats_row.goals_scored,
        "assists": assists,
        "assists_count": stats_row.assists,
        "clean_sheet": clean_sheet_pts,
        "clean_sheet_achieved": clean_sheet,
        "clean_sheet_eligible": position != "FWD",  # FWD's CLEAN_SHEET_POINTS is always 0
        "goals_conceded": goals_conceded_pts,
        "goals_conceded_count": stats_row.goals_conceded,
        "goals_conceded_eligible": conceded_eligible,
        "saves": saves_pts,
        "saves_count": stats_row.saves,
        "saves_eligible": saves_eligible,
        "yellow_cards": yellow_cards,
        "yellow_cards_count": stats_row.yellow_cards,
        "red_cards": red_cards,
        "red_cards_count": stats_row.red_cards,
        "own_goals": own_goals,
        "own_goals_count": stats_row.own_goals,
        "penalties_saved": penalties_saved,
        "penalties_saved_count": stats_row.penalties_saved,
        "penalties_missed": penalties_missed,
        "penalties_missed_count": stats_row.penalties_missed,
        "total": total,
    }


def calculate_dream11_points(stats_row, position: str) -> int:
    return dream11_points_breakdown(stats_row, position)["total"]


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


def _score_one_team(
    engine, contest_id: int, team_id: int, user_id: int, season: str, gameweek: int, fixture_id: int
) -> int:
    with engine.connect() as conn:
        rows = conn.execute(
            TEAM_PLAYERS_STATS_QUERY,
            {"team_id": team_id, "season": season, "gameweek": gameweek, "fixture_id": fixture_id},
        ).all()

    # Now that the join is fixture-scoped this can only trip on genuine
    # team_players corruption -- a team with the wrong number of picks --
    # rather than on a double gameweek, which used to be its most likely
    # cause and was not a corruption at all.
    if len(rows) != DREAM11_TEAM_SIZE:
        raise ValueError(f"expected {DREAM11_TEAM_SIZE} team_players rows for team_id={team_id}, got {len(rows)}")

    per_player_points = {r.player_id: calculate_dream11_points(r, r.position) for r in rows}
    raw_points = sum(per_player_points.values())

    captain_row = next((r for r in rows if r.is_captain), None)
    vice_row = next((r for r in rows if r.is_vice_captain), None)

    captain_bonus = per_player_points[captain_row.player_id] * (CAPTAIN_MULTIPLIER - 1.0) if captain_row is not None else 0
    vice_bonus = per_player_points[vice_row.player_id] * (VICE_CAPTAIN_MULTIPLIER - 1.0) if vice_row is not None else 0

    final_points = round(raw_points + captain_bonus + vice_bonus)

    # Team total and per-player breakdown in ONE transaction: they are two
    # halves of the same result, and a crash between them would leave a
    # stored breakdown that does not sum to the stored total.
    with engine.begin() as conn:
        conn.execute(UPDATE_CONTEST_MEMBER_POINTS_STMT, {"contest_id": contest_id, "user_id": user_id, "total_points": final_points})
        conn.execute(
            UPDATE_TEAM_PLAYER_FINAL_STMT,
            [
                {
                    "team_id": team_id,
                    "player_id": r.player_id,
                    "final_points": per_player_points[r.player_id],
                    "final_minutes": r.minutes,
                }
                for r in rows
            ],
        )

    return final_points


def score_dream11_contest(engine, contest_id: int) -> dict:
    """Score every dream11.teams row for this contest.

    Returns {"scored": [...], "failed": [(user_id, error_message), ...],
    "skipped_finalized": bool}.

    A FINALIZED contest is skipped entirely. Its result is frozen, and
    rescoring it would be exactly the recompute-forever behaviour
    finalization exists to end -- a stale checkpoint ETA firing late, or a
    manual re-run, must not move a settled leaderboard. The database
    refuses it too (enforce_contest_result_immutability), so this check is
    the polite half of a guarantee that does not depend on it.
    """
    with engine.connect() as conn:
        contest_fixture = conn.execute(CONTEST_FIXTURE_QUERY, {"contest_id": contest_id}).first()
        teams = conn.execute(TEAMS_QUERY, {"contest_id": contest_id}).all()

    if contest_fixture is None:
        raise ValueError(f"contest_id {contest_id} does not exist")

    if contest_fixture.finalized_at is not None:
        logger.info(
            "score_dream11_contest: contest_id=%s was finalized at %s -- not rescoring",
            contest_id, contest_fixture.finalized_at,
        )
        return {"scored": [], "failed": [], "skipped_finalized": True}

    scored = []
    failed = []
    team_points: dict[int, int] = {}
    for t in teams:
        try:
            points = _score_one_team(
                engine, contest_id, t.team_id, t.user_id,
                contest_fixture.season, contest_fixture.gameweek, contest_fixture.fixture_id,
            )
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

    return {"scored": scored, "failed": failed, "skipped_finalized": False}


# A contest whose match will not produce a result: POSTPONED (FPL removed
# the kickoff -- Data/fpl_ingest.py's ingest_upcoming_fixtures clears
# ml.fixtures.kickoff_time) or ABANDONED (kicked off but still not finished
# a week later). Without this such a contest waits forever, since
# finalization needs fixtures.finished. Voiding locks it (no more team
# edits, enforce_contest_lock) and stamps finalized_at too, so it stops
# being rescored and every existing "is it over" check sees it closed.
# A postponed-then-rescheduled match does not revive the contest; managers
# create a new one against the new date.
ABANDONED_AFTER = "7 days"

VOID_UNPLAYABLE_CONTESTS_STMT = text(
    f"""
    UPDATE dream11.contests c
    SET is_locked = TRUE,
        finalized_at = now(),
        voided_at = now(),
        void_reason = CASE WHEN f.kickoff_time IS NULL THEN 'postponed' ELSE 'abandoned' END
    FROM ml.fixtures f
    WHERE f.id = c.fixture_id
      AND c.finalized_at IS NULL
      AND f.finished IS NOT TRUE
      AND (f.kickoff_time IS NULL OR f.kickoff_time < NOW() - INTERVAL '{ABANDONED_AFTER}')
    RETURNING c.id AS contest_id, c.void_reason
    """
)


def void_unplayable_contests(engine) -> list[tuple[int, str]]:
    """Void every open contest on a postponed or abandoned fixture.
    Returns [(contest_id, reason), ...]."""
    with engine.begin() as conn:
        return [(r.contest_id, r.void_reason) for r in conn.execute(VOID_UNPLAYABLE_CONTESTS_STMT).all()]


def find_contests_needing_finalization(engine) -> list[int]:
    """Contest ids whose fixture is finished but whose result is still
    unfrozen. Beat-callable; see Worker/tasks.py's finalize_dream11_contests."""
    with engine.connect() as conn:
        return [r.contest_id for r in conn.execute(CONTESTS_NEEDING_FINALIZATION_QUERY).all()]


def finalize_dream11_contest(engine, contest_id: int) -> dict:
    """Score a contest one last time and freeze the result permanently.

    Returns {"finalized": bool, "reason": str | None, "score": <summary>}.

    THE GATE IS ml.fixtures.finished, NOT THE CLOCK. The obvious place to
    finalize is the kickoff+115min checkpoint that already scores the
    contest, and that task does call this -- but the offset is only ever a
    guess at when the match ended, and a result frozen from a guess is
    frozen wrong with no way back. So the offset decides when to LOOK and
    the fixture's own finished flag decides whether to FREEZE, the same
    split ml.player_gw_stats' settle path should have had.

    That gate is only trustworthy because ml.fixtures is now refreshed on
    a schedule (Worker/tasks.py's refresh_fixtures). Before that task
    existed the flag went stale for days at a time, and gating on it would
    have meant never finalizing anything.

    Two conditions must BOTH hold, and neither is negotiable:

      1. The fixture reports finished. A match still in play, postponed or
         abandoned has no final result to freeze.
      2. Every team scored. score_dream11_contest isolates each team in
         its own try/except so one broken team cannot abort the batch --
         but freezing while a team is in `failed` would make that team's
         zero permanent. A contest with any failure stays open so the
         sweep can retry it.

    Idempotent: a contest already finalized returns finalized=False with a
    reason and is not rescored.
    """
    with engine.connect() as conn:
        state = conn.execute(CONTEST_FINALIZATION_STATE_QUERY, {"contest_id": contest_id}).first()

    if state is None:
        raise ValueError(f"contest_id {contest_id} does not exist")

    if state.finalized_at is not None:
        return {"finalized": False, "reason": "already finalized", "score": None}

    if not state.finished:
        # Score anyway -- a live contest still wants a fresh leaderboard --
        # but leave it open.
        summary = score_dream11_contest(engine, contest_id)
        return {"finalized": False, "reason": "fixture is not finished", "score": summary}

    summary = score_dream11_contest(engine, contest_id)
    if summary["failed"]:
        logger.error(
            "finalize_dream11_contest: contest_id=%s NOT finalized -- %d team(s) failed to score: %s",
            contest_id, len(summary["failed"]), summary["failed"],
        )
        return {"finalized": False, "reason": "one or more teams failed to score", "score": summary}

    with engine.begin() as conn:
        conn.execute(STAMP_CONTEST_FINALIZED_STMT, {"contest_id": contest_id})

    logger.info(
        "finalize_dream11_contest: contest_id=%s finalized with %d team(s) scored",
        contest_id, len(summary["scored"]),
    )
    return {"finalized": True, "reason": None, "score": summary}
