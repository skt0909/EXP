"""
gameweek_finalize.py — scores the gameweeks that are currently in play,
and recomputes the league tables that depend on them.

Third extraction from Game_logic/scheduling.py's gameweek engine, after
deadlines.py and gameweek_lock.py. This is the module that reaches into
the Results box: it is the only part of the old scheduling.py that
imported score_gameweek and compute_league_standings, so those imports
move with it and scheduling.py is left depending on neither.

WHAT "ACTIVE" MEANS, and why it is a time window rather than a flag. A
gameweek is active for a fixed window after its first kickoff (default 5
days), NOT while ml.fixtures.finished is false or
ml.player_gw_stats.is_live is true. Neither column is a usable recency
signal under this project's ingestion pipeline: is_live is written FALSE
by fpl_ingest's ingest_gameweek_stats, and ingest_fixtures refuses to
write any row for a gameweek until every fixture in it is finished, so
finished/updated_at flip together in one atomic batch per gameweek rather
than incrementally as matches complete. A real match window (Fri/Sat
through Monday) plus FPL's usual 24-48h bonus-point confirmation lag fits
comfortably inside 5 days.

Re-running is safe and expected. score_gameweek upserts on
(user_id, season, gameweek) and recomputes season_total from stored prior
rows, so a gameweek inside the window is rescored on every pass as more
player_gw_stats arrive. That is also what freezes history: once a
gameweek falls outside the window nothing revisits it, which is why
gw_scores.rules_version is only ever stamped forward and old rows keep
the version they were written under.

ORDERING: score before standings, per pair. Not transactional -- each
function manages its own -- but a real requirement, since
compute_league_standings reads the gw_scores rows score_gameweek has just
written. Do not reorder or parallelise these two.

One try/except per pair, deliberately: a single gameweek that fails to
score must not stop the others, and the failure is returned rather than
raised so the Beat task wrapper can log it without retrying the whole
batch.
"""

import logging

from sqlalchemy import text

# Phase 4a: the batched tactical job replaces the classic per-manager loop.
# Results/scoring.py stays on disk because Results/team_dashboard.py still
# imports resolve_autosubs from it -- unpicking that is Phase 4b.
from Results.scoring_job import score_gameweek_tactical as score_gameweek
from Results.standings import compute_league_standings

logger = logging.getLogger(__name__)

DEFAULT_ACTIVE_WINDOW_DAYS = 5

# Bind-parameterised on the window, and both comparisons stay in SQL so
# the DB clock is authoritative -- the same stance deadlines.py and
# gameweek_lock.py take, and the reason the test suite crosses a deadline
# by UPDATE-ing kickoff_time rather than by faking Python's clock.
ACTIVE_GAMEWEEKS_QUERY = text(
    """
    SELECT season, gameweek
    FROM ml.fixtures
    GROUP BY season, gameweek
    HAVING MIN(kickoff_time) IS NOT NULL
       AND MIN(kickoff_time) <= NOW()
       AND MIN(kickoff_time) > NOW() - make_interval(days => :window_days)
    """
)


# --- LOCK vs SCORING: a race that exists but currently costs nothing ---
#
# THIS ANALYSIS MUST TRAVEL WITH THIS CODE. Locking and scoring live in
# separate modules, so per this comment's own standing instruction it
# appears in BOTH -- here, and above lock_expired_gameweeks in
# GameEngine/gameweek_lock.py. Each side states the whole race, so
# neither file alone leaves a reader with half the picture. Keep them in
# step, or delete both together if the race is ever closed properly.
#
# THE RACE, in mechanism. Two independent Beat tasks, neither waiting on
# the other:
#
#   lock_expired_gameweeks    every 300s   eligible from  kickoff - 90min
#   refresh_active_gameweeks  every 900s   eligible from  kickoff
#
# Those cadences are set in Worker/celery_app.py's beat_schedule and are
# restated here only as prose -- nothing keeps them in sync, so if you
# change either one, re-check the arithmetic in reason (a) below.
#
# find_active_gameweeks in this file keys off MIN(kickoff_time), NOT off
# gw_selections.is_locked. Nothing structurally prevents scoring from
# running on a gameweek the lock task has not yet flagged.
#
# WHY IT IS HARMLESS TODAY. Two INDEPENDENT reasons -- each could be
# removed on its own, so neither is a safety net for the other:
#
#   (a) The deadline is kickoff - DEADLINE_OFFSET_MINUTES (90), defined
#       in Shared/deadlines.py and applied by gameweek_lock.py's
#       EXPIRED_UNLOCKED_GAMEWEEKS_QUERY. Locking therefore becomes
#       eligible 90 minutes before scoring does -- about eighteen
#       300-second attempts of head start. Scoring could only see an
#       unlocked gameweek if locking had been failing for an hour and a
#       half.
#
#   (b) More fundamentally, scoring does not care. Results/scoring.py's
#       GW_SELECTIONS_QUERY selects by (season, gameweek) and does NOT
#       filter on is_locked. The flag gates nothing in the scoring path,
#       and the endpoints that must refuse late edits gate on
#       deadline_has_passed rather than on is_locked. is_locked is
#       belt-and-braces state, not a precondition.
#
# So the race is real in mechanism and inert in effect -- but inert by
# two accidents of the current design, not by an invariant that would
# defend itself if either changed.
#
# WHAT WOULD MAKE IT LIVE AGAIN. This was raised for draft-transfer
# promotion at lock time -- the deferred PR4 of the transfer-drafts
# project, where locking would promote staged drafts into real transfers.
# Under that design scoring WOULD depend on the lock having run, reason
# (b) disappears entirely, and only the 90-minute offset in (a) stands
# between a manager and a gameweek scored without their confirmed
# transfers.
#
# RE-VERIFY, do not assume, if: that work lands, scoring starts reading
# is_locked, DEADLINE_OFFSET_MINUTES is reduced (it lives in a third
# file, deadlines.py, which carries a pointer back here), or either Beat
# cadence changes.


def find_active_gameweeks(engine, window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS) -> list[tuple[str, int]]:
    with engine.connect() as conn:
        rows = conn.execute(ACTIVE_GAMEWEEKS_QUERY, {"window_days": window_days}).all()
    return [(r.season, r.gameweek) for r in rows]


def refresh_active_gameweeks(engine, window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS) -> dict:
    """Runs score_gameweek then compute_league_standings for every
    currently-active (season, gameweek) pair -- the automatic version of
    what the manually-triggered compute_gw_scores/compute_league_standings
    tasks already do; no new scoring logic here.

    Returns {"refreshed": [(season, gameweek), ...], "failed": [(season, gameweek, error_message), ...]}.
    """
    active = find_active_gameweeks(engine, window_days)

    refreshed = []
    failed = []
    for season, gameweek in active:
        try:
            score_gameweek(engine, season, gameweek)
            compute_league_standings(engine, season, gameweek)
            refreshed.append((season, gameweek))
        except Exception as e:
            logger.error(
                "refresh_active_gameweeks: failed for season=%s gameweek=%s: %s: %s",
                season, gameweek, type(e).__name__, e,
            )
            failed.append((season, gameweek, str(e)))

    return {"refreshed": refreshed, "failed": failed}
