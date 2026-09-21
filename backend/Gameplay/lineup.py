"""
lineup.py — the read side of a submitted gameweek selection:
GET /gw_selection.

Extracted from starting_xi.py, which was investigated for a full
three-way split and found only partly separable (see that file's DESIGN
NOTE). This endpoint was one of the clean pieces: it reads two tables,
writes nothing, and shares no state with the submission path beyond the
rows that path already committed.

Deliberately read-only. Every rule about what a *valid* lineup is --
formation, captain/vice, bench composition, chip eligibility -- stays
with the write path in starting_xi.py, because those rules are enforced
at submission time and this endpoint only plays back what was accepted.
That is why the two queries here are the only SQL this module owns:
nothing else in the codebase reads them.

has_selection is False rather than a 404 when nothing has been submitted
yet -- the same "unstarted state is not an error" stance as GET /squad's
empty list and GET /transfer-drafts' empty cart.
"""

import logging

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
from Shared.rules import STARTING_XI_SIZE

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

router = APIRouter()

GW_SELECTION_ROW_QUERY = text(
    "SELECT id AS gw_selection_id, tactic FROM gw_selections "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek"
)

STARTING_XI_SLOTS_QUERY = text(
    "SELECT player_id, position_slot, is_bonus FROM starting_xi "
    "WHERE gw_selection_id = :gw_selection_id ORDER BY position_slot"
)

TACTICAL_SWAPS_QUERY = text(
    "SELECT player_out_id, player_in_id FROM tactical_swaps "
    "WHERE gw_selection_id = :gw_selection_id ORDER BY id"
)


class SwapPayload(BaseModel):
    player_out_id: int
    player_in_id: int


class CurrentSelectionResponse(BaseModel):
    user_id: int
    season: str
    gameweek: int
    has_selection: bool
    tactic: str | None
    player_ids: list[int]
    bench_order: list[int]
    bonus_player_ids: list[int]
    swaps: list[SwapPayload]


@router.get("/gw_selection", response_model=CurrentSelectionResponse)
def get_current_selection(
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> CurrentSelectionResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        sel_row = conn.execute(
            GW_SELECTION_ROW_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).first()

        if sel_row is None:
            return CurrentSelectionResponse(
                user_id=user_id,
                season=season,
                gameweek=gameweek,
                has_selection=False,
                tactic=None,
                player_ids=[],
                bench_order=[],
                bonus_player_ids=[],
                swaps=[],
            )

        slot_rows = conn.execute(STARTING_XI_SLOTS_QUERY, {"gw_selection_id": sel_row.gw_selection_id}).all()
        swap_rows = conn.execute(TACTICAL_SWAPS_QUERY, {"gw_selection_id": sel_row.gw_selection_id}).all()

    # Slots 1-11 are the XI, 12-15 the bench in substitution priority
    # order -- the split is the slot number, never the player's position.
    return CurrentSelectionResponse(
        user_id=user_id,
        season=season,
        gameweek=gameweek,
        has_selection=True,
        tactic=sel_row.tactic,
        player_ids=[r.player_id for r in slot_rows if r.position_slot <= STARTING_XI_SIZE],
        bench_order=[r.player_id for r in slot_rows if r.position_slot > STARTING_XI_SIZE],
        # Bonus Players are always starters, but the flag is read from the row
        # rather than inferred from the slot -- the database is the authority.
        bonus_player_ids=[r.player_id for r in slot_rows if r.is_bonus],
        swaps=[SwapPayload(player_out_id=r.player_out_id, player_in_id=r.player_in_id)
               for r in swap_rows],
    )
