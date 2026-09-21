"""
transfers.py — FastAPI endpoint for submitting a batch of transfers
(player swaps) for a user's squad in a given gameweek.

DESIGN NOTE: THIS FILE IS DELIBERATELY NOT SPLIT FURTHER.

A three-way split (transfer rules / chips / gameweek engine) was proposed
for this file in several planning documents and has now been
investigated properly. It is not achievable, for two reasons that are
properties of the game rather than of this code. The pure money rules
that COULD leave have left -- _selling_price and the banking recurrence
both live in Shared/rules.py now -- and what remains is one
submission path that does not come apart:

  1. _validate_transfers is a single ordered pass spanning all three
     categories: transfer rules (duplicates, ownership, position match,
     club cap, budget), the allowance, and the deadline lock. The
     endpoint's contract is that every failure is reported at once, in
     one list; splitting the function would produce several lists whose
     concatenation changes the ORDER errors appear in the 422 response.
     test_multiple_simultaneous_violations_all_reported_together asserts
     against that array.

  2. Budget and the club cap are whole-squad questions. Both are
     re-checked against the ENTIRE resulting squad rather than the
     transferred players, because one swap can tip an unrelated club over
     the limit through cumulative batch effects, so neither can be
     answered without the squad this module already holds.

PHASE 4c UPDATE. The second reason this file used to give was chip state:
chip_active was read from gw_selections.chip_used, tested against
rules.FREE_CHIPS, and decided whether the 20-transfer cap applied, how
many free slots existed, and what is_free each row was stamped with.
None of that exists any more -- there are no chips, no paid transfers and
no 20-transfer cap, every accepted transfer is free, and anything beyond
the allowance is rejected. The chip_active RESPONSE key survives as a
literal False so the current frontend keeps rendering; Phase 5 removes it.

This is a settled decision, not outstanding work.

CONCURRENCY, now closed. The reads that produce budget_remaining_after,
free_used_count and allowance used to happen in a connect() block that
closed before the write transaction opened, so two concurrent batches for
the same manager could each validate against the same pre-state and both
commit -- overspending the budget, or both claiming the last free
transfer. Read, validate and write are now ONE transaction, guarded by a
per-user advisory lock taken before the first read (see
ACQUIRE_USER_TRANSFER_LOCK_STMT).

Locking only the write would have closed nothing: by that point both
requests have already decided is_free and budget_remaining_after from the
same stale reads, and the writes are unconditional inserts of a settled
plan. That is why this was a transaction-boundary change rather than one
added line, and it is pinned by
test_transfer_concurrency.py::test_two_concurrent_batches_cannot_both_spend_the_last_free_transfer,
which fails against both the old code AND against a write-only lock.

transfers is APPEND-ONLY (enforce_transfers_immutability_fn blocks any
UPDATE/DELETE) -- unlike squad_selection.py/starting_xi.py, there is no
upsert/delete-reinsert resubmission pattern here. Every valid call
permanently records new transfer rows; nothing here can undo a prior
submission. Submitting the exact same transfer twice is not deduplicated
-- that's a deliberate choice (transfers are real historical events;
idempotency protection, if ever needed, belongs on the client/API-key
layer, not here).

Sell price follows the real FPL rule: a price RISE is only half
returned, rounded down to the nearest £0.1m, while a FALL is absorbed in
full. The arithmetic is rules._selling_price; it is called twice per
transfer -- once to validate the budget, once to record what was
actually paid -- and the result becomes both the outgoing squad_players
row's sell_price and the transfer row's price_out. Buying reads
ml.players.now_cost live, so the two directions are deliberately
asymmetric.

Selling a player does NOT remove their squad_players row, only flips it
to is_active = FALSE, and uq_squad_players_active is UNIQUE
(user_squad_id, player_id) with no is_active predicate -- one row per
player per squad for the whole season. Buying a player back later
therefore reactivates that same row (UPSERT_SQUAD_PLAYER_STMT) instead
of adding a second one. Repricing it clears the sell_price the earlier
sale wrote, so squad_players stops showing that the player was ever
sold; that is not a loss of history, since transfers is append-only and
still holds both the sale and the rebuy as separate rows. squad_players
answers "what does this squad hold now", transfers answers "what
happened".

Free transfers follow the real FPL banking rule: one is earned each
gameweek, unused ones roll over, and the bank is capped at 5. The
recurrence itself now lives in Shared/rules.py as the pure function
_free_transfers_available(), along with its constants and HIT_COST:

    available = min(5, max(0, available_prev - used_prev) + 1)

starting from 1. The max(0, ...) is what stops an over-spent gameweek
from borrowing against the next one -- going beyond the allowance costs
points (below), it never leaves a negative bank.

What stays here is free_transfers_available(conn, ...) below, the DB
half: it reads each prior gameweek's consumption and hands the plain
dict to the pure function. That split is why the rule can be tested on
hand-built histories without a database.

The allowance is DERIVED, not stored: it is recomputed from the
transfers table on every call by replaying that recurrence over the
prior gameweeks. There is deliberately no free_transfers column to drift
out of sync, and it cannot drift, because transfers is append-only --
once gameweek N's rows exist, the allowance for gameweek N+1 is fixed.
Each row's is_free flag records how the allowance was spent AT THE TIME,
so counting is_free = TRUE rows per gameweek gives exactly "how much of
that gameweek's allowance was consumed", which is what the recurrence
needs.

Within a gameweek the still-available free slots (allowance minus
is_free rows already recorded for it) go to the first transfer(s) in
submission order; the rest are paid (is_free = FALSE). Non-chip
gameweeks are capped at 20 total transfers. Results/scoring.py then
charges 4 points per is_free = FALSE row, which works out to the rule's
4 * max(0, made - available) without scoring needing to know anything
about banking.

If gw_selections.chip_used for this gameweek is 'wildcard' or
'free_hit', every transfer in the batch is free instead, uncapped.
Chip gameweeks are excluded from the banking recurrence, so saved free
transfers are retained for the following gameweek even though the chip
transfers themselves are stamped is_free = TRUE.

transfers has no FK/trigger tie to gw_selections, so this module
explicitly pre-checks the lock for (user_id, season, gameweek) before
writing, from TWO independent sources:

  1. gw_selections.is_locked -- the flag Beat's lock_expired_gameweeks
     sets (Worker/tasks.py).
  2. deadlines.deadline_has_passed -- 90 minutes before
     MIN(ml.fixtures.kickoff_time), asked fresh.

(2) is not redundant. is_locked only exists on rows a user has actually
submitted, and lock_expired_gameweeks only ever locks gameweeks that
already have a gw_selections row, so "no row yet" used to mean
"unlocked" unconditionally -- a user who never submitted a selection
could keep transferring into a gameweek that kicked off days ago. Both
sources are OR'd into the same single error message, so the 422 detail
is unchanged from a client's point of view.

A transfer must swap like-for-like position (player_in.position ==
player_out.position) to keep the squad's 2 GK/5 DEF/5 MID/3 FWD shape
valid without re-running full squad validation, and the max-3-per-club
cap (rules.MAX_PER_CLUB) is re-checked against the *entire*
resulting squad, not just the transferred players, since one swap can
tip an unrelated club over the cap via cumulative batch effects.

GET /transfers/used is read-only, reporting how many free/paid
transfers this user has already committed for a gameweek -- exposes
the exact free-slot/wildcard arithmetic submit_transfers computes
internally (FREE_TRANSFERS_USED_QUERY/FREE_CHIPS above) so a client
can show an accurate "N free transfers left" / cost estimate *before*
submitting, instead of guessing client-side.
"""

import logging
from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Shared.db_utils import get_engine
from Shared.rules import (
    FIRST_GAMEWEEK,
    FREE_TRANSFER_BANK_CAP,
    RULES_VERSION,
    MAX_PER_CLUB,
    _selling_price,
    _tactical_free_transfers_available,
)
from Data.auth import CurrentUser, get_current_user
from Shared.deadlines import deadline_has_passed

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


router = APIRouter()

USER_SQUAD_QUERY = text(
    "SELECT id AS user_squad_id, budget_remaining FROM user_squads "
    "WHERE user_id = :user_id AND season = :season"
)

ACTIVE_SQUAD_QUERY = text(
    """
    SELECT sp.id AS squad_player_id, sp.player_id, sp.purchase_price
    FROM squad_players sp
    JOIN user_squads us ON us.id = sp.user_squad_id
    WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE
    """
)

PLAYERS_LOOKUP_QUERY = text(
    "SELECT fpl_id, position, team_id, cost_start, COALESCE(now_cost, cost_start) AS now_cost FROM ml.players "
    "WHERE season = :season AND fpl_id = ANY(:player_ids)"
)

GW_SELECTION_QUERY = text(
    "SELECT is_locked FROM gw_selections "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek"
)

# Every query below excludes rows listed in cancelled_transfers. A
# cancelled transfer is one a Free Hit cancellation reversed, and the
# rule is simply that it did not happen: it consumed no allowance in its
# own gameweek and contributes nothing to the banking recurrence after
# it. The exclusion lives in each query rather than in a view because
# `transfers` stays the honest historical record -- it really does hold
# the row -- and only the arithmetic pretends otherwise.
NOT_CANCELLED = (
    "NOT EXISTS (SELECT 1 FROM cancelled_transfers c WHERE c.transfer_id = t.id)"
)

FREE_TRANSFERS_USED_QUERY = text(
    "SELECT COUNT(*) FROM transfers t "
    "WHERE t.user_id = :user_id AND t.season = :season AND t.gameweek = :gameweek "
    f"AND t.is_free = TRUE AND {NOT_CANCELLED}"
)

TOTAL_TRANSFERS_USED_QUERY = text(
    "SELECT COUNT(*) FROM transfers t "
    f"WHERE t.user_id = :user_id AND t.season = :season AND t.gameweek = :gameweek AND {NOT_CANCELLED}"
)

# Per-gameweek allowance consumption for every gameweek BEFORE this one --
# the input to the banking recurrence. Grouped in SQL so replaying a whole
# season is one round trip, not one per gameweek.
PRIOR_FREE_TRANSFERS_USED_QUERY = text(
    "SELECT t.gameweek, COUNT(*) AS used FROM transfers t "
    "WHERE t.user_id = :user_id AND t.season = :season AND t.gameweek < :gameweek "
    f"AND t.is_free = TRUE AND {NOT_CANCELLED} "
    "GROUP BY t.gameweek"
)

DEACTIVATE_SQUAD_PLAYER_STMT = text(
    "UPDATE squad_players SET is_active = FALSE, sell_price = :sell_price WHERE id = :squad_player_id"
)

# Buying a player back after selling them is ordinary FPL usage, but
# uq_squad_players_active is UNIQUE (user_squad_id, player_id) with NO
# is_active predicate -- a squad holds at most ONE row per player for the
# whole season, and selling only flips that row to is_active = FALSE
# rather than removing it. So a plain INSERT here collides with the
# seller's own leftover row and 500s. The upsert reactivates that row in
# place, repricing it at what the manager is paying NOW and clearing the
# stale sell_price from the earlier sale.
#
# Identical in shape to RESTORE_SQUAD_PLAYER_STMT in
# GameEngine/free_hit_revert.py, which hit this same constraint restoring a
# free-hit snapshot -- deliberately kept consistent between the two
# writers of squad_players. The ON CONFLICT arm is the rebuy path; the
# INSERT arm is a first-time buy.
UPSERT_SQUAD_PLAYER_STMT = text(
    """
    INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active)
    VALUES (:user_squad_id, :player_id, :purchase_price, NULL, TRUE)
    ON CONFLICT ON CONSTRAINT uq_squad_players_active DO UPDATE SET
        purchase_price = EXCLUDED.purchase_price,
        sell_price = NULL,
        is_active = TRUE
    """
)

INSERT_TRANSFER_STMT = text(
    """
    INSERT INTO transfers (user_id, season, gameweek, player_in_id, player_out_id, price_in, price_out, is_free, transferred_at)
    VALUES (:user_id, :season, :gameweek, :player_in_id, :player_out_id, :price_in, :price_out, :is_free, now())
    """
)

UPDATE_USER_SQUAD_STMT = text(
    "UPDATE user_squads SET budget_remaining = :budget_remaining, updated_at = now() "
    "WHERE id = :user_squad_id"
)


# --- per-user serialisation -------------------------------------------
#
# The first advisory lock in this codebase. Two-argument form deliberately:
# pg_advisory_xact_lock(bigint) shares ONE global key space across the whole
# database, so a bare pg_advisory_xact_lock(user_id) would collide with any
# future subsystem that happens to lock on the same integer. The (int, int)
# form partitions that space, so the namespace below reads as "this number is
# a user id, held for the transfer path", and a later per-user lock on some
# other resource takes its own namespace and can never contend with this one.
#
# Transaction-scoped, not session-scoped: it releases on COMMIT and equally on
# ROLLBACK, so there is no unlock call to forget and no way for a request that
# raises mid-transaction to strand it. Confirmed against this Postgres rather
# than assumed -- including that a second transaction genuinely waits, and
# that two different user ids do not block each other.
#
# Keyed on current_user.id, which is why this could not have been built before
# the auth cutover: on a caller-supplied id, any client could have taken any
# manager's lock and held their transfers hostage.
USER_TRANSFER_LOCK_NAMESPACE = 1

ACQUIRE_USER_TRANSFER_LOCK_STMT = text("SELECT pg_advisory_xact_lock(:namespace, :user_id)")


class TransferPair(BaseModel):
    player_out_id: int
    player_in_id: int


class TransfersRequest(BaseModel):
    season: str
    gameweek: int
    transfers: list[TransferPair]  # at least 1


class TransferOut(BaseModel):
    player_out_id: int
    player_in_id: int
    price_out: int
    price_in: int
    is_free: bool


class TransfersResponse(BaseModel):
    user_id: int
    season: str
    gameweek: int
    budget_remaining: int
    transfers: list[TransferOut]


class TransfersUsedResponse(BaseModel):
    user_id: int
    season: str
    gameweek: int
    free_transfers_used: int
    free_transfers_remaining: int
    # B6: chips are gone. The KEY stays so the current frontend keeps
    # rendering -- same stance as the dashboard's inert captaincy keys -- but
    # it is a literal False now, not read from anything. The frontend phase
    # removes it.
    chip_active: bool = False
    total_transfers_this_gameweek: int


RULESET_EPOCH_QUERY = text(
    "SELECT first_gameweek FROM ruleset_epochs "
    "WHERE season = :season AND rules_version = :rules_version"
)


def ruleset_first_gameweek(conn, season: str) -> int:
    """The first gameweek played under the current ruleset, from ruleset_epochs.

    STORED, never derived. See migration e7c4d81b3a95 for why: every candidate
    derived from gameplay data (gw_selections, gw_scores) sits on rows that can
    be deleted, and deleting one could move the anchor and silently restate
    every manager's allowance.

    A MISSING ROW IS NOT AN ERROR. It means this database has never known any
    other ruleset -- a fresh CI database, or a season that began under these
    rules -- so the answer is FIRST_GAMEWEEK. That is also what keeps every
    existing test working without an epoch fixture.
    """
    stored = conn.execute(
        RULESET_EPOCH_QUERY, {"season": season, "rules_version": RULES_VERSION}
    ).scalar()
    return FIRST_GAMEWEEK if stored is None else int(stored)


def free_transfers_available(conn, user_id: int, season: str, gameweek: int) -> int:
    """How many free transfers this manager has for this gameweek.

    Takes a Connection so callers that already hold one don't open a
    second -- submit_transfers reads it alongside everything else it needs
    in a single connect() block.
    """
    rows = conn.execute(
        PRIOR_FREE_TRANSFERS_USED_QUERY,
        {"user_id": user_id, "season": season, "gameweek": gameweek},
    ).all()
    first_gameweek = ruleset_first_gameweek(conn, season)

    # Transfers made BEFORE the ruleset began cannot consume an allowance that
    # did not exist yet, so they are dropped from the recurrence. They should
    # not be there at all -- the release happens between gameweeks -- so each
    # one is a data-integrity signal worth a warning rather than a silent skip.
    stale = {r.gameweek: r.used for r in rows if r.gameweek < first_gameweek}
    if stale:
        logger.warning(
            "user_id=%s season=%s: %s transfer(s) recorded in gameweek(s) %s, "
            "before the ruleset epoch (first_gameweek=%s). Ignored by the free-transfer "
            "recurrence. This should not happen: the ruleset starts between gameweeks.",
            user_id, season, sum(stale.values()), sorted(stale), first_gameweek,
        )

    used = {r.gameweek: r.used for r in rows if r.gameweek >= first_gameweek}
    return _tactical_free_transfers_available(used, gameweek, first_gameweek)


def _validate_transfers(
    req: TransfersRequest,
    user_id: int,
    user_squad_row,
    active_squad: dict[int, object],
    players: dict[int, object],
    gw_selection_row,
    free_used_count: int,
    total_used_count: int,
    deadline_passed: bool,
    allowance: int,
) -> tuple[list[str], int]:
    """Collect every validation failure instead of stopping at the first.

    Returns (errors, budget_remaining_after) -- the latter is only
    meaningful when errors is empty.
    """
    errors: list[str] = []

    if user_squad_row is None:
        errors.append(f"no squad found for user_id {user_id}, season {req.season} -- submit a squad first")

    if not req.transfers:
        errors.append("at least one transfer is required")

    # No paid transfers and no hits: anything beyond the free allowance is
    # REJECTED rather than charged 4 points. The allowance banks to
    # FREE_TRANSFER_BANK_CAP (2), so this is the whole cost model.
    free_left = max(0, allowance - free_used_count)
    if req.transfers and len(req.transfers) > free_left:
        errors.append(
            f"only {free_left} free transfer(s) available this gameweek "
            f"({allowance} allowance, {free_used_count} already used), "
            f"{len(req.transfers)} submitted -- there are no paid transfers, "
            f"so transfers beyond the allowance are rejected"
        )

    # Two independent lock sources, deliberately OR'd into ONE error so the
    # message stays exactly what clients already match on. is_locked only
    # exists once a selection has been submitted, so on its own it lets a
    # user who never picked an XI keep transferring into a gameweek that
    # kicked off days ago; deadline_passed closes that by asking
    # ml.fixtures directly. See deadlines.deadline_has_passed.
    locked_by_flag = gw_selection_row is not None and gw_selection_row.is_locked
    if locked_by_flag or deadline_passed:
        errors.append(f"gameweek {req.gameweek} is locked and can no longer be modified")

    out_ids = [t.player_out_id for t in req.transfers]
    in_ids = [t.player_in_id for t in req.transfers]

    same_player_pairs = [t for t in req.transfers if t.player_out_id == t.player_in_id]
    if same_player_pairs:
        errors.append(f"player_out_id and player_in_id must differ: {[t.player_out_id for t in same_player_pairs]}")

    seen_out: set[int] = set()
    dup_out = sorted({pid for pid in out_ids if pid in seen_out or seen_out.add(pid)})
    if dup_out:
        errors.append(f"duplicate player_out_id(s) in the same batch: {dup_out}")

    seen_in: set[int] = set()
    dup_in = sorted({pid for pid in in_ids if pid in seen_in or seen_in.add(pid)})
    if dup_in:
        errors.append(f"duplicate player_in_id(s) in the same batch: {dup_in}")

    overlap = sorted(set(out_ids) & set(in_ids))
    if overlap:
        errors.append(f"player_id(s) cannot appear as both an in and an out in the same batch: {overlap}")

    not_owned = sorted(pid for pid in set(out_ids) if pid not in active_squad)
    if not_owned:
        errors.append(f"player_out_id(s) not in user's active squad for season {req.season}: {not_owned}")

    already_owned = sorted(pid for pid in set(in_ids) if pid in active_squad)
    if already_owned:
        errors.append(f"player_in_id(s) already in user's active squad: {already_owned}")

    unresolved_in = sorted(pid for pid in set(in_ids) if pid not in players)
    if unresolved_in:
        errors.append(f"player_in_id(s) not found in ml.players for season {req.season}: {unresolved_in}")

    # Position match, only checkable for pairs where both sides resolved
    # against ml.players (lookup_ids always includes every out_id/in_id
    # regardless of ownership validity, so this doesn't depend on the
    # not_owned/already_owned checks above having passed).
    mismatched = []
    for t in req.transfers:
        out_player = players.get(t.player_out_id)
        in_player = players.get(t.player_in_id)
        if out_player is None or in_player is None:
            continue
        if out_player.position != in_player.position:
            mismatched.append(
                f"{t.player_out_id} ({out_player.position}) -> {t.player_in_id} ({in_player.position})"
            )
    if mismatched:
        errors.append(f"transfer(s) must swap the same position: {mismatched}")

    # Whole-squad club-cap re-check: start from the current active squad's
    # club counts, then simulate every out/in in the batch, regardless of
    # whether any single pair looks fine in isolation.
    club_counts = Counter(
        players[pid].team_id for pid in active_squad if pid in players
    )
    for t in req.transfers:
        out_info = players.get(t.player_out_id)
        in_info = players.get(t.player_in_id)
        if out_info is not None:
            club_counts[out_info.team_id] -= 1
        if in_info is not None:
            club_counts[in_info.team_id] += 1
    over_cap = {team_id: n for team_id, n in club_counts.items() if n > MAX_PER_CLUB}
    if over_cap:
        errors.append(f"max {MAX_PER_CLUB} players per club exceeded after transfer(s) for team_id(s): {over_cap}")

    # Always computed, regardless of other errors already found -- gating
    # this behind "no errors yet" would silently skip the budget check
    # whenever any other validation failure exists, contradicting
    # "collect every error, not just the first." Safe to compute
    # unconditionally: both sums only include pids that actually resolved.
    budget_remaining_after = user_squad_row.budget_remaining if user_squad_row is not None else None
    if user_squad_row is not None:
        total_price_out = sum(
            _selling_price(active_squad[pid].purchase_price, players[pid].now_cost)
            for pid in out_ids
            if pid in active_squad and pid in players
        )
        total_price_in = sum(players[pid].now_cost for pid in in_ids if pid in players)
        budget_remaining_after = user_squad_row.budget_remaining + total_price_out - total_price_in
        if budget_remaining_after < 0:
            errors.append(
                f"insufficient budget: transfer(s) would leave budget_remaining at {budget_remaining_after}"
            )

    return errors, budget_remaining_after


@router.get("/transfers/used", response_model=TransfersUsedResponse)
def get_transfers_used(
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> TransfersUsedResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        gw_selection_row = conn.execute(
            GW_SELECTION_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).first()
        free_used = conn.execute(
            FREE_TRANSFERS_USED_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).scalar()
        total_used = conn.execute(
            TOTAL_TRANSFERS_USED_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).scalar()
        allowance = free_transfers_available(conn, user_id, season, gameweek)


    # 0 under a chip means "uncapped", not "none left" -- the client reads
    free_remaining = max(0, allowance - free_used)

    return TransfersUsedResponse(
        user_id=user_id,
        season=season,
        gameweek=gameweek,
        free_transfers_used=free_used,
        free_transfers_remaining=free_remaining,
        chip_active=False,
        total_transfers_this_gameweek=total_used,
    )


@router.post("/transfers", response_model=TransfersResponse)
def submit_transfers(
    req: TransfersRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> TransfersResponse:
    user_id = current_user.id
    engine = get_engine()

    out_ids = [t.player_out_id for t in req.transfers]
    in_ids = [t.player_in_id for t in req.transfers]
    lookup_ids = list(dict.fromkeys(out_ids + in_ids))

    # Deliberately OUTSIDE the transaction below. deadline_has_passed takes an
    # Engine and opens its own connection, so calling it while already holding
    # one would make every in-flight request hold two -- with the default pool
    # (5 + 10 overflow) that is a deadlock waiting for enough concurrency. It
    # reads gameweek-wide fixture state, not this manager's, so it gains
    # nothing from being inside the per-user lock either.
    deadline_passed = deadline_has_passed(engine, req.season, req.gameweek)

    try:
        with engine.begin() as conn:
            # THE LOCK HAS TO COVER THE READS, NOT JUST THE WRITES.
            #
            # This used to be two connections: an engine.connect() block that
            # read the budget, the free-transfer consumption and the allowance
            # and then CLOSED, followed by a separate engine.begin() that wrote
            # a plan already decided from those reads. Serialising only the
            # write would have closed nothing, because by then both requests
            # have already computed budget_remaining_after and is_free from the
            # same pre-state -- the writes are unconditional inserts of a
            # settled plan, so running them one after another still lands two
            # transfers on one free slot, and still subtracts both costs from
            # the same starting budget.
            #
            # So read, validate and write are now one transaction, and the lock
            # is taken before the first read. Everything between the SELECTs and
            # the INSERTs is pure computation on values that cannot change
            # underneath us while it is held.
            conn.execute(
                ACQUIRE_USER_TRANSFER_LOCK_STMT,
                {"namespace": USER_TRANSFER_LOCK_NAMESPACE, "user_id": user_id},
            )

            user_squad_row = conn.execute(
                USER_SQUAD_QUERY, {"user_id": user_id, "season": req.season}
            ).first()

            active_squad = {
                row.player_id: row
                for row in conn.execute(ACTIVE_SQUAD_QUERY, {"user_id": user_id, "season": req.season})
            }

            all_relevant_ids = list(dict.fromkeys(list(active_squad.keys()) + lookup_ids))
            players = {
                row.fpl_id: row
                for row in conn.execute(PLAYERS_LOOKUP_QUERY, {"season": req.season, "player_ids": all_relevant_ids})
            }

            gw_selection_row = conn.execute(
                GW_SELECTION_QUERY, {"user_id": user_id, "season": req.season, "gameweek": req.gameweek}
            ).first()

            free_used_count = conn.execute(
                FREE_TRANSFERS_USED_QUERY, {"user_id": user_id, "season": req.season, "gameweek": req.gameweek}
            ).scalar()
            total_used_count = conn.execute(
                TOTAL_TRANSFERS_USED_QUERY, {"user_id": user_id, "season": req.season, "gameweek": req.gameweek}
            ).scalar()
            allowance = free_transfers_available(conn, user_id, req.season, req.gameweek)

            errors, budget_remaining_after = _validate_transfers(
                req, user_id, user_squad_row, active_squad, players, gw_selection_row,
                free_used_count, total_used_count, deadline_passed, allowance,
            )
            # Raising here rolls the transaction back, which is what releases
            # the lock -- pg_advisory_xact_lock is transaction-scoped, so it
            # comes off on rollback exactly as it does on commit. A rejected
            # batch has written nothing and holds nothing.
            if errors:
                raise HTTPException(status_code=422, detail=errors)

            transfer_plan = []
            for i, t in enumerate(req.transfers):
                out_row = active_squad[t.player_out_id]
                in_row = players[t.player_in_id]
                out_player = players[t.player_out_id]
                price_out = _selling_price(out_row.purchase_price, out_player.now_cost)
                # No paid transfers: anything beyond the allowance was rejected
                # in validation above, so every row that reaches here is free.
                is_free = True
                transfer_plan.append(
                    {
                        "player_out_id": t.player_out_id,
                        "player_in_id": t.player_in_id,
                        "squad_player_id": out_row.squad_player_id,
                        "price_out": price_out,
                        "price_in": in_row.now_cost,
                        "is_free": is_free,
                    }
                )

            for plan in transfer_plan:
                conn.execute(
                    DEACTIVATE_SQUAD_PLAYER_STMT,
                    {"squad_player_id": plan["squad_player_id"], "sell_price": plan["price_out"]},
                )
                conn.execute(
                    UPSERT_SQUAD_PLAYER_STMT,
                    {
                        "user_squad_id": user_squad_row.user_squad_id,
                        "player_id": plan["player_in_id"],
                        "purchase_price": plan["price_in"],
                    },
                )
                conn.execute(
                    INSERT_TRANSFER_STMT,
                    {
                        "user_id": user_id,
                        "season": req.season,
                        "gameweek": req.gameweek,
                        "player_in_id": plan["player_in_id"],
                        "player_out_id": plan["player_out_id"],
                        "price_in": plan["price_in"],
                        "price_out": plan["price_out"],
                        "is_free": plan["is_free"],
                    },
                )

            conn.execute(
                UPDATE_USER_SQUAD_STMT,
                {
                    "user_squad_id": user_squad_row.user_squad_id,
                    "budget_remaining": budget_remaining_after,
                },
            )
    except HTTPException:
        # The 422 raised above to abort the transaction is the endpoint's real
        # answer -- let it through untouched rather than reporting a validation
        # failure as a server error. Only reachable now that validation runs
        # inside the transaction.
        raise
    except SQLAlchemyError as e:
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return TransfersResponse(
        user_id=user_id,
        season=req.season,
        gameweek=req.gameweek,
        budget_remaining=budget_remaining_after,
        transfers=[
            TransferOut(
                player_out_id=plan["player_out_id"],
                player_in_id=plan["player_in_id"],
                price_out=plan["price_out"],
                price_in=plan["price_in"],
                is_free=plan["is_free"],
            )
            for plan in transfer_plan
        ],
    )
