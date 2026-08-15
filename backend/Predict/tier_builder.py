"""
tier_builder.py — turn raw predicted_points into confidence flags and tiers
before this data ever reaches an LLM or a user-facing view.

Two problems raw XGBoost output has on its own:
  1. Players with zero prior gameweek history get a flat, near-identical
     prediction (~7.5 pts) -- an artifact of XGBoost's missing=nan routing
     through a fixed default branch at every split, not a real assessment.
  2. The model has no concept of whether a player will actually play --
     it happily predicts a number for someone injured or unavailable.

build_tiers() filters both cases out into explicit labels, and only assigns
a percentile-based tier to players who are both known (has_history) and
available (status == 'a'). Read-only: no writes to the database.
"""

from __future__ import annotations

import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

TIER_COLS = [
    "player_id",
    "web_name",
    "predicted_points",
    "has_history",
    "availability_status",
    "tier_or_label",
]


def _load_player_info(engine: Engine, season: str) -> pd.DataFrame:
    query = text(
        """
        SELECT id AS player_id, web_name, status
        FROM ml.players
        WHERE season = :season
        """
    )
    return pd.read_sql(query, engine, params={"season": season}).set_index("player_id")


def _assign_tier(pct_rank: pd.Series) -> pd.Series:
    """Elite: top 10%, Strong: next 25%, Average: middle 40%, Weak: bottom 25%."""
    tiers = pd.Series(index=pct_rank.index, dtype=object)
    tiers[pct_rank > 0.90] = "Elite"
    tiers[(pct_rank > 0.65) & (pct_rank <= 0.90)] = "Strong"
    tiers[(pct_rank > 0.25) & (pct_rank <= 0.65)] = "Average"
    tiers[pct_rank <= 0.25] = "Weak"
    return tiers


def build_tiers(engine: Engine, season: str, features: pd.DataFrame, predictions) -> pd.DataFrame:
    """
    features: DataFrame indexed by player_id from feature_builder.build_features
              (only pts_rolling_3gw is used, to derive has_history).
    predictions: DataFrame with a predicted_points column, or a Series of
                 predicted_points, indexed by player_id either way.

    Returns a DataFrame with columns: player_id, web_name, predicted_points,
    has_history, availability_status, tier_or_label.
    """
    if isinstance(predictions, pd.DataFrame):
        predicted_points = predictions["predicted_points"]
    else:
        predicted_points = predictions

    info = _load_player_info(engine, season)

    df = pd.DataFrame(index=features.index)
    df["has_history"] = features["pts_rolling_3gw"].notna()
    df["predicted_points"] = predicted_points
    df = df.join(info)
    df["availability_status"] = df["status"]

    available = df["status"] == "a"
    eligible = df["has_history"] & available

    pct_rank = df.loc[eligible, "predicted_points"].rank(pct=True)

    df["tier_or_label"] = pd.NA
    df.loc[eligible, "tier_or_label"] = _assign_tier(pct_rank)
    # Availability takes priority over the has_history label when both apply.
    df.loc[~available, "tier_or_label"] = "Doubtful/Injured/Unavailable"
    df.loc[available & ~df["has_history"], "tier_or_label"] = "New/Insufficient Data"

    return df.reset_index().rename(columns={"index": "player_id"})[TIER_COLS]
