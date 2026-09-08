"""
beat_registry.py — the index of every function Celery Beat schedules,
and where each one actually lives.

WHY THIS FILE EXISTS. The functions behind the Beat schedule used to all
sit in Game_logic/scheduling.py -- a file that has since been split up
entirely and no longer exists -- so "what does Beat call?" was answerable
by opening one file. That co-location was misleading, though: several of
those functions had nothing to do with the gameweek engine and were only
neighbours because they shared a scheduler. Once they were moved to their
real domains -- Dream11 locking, ML prediction scheduling, live-poll
bookkeeping -- the answer to that question was scattered across four
modules.

This file restores the single answer without restoring the bad grouping.
It is an INDEX, not a layer: it contains no logic, no queries and no
error handling, and nothing should ever be added here that does work.
Worker/tasks.py remains the place where the retry/logging wrappers live,
and each function's own module remains the place its behaviour is
defined and tested.

THE SPLIT IS COMPLETE. scheduling.py no longer exists; it came apart in
four ordered steps, safest first, each verified against the full suite
before the next began:

    1. deadlines.py         deadline resolution (two pure reads)
    2. gameweek_lock.py     flipping gw_selections.is_locked
    3. gameweek_finalize.py scoring active gameweeks + standings
    4. free_hit_revert.py   the Free Hit revert -- a pure RENAME, done
                            last precisely because it was riskiest, so
                            that by the time its turn came nothing was
                            left in the file to move

Three unrelated tenants had already been evicted to their own domains
first: dream11_locking.py, Predict/prediction_scheduling.py, and the
poll-scheduling half of Data/live_poll.py. Nothing further is
outstanding -- treat this as settled rather than as work in progress.

Read this to find out WHAT Beat runs. Read Worker/celery_app.py's
beat_schedule to find out HOW OFTEN. Read the linked module to find out
what the function does.
"""

import sys
from pathlib import Path

# Predict/ and Data/ are not packages, so two of the imports
# below are flat and need those directories on sys.path. Done here rather
# than relying on the caller so this file can be imported on its own --
# an index nobody can open without first importing something else would
# defeat its purpose. Worker/tasks.py does the same inserts; both are
# idempotent.
_ROOT = Path(__file__).resolve().parent.parent
for _sub in ("Predict", "Data"):
    _path = str(_ROOT / _sub)
    if _path not in sys.path:
        sys.path.insert(0, _path)


# --- Gameweek Engine · GameEngine/gameweek_lock.py --------------------

# Flips gw_selections.is_locked FALSE -> TRUE once a gameweek's deadline
# (kickoff - 90 min) has passed. Every 300s.
from GameEngine.gameweek_lock import lock_expired_gameweeks


# --- Gameweek Engine · GameEngine/gameweek_finalize.py ----------------

# Scores every gameweek inside the active window, then recomputes league
# standings from the result. Every 900s. The lock above and this one race
# by design; the analysis appears in both modules' source.
from GameEngine.gameweek_finalize import refresh_active_gameweeks


# --- Free Hit · GameEngine/free_hit_revert.py -------------------------

# Restores the pre-Free-Hit squad once a chip's gameweek is over, and
# marks the snapshot reverted. Every 900s.
from GameEngine.free_hit_revert import revert_expired_free_hits


# --- Dream11 · Game_logic/dream11_locking.py --------------------------

# Locks each Dream11 contest at its OWN fixture's kickoff -- the only
# writer of dream11.contests.is_locked anywhere. Every 300s.
from Game_logic.dream11_locking import lock_started_contests


# --- Dream11 · Game_logic/dream11_scoring.py --------------------------

# Freezes the result of every contest whose fixture has finished: scores
# it one last time and stamps dream11.contests.finalized_at, after which
# nothing recomputes it. Every 900s. This is the GUARANTEE that a contest
# gets finalized -- the kickoff+115min checkpoint also tries, but it fires
# before FPL usually sets fixtures.finished, and its one-off ETA may never
# have been booked at all if the broker was down at contest creation.
from Game_logic.dream11_scoring import (
    find_contests_needing_finalization,
    finalize_dream11_contest,
)


# --- ML pipeline · Predict/prediction_scheduling.py -------------------

# Finds the earliest upcoming gameweek with no ml.ml_predictions rows.
# Weekly (Tue 06:00 UTC). Worker/tasks.py runs the pipeline on the result.
from prediction_scheduling import find_next_gameweek_needing_predictions


# --- Match Events · Data/live_poll.py -----------------------

# Finds unfinished future fixtures whose halftime/fulltime polls are not
# yet booked, and records that they have been. Every 900s.
from live_poll import find_fixtures_needing_poll_schedule, mark_fixture_polls_scheduled


# --- Match Events · Data/fpl_ingest.py --------------------------------

# Re-pulls the current season's full fixture list and upserts it, so
# scores, kickoff changes and the finished flag stay current. Every 900s,
# matching the poll-scheduling task above -- which reads the rows this
# writes, and so is only ever as fresh as this is.
from fpl_ingest import refresh_current_season_fixtures


# Every name Beat ultimately depends on, in one list -- so an import that
# silently stops resolving fails here rather than at the first tick.
__all__ = [
    "lock_expired_gameweeks",
    "refresh_active_gameweeks",
    "revert_expired_free_hits",
    "lock_started_contests",
    "find_contests_needing_finalization",
    "finalize_dream11_contest",
    "find_next_gameweek_needing_predictions",
    "find_fixtures_needing_poll_schedule",
    "mark_fixture_polls_scheduled",
    "refresh_current_season_fixtures",
]
