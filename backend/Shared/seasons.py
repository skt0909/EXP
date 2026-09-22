"""What counts as a REAL season, in one place.

THE BUG THIS EXISTS TO PREVENT. `ml.fixtures` is shared with the simulation
harness and with test runs, so it holds seasons that are not the game:
`SIM38OK`, `SIM38TST`, `SIMSMOKE`, `SIMGWE2E`, and -- less obviously -- the
numeric sentinels `9998-00` and `9999-00`, both of which are sitting in
`fpl_game` today. A season lookup with no filter, or with a filter that is
merely shaped right, treats those as game data.

`^[0-9]{4}-[0-9]{2}$` is NOT tight enough. It excludes the `SIM*` names but
matches both numeric sentinels, and they sort ABOVE every real season, so any
`max(season)` or "latest" ranking selects them outright. Measured against a
clone of `fpl_game` when GET /gameweeks/current started ranking by season:

    '^[0-9]{4}-[0-9]{2}$'    -> ('9998-00', 5)   <-- wrong season, live
    '^20[0-9]{2}-[0-9]{2}$'  -> ('2026-27', 5)

Hence the century prefix. It is the whole point of this module, and the
reason the pattern is defined once rather than written out at each call site
-- it was written out twice before, tightened in one of them, and the two
disagreed.

THIS IS A PATTERN, NOT A GUARANTEE. `20YY-YY` cannot distinguish a real
season from a test one that happens to be shaped like it; `TEST_SEASON` is
`2099-00` precisely so the suite exercises the real-season path. The actual
defence is that test and simulation seasons must never reach the production
database -- and the `9998-00` and `9999-00` rows in `fpl_game` are proof that
this has already failed once. Treat a filter here as the second line, not the
first.
"""
import re

# 2000-2099. A season is 'YYYY-YY' where the second half is the FOLLOWING
# year's last two digits, so 2099-00 is well formed (str(2100)[-2:] == '00')
# and is what TEST_SEASON uses.
REAL_SEASON_PATTERN = r"^20[0-9]{2}-[0-9]{2}$"

REAL_SEASON_RE = re.compile(REAL_SEASON_PATTERN)


def real_season_sql(column: str = "season") -> str:
    """The SQL predicate, for interpolation into a query string.

    Returns e.g. ``f.season ~ '^20[0-9]{2}-[0-9]{2}$'``. Interpolated as a
    STRUCTURAL part of the query, not a bind parameter, for the same reason
    deadlines.py interpolates its interval literal: Postgres will not accept a
    placeholder there. Safe because the input is a module-level constant and
    the column name is never user-supplied.

    Note for callers building f-strings: the value returned here contains
    braces, but it is substituted AFTER the f-string is evaluated, so those
    braces are never re-processed and must NOT be doubled.
    """
    return f"{column} ~ '{REAL_SEASON_PATTERN}'"


def is_real_season(season: str | None) -> bool:
    """True for a real game season. None and '' are False, never an error."""
    return bool(REAL_SEASON_RE.match(season or ""))
