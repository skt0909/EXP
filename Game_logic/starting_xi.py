"""
starting_xi.py — FastAPI endpoint for submitting/replacing a user's
starting XI + bench + captain/vice-captain + chip for a single gameweek.

starting_xi.player_id is the RAW FPL id, same convention as
squad_players.player_id -- ml.players is only consulted to validate
positions, never to translate ids before insert.

position_slot 1-11 = starting XI (unchanged meaning). 12-15 = bench, in
substitution priority order (12 subs in first, 15 last). DECISION: bench
GK identity is NEVER encoded via slot number -- slot position only ever
means "priority order among the 4 bench players." Whichever of the 4
bench players is the GK is always determined by joining to
ml.players.position at query/scoring time, not by convention (e.g. "slot
15 is always the GK"). Any autosub logic built later must derive bench
GK this way, not via slot number.

gw_selections has a DB trigger (enforce_selection_lock_fn) that RAISES
on any UPDATE where OLD.is_locked = TRUE -- that trigger is the source
of truth for locking, not application code. This module does not
pre-check is_locked; it lets the upsert attempt the UPDATE and catches
the resulting DB exception, turning it into a 422.

chips has its own trigger (enforce_chip_limit_fn) capping
bench_boost/triple_captain/free_hit at 1 use/season and wildcard at 2
uses/season (DB migration applied directly: dropped
uq_chips_user_season_type, added a partial unique index that excludes
wildcard, and updated the trigger function to match). This module
pre-checks the same rule in application code before attempting the
insert, purely so a chip conflict shows up as a normal entry in the
validation-errors list instead of a raw trigger exception -- the DB
trigger remains the real enforcement point either way.

Resubmission (same gameweek, not yet locked) always overwrites, same
philosophy as squad_selection.py: gw_selections is upserted via
INSERT ... ON CONFLICT, starting_xi rows are deleted and reinserted,
and any existing chips row for this exact gameweek is deleted before
conditionally inserting a new one (covers switching chips, or
unsetting one, on resubmit -- not just re-picking the same chip).
"""

import logging
from collections import Counter

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Game_logic.db_utils import get_engine

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

STARTING_XI_SIZE = 11
BENCH_SIZE = 4
RESTRICTED_CHIPS = {"bench_boost", "triple_captain", "free_hit"}  # 1 use/season
VALID_CHIPS = RESTRICTED_CHIPS | {"wildcard"}  # wildcard: 2 uses/season

# enforce_selection_lock_fn's exact RAISE EXCEPTION text (see gw_selections
# DDL). Matching on this string is inherently fragile -- if that trigger's
# message is ever reworded, this stops recognizing a lock violation and the
# lock error starts falling through to the generic 500 branch instead of a
# clean 422. Keep this in sync with the trigger by hand.
LOCK_ERROR_SUBSTRING = "Selection is locked"

router = APIRouter()

ACTIVE_SQUAD_QUERY = text(
    """
    SELECT sp.player_id
    FROM squad_players sp
    JOIN user_squads us ON us.id = sp.user_squad_id
    WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE
    """
)

PLAYERS_LOOKUP_QUERY = text(
    "SELECT fpl_id, position FROM ml.players WHERE season = :season AND fpl_id = ANY(:player_ids)"
)

# Chip usage elsewhere this season -- excludes this exact gameweek's own row
# (if any) so resubmitting the same chip for the same gw isn't mistaken for
# a second use.
CHIP_USAGE_QUERY = text(
    """
    SELECT chip_type, COUNT(*) AS uses
    FROM chips
    WHERE user_id = :user_id AND season = :season AND gameweek_used <> :gameweek
    GROUP BY chip_type
    """
)

UPSERT_GW_SELECTION_STMT = text(
    """
    INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id, chip_used, submitted_at)
    VALUES (:user_id, :season, :gameweek, :captain_id, :vice_captain_id, :chip_used, now())
    ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
        captain_id = EXCLUDED.captain_id,
        vice_captain_id = EXCLUDED.vice_captain_id,
        chip_used = EXCLUDED.chip_used,
        submitted_at = now()
    RETURNING id
    """
)

DELETE_STARTING_XI_STMT = text("DELETE FROM starting_xi WHERE gw_selection_id = :gw_selection_id")

INSERT_STARTING_XI_STMT = text(
    """
    INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain)
    VALUES (:gw_selection_id, :player_id, :position_slot, :is_captain, :is_vice_captain)
    """
)

DELETE_CHIP_FOR_GW_STMT = text(
    "DELETE FROM chips WHERE user_id = :user_id AND season = :season AND gameweek_used = :gameweek"
)

INSERT_CHIP_STMT = text(
    """
    INSERT INTO chips (user_id, season, chip_type, gameweek_used, used_at)
    VALUES (:user_id, :season, :chip_type, :gameweek, now())
    """
)


class GwSelectionRequest(BaseModel):
    user_id: int
    season: str
    gameweek: int
    player_ids: list[int]  # exactly 11 raw FPL ids, starting XI
    bench_order: list[int]  # exactly 4 raw FPL ids, sub-priority order
    captain_id: int
    vice_captain_id: int
    chip_used: str | None = None


class GwSelectionResponse(BaseModel):
    gw_selection_id: int
    user_id: int
    season: str
    gameweek: int
    player_ids: list[int]
    bench_order: list[int]
    captain_id: int
    vice_captain_id: int
    chip_used: str | None


def _validate_selection(
    req: GwSelectionRequest,
    active_squad_ids: set[int],
    positions: dict[int, str],
    chip_usage: dict[str, int],
) -> list[str]:
    """Collect every validation failure instead of stopping at the first."""
    errors: list[str] = []

    if len(req.player_ids) != STARTING_XI_SIZE:
        errors.append(f"starting XI must contain exactly {STARTING_XI_SIZE} players, got {len(req.player_ids)}")

    seen: set[int] = set()
    duplicates = sorted({pid for pid in req.player_ids if pid in seen or seen.add(pid)})
    if duplicates:
        errors.append(f"duplicate player_id(s): {duplicates}")

    unique_ids = list(dict.fromkeys(req.player_ids))

    not_in_squad = sorted(pid for pid in unique_ids if pid not in active_squad_ids)
    if not_in_squad:
        errors.append(f"player_id(s) not in user's active squad for season {req.season}: {not_in_squad}")

    resolvable_ids = [pid for pid in unique_ids if pid in active_squad_ids]
    missing_position = sorted(pid for pid in resolvable_ids if pid not in positions)
    if missing_position:
        errors.append(f"player_id(s) in squad but not found in ml.players for season {req.season}: {missing_position}")

    position_counts = Counter(positions[pid] for pid in resolvable_ids if pid in positions)
    gk_count = position_counts.get("GK", 0)
    if gk_count != 1:
        errors.append(f"expected exactly 1 GK, got {gk_count}")

    def_count = position_counts.get("DEF", 0)
    if not (3 <= def_count <= 5):
        errors.append(f"DEF count must be between 3 and 5, got {def_count}")

    mid_count = position_counts.get("MID", 0)
    if not (2 <= mid_count <= 5):
        errors.append(f"MID count must be between 2 and 5, got {mid_count}")

    fwd_count = position_counts.get("FWD", 0)
    if not (1 <= fwd_count <= 3):
        errors.append(f"FWD count must be between 1 and 3, got {fwd_count}")

    outfield_total = def_count + mid_count + fwd_count
    if outfield_total != 10:
        errors.append(f"outfield players (DEF+MID+FWD) must sum to 10, got {outfield_total}")

    if len(req.bench_order) != BENCH_SIZE:
        errors.append(f"bench_order must contain exactly {BENCH_SIZE} players, got {len(req.bench_order)}")

    bench_seen: set[int] = set()
    bench_duplicates = sorted({pid for pid in req.bench_order if pid in bench_seen or bench_seen.add(pid)})
    if bench_duplicates:
        errors.append(f"duplicate player_id(s) in bench_order: {bench_duplicates}")

    bench_overlap = sorted(set(req.bench_order) & set(req.player_ids))
    if bench_overlap:
        errors.append(f"player_id(s) cannot appear in both player_ids and bench_order: {bench_overlap}")

    combined_ids = set(req.player_ids) | set(req.bench_order)
    missing_from_squad = sorted(active_squad_ids - combined_ids)
    extra_beyond_squad = sorted(combined_ids - active_squad_ids)
    if missing_from_squad or extra_beyond_squad:
        errors.append(
            f"player_ids + bench_order must exactly match the user's active squad for season {req.season} "
            f"-- missing from submission: {missing_from_squad}, not in squad: {extra_beyond_squad}"
        )

    bench_position_counts = Counter(positions[pid] for pid in req.bench_order if pid in positions)
    bench_gk_count = bench_position_counts.get("GK", 0)
    if bench_gk_count != 1:
        errors.append(f"bench_order must contain exactly 1 GK, got {bench_gk_count}")

    if req.captain_id == req.vice_captain_id:
        errors.append("captain_id and vice_captain_id must be different players")
    if req.captain_id not in req.player_ids:
        errors.append(f"captain_id {req.captain_id} is not in the submitted starting XI")
    if req.vice_captain_id not in req.player_ids:
        errors.append(f"vice_captain_id {req.vice_captain_id} is not in the submitted starting XI")

    if req.chip_used is not None:
        if req.chip_used not in VALID_CHIPS:
            errors.append(f"chip_used must be one of {sorted(VALID_CHIPS)} or null, got {req.chip_used!r}")
        elif req.chip_used in RESTRICTED_CHIPS:
            if chip_usage.get(req.chip_used, 0) >= 1:
                errors.append(f"chip '{req.chip_used}' has already been used this season")
        elif req.chip_used == "wildcard":
            if chip_usage.get("wildcard", 0) >= 2:
                errors.append("wildcard has already been used twice this season")

    return errors


def _is_lock_violation(exc: SQLAlchemyError) -> bool:
    """Detects enforce_selection_lock_fn's RAISE EXCEPTION by matching its
    message text -- see the LOCK_ERROR_SUBSTRING comment above."""
    return LOCK_ERROR_SUBSTRING in str(exc)


@router.post("/gw_selection", response_model=GwSelectionResponse)
def select_starting_xi(req: GwSelectionRequest) -> GwSelectionResponse:
    engine = get_engine()

    unique_ids = list(dict.fromkeys(req.player_ids + req.bench_order))
    with engine.connect() as conn:
        active_squad_ids = {
            row.player_id
            for row in conn.execute(ACTIVE_SQUAD_QUERY, {"user_id": req.user_id, "season": req.season})
        }
        positions = {
            row.fpl_id: row.position
            for row in conn.execute(PLAYERS_LOOKUP_QUERY, {"season": req.season, "player_ids": unique_ids})
        }
        chip_usage = {
            row.chip_type: row.uses
            for row in conn.execute(
                CHIP_USAGE_QUERY, {"user_id": req.user_id, "season": req.season, "gameweek": req.gameweek}
            )
        }

    errors = _validate_selection(req, active_squad_ids, positions, chip_usage)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    try:
        with engine.begin() as conn:
            gw_selection_id = conn.execute(
                UPSERT_GW_SELECTION_STMT,
                {
                    "user_id": req.user_id,
                    "season": req.season,
                    "gameweek": req.gameweek,
                    "captain_id": req.captain_id,
                    "vice_captain_id": req.vice_captain_id,
                    "chip_used": req.chip_used,
                },
            ).scalar()

            conn.execute(DELETE_STARTING_XI_STMT, {"gw_selection_id": gw_selection_id})
            starting_rows = [
                {
                    "gw_selection_id": gw_selection_id,
                    "player_id": pid,
                    "position_slot": slot,
                    "is_captain": pid == req.captain_id,
                    "is_vice_captain": pid == req.vice_captain_id,
                }
                for slot, pid in enumerate(req.player_ids, start=1)
            ]
            bench_rows = [
                {
                    "gw_selection_id": gw_selection_id,
                    "player_id": pid,
                    "position_slot": slot,
                    "is_captain": False,
                    "is_vice_captain": False,
                }
                for slot, pid in enumerate(req.bench_order, start=STARTING_XI_SIZE + 1)
            ]
            conn.execute(INSERT_STARTING_XI_STMT, starting_rows + bench_rows)

            conn.execute(
                DELETE_CHIP_FOR_GW_STMT,
                {"user_id": req.user_id, "season": req.season, "gameweek": req.gameweek},
            )
            if req.chip_used is not None:
                conn.execute(
                    INSERT_CHIP_STMT,
                    {
                        "user_id": req.user_id,
                        "season": req.season,
                        "chip_type": req.chip_used,
                        "gameweek": req.gameweek,
                    },
                )
    except SQLAlchemyError as e:
        if _is_lock_violation(e):
            raise HTTPException(
                status_code=422, detail="This gameweek's selection is locked and can no longer be changed"
            ) from e
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return GwSelectionResponse(
        gw_selection_id=gw_selection_id,
        user_id=req.user_id,
        season=req.season,
        gameweek=req.gameweek,
        player_ids=req.player_ids,
        bench_order=req.bench_order,
        captain_id=req.captain_id,
        vice_captain_id=req.vice_captain_id,
        chip_used=req.chip_used,
    )
