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
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user
from Shared.rules import STARTING_XI_SIZE
from Shared.deadlines import deadline_has_passed
from Gameplay.selection_rules import last_fixture_end

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

# Every player in the manager's ACTIVE squad, independent of whether a
# selection has ever been submitted for this gameweek -- fixture_windows
# has to cover all 15 so the Starting XI screen can show swap eligibility
# before the first save, not just play back a saved one.
ACTIVE_SQUAD_IDS_QUERY = text(
    "SELECT sp.player_id FROM squad_players sp "
    "JOIN user_squads us ON us.id = sp.user_squad_id "
    "WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE"
)

# Same join shape as Gameplay/starting_xi.py's PLAYER_FIXTURES_QUERY (keyed by
# fpl_id, via ml.players -> ml.fixtures on team) -- one row per fixture, so a
# double Gameweek player appears twice and last_fixture_end (imported from
# selection_rules, the swap validator's own function) collapses that
# correctly to a single "last_end".
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


class FixtureWindow(BaseModel):
    first_kickoff: datetime
    last_end: datetime


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
    # Additive (Phase 5+): per-squad-player gameweek fixture timing, so the
    # client can show Tactical Swap eligibility before submitting rather than
    # discovering a timing violation from a 422. Keyed by fpl_id as a string
    # (JSON object keys are always strings); absent for a player with no
    # fixture this gameweek -- that is not itself an error, see
    # selection_rules.py's own comment on the same point.
    fixture_windows: dict[str, FixtureWindow] = {}
    # Whether Gameplay/starting_xi.py would currently reject a submission as
    # too late -- same Shared.deadlines.deadline_has_passed() the write path
    # itself checks, not re-derived.
    deadline_passed: bool = False


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

        squad_ids = [
            r.player_id
            for r in conn.execute(ACTIVE_SQUAD_IDS_QUERY, {"user_id": user_id, "season": season})
        ]
        fixture_windows = _fixture_windows(conn, season, gameweek, squad_ids)

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
                fixture_windows=fixture_windows,
                deadline_passed=deadline_has_passed(engine, season, gameweek),
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
        fixture_windows=fixture_windows,
        deadline_passed=deadline_has_passed(engine, season, gameweek),
    )


def _fixture_windows(conn, season: str, gameweek: int, player_ids: list[int]) -> dict[str, FixtureWindow]:
    """{fpl_id (as str): FixtureWindow} for every id in player_ids that has at
    least one fixture this gameweek. A player with none is simply absent --
    matching selection_rules.py's own stance that no fixture is not an error,
    just something that blocks a swap."""
    if not player_ids:
        return {}

    kickoffs_by_player: dict[int, list] = {}
    for row in conn.execute(
        PLAYER_FIXTURES_QUERY, {"season": season, "gameweek": gameweek, "player_ids": player_ids}
    ):
        kickoffs_by_player.setdefault(row.player_id, []).append(row.kickoff_time)

    return {
        str(pid): FixtureWindow(first_kickoff=min(kickoffs), last_end=last_fixture_end(kickoffs))
        for pid, kickoffs in kickoffs_by_player.items()
    }
