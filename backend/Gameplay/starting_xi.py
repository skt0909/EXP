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

# The rules themselves. Phase 3 moved them out of this file into a pure module
# so they can be tested without a database -- this module now only reads the
# rows the rules need and translates the request into their input.
from Gameplay.selection_rules import SelectionInput, SwapInput, validate_selection

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
    INSERT INTO gw_selections (user_id, season, gameweek, tactic, submitted_at)
    VALUES (:user_id, :season, :gameweek, :tactic, now())
    ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
        tactic = EXCLUDED.tactic,
        submitted_at = now()
    RETURNING id
    """
)

DELETE_STARTING_XI_STMT = text("DELETE FROM starting_xi WHERE gw_selection_id = :gw_selection_id")

INSERT_STARTING_XI_STMT = text(
    """
    INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_bonus)
    VALUES (:gw_selection_id, :player_id, :position_slot, :is_bonus)
    """
)

DELETE_TACTICAL_SWAPS_STMT = text(
    "DELETE FROM tactical_swaps WHERE gw_selection_id = :gw_selection_id"
)

INSERT_TACTICAL_SWAP_STMT = text(
    """
    INSERT INTO tactical_swaps (gw_selection_id, player_out_id, player_in_id)
    VALUES (:gw_selection_id, :player_out_id, :player_in_id)
    """
)

# Which fixtures each of the submitted players has this gameweek, for swap
# timing. A player's club plays a fixture when it is either side of it, so the
# OR is the whole join condition; a double gameweek simply returns two rows.
PLAYER_FIXTURES_QUERY = text(
    """
    SELECT p.fpl_id AS player_id, f.kickoff_time
    FROM ml.players p
    JOIN ml.fixtures f
      ON f.season = p.season
     AND (f.home_team_id = p.team_id OR f.away_team_id = p.team_id)
    WHERE p.season = :season
      AND f.gameweek = :gameweek
      AND p.fpl_id = ANY(:player_ids)
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

class SwapPayload(BaseModel):
    player_out_id: int
    player_in_id: int


class GwSelectionRequest(BaseModel):
    season: str
    gameweek: int
    tactic: str
    player_ids: list[int]  # exactly 11 raw FPL ids, slots 1-11
    # exactly 4 raw FPL ids, and the ORDER is the meaning: slot 12 backup GK,
    # 13 outfield Auto Sub, 14 and 15 Tactical Subs.
    bench_order: list[int]
    bonus_player_ids: list[int]  # exactly 2, both starters, both the tactic's position
    swaps: list[SwapPayload] = []


class GwSelectionResponse(BaseModel):
    gw_selection_id: int
    user_id: int
    season: str
    gameweek: int
    tactic: str
    player_ids: list[int]
    bench_order: list[int]
    bonus_player_ids: list[int]
    swaps: list[SwapPayload]


def _validate_selection(req, squad_ids, positions, fixtures_by_player) -> list[str]:
    """Adapter: turn the request into the pure validator's input and call it.

    The rules themselves live in Gameplay/selection_rules.py so they can be
    tested on hand-built data with no database (decision A7). This function
    exists only to translate, and deliberately holds no rule of its own -- the
    old body it replaces had the 2-MID minimum and the captain/vice/chip checks
    inline, which is exactly why they could not be tested without Postgres.
    """
    return validate_selection(
        SelectionInput(
            season=req.season,
            gameweek=req.gameweek,
            tactic=req.tactic,
            player_ids=list(req.player_ids),
            bench_order=list(req.bench_order),
            bonus_player_ids=list(req.bonus_player_ids),
            swaps=[SwapInput(s.player_out_id, s.player_in_id) for s in req.swaps],
        ),
        squad_ids,
        positions,
        fixtures_by_player,
    )


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
    with engine.connect() as conn:
        squad_ids = {
            row.player_id
            for row in conn.execute(ACTIVE_SQUAD_QUERY, {"user_id": user_id, "season": req.season})
        }
        positions = {
            row.fpl_id: row.position
            for row in conn.execute(
                PLAYERS_LOOKUP_QUERY, {"season": req.season, "player_ids": unique_ids}
            )
        }
        fixtures_by_player: dict[int, list] = {}
        for row in conn.execute(
            PLAYER_FIXTURES_QUERY,
            {"season": req.season, "gameweek": req.gameweek, "player_ids": unique_ids},
        ):
            fixtures_by_player.setdefault(row.player_id, []).append(row.kickoff_time)

    errors = _validate_selection(req, squad_ids, positions, fixtures_by_player)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    bonus_ids = set(req.bonus_player_ids)

    try:
        # ONE transaction. The child triggers are DEFERRABLE INITIALLY
        # DEFERRED, so delete-and-reinsert is legal inside it: the bonus count
        # and the swap references are checked once at COMMIT, against the final
        # state, not against each intermediate row.
        with engine.begin() as conn:
            gw_selection_id = conn.execute(
                UPSERT_GW_SELECTION_STMT,
                {
                    "user_id": user_id,
                    "season": req.season,
                    "gameweek": req.gameweek,
                    "tactic": req.tactic,
                },
            ).scalar()

            # Swaps first: they reference starting_xi rows, so clearing them
            # before the XI is deleted keeps the intermediate state sane even
            # though the trigger would not look until commit.
            conn.execute(DELETE_TACTICAL_SWAPS_STMT, {"gw_selection_id": gw_selection_id})
            conn.execute(DELETE_STARTING_XI_STMT, {"gw_selection_id": gw_selection_id})

            rows = [
                {
                    "gw_selection_id": gw_selection_id,
                    "player_id": pid,
                    "position_slot": slot,
                    "is_bonus": pid in bonus_ids,
                }
                for slot, pid in enumerate(
                    list(req.player_ids) + list(req.bench_order), start=1
                )
            ]
            conn.execute(INSERT_STARTING_XI_STMT, rows)

            if req.swaps:
                conn.execute(
                    INSERT_TACTICAL_SWAP_STMT,
                    [
                        {
                            "gw_selection_id": gw_selection_id,
                            "player_out_id": s.player_out_id,
                            "player_in_id": s.player_in_id,
                        }
                        for s in req.swaps
                    ],
                )
    except SQLAlchemyError as exc:
        if _is_lock_violation(exc):
            raise HTTPException(status_code=422, detail=LOCKED_DETAIL)
        logger.error("Database write failed: %s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail="Internal server error")

    return GwSelectionResponse(
        gw_selection_id=gw_selection_id,
        user_id=user_id,
        season=req.season,
        gameweek=req.gameweek,
        tactic=req.tactic,
        player_ids=list(req.player_ids),
        bench_order=list(req.bench_order),
        bonus_player_ids=list(req.bonus_player_ids),
        swaps=list(req.swaps),
    )
