"""
starting_xi.py — FastAPI endpoint for submitting/replacing a user's
starting XI + bench + captain/vice-captain + chip for a single gameweek.

DESIGN NOTE: THIS FILE IS DELIBERATELY NOT SPLIT FURTHER.

It was investigated in full against a proposed three-way split (Squad/XI,
Chips, Gameweek Engine). The clean parts have since been extracted --
chip accounting and GET /chips/used to Gameplay/chips.py, GET
/gw_selection to Gameplay/lineup.py, the pure rule constants to
Shared/rules.py. What remains is one submission path that resisted
separation for two specific reasons, both of which are properties of the
game rather than of this code:

  1. _validate_selection is a single error-collection pass spanning both
     squad rules and chip rules. The endpoint's contract is that every
     failure is reported at once, in one ordered list; splitting the
     function would produce two lists whose concatenation changes the
     ORDER errors appear in the 422 response. Tests assert against that
     order, and clients render it.

  2. Chip state decides which squad the squad rules are checked against.
     A pending Free Hit means this submission may be cancelling it, in
     which case the XI must match the SNAPSHOT's 15 rather than the
     currently-active 15 -- and the error wording changes with it (see
     squad_label). Squad validation therefore cannot be evaluated until
     a chip question has been answered. That is a data dependency, not a
     layering accident, and no file boundary removes it.

Two further things would have had to be forced somewhere they do not
belong: gw_selections holds captain/vice, chip_used and is_locked in one
row, so no split of this file splits that row; and the Free Hit cancel
path writes cancelled_transfers, a table owned by transfers.py.

The write transaction below also carries ordering invariants that only
hold while it stays one transaction -- restore -> cancel -> delete, and
the snapshot-once guard reading the same has_pending_free_hit the
validation used. Decoupling the latter reintroduces a real bug this
project has already fixed once, where resubmitting an XI mid-chip
overwrote the pre-chip squad.

This is a settled decision, not outstanding work.

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
on any UPDATE where OLD.is_locked = TRUE. This module does not
pre-check is_locked; it lets the upsert attempt the UPDATE and catches
the resulting DB exception, turning it into a 422.

That trigger alone is NOT sufficient, because it is BEFORE UPDATE and
keys off OLD.is_locked: a gameweek the user has never submitted has no
row for it to fire on, so the INSERT half of the upsert was completely
unguarded -- you could pick a starting XI for a gameweek that kicked
off days ago, with the results already known. So this module also
pre-checks deadlines.deadline_has_passed (90 minutes before
MIN(ml.fixtures.kickoff_time), asked fresh) and rejects with the SAME
422 detail string the trigger path produces, so a client can't tell the
two apart. The
trigger remains the enforcement point for the already-locked-row case;
the pre-check covers the never-submitted case it structurally cannot
see. Gameplay/transfers.py closes the same hole the same way.

chips has its own trigger (enforce_chip_limit_fn) capping every chip to
one use per half-season, with the GW19/GW20 boundary enforced from
chips.gameweek_used. This module pre-checks the same rule in application
code before attempting the insert, purely so a chip conflict shows up as
a normal entry in the validation-errors list instead of a raw trigger
exception -- the DB trigger remains the real enforcement point either
way.

It also blocks consecutive Free Hit gameweeks, which is a REAL FPL rule
and not an invention of this codebase -- worth stating because it looks
like one. Since each half grants exactly one Free Hit, the only pair of
consecutive gameweeks it can ever apply to is GW19 -> GW20, and the
Premier League's own chip guidance is explicit that using the first-half
Free Hit in GW19 rules out the second-half one in GW20: the squad has to
revert to its pre-chip state at the next deadline, which a back-to-back
Free Hit would have nothing coherent to revert to. Source:
premierleague.com's 2025/26 two-sets-of-chips guidance
(https://www.premierleague.com/en/news/4362027). The abs(gw - N) == 1
check below is deliberately symmetric so it catches the pair from
whichever side is submitted second.

Activating chip_used='free_hit' additionally snapshots the user's
current active squad + budget into free_hit_squads, in the same
transaction. That snapshot is what GameEngine/free_hit_revert.py's
revert_expired_free_hits restores once the gameweek is over -- without
it a Free Hit was just a permanent second Wildcard, since
Gameplay/transfers.py made its transfers free but nothing ever put
the old squad back.

The snapshot is taken exactly ONCE per (user, season, gameweek), on the
submission that activates the chip, and is never retaken while it is
still pending -- the contract this table's migration
(c4e1a7b92f30) states outright. Resubmitting the XI is not only allowed
mid-chip, it is REQUIRED after making free-hit transfers (the previous
XI names players the manager no longer owns), so retaking the snapshot
on every submission meant the ordinary flow silently overwrote the
pre-chip squad with the post-chip one and the revert restored the
free-hit team to itself. That also removes the old "activate the chip
BEFORE making this gameweek's transfers" ordering trap: activation
order no longer changes what gets recorded.

Switching AWAY from a pending Free Hit (resubmitting the same gameweek
with a different chip, or none) CANCELS it: the snapshot is restored
via free_hit_revert.restore_free_hit_snapshot and then deleted, so an
unplayed chip leaves no trace and the chip itself is refunded by
DELETE_CHIP_FOR_GW_STMT below. Deleting rather than marking reverted is
required, not stylistic -- uq_free_hit_squads_user_season_gw_player has
no reverted_at predicate, so a kept row would collide if the manager
re-activated Free Hit for the same gameweek.

Because cancelling restores the squad, a cancelling submission is
validated against the SNAPSHOT's 15, not the currently-active 15: after
this request the manager owns the pre-chip squad, so that is the squad
their XI has to come from. When no free-hit transfers were made the two
sets are identical and cancelling is transparent.

Cancelling also gives back the free transfers the chip's moves
consumed, which is the other half of "your previous transfer situation
is restored". transfers is append-only
(enforce_transfers_immutability_fn), so those rows cannot be removed or
unmarked; instead CANCEL_FREE_HIT_TRANSFERS_STMT names them in
cancelled_transfers, and every query that counts transfers towards the
allowance -- transfers.py's three, plus scoring.py's hit deduction --
skips anything listed there. The effect is that the gameweek contributes
to the banking recurrence exactly as if the chip had never been
activated.

Only transfers made AFTER activation are reversed, bounded by the
snapshot's created_at. A transfer made earlier in the same gameweek was
never part of the chip, still stands in the restored squad, and still
counts against the allowance.

Resubmission (same gameweek, not yet locked) always overwrites, same
philosophy as squad_selection.py: gw_selections is upserted via
INSERT ... ON CONFLICT, starting_xi rows are deleted and reinserted,
and any existing chips row for this exact gameweek is deleted before
conditionally inserting a new one (covers switching chips, or
unsetting one, on resubmit -- not just re-picking the same chip).

GET /gw_selection is read-only, backing the Starting XI editor's
"load whatever was last saved for this gameweek" case -- has_selection
is False (not an error) when nothing's been submitted yet, same
"unstarted state" philosophy used throughout this project.

GET /chips/used is read-only, reporting season-wide chip availability
(excluding this exact gameweek's own row, same as CHIP_USAGE_QUERY
above) so a client can grey out an already-spent chip and populate an
accurate confirmation prompt *before* the user taps Activate, instead
of only finding out via the 422 on save.
"""

import logging
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Shared.db_utils import get_engine
from Gameplay.chips import (
    CHIP_USAGE_QUERY,
    adjacent_free_hit_gameweeks,
    chip_period,
    chip_usage_by_period,
)
from Data.auth import CurrentUser, get_current_user
from Shared.rules import BENCH_SIZE, STARTING_XI_SIZE, VALID_CHIPS
from Shared.deadlines import deadline_has_passed
from GameEngine.free_hit_revert import restore_free_hit_snapshot

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# enforce_selection_lock_fn's exact RAISE EXCEPTION text (see gw_selections
# DDL). Matching on this string is inherently fragile -- if that trigger's
# message is ever reworded, this stops recognizing a lock violation and the
# lock error starts falling through to the generic 500 branch instead of a
# clean 422. Keep this in sync with the trigger by hand.
LOCK_ERROR_SUBSTRING = "Selection is locked"

# Single 422 detail for BOTH lock paths -- the trigger catching an
# already-locked row, and the deadline pre-check catching a gameweek
# with no row yet. Deliberately identical so a client sees one
# consistent "too late" response either way.
LOCKED_DETAIL = "This gameweek's selection is locked and can no longer be changed"

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

# Free Hit snapshot. Written in the SAME transaction that activates the
# chip, so a squad can never be left with the chip set but no way back.
# Deleted (not marked reverted) when the chip is switched away on
# resubmit, mirroring DELETE_CHIP_FOR_GW_STMT -- an unplayed chip leaves
# no trace. Only ever taken for a gameweek that hasn't been reverted yet;
# see GameEngine/free_hit_revert.py's revert_expired_free_hits for the other end.
DELETE_FREE_HIT_SNAPSHOT_STMT = text(
    "DELETE FROM free_hit_squads WHERE user_id = :user_id AND season = :season "
    "AND gameweek = :gameweek AND reverted_at IS NULL"
)

# The "is this submission activating the chip, or resubmitting under a
# chip that is already live?" test -- and, when the chip is being
# switched away, the squad the manager is about to get back.
PENDING_FREE_HIT_SNAPSHOT_QUERY = text(
    "SELECT player_id FROM free_hit_squads WHERE user_id = :user_id AND season = :season "
    "AND gameweek = :gameweek AND reverted_at IS NULL"
)

# Reverses the free transfers a cancelled Free Hit consumed, by naming
# its transfers in cancelled_transfers -- `transfers` is append-only, so
# the rows themselves cannot be removed or unmarked (see that table's
# migration). transfers.py and scoring.py then count as if those moves
# never happened.
#
# Bounded by the snapshot's own created_at, NOT by the whole gameweek:
# transfers made BEFORE the chip was activated are not part of it and
# must keep counting, exactly as restore_free_hit_snapshot leaves them in
# the restored squad. All 15 snapshot rows are written by one statement
# so they share a created_at; MIN is just "the instant of activation".
#
# MUST run before DELETE_FREE_HIT_SNAPSHOT_STMT below -- it reads the
# snapshot it is bounded by.
CANCEL_FREE_HIT_TRANSFERS_STMT = text(
    """
    INSERT INTO cancelled_transfers (transfer_id)
    SELECT t.id
    FROM transfers t
    WHERE t.user_id = :user_id AND t.season = :season AND t.gameweek = :gameweek
      AND t.transferred_at >= (
          SELECT MIN(fhs.created_at) FROM free_hit_squads fhs
          WHERE fhs.user_id = :user_id AND fhs.season = :season
            AND fhs.gameweek = :gameweek AND fhs.reverted_at IS NULL
      )
    ON CONFLICT ON CONSTRAINT cancelled_transfers_pkey DO NOTHING
    """
)

INSERT_FREE_HIT_SNAPSHOT_STMT = text(
    """
    INSERT INTO free_hit_squads (user_id, season, gameweek, player_id, purchase_price, budget_remaining)
    SELECT :user_id, :season, :gameweek, sp.player_id, sp.purchase_price, us.budget_remaining
    FROM squad_players sp
    JOIN user_squads us ON us.id = sp.user_squad_id
    WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE
    """
)

class GwSelectionRequest(BaseModel):
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
    chip_usage_rows,
    cancelling_free_hit: bool = False,
) -> list[str]:
    """Collect every validation failure instead of stopping at the first.

    active_squad_ids is whatever squad this submission is authoritative
    against -- normally the currently-active 15, but the Free Hit
    snapshot's 15 when this request cancels a pending Free Hit, since
    that is what the manager will own once it commits. cancelling_free_hit
    only changes how the two squad-membership errors are WORDED, so a
    manager who submits their (now reverted) free-hit XI is told why the
    squad they can see isn't the squad being checked.
    """
    errors: list[str] = []
    squad_label = (
        "the squad restored by cancelling this gameweek's Free Hit"
        if cancelling_free_hit
        else f"the user's active squad for season {req.season}"
    )

    if len(req.player_ids) != STARTING_XI_SIZE:
        errors.append(f"starting XI must contain exactly {STARTING_XI_SIZE} players, got {len(req.player_ids)}")

    seen: set[int] = set()
    duplicates = sorted({pid for pid in req.player_ids if pid in seen or seen.add(pid)})
    if duplicates:
        errors.append(f"duplicate player_id(s): {duplicates}")

    unique_ids = list(dict.fromkeys(req.player_ids))

    not_in_squad = sorted(pid for pid in unique_ids if pid not in active_squad_ids)
    if not_in_squad:
        errors.append(f"player_id(s) not in {squad_label}: {not_in_squad}")

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
            f"player_ids + bench_order must exactly match {squad_label} "
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
        period = chip_period(req.gameweek)
        chip_usage = chip_usage_by_period(chip_usage_rows)[period]
        if req.chip_used not in VALID_CHIPS:
            errors.append(f"chip_used must be one of {sorted(VALID_CHIPS)} or null, got {req.chip_used!r}")
        elif chip_usage.get(req.chip_used, 0) >= 1:
            half = "first" if period == 1 else "second"
            errors.append(f"chip '{req.chip_used}' has already been used in the {half} half of this season")

        if req.chip_used == "free_hit":
            consecutive = adjacent_free_hit_gameweeks(chip_usage_rows, req.gameweek)
            if consecutive:
                errors.append(f"free_hit cannot be used in consecutive gameweeks: {consecutive}")

    return errors


def _is_lock_violation(exc: SQLAlchemyError) -> bool:
    """Detects enforce_selection_lock_fn's RAISE EXCEPTION by matching its
    message text -- see the LOCK_ERROR_SUBSTRING comment above."""
    return LOCK_ERROR_SUBSTRING in str(exc)


@router.post("/gw_selection", response_model=GwSelectionResponse)
def select_starting_xi(
    req: GwSelectionRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> GwSelectionResponse:
    user_id = current_user.id
    engine = get_engine()

    # Before any validation: the trigger below can only guard gameweeks
    # that already have a row (see module docstring), so a first-time
    # submission after kickoff has to be stopped here or not at all.
    if deadline_has_passed(engine, req.season, req.gameweek):
        raise HTTPException(status_code=422, detail=LOCKED_DETAIL)

    unique_ids = list(dict.fromkeys(req.player_ids + req.bench_order))
    chip_params = {"user_id": user_id, "season": req.season, "gameweek": req.gameweek}
    with engine.connect() as conn:
        active_squad_ids = {
            row.player_id
            for row in conn.execute(ACTIVE_SQUAD_QUERY, {"user_id": user_id, "season": req.season})
        }
        positions = {
            row.fpl_id: row.position
            for row in conn.execute(PLAYERS_LOOKUP_QUERY, {"season": req.season, "player_ids": unique_ids})
        }
        chip_usage_rows = conn.execute(
            CHIP_USAGE_QUERY, {"user_id": user_id, "season": req.season, "gameweek": req.gameweek}
        ).all()
        pending_free_hit_ids = {
            row.player_id for row in conn.execute(PENDING_FREE_HIT_SNAPSHOT_QUERY, chip_params)
        }

    # A pending snapshot means Free Hit is already live for this gameweek.
    # Keeping it selected is an ordinary resubmit; anything else cancels
    # the chip, which restores that snapshot below -- so it, not the
    # currently-active squad, is what this submission must match.
    has_pending_free_hit = bool(pending_free_hit_ids)
    cancelling_free_hit = has_pending_free_hit and req.chip_used != "free_hit"
    squad_ids_for_validation = pending_free_hit_ids if cancelling_free_hit else active_squad_ids

    errors = _validate_selection(
        req, squad_ids_for_validation, positions, chip_usage_rows, cancelling_free_hit
    )
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    try:
        with engine.begin() as conn:
            gw_selection_id = conn.execute(
                UPSERT_GW_SELECTION_STMT,
                {
                    "user_id": user_id,
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
                {"user_id": user_id, "season": req.season, "gameweek": req.gameweek},
            )
            if req.chip_used is not None:
                conn.execute(
                    INSERT_CHIP_STMT,
                    {
                        "user_id": user_id,
                        "season": req.season,
                        "chip_type": req.chip_used,
                        "gameweek": req.gameweek,
                    },
                )

            # Free Hit is the only chip that has to remember anything: the
            # squad it replaces. Three cases, see the module docstring --
            # take the snapshot once at activation, leave it strictly
            # alone while the chip is live, and restore-then-drop it if
            # the chip is cancelled before the deadline.
            if req.chip_used == "free_hit":
                if not has_pending_free_hit:
                    conn.execute(INSERT_FREE_HIT_SNAPSHOT_STMT, chip_params)
            elif has_pending_free_hit:
                restore_free_hit_snapshot(conn, user_id, req.season, req.gameweek)
                # Order matters: this reads the snapshot's created_at, so
                # it has to happen before the snapshot is dropped.
                conn.execute(CANCEL_FREE_HIT_TRANSFERS_STMT, chip_params)
                conn.execute(DELETE_FREE_HIT_SNAPSHOT_STMT, chip_params)
    except SQLAlchemyError as e:
        if _is_lock_violation(e):
            raise HTTPException(status_code=422, detail=LOCKED_DETAIL) from e
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return GwSelectionResponse(
        gw_selection_id=gw_selection_id,
        user_id=user_id,
        season=req.season,
        gameweek=req.gameweek,
        player_ids=req.player_ids,
        bench_order=req.bench_order,
        captain_id=req.captain_id,
        vice_captain_id=req.vice_captain_id,
        chip_used=req.chip_used,
    )
