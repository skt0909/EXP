"""
main.py — FastAPI context assembler for FPL chat-style advice.

Looks up a user's real starting XI for a gameweek from the public schema
(starting_xi / gw_selections / user_squads / users), maps FPL ids to
internal ml.players ids, and reads already-computed predictions/tiers from
ml.ml_predictions (written by Worker.tasks.run_ml_pipeline) -- this
endpoint does NOT call Feature_engineering.feature_builder or run any
predictions itself anymore. That live-compute pipeline now lives solely in
the Celery task; /chat just reads its output.

If ml.ml_predictions is missing a row for a player in the starting XI
(the scheduled task hasn't run yet, or ran for a different gameweek),
that player is treated as "New/Insufficient Data" and a warning is logged
-- not an error, since a stale table shouldn't break chat for players who
do have data.

Pipeline per request:
  chat-availability gate -> starting_xi lookup -> ml.ml_predictions lookup
  -> assemble a tier-only prompt (no raw predicted_points ever reaches
  Groq, captain/vice-captain flagged) -> call_groq.

Read-only against the database.

If a user has no gw_selections/starting_xi rows for the requested
season/gameweek, that's a 200 with a "please select your squad" message,
not an error -- fail fast. A starting XI is always <= 11 players, so it
naturally respects MAX_PLAYERS (kept as a defensive cap regardless).
"""

import logging
import sys
from pathlib import Path

# Explicit dotted imports (from Game_logic.db_utils import ...) still need
# the repo root on sys.path to resolve Game_logic as a package -- that's
# not implied by running this file directly (e.g. `uvicorn main:app` from
# within this directory only puts Context_assembler/ on sys.path, which is
# what groq_client's own bare import below needs). Confirmed by actually
# booting uvicorn both with and without this line before settling on it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from Game_logic.db_utils import get_engine
from Game_logic.squad_selection import router as squad_router
from Game_logic.starting_xi import router as starting_xi_router
from Game_logic.transfers import router as transfers_router
from Game_logic.leagues import router as leagues_router
from groq_client import call_groq, GroqError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

DB_UNAVAILABLE_MESSAGE = "Having trouble reaching player data right now -- please try again shortly."
GROQ_UNAVAILABLE_MESSAGE = "Having trouble generating advice right now -- please try again in a moment."

# Must match Worker/tasks.py's MODEL_VERSION -- confirmed via
# `SELECT DISTINCT model_version FROM ml.ml_predictions` that this is the
# actual value the Celery task writes.
MODEL_VERSION = "xgboost_v1"

MAX_PLAYERS = 15

INSTRUCTIONS = """You are helping a Fantasy Premier League manager make squad decisions.

You are given each player's tier, not a raw predicted score. Tiers reflect
recent form ranking, not exact point forecasts:
- Elite: top 10% of predicted performers this gameweek
- Strong: next 25%
- Average: middle 40%
- Weak: bottom 25%
- New/Insufficient Data: this player has no games played yet this season --
  you have no form read on them. Say so plainly. Do not guess or invent a
  performance expectation for them.
- Doubtful/Injured/Unavailable: do not recommend starting this player.

The underlying prediction model is known to underestimate big, explosive
performances (goals, hauls, clean sheet bonuses) -- treat a tier as a
FLOOR on expected performance, not a ceiling. A "Strong" player could
easily have a huge game; an "Elite" player is not guaranteed a big haul,
just the most likely to perform well among this group.

One player in the context below may be marked [CURRENT CAPTAIN] or
[CURRENT VICE-CAPTAIN] -- that's who the manager has already picked for
this gameweek. If asked about captaincy, compare your suggestion against
that current pick explicitly, don't just rank players in a vacuum.

Never state a specific predicted-points number or invent one, even if
asked directly -- you were not given one and do not have one."""

app = FastAPI(title="FPL Context Assembler")

# playground.html is opened directly as a file:// page, not served by
# FastAPI, so without this the browser blocks the fetch() call. Dev-only
# tool, so wide open is fine -- don't carry allow_origins=["*"] into
# anything that touches real user auth.
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

app.include_router(squad_router)
app.include_router(starting_xi_router)
app.include_router(transfers_router)
app.include_router(leagues_router)


STARTING_XI_QUERY = text(
    """
    SELECT sx.player_id AS fpl_id, ml.id AS internal_id
    FROM starting_xi sx
    JOIN gw_selections gs ON gs.id = sx.gw_selection_id
    JOIN ml.players ml ON ml.fpl_id = sx.player_id AND ml.season = gs.season
    WHERE gs.user_id = :user_id AND gs.season = :season AND gs.gameweek = :gameweek
    """
)

CAPTAIN_QUERY = text(
    """
    SELECT captain_id, vice_captain_id
    FROM gw_selections
    WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek
    """
)

PREDICTIONS_QUERY = text(
    """
    SELECT player_id, tier_or_label
    FROM ml.ml_predictions
    WHERE season = :season AND gameweek = :gameweek
      AND player_id = ANY(:player_ids)
      AND model_version = :model_version
    """
)

# player_id/position/web_name and price are read directly here -- not via
# feature_builder, per the "no longer import Feature_engineering" rule.
PLAYER_INFO_QUERY = text(
    "SELECT id AS player_id, web_name, position FROM ml.players WHERE season = :season"
)

PRICE_QUERY = text(
    """
    SELECT DISTINCT ON (player_id) player_id, value
    FROM ml.player_gw_stats
    WHERE season = :season AND gameweek < :gameweek
    ORDER BY player_id, gameweek DESC
    """
)

CHAT_AVAILABILITY_MESSAGE = (
    "Chat advice will be available once Gameweek 1 results are in -- "
    "check back after the first matches finish."
)


def is_chat_available(engine, season: str) -> bool:
    """False until GW1 data exists -- before that, every player would show
    New/Insufficient Data (no rolling history), making chat technically
    functional but practically useless."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM ml.player_gw_stats WHERE season = :season AND gameweek = 1"),
            {"season": season},
        ).scalar()
    return result > 0


class ChatRequest(BaseModel):
    user_id: int
    season: str
    gameweek: int
    message: str


class ChatResponse(BaseModel):
    response: str


def _build_prompt(
    context_df: pd.DataFrame,
    captain_internal_id: int | None,
    vice_captain_internal_id: int | None,
    message: str,
) -> str:
    lines = [INSTRUCTIONS, "", "Player context (your current starting XI):"]
    for player_id, row in context_df.iterrows():
        price = f"£{row['price_current']}m" if pd.notna(row["price_current"]) else "price unknown"
        tag = ""
        if player_id == captain_internal_id:
            tag = " [CURRENT CAPTAIN]"
        elif player_id == vice_captain_internal_id:
            tag = " [CURRENT VICE-CAPTAIN]"
        lines.append(
            f"- {row['web_name']} (player_id {player_id}, {row['position']}, "
            f"{price}): {row['tier_or_label']}{tag}"
        )
    lines.append("")
    lines.append(f"User question: {message}")
    return "\n".join(lines)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    engine = get_engine()

    try:
        if not is_chat_available(engine, req.season):
            return ChatResponse(response=CHAT_AVAILABILITY_MESSAGE)

        squad = pd.read_sql(
            STARTING_XI_QUERY,
            engine,
            params={"user_id": req.user_id, "season": req.season, "gameweek": req.gameweek},
        )

        if squad.empty:
            return ChatResponse(response="Please select your squad for this gameweek first.")

        if len(squad) > MAX_PLAYERS:
            raise HTTPException(
                status_code=400,
                detail=f"too many players -- max {MAX_PLAYERS} per request, got {len(squad)}",
            )

        fpl_to_internal = dict(zip(squad["fpl_id"], squad["internal_id"]))
        player_ids = squad["internal_id"].tolist()

        selection = pd.read_sql(
            CAPTAIN_QUERY,
            engine,
            params={"user_id": req.user_id, "season": req.season, "gameweek": req.gameweek},
        )
        captain_internal_id = fpl_to_internal.get(int(selection.iloc[0]["captain_id"]))
        vice_captain_internal_id = fpl_to_internal.get(int(selection.iloc[0]["vice_captain_id"]))

        predictions = pd.read_sql(
            PREDICTIONS_QUERY,
            engine,
            params={
                "season": req.season,
                "gameweek": req.gameweek,
                "player_ids": player_ids,
                "model_version": MODEL_VERSION,
            },
        ).set_index("player_id")

        tier_map = predictions["tier_or_label"].to_dict()
        missing_ids = [pid for pid in player_ids if pid not in tier_map]
        if missing_ids:
            logger.warning(
                "ml.ml_predictions missing row(s) for player_id %s (season=%s, gameweek=%s, "
                "model_version=%s) -- table may be stale or not yet populated for this "
                "gameweek. Treating as New/Insufficient Data.",
                missing_ids, req.season, req.gameweek, MODEL_VERSION,
            )
            for pid in missing_ids:
                tier_map[pid] = "New/Insufficient Data"

        player_info = pd.read_sql(
            PLAYER_INFO_QUERY, engine, params={"season": req.season}
        ).set_index("player_id")

        price_rows = pd.read_sql(
            PRICE_QUERY, engine, params={"season": req.season, "gameweek": req.gameweek}
        ).set_index("player_id")
        price_rows["price_current"] = price_rows["value"] / 10

        context_df = player_info.loc[player_ids].copy()
        context_df["tier_or_label"] = pd.Series(tier_map)
        context_df = context_df.join(price_rows["price_current"])
    except OperationalError as e:
        logger.error("Database connection failure: %s: %s", type(e).__name__, e)
        return ChatResponse(response=DB_UNAVAILABLE_MESSAGE)
    except SQLAlchemyError as e:
        logger.error("Database query failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    prompt = _build_prompt(
        context_df.loc[player_ids], captain_internal_id, vice_captain_internal_id, req.message
    )

    logger.info("Assembled prompt:\n%s", prompt)

    try:
        answer = call_groq(prompt)
    except GroqError as e:
        logger.error("Groq API call failed: %s: %s", type(e).__name__, e)
        return ChatResponse(response=GROQ_UNAVAILABLE_MESSAGE)

    return ChatResponse(response=answer)
