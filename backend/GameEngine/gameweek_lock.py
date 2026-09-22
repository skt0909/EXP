"""
gameweek_lock.py — flips gw_selections.is_locked once a gameweek's
deadline has passed.

Second extraction from Game_logic/scheduling.py's gameweek engine, after
deadlines.py. Self-contained: reads two queries, writes one statement,
shares no transaction or state with the scoring refresh
(gameweek_finalize.py) or the free-hit revert it was split from.

WHAT LOCKING IS AND IS NOT. is_locked is belt-and-braces, not the gate.
The endpoints that must refuse late edits (starting_xi.py, transfers.py)
check deadlines.deadline_has_passed directly, because a gameweek a
manager never submitted has no gw_selections row for this task to flag.
The flag exists so an already-submitted row is defended by the
enforce_selection_lock trigger as well, and the two mechanisms
deliberately overlap.

The deadline is computed here rather than by calling
deadlines.deadline_has_passed per gameweek: this is a set-based sweep
over every unlocked pair at once, and a per-row Python call would be one
round trip each. Both derive the same value from the same constant --
see DEADLINE_OFFSET_MINUTES below.

Two queries rather than one with a CASE, so the comparison stays in SQL
(no Python/DB clock skew) and "no deadline yet" is trivially "the LEFT
JOIN found nothing". A gameweek whose fixtures have not been ingested is
skipped with a warning rather than locked -- an unknown deadline must
never freeze a gameweek nobody can play.

One transaction per pair, deliberately: a single failing gameweek cannot
abort the batch. LOCK_GAMEWEEK_STMT carries its own
`AND is_locked = FALSE`, which is what makes concurrent Beat runs safe
and keeps the direction FALSE -> TRUE only -- the enforce_selection_lock
trigger raises on any UPDATE of an already-locked row, so the WHERE
clause is load-bearing, not decorative.
"""

import logging

from sqlalchemy import text

from Shared.deadlines import DEADLINE_OFFSET_MINUTES
from Shared.seasons import real_season_sql

logger = logging.getLogger(__name__)

# The deadline offset is interpolated from deadlines.py rather than
# written out again. It used to be a bare '90 minutes' literal in four
# separate SQL strings across two files, with DEADLINE_OFFSET_MINUTES
# sitting alongside them referenced by nothing but comments -- a constant
# that looked authoritative and was not. All four now derive from it, so
# changing the deadline is one edit.
#
# f-string, not a bind parameter: an interval literal is part of the
# query's structure, not a value, and Postgres will not accept a
# placeholder there. Safe because the input is a module-level int, never
# anything user-supplied.
_DEADLINE_EXPR = f"MIN(kickoff_time) - interval '{DEADLINE_OFFSET_MINUTES} minutes'"

EXPIRED_UNLOCKED_GAMEWEEKS_QUERY = text(
    f"""
    SELECT gs.season, gs.gameweek
    FROM (SELECT DISTINCT season, gameweek FROM gw_selections WHERE is_locked = FALSE) gs
    JOIN (
        SELECT season, gameweek, {_DEADLINE_EXPR} AS deadline
        FROM ml.fixtures WHERE {real_season_sql()} GROUP BY season, gameweek
    ) f ON f.season = gs.season AND f.gameweek = gs.gameweek
    WHERE f.deadline IS NOT NULL AND f.deadline <= NOW()
    """
)

UNLOCKED_NO_DEADLINE_QUERY = text(
    f"""
    SELECT gs.season, gs.gameweek
    FROM (SELECT DISTINCT season, gameweek FROM gw_selections WHERE is_locked = FALSE
          AND {real_season_sql()}) gs
    LEFT JOIN (
        SELECT season, gameweek, {_DEADLINE_EXPR} AS deadline
        FROM ml.fixtures WHERE {real_season_sql()} GROUP BY season, gameweek
    ) f ON f.season = gs.season AND f.gameweek = gs.gameweek
    WHERE f.deadline IS NULL
    """
)

LOCK_GAMEWEEK_STMT = text(
    "UPDATE gw_selections SET is_locked = TRUE WHERE season = :season AND gameweek = :gameweek AND is_locked = FALSE"
)


# --- LOCK vs SCORING: a race that exists but currently costs nothing ---
#
# THIS ANALYSIS MUST TRAVEL WITH THIS CODE. It was written when locking
# and scoring lived in one file, where both eligibility windows could be
# read side by side; that proximity is gone, so it now appears in BOTH
# halves -- here, and above find_active_gameweeks in
# GameEngine/gameweek_finalize.py. Each side states the whole race, so neither
# file alone leaves a reader with half the picture. Keep them in step, or
# delete both together if the race is ever closed properly.
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
# gameweek_finalize.find_active_gameweeks keys off MIN(kickoff_time), NOT off
# gw_selections.is_locked. Nothing structurally stops scoring running on
# a gameweek this task has not yet flagged.
#
# WHY IT IS HARMLESS TODAY. Two INDEPENDENT reasons -- each could be
# removed on its own, so neither is a safety net for the other:
#
#   (a) The deadline is kickoff - DEADLINE_OFFSET_MINUTES (90), so this
#       task becomes eligible 90 minutes before scoring does -- about
#       eighteen 300-second attempts of head start. Scoring could only
#       see an unlocked gameweek if locking had been failing for an hour
#       and a half.
#
#   (b) More fundamentally, scoring does not care. scoring.py's
#       GW_SELECTIONS_QUERY selects by (season, gameweek) and does NOT
#       filter on is_locked. The flag gates nothing in the scoring path.
#
# So the race is real in mechanism and inert in effect -- but inert by
# two accidents of the current design, not by an invariant that would
# defend itself if either changed.
#
# WHAT WOULD MAKE IT LIVE AGAIN. This was raised for draft-transfer
# promotion at lock time -- the deferred PR4 of the transfer-drafts
# project, where locking would promote staged drafts into real transfers.
# Under that design scoring WOULD depend on this task having run, reason
# (b) disappears entirely, and only the 90-minute offset in (a) stands
# between a manager and a gameweek scored without their confirmed
# transfers.
#
# RE-VERIFY, do not assume, if: that work lands, scoring starts reading
# is_locked, DEADLINE_OFFSET_MINUTES is reduced (it lives in a third
# file, deadlines.py, which carries a pointer back here), or either Beat
# cadence changes.


def lock_expired_gameweeks(engine) -> dict:
    """Locks every (season, gameweek) with an unlocked gw_selections row
    whose deadline has passed. Skips (with a warning, not an error) any
    pair whose deadline can't be computed yet.

    Returns {"locked": [(season, gameweek), ...], "skipped_no_deadline": [(season, gameweek), ...]}.
    """
    with engine.connect() as conn:
        expired = conn.execute(EXPIRED_UNLOCKED_GAMEWEEKS_QUERY).all()
        no_deadline = conn.execute(UNLOCKED_NO_DEADLINE_QUERY).all()

    for p in no_deadline:
        logger.warning(
            "lock_expired_gameweeks: no fixtures/deadline yet for season=%s gameweek=%s -- skipping",
            p.season, p.gameweek,
        )

    locked = []
    for p in expired:
        with engine.begin() as conn:
            conn.execute(LOCK_GAMEWEEK_STMT, {"season": p.season, "gameweek": p.gameweek})
        locked.append((p.season, p.gameweek))

    return {
        "locked": locked,
        "skipped_no_deadline": [(p.season, p.gameweek) for p in no_deadline],
    }
