"""
main.py — FastAPI context assembler for FPL chat-style advice.

Standalone experiment: there's no real Django/Postgres squad table yet, so
"the user's squad" is just a list of player_ids in the request body. Swap
that for a real squad join once one exists.

Pipeline per request (all live, nothing precomputed):
  build_features -> predict with model.json -> build_tiers -> assemble a
  tier-only prompt (no raw predicted_points ever reaches Groq) -> call_groq.

Read-only against the database. Imports from Data_ingestion/,
Feature_engineering/, and Predict/ -- does not modify any of them.

Known limitation: an empty player_ids list ("full pool" placeholder,
841 players) produces a prompt too large for Groq's chat completions
endpoint -- confirmed 413 Payload Too Large in testing. Left unfixed for
now since this is a placeholder for a real squad lookup, not a real use
case yet. If this needs to be usable before real squads exist, cap the
full-pool prompt to a subset (e.g. Elite/Strong tiers only) rather than
listing all 841 players.
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))

import numpy as np
import pandas as pd
import xgboost as xgb
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import text

from db_utils import get_engine
from feature_builder import build_features, FEATURE_COLS
from tier_builder import build_tiers
from groq_client import call_groq, GroqError

SEASON = "2025-26"
TARGET_GW = 4
MODEL_JSON = _ROOT / "Data_ingestion" / "model.json"

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

Never state a specific predicted-points number or invent one, even if
asked directly -- you were not given one and do not have one."""

app = FastAPI(title="FPL Context Assembler")

_booster: xgb.Booster | None = None


def _get_booster() -> xgb.Booster:
    global _booster
    if _booster is None:
        _booster = xgb.Booster()
        _booster.load_model(str(MODEL_JSON))
    return _booster


class ChatRequest(BaseModel):
    player_ids: list[int] = []
    message: str


class ChatResponse(BaseModel):
    response: str


def _build_prompt(context_df: pd.DataFrame, message: str) -> str:
    lines = [INSTRUCTIONS, "", "Player context:"]
    for player_id, row in context_df.iterrows():
        price = f"£{row['price_current']}m" if pd.notna(row["price_current"]) else "price unknown"
        lines.append(
            f"- {row['web_name']} (player_id {player_id}, {row['position']}, "
            f"{price}): {row['tier_or_label']}"
        )
    lines.append("")
    lines.append(f"User question: {message}")
    return "\n".join(lines)


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    engine = get_engine()

    features = build_features(engine, SEASON, TARGET_GW)

    booster = _get_booster()
    dmatrix = xgb.DMatrix(features[FEATURE_COLS], feature_names=FEATURE_COLS, missing=np.nan)
    predicted = booster.predict(dmatrix)
    predictions = pd.DataFrame({"predicted_points": predicted}, index=features.index)

    tiers = build_tiers(engine, SEASON, features, predictions).set_index("player_id")

    positions = pd.read_sql(
        text("SELECT id AS player_id, position FROM ml.players WHERE season = :season"),
        engine,
        params={"season": SEASON},
    ).set_index("player_id")

    context_df = tiers[["web_name", "tier_or_label"]].join(features["price_current"]).join(positions)

    player_ids = req.player_ids if req.player_ids else context_df.index.tolist()

    missing = [pid for pid in player_ids if pid not in context_df.index]
    if missing:
        raise HTTPException(status_code=400, detail=f"unknown player_id(s): {missing}")

    prompt = _build_prompt(context_df.loc[player_ids], req.message)

    print("\n===== ASSEMBLED PROMPT =====")
    print(prompt)
    print("=============================\n")

    try:
        answer = call_groq(prompt)
    except GroqError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e

    return ChatResponse(response=answer)
