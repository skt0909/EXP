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

STEP 4 IS NOW GONE ENTIRELY, not just moved. Free Hit itself (the chip, the
free_hit_squads table, and this revert task) was removed from the game
along with every other classic-FPL/chip concept when the tactical rules
replaced them -- there is nothing to schedule or read for it any more.
The step above is kept as history, not as a pointer to current code.

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


# --- Gameweek Engine · GameEngine/selection_carry_forward.py ----------

# Clones each manager's last submitted Starting XI into the next open
# gameweek, unless a transfer changed their squad since -- keeps the
# Dashboard's pitch/bench view unchanged across gameweeks by default.
# Every 300s, same cadence as the lock sweep it runs alongside.
from GameEngine.selection_carry_forward import carry_forward_selections


# --- Dream11 · Game_logic/dream11_locking.py --------------------------

# Locks each Dream11 contest at its OWN fixture's kickoff -- the only
# writer of dream11.contests.is_locked anywhere. Every 300s.
from Game_logic.dream11_locking import lock_started_contests


# --- Dream11 · Game_logic/dream11_scoring.py --------------------------

# Freezes the result of every contest whose fixture has finished: scores
# it one last time and stamps dream11.contests.finalized_at, after which
# nothing recomputes it. Every 900s. poll_due_fixtures' 'final' checkpoint
# normally does this first; this sweep is the guarantee behind it (a
# contest with a team that failed to score is retried here). The same
# task first voids contests on postponed or abandoned fixtures.
from Game_logic.dream11_scoring import (
    find_contests_needing_finalization,
    finalize_dream11_contest,
    void_unplayable_contests,
)


# --- ML predictions · Predict/prediction_scheduling.py ----------------

# Finds the earliest upcoming gameweek with no ml.ml_predictions rows. NOT
# on Beat any more: predictions are backfilled by
# Predict/backfill_predictions.py --auto (a systemd timer, outside the
# worker). Kept here because Worker/tasks.py's commented-out
# schedule_predictions still names it.
from prediction_scheduling import find_next_gameweek_needing_predictions


# --- Match Events · Data/live_poll.py -----------------------

# Which fixtures have a halftime/fulltime/final checkpoint due now, and a
# record that one has run. Every 60s (poll_due_fixtures). Also whether
# ml.fixtures needs a refresh at all (refresh_fixtures, below).
from live_poll import find_due_checkpoints, mark_checkpoint_done, fixtures_refresh_reason


# --- Match Events · Data/fpl_ingest.py --------------------------------

# Re-pulls the current season's full fixture list and upserts it, so
# scores, kickoff changes, postponements and the finished flag stay
# current. Beat ticks every 900s, but it only calls the API while a
# fixture is in play or once a day (fixtures_refresh_reason). The
# finished flag it writes is what triggers the 'final' checkpoint.
from fpl_ingest import refresh_current_season_fixtures

# Re-pulls bootstrap-static: every player's now_cost (GW mode's buy/sell
# price) and any player new to the game. Daily, after FPL's overnight
# price changes.
from fpl_ingest import refresh_player_prices


# Every name Beat ultimately depends on, in one list -- so an import that
# silently stops resolving fails here rather than at the first tick.
__all__ = [
    "lock_expired_gameweeks",
    "refresh_active_gameweeks",
    "carry_forward_selections",
    "lock_started_contests",
    "find_contests_needing_finalization",
    "finalize_dream11_contest",
    "void_unplayable_contests",
    "find_next_gameweek_needing_predictions",
    "find_due_checkpoints",
    "mark_checkpoint_done",
    "fixtures_refresh_reason",
    "refresh_current_season_fixtures",
    "refresh_player_prices",
]
