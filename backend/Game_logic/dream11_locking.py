"""
dream11_locking.py — locks Dream11 contests once their fixture kicks off.

Relocated verbatim from Game_logic/scheduling.py, which had accumulated
this alongside the classic-FPL gameweek engine purely because both are
called by Celery Beat. Beat is a deployment concern, not a domain one:
this function shares no code, no table and no invariant with the
gameweek lock it used to sit beside. The only thing they had in common
was a cadence.

A separate small module rather than an addition to dream11.py: that file
is the FastAPI router, and importing it from Worker/tasks.py would drag
the whole endpoint surface -- and FastAPI itself -- into the Celery
worker just to reach one function. Naming follows the existing
dream11_scoring.py convention.

Contest deadlines differ from gameweek deadlines in kind, not just
value. A gameweek's deadline is MIN(kickoff_time) across all its
fixtures, offset by 90 minutes; a contest is scoped to exactly ONE
fixture, so there is nothing to take a MIN over and no offset applies --
a contest locks at its own kickoff, precisely.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# Split from the "no kickoff_time yet" case rather than folded into one
# query with a CASE, so the <= NOW() comparison stays in SQL (no
# Python/DB clock-skew risk) and "not ingested yet" is simply "this query
# didn't match it" -- the same stance lock_expired_gameweeks takes.
STARTED_UNLOCKED_CONTESTS_QUERY = text(
    """
    SELECT c.id AS contest_id, c.fixture_id, f.kickoff_time
    FROM dream11.contests c
    JOIN ml.fixtures f ON f.id = c.fixture_id
    WHERE c.is_locked = FALSE
      AND f.kickoff_time IS NOT NULL
      AND f.kickoff_time <= NOW()
    """
)

UNLOCKED_CONTESTS_NO_KICKOFF_QUERY = text(
    """
    SELECT c.id AS contest_id, c.fixture_id
    FROM dream11.contests c
    JOIN ml.fixtures f ON f.id = c.fixture_id
    WHERE c.is_locked = FALSE AND f.kickoff_time IS NULL
    """
)

# is_locked FALSE -> TRUE only, enforced by the WHERE clause. dream11's
# enforce_contest_lock trigger is BEFORE INSERT on dream11.teams, not an
# UPDATE trigger on contests, so nothing blocks this the way
# enforce_selection_lock_fn constrains the classic gameweek lock.
LOCK_CONTEST_STMT = text(
    "UPDATE dream11.contests SET is_locked = TRUE WHERE id = :contest_id AND is_locked = FALSE"
)


def lock_started_contests(engine) -> dict:
    """Locks every dream11 contest whose fixture has kicked off.

    This is the ONLY thing that ever sets dream11.contests.is_locked TRUE.
    Both places that read the flag -- dream11.py's join_contest and the
    enforce_contest_lock trigger on dream11.teams -- were fully implemented
    and tested against a hand-set flag, but nothing in production code
    flipped it, so contests stayed joinable and submittable indefinitely
    after kickoff. Beat runs this on a 5-minute cadence, which bounds how
    long that window stays open.

    Contests whose fixture has no kickoff_time yet are skipped with a
    warning, not an error.

    One transaction per contest, deliberately: a single bad row cannot
    abort the batch.

    Returns {"locked": [contest_id, ...], "skipped_no_kickoff": [contest_id, ...]}.
    """
    with engine.connect() as conn:
        started = conn.execute(STARTED_UNLOCKED_CONTESTS_QUERY).all()
        no_kickoff = conn.execute(UNLOCKED_CONTESTS_NO_KICKOFF_QUERY).all()

    for c in no_kickoff:
        logger.warning(
            "lock_started_contests: no kickoff_time yet for contest_id=%s (fixture_id=%s) -- skipping",
            c.contest_id, c.fixture_id,
        )

    locked = []
    for c in started:
        with engine.begin() as conn:
            conn.execute(LOCK_CONTEST_STMT, {"contest_id": c.contest_id})
        locked.append(c.contest_id)

    return {"locked": locked, "skipped_no_kickoff": [c.contest_id for c in no_kickoff]}
