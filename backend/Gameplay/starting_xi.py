"""
starting_xi.py - FastAPI endpoint for submitting/replacing a user's tactical
starting XI, bench roles, Bonus Players, and swap plan.

The read side lives in Gameplay/lineup.py. The pure lineup rules live in
Gameplay/selection_rules.py, where they can be tested without a database. This
module owns the write boundary: load the manager's squad/player fixture
context, adapt the request into SelectionInput, collect validation errors, then
replace the gameweek selection inside one transaction.

The endpoint's validation contract is "collect every failure at once" rather
than failing fast. That is user-visible because clients render the returned 422
detail list directly, so this module keeps one ordered validator call instead
of scattering partial checks through the route.

starting_xi.player_id is the raw FPL id, same convention as
squad_players.player_id. ml.players is consulted to validate positions and
fixture timing, never to translate ids before insert.

position_slot 1-11 = starting XI. Slots 12-15 are fixed tactical bench roles:
12 is Auto GK, 13 is Auto Sub, and 14-15 are Tactical Subs. The database stores
the slot; Shared.rules.BENCH_SLOT_ROLES is the role mapping the scorer and read
models derive from.

gw_selections has a DB trigger (enforce_selection_lock_fn) that raises on any
UPDATE where OLD.is_locked = TRUE. This module also pre-checks
deadlines.deadline_has_passed for first-time submissions, because the trigger
cannot fire when there is no row yet. Both lock paths return the same 422
detail string so the UI sees one consistent "too late" response.

Resubmission before lock always overwrites, same philosophy as
squad_selection.py: gw_selections is upserted, tactical_swaps rows are cleared,
and starting_xi rows are deleted and reinserted. The child triggers are
DEFERRABLE INITIALLY DEFERRED, so the transaction is validated at commit
against the final state rather than each intermediate delete/insert step.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
from Shared.deadlines import deadline_has_passed
from Gameplay.selection_rules import SelectionInput, SwapInput, validate_selection

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

# enforce_selection_lock_fn's exact RAISE EXCEPTION text (see gw_selections
# DDL). Keep this in sync with the trigger by hand.
LOCK_ERROR_SUBSTRING = "Selection is locked"

# Single 422 detail for both lock paths: the trigger catching an already-locked
# row, and the deadline pre-check catching a gameweek with no row yet.
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

# Which fixtures each submitted player has this gameweek, for swap timing. A
# player can have two rows in a double gameweek.
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


class SwapPayload(BaseModel):
    player_out_id: int
    player_in_id: int


class GwSelectionRequest(BaseModel):
    season: str
    gameweek: int
    tactic: str
    player_ids: list[int]  # exactly 11 raw FPL ids, slots 1-11
    # Exactly 4 raw FPL ids. Order is meaning: slot 12 Auto GK, 13 Auto Sub,
    # 14 and 15 Tactical Subs.
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
    """Adapter: turn the request into the pure validator's input and call it."""
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
    """Detect enforce_selection_lock_fn's RAISE EXCEPTION by matching text."""
    return LOCK_ERROR_SUBSTRING in str(exc)


@router.post("/gw_selection", response_model=GwSelectionResponse)
def select_starting_xi(
    req: GwSelectionRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> GwSelectionResponse:
    user_id = current_user.id
    engine = get_engine()

    # The trigger can only guard gameweeks that already have a row, so a
    # first-time submission after kickoff has to be stopped here.
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

            # Swaps reference starting_xi rows, so clear them before replacing
            # the XI.
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
