"""
transfer_drafts.py — FastAPI endpoints for the transfer "cart": pairs a
manager has staged for a gameweek but not yet confirmed.

This is storage for something that already existed, not a new stage in
the game. frontend/src/pages/Transfers/TransfersPage.jsx has always kept
a `pendingTransfers` list in local React state -- add a pair, drop a
pair, watch the cost estimate, then hit Confirm. Because that list lived
in the browser, a refresh or a different device lost it. These three
endpoints move it to the server; nothing else about the flow changes.

WHAT THIS MODULE DELIBERATELY DOES NOT DO, and why it matters:

  * It does not make anything permanent. Confirm still calls
    POST /transfers (Gameplay/transfers.py), which writes to the
    append-only `transfers` table exactly as before. A draft is not a
    transfer and never becomes one here.

  * Nothing outside this module reads transfer_drafts. The free-transfer
    arithmetic (transfers.FREE_TRANSFERS_USED_QUERY), the hit deduction
    (scoring.TRANSFER_HITS_QUERY), squad_players and every chip path all
    still read `transfers` alone, so a draft cannot alter a squad, a
    budget, a free-transfer count or a score. That isolation is the
    whole safety property of this change: adding rows here is inert.

  * It does not validate the transfer. No ownership check, no
    position-match, no club cap, no budget, no deadline. Two reasons.
    First, all of that lives in transfers.py's _validate_transfers and
    runs at Confirm, where it is authoritative; duplicating it here
    would mean two implementations of the same rules drifting apart.
    Second, a cart is explicitly a place to hold a half-finished idea --
    a manager mid-edit may be over budget or three players deep into an
    illegal club count before they resolve it, and rejecting those
    intermediate states would make the cart useless. The ONLY rule
    enforced here is player_out_id <> player_in_id, which is not a game
    rule but a nonsense check, and is additionally backed by a CHECK
    constraint.

PUT rather than POST for the write, unlike every other endpoint in this
app: staging a pair is idempotent and keyed on the outgoing player, so
sending the same body twice must leave one row, not two. (This is why
/transfer-drafts is absent from test_api_contracts.py's POST_ENDPOINTS
list -- that file's parametrized checks POST to every path, which a
PUT-only route correctly answers with 405.)

Re-drafting the same player_out_id is an upsert against
uq_transfer_drafts_user_season_gw_out: the row keeps its id and gets a
new player_in_id and updated_at. Nothing constrains player_in_id, so the
same incoming target may sit against two different outgoing players
while the manager decides between them -- a normal intermediate state,
not an error.

DELETE takes user_id as a required query parameter and scopes the
delete to (id, user_id). This app passes user_id explicitly everywhere
rather than deriving it from the bearer token (see Data/auth.py's
note that get_current_user is built but not yet wired into the game
endpoints), so scoping by owner is what stops one manager's guessed id
from deleting another's row. An id that exists but belongs to someone
else is reported as 404, identically to one that does not exist -- the
same "don't confirm what you can't have" stance auth.py takes with its
deliberately identical login failures.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

router = APIRouter()

# ON CONFLICT targets the unique constraint by name rather than by column
# list, matching UPSERT_SQUAD_PLAYER_STMT in transfers.py -- if the
# constraint is ever renamed this fails loudly at the query rather than
# silently resolving to some other index.
UPSERT_DRAFT_STMT = text(
    """
    INSERT INTO transfer_drafts (user_id, season, gameweek, player_out_id, player_in_id)
    VALUES (:user_id, :season, :gameweek, :player_out_id, :player_in_id)
    ON CONFLICT ON CONSTRAINT uq_transfer_drafts_user_season_gw_out DO UPDATE SET
        player_in_id = EXCLUDED.player_in_id,
        updated_at = now()
    RETURNING id, user_id, season, gameweek, player_out_id, player_in_id
    """
)

DELETE_DRAFT_STMT = text(
    "DELETE FROM transfer_drafts WHERE id = :draft_id AND user_id = :user_id RETURNING id"
)

LIST_DRAFTS_QUERY = text(
    """
    SELECT id, user_id, season, gameweek, player_out_id, player_in_id
    FROM transfer_drafts
    WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek
    ORDER BY created_at, id
    """
)


class TransferDraftRequest(BaseModel):
    season: str
    gameweek: int
    player_out_id: int
    player_in_id: int


class TransferDraftOut(BaseModel):
    id: int
    user_id: int
    season: str
    gameweek: int
    player_out_id: int
    player_in_id: int


class TransferDraftsResponse(BaseModel):
    user_id: int
    season: str
    gameweek: int
    drafts: list[TransferDraftOut]


@router.get("/transfer-drafts", response_model=TransferDraftsResponse)
def get_transfer_drafts(
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> TransferDraftsResponse:
    user_id = current_user.id
    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(
            LIST_DRAFTS_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).all()

    # An empty cart is an unstarted state, not an error -- same stance as
    # GET /gw_selection's has_selection=False and GET /squad's empty list.
    return TransferDraftsResponse(
        user_id=user_id,
        season=season,
        gameweek=gameweek,
        drafts=[TransferDraftOut(**row._mapping) for row in rows],
    )


@router.put("/transfer-drafts", response_model=TransferDraftOut)
def put_transfer_draft(
    req: TransferDraftRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> TransferDraftOut:
    user_id = current_user.id
    # The only check here -- see the module docstring on why every real
    # transfer rule is left to POST /transfers at Confirm time.
    if req.player_out_id == req.player_in_id:
        raise HTTPException(
            status_code=422,
            detail=[f"player_out_id and player_in_id must differ: {req.player_out_id}"],
        )

    engine = get_engine()
    try:
        with engine.begin() as conn:
            row = conn.execute(
                UPSERT_DRAFT_STMT,
                {
                    "user_id": user_id,
                    "season": req.season,
                    "gameweek": req.gameweek,
                    "player_out_id": req.player_out_id,
                    "player_in_id": req.player_in_id,
                },
            ).first()
    except SQLAlchemyError as e:
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return TransferDraftOut(**row._mapping)


@router.delete("/transfer-drafts/{draft_id}", response_model=TransferDraftsResponse)
def delete_transfer_draft(
    draft_id: int,
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> TransferDraftsResponse:
    """Removes one staged pair and returns the remaining cart.

    Returning the whole cart rather than 204 keeps the client from having
    to follow every delete with a list call to stay truthful -- the same
    reason POST /transfers returns the resulting squad state rather than
    just an acknowledgement. season/gameweek scope that returned cart;
    they are not part of the delete's own WHERE, which is keyed on the
    row's id and owner.
    """
    user_id = current_user.id
    engine = get_engine()

    try:
        with engine.begin() as conn:
            deleted = conn.execute(
                DELETE_DRAFT_STMT, {"draft_id": draft_id, "user_id": user_id}
            ).first()
            if deleted is None:
                # Also the answer when the row exists but belongs to
                # someone else -- see the module docstring.
                raise HTTPException(status_code=404, detail="transfer draft not found")

            rows = conn.execute(
                LIST_DRAFTS_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
            ).all()
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        logger.error("Database write failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return TransferDraftsResponse(
        user_id=user_id,
        season=season,
        gameweek=gameweek,
        drafts=[TransferDraftOut(**row._mapping) for row in rows],
    )
