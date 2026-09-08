"""
free_hit_revert.py — the end-of-gameweek half of the Free Hit chip:
restoring the pre-chip squad once a chip's gameweek is over.

FORMERLY scheduling.py, and this is the last step of that file's
break-up. scheduling.py was originally "everything Celery Beat calls", a
deployment grouping rather than a domain one; it has been taken apart
along domain lines and no longer exists:

    Shared/deadlines.py             deadline resolution
    GameEngine/gameweek_lock.py     flipping gw_selections.is_locked
    GameEngine/gameweek_finalize.py scoring active gameweeks + standings
    GameEngine/free_hit_revert.py   this file
    Game_logic/dream11_locking.py   the Dream11 contest lock
    Predict/prediction_scheduling.py    next gameweek needing predictions
    Data/live_poll.py         live-poll scheduling

The rename was left until last because this is the riskiest of the four
pieces -- seven ordered statements, two nested transaction invariants,
and an external caller relying on the Connection contract below -- so it
was done only once nothing but the Free Hit revert remained here, making
it a pure rename with no logic moving.

Git history for this file predates the rename: use `git log --follow`.

Kept celery-independent, same reason scoring.py and standings.py are:
testable without a broker, with the Celery wrapper in Worker/tasks.py
staying thin. Worker/beat_registry.py is the index of what Beat actually
calls and which module now owns each piece.

revert_expired_free_hits backs the revert_free_hits Beat task and is the
second half of the chip -- Gameplay/starting_xi.py snapshots the
pre-chip squad into free_hit_squads on activation, this puts it back once
the gameweek is over. Unlike locking or scoring it waits for the gameweek
to FINISH rather than to start, because the free-hit squad has to stay
live through GET /squad while its own gameweek is being played. See the
comment block above the queries for how "over" is defined against two
ingestion paths with different reliability.

restore_free_hit_snapshot is shared: it takes a Connection rather than an
Engine so both this module and starting_xi.py's mid-gameweek chip
cancellation can commit different follow-ups alongside it, atomically.
That contract is load-bearing -- see the function's own docstring.
"""

import logging

from sqlalchemy import text


logger = logging.getLogger(__name__)

# --- Free Hit revert -------------------------------------------------
#
# A Free Hit squad lasts exactly one gameweek, then the pre-chip squad
# comes back. The revert must NOT fire at the deadline the way locking
# does -- the free-hit squad has to stay visible through GET /squad
# while its gameweek is actually being played -- so it waits until the
# gameweek is OVER.
#
# "Over" is deliberately two conditions OR'd per fixture, because this
# project has two ingestion paths writing ml.fixtures with different
# reliability (see this module's docstring on why `finished` is not a
# usable incremental signal): a fixture counts as done if it is flagged
# finished, or if its kickoff is far enough in the past that it cannot
# still be in progress. The gameweek is over when EVERY fixture in it is
# done. A gameweek with no fixtures ingested is never "over" -- same
# stance as an unknown deadline never being "passed".
#
# Scoring is unaffected by the revert: Results/scoring.py reads
# starting_xi (frozen at submission), never squad_players.
FREE_HIT_REVERT_BUFFER_HOURS = 3  # a match plus stoppage/overrun, comfortably

REVERTABLE_FREE_HITS_QUERY = text(
    """
    SELECT DISTINCT fhs.user_id, fhs.season, fhs.gameweek
    FROM free_hit_squads fhs
    WHERE fhs.reverted_at IS NULL
      AND EXISTS (SELECT 1 FROM ml.fixtures f WHERE f.season = fhs.season AND f.gameweek = fhs.gameweek)
      AND NOT EXISTS (
          SELECT 1 FROM ml.fixtures f
          WHERE f.season = fhs.season AND f.gameweek = fhs.gameweek
            AND f.finished = FALSE
            AND (f.kickoff_time IS NULL
                 OR f.kickoff_time > NOW() - make_interval(hours => :buffer_hours))
      )
    ORDER BY fhs.season, fhs.gameweek, fhs.user_id
    """
)

FREE_HIT_SNAPSHOT_QUERY = text(
    "SELECT player_id, purchase_price, budget_remaining FROM free_hit_squads "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek AND reverted_at IS NULL"
)

USER_SQUAD_ID_QUERY = text("SELECT id FROM user_squads WHERE user_id = :user_id AND season = :season")

# Deactivate rather than delete: the free-hit players really were owned
# during that gameweek, and squad_players already keeps sold players as
# inactive history.
DEACTIVATE_ALL_ACTIVE_STMT = text(
    "UPDATE squad_players SET is_active = FALSE WHERE user_squad_id = :user_squad_id AND is_active = TRUE"
)

# uq_squad_players_active is UNIQUE (user_squad_id, player_id) with NO
# is_active predicate, so a squad holds at most ONE row per player for
# the whole season. That makes the restore unambiguous -- "the pre-chip
# player's row" is a single well-defined row, already sitting there
# deactivated -- but it also means a plain INSERT is wrong: it collides
# with that very row. Hence the upsert, which reactivates in place and
# resets sell_price/purchase_price to the snapshot's values. The
# ON CONFLICT arm is the normal path here, not the exception; the INSERT
# arm only covers a snapshot player whose row was somehow removed.
RESTORE_SQUAD_PLAYER_STMT = text(
    """
    INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active)
    VALUES (:user_squad_id, :player_id, :purchase_price, NULL, TRUE)
    ON CONFLICT ON CONSTRAINT uq_squad_players_active DO UPDATE SET
        purchase_price = EXCLUDED.purchase_price,
        sell_price = NULL,
        is_active = TRUE
    """
)

# Restores the budget only -- there is no transfer-count field left to
# roll back, and deliberately so. Every count that affects a manager is
# derived live from `transfers` at the point it is needed: the banking
# recurrence (transfers.py) and the hit deduction (scoring.py) both
# exclude anything listed in cancelled_transfers, so a cancelled Free
# Hit's transfers stop counting without anything here decrementing a
# stored total. user_squads.total_transfers used to be that stored
# total; it was dropped (d8f4a2c60b19) precisely because nothing read it
# and it could not stay accurate across a cancellation.
#
# Note this revert path is the END-OF-GAMEWEEK one, where the chip was
# actually played -- those transfers really happened and are not
# cancelled. Mid-gameweek cancellation is a different path entirely, in
# starting_xi.py.
RESTORE_BUDGET_STMT = text(
    "UPDATE user_squads SET budget_remaining = :budget_remaining, updated_at = now() WHERE id = :user_squad_id"
)

MARK_FREE_HIT_REVERTED_STMT = text(
    "UPDATE free_hit_squads SET reverted_at = now() "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek AND reverted_at IS NULL"
)


def restore_free_hit_snapshot(conn, user_id: int, season: str, gameweek: int) -> bool:
    """Puts the pre-Free-Hit squad and budget back from the pending
    snapshot for (user_id, season, gameweek). Returns False if there is
    no pending snapshot to restore.

    Runs inside the CALLER's transaction (takes a Connection, not an
    Engine) because it has two callers that must each commit something
    different alongside it, atomically:

      - revert_expired_free_hits below, at the end of the gameweek, which
        MARKS the snapshot reverted (reverted_at) to keep an audit trail
        and make itself idempotent.
      - Gameplay/starting_xi.py, when a manager cancels the chip before
        the deadline, which DELETES the snapshot instead -- an unplayed
        chip leaves no trace, and uq_free_hit_squads_user_season_gw_player
        has no reverted_at predicate, so a marked-but-kept row would
        collide if they re-activated Free Hit for the same gameweek.

    Deliberately does neither itself, so the "which one" decision stays
    with the caller and there is still exactly one implementation of the
    restore.
    """
    key = {"user_id": user_id, "season": season, "gameweek": gameweek}
    snapshot = conn.execute(FREE_HIT_SNAPSHOT_QUERY, key).all()
    if not snapshot:
        return False

    user_squad_id = conn.execute(USER_SQUAD_ID_QUERY, {"user_id": user_id, "season": season}).scalar()
    if user_squad_id is None:
        raise ValueError(f"no user_squads row for user_id={user_id} season={season}")

    conn.execute(DEACTIVATE_ALL_ACTIVE_STMT, {"user_squad_id": user_squad_id})
    conn.execute(
        RESTORE_SQUAD_PLAYER_STMT,
        [
            {
                "user_squad_id": user_squad_id,
                "player_id": r.player_id,
                "purchase_price": r.purchase_price,
            }
            for r in snapshot
        ],
    )
    conn.execute(
        RESTORE_BUDGET_STMT,
        {"user_squad_id": user_squad_id, "budget_remaining": snapshot[0].budget_remaining},
    )
    return True


def revert_expired_free_hits(engine, buffer_hours: int = FREE_HIT_REVERT_BUFFER_HOURS) -> dict:
    """Restores the pre-Free-Hit squad for every snapshot whose gameweek
    is over, then marks it reverted.

    Idempotent: candidates are selected on reverted_at IS NULL and the
    whole restore + mark for one user happens in a single transaction,
    so a retry or a duplicate manual run is a no-op rather than a second
    revert.

    Returns {"reverted": [(user_id, season, gameweek), ...],
             "failed": [(user_id, season, gameweek, error_message), ...]}.
    """
    with engine.connect() as conn:
        candidates = conn.execute(REVERTABLE_FREE_HITS_QUERY, {"buffer_hours": buffer_hours}).all()

    reverted = []
    failed = []
    for c in candidates:
        key = {"user_id": c.user_id, "season": c.season, "gameweek": c.gameweek}
        try:
            with engine.begin() as conn:
                if not restore_free_hit_snapshot(conn, c.user_id, c.season, c.gameweek):
                    continue  # reverted or cancelled between the scan and here
                conn.execute(MARK_FREE_HIT_REVERTED_STMT, key)

            reverted.append((c.user_id, c.season, c.gameweek))
        except Exception as e:
            logger.error(
                "revert_expired_free_hits: failed for user_id=%s season=%s gameweek=%s: %s: %s",
                c.user_id, c.season, c.gameweek, type(e).__name__, e,
            )
            failed.append((c.user_id, c.season, c.gameweek, str(e)))

    return {"reverted": reverted, "failed": failed}


