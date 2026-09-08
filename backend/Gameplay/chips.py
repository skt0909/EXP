"""
chips.py — chip availability: the per-half accounting of which chips a
manager has left, and the read endpoint that exposes it.

Extracted from starting_xi.py, which was investigated for a full
three-way split and found to be only partly separable (see that file's
DESIGN NOTE). This module is the part that came out cleanly: the chip
period arithmetic, the usage tallies, and GET /chips/used, none of which
depend on squad or lineup state.

WHAT IS NOT HERE, deliberately. Chip *activation* stays in
starting_xi.py, because activating a chip is inseparable from submitting
the XI it applies to -- one request, one row in gw_selections, one
transaction alongside the Free Hit snapshot. Splitting the write path
would have meant splitting _validate_selection, which collects squad and
chip failures into one ordered error list, and breaking the dependency
where a pending Free Hit decides which squad the XI is validated
against. Both were judged genuine data dependencies rather than
organisational artefacts.

The dependency runs one way: starting_xi.py imports from here, never the
reverse. The pure rule constants (which chips exist, where the half
boundary falls) live further down still, in rules.py.

CONSECUTIVE FREE HITS. adjacent_free_hit_gameweeks() below is the single
implementation of that rule. It previously existed twice -- once in
starting_xi.py's validation and again inline in this endpoint -- which
meant the availability flag a client renders and the error it gets on
save were computed by two separate pieces of code that merely happened
to agree. They now cannot disagree.
"""

import logging
from collections import Counter

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
from Shared.rules import FIRST_HALF_LAST_GAMEWEEK

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

router = APIRouter()

# Chip usage elsewhere this season -- excludes this exact gameweek's own row
# (if any) so resubmitting the same chip for the same gw isn't mistaken for
# a second use. Shared with starting_xi.py's write path.
CHIP_USAGE_QUERY = text(
    """
    SELECT chip_type, gameweek_used
    FROM chips
    WHERE user_id = :user_id AND season = :season AND gameweek_used <> :gameweek
    ORDER BY gameweek_used
    """
)


class ChipsUsedResponse(BaseModel):
    user_id: int
    season: str
    gameweek: int
    wildcard_used: int
    wildcard_remaining: int
    bench_boost_available: bool
    triple_captain_available: bool
    free_hit_available: bool


def chip_period(gameweek: int) -> int:
    """1 for the first half of the season, 2 for the second."""
    return 1 if gameweek <= FIRST_HALF_LAST_GAMEWEEK else 2


def chip_usage_by_period(rows) -> dict[int, Counter]:
    usage = {1: Counter(), 2: Counter()}
    for row in rows:
        usage[chip_period(row.gameweek_used)][row.chip_type] += 1
    return usage


def free_hit_gameweeks(rows) -> set[int]:
    return {row.gameweek_used for row in rows if row.chip_type == "free_hit"}


def adjacent_free_hit_gameweeks(rows, gameweek: int) -> list[int]:
    """Gameweeks either side of `gameweek` in which a Free Hit was already
    played -- the consecutive-Free-Hit rule, in one place.

    A real FPL rule, not an invention of this codebase: since each half
    grants exactly one Free Hit, the only pair this can ever match is
    GW19 -> GW20, and the Premier League's own chip guidance is explicit
    that using the first-half Free Hit in GW19 rules out the second-half
    one in GW20 -- the squad has to revert at the next deadline, which a
    back-to-back Free Hit would have nothing coherent to revert to.
    Source: premierleague.com/en/news/4362027.

    Symmetric by design (abs), so it catches the pair from whichever side
    is submitted second. Returned sorted so the caller can put the
    offending gameweeks straight into an error message; callers that only
    need a yes/no just test truthiness.
    """
    return sorted(gw for gw in free_hit_gameweeks(rows) if abs(gw - gameweek) == 1)


@router.get("/chips/used", response_model=ChipsUsedResponse)
def get_chips_used(
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> ChipsUsedResponse:
    """Season-wide chip availability for this gameweek's half, so a client
    can grey out a spent chip and populate an accurate confirmation prompt
    *before* the user taps Activate, instead of only finding out via the
    422 on save."""
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        chip_usage_rows = conn.execute(
            CHIP_USAGE_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).all()

    chip_usage = chip_usage_by_period(chip_usage_rows)[chip_period(gameweek)]
    wildcard_used = chip_usage.get("wildcard", 0)

    free_hit_available = chip_usage.get("free_hit", 0) < 1
    if adjacent_free_hit_gameweeks(chip_usage_rows, gameweek):
        free_hit_available = False

    return ChipsUsedResponse(
        user_id=user_id,
        season=season,
        gameweek=gameweek,
        wildcard_used=wildcard_used,
        wildcard_remaining=max(0, 1 - wildcard_used),
        bench_boost_available=chip_usage.get("bench_boost", 0) < 1,
        triple_captain_available=chip_usage.get("triple_captain", 0) < 1,
        free_hit_available=free_hit_available,
    )
