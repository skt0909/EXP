"""
deadlines.py — when a gameweek's FPL deadline falls, and whether it has
passed.

First extraction from Game_logic/scheduling.py's gameweek engine, and the
safest: both functions here are pure reads. They open a connection, run
one query, return a value. No writes, and no transaction shared with
anything left behind. None of the modules it was split alongside call
these either -- lock_expired_gameweeks answers the same question with its
own set-based query rather than calling deadline_has_passed per gameweek.

WHY THIS LIVES IN Shared/ RATHER THAN WITH THE GAMEWEEK ENGINE. It was
extracted from the engine and moved here later, deliberately. Four modules
consume it -- starting_xi and transfers (Gameplay), team_dashboard
(Results), gameweek_lock (Game Engine) -- so leaving it in the engine made
Results depend on Game Engine while Game Engine already depended on
Results through gameweek_finalize, a dependency cycle between two
packages. Nothing here imports any of them back: this module's only
imports are logging and sqlalchemy, which is what makes it safe to sit
underneath everything. Same reasoning, and the same move, as rules.py.

The deadline is MIN(ml.fixtures.kickoff_time) for the gameweek, minus 90
minutes. It is DERIVED, never stored: there is no deadline column, and
asking twice can legitimately give different answers if fixtures are
re-ingested. None means "this gameweek's fixtures have not been ingested
yet", which is a legitimate expected state rather than an error --
gw_selections rows can exist for a gameweek before its kickoff times are
loaded.

Both comparisons happen in SQL so the DATABASE clock is authoritative.
That matters more than it looks: a Python-side comparison would introduce
clock skew between the API process and the database, and the whole
lock/deadline design assumes one clock. It is also why the test suite
crosses a deadline by UPDATE-ing ml.fixtures.kickoff_time rather than by
freezing Python's clock -- freezegun and time-machine would move the
wrong clock and silently do nothing.

DEADLINE_OFFSET_MINUTES below is the single source of truth for the
offset. All four queries that apply it -- the two here, and the two in
GameEngine/gameweek_lock.py that compute the same deadline set-based for
the lock sweep -- interpolate this constant rather than repeating the
literal. It used to be written out four times across two files with this
constant referenced by nothing but comments; changing the deadline is now
one edit.

Changing it invalidates a timing analysis, though. The lock-vs-scoring
race is currently harmless partly BECAUSE this offset gives locking a
90-minute head start over scoring, and that argument weakens as the
number shrinks. Read the race comment -- it appears in both
gameweek_lock.py and gameweek_finalize.py -- before touching this.
"""

import logging

from sqlalchemy import text

logger = logging.getLogger(__name__)

# The one place the FPL deadline offset is defined. GameEngine/gameweek_lock.py
# imports it for its own two queries; nothing else should restate the number.
DEADLINE_OFFSET_MINUTES = 90

# f-string, not a bind parameter: an interval literal is part of the query's
# structure, not a value, so Postgres will not accept a placeholder there.
# Safe because the input is a module-level int, never anything user-supplied.
_DEADLINE_EXPR = f"MIN(kickoff_time) - interval '{DEADLINE_OFFSET_MINUTES} minutes'"

GAMEWEEK_DEADLINE_QUERY = text(
    f"SELECT {_DEADLINE_EXPR} "
    "FROM ml.fixtures WHERE season = :season AND gameweek = :gameweek"
)

# Comparison stays in SQL so the DB clock is authoritative, same reason
# EXPIRED_UNLOCKED_GAMEWEEKS_QUERY does it that way. MIN over zero rows
# (or all-NULL kickoff_times) yields NULL, and NULL <= NOW() is NULL, so
# "not ingested yet" comes back as None rather than TRUE/FALSE --
# deadline_has_passed() maps that to False, matching
# lock_expired_gameweeks' "skip, don't lock" stance for the same state.
DEADLINE_PASSED_QUERY = text(
    f"SELECT {_DEADLINE_EXPR} <= NOW() "
    "FROM ml.fixtures WHERE season = :season AND gameweek = :gameweek"
)


def resolve_gameweek_deadline(engine, season: str, gameweek: int):
    """Returns the tz-aware deadline datetime, or None if this
    gameweek's fixtures haven't been ingested yet."""
    with engine.connect() as conn:
        return conn.execute(GAMEWEEK_DEADLINE_QUERY, {"season": season, "gameweek": gameweek}).scalar()


def deadline_has_passed(engine, season: str, gameweek: int) -> bool:
    """True once this gameweek's FPL deadline is in the past.

    Derived fresh from ml.fixtures every call, exactly like
    resolve_gameweek_deadline -- NOT read from gw_selections.is_locked.
    That distinction is the whole point: is_locked only exists on rows a
    user has actually submitted, so it cannot gate a gameweek the user
    has never touched. Gameplay/transfers.py and
    Gameplay/starting_xi.py both consult this in addition to the
    is_locked flag, so a user who never submitted a selection still
    can't act on a gameweek whose deadline has gone.

    Returns False when the gameweek's fixtures haven't been ingested yet
    -- an unknown deadline is treated as "not passed", so missing
    ingestion can never silently freeze a gameweek nobody can play.
    """
    with engine.connect() as conn:
        return bool(
            conn.execute(DEADLINE_PASSED_QUERY, {"season": season, "gameweek": gameweek}).scalar()
        )
