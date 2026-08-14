"""
feature_builder.py — build the 21 model features for a (season, target_gameweek).

Reads raw data from ml.player_gw_stats, ml.players, ml.fixtures and computes
the exact feature set used to train xgboost_v1 (see
Data_ingestion/model_metadata.json -> feature_cols). Read-only: no writes to
the database.

Rolling features (*_rolling_3gw / *_rolling_5gw) are the mean of a player's
last N *completed* gameweeks strictly before target_gameweek, computed from
raw player_gw_stats rows -- not the precomputed ml.player_gw_features table,
since these rules were confirmed independently against the training
notebook's saved output. If a player has fewer than N prior games, the mean
is taken over however many are available; if a player has zero prior games,
the feature is a genuine NaN (no zero-imputation -- the model was trained
with missing=nan).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sqlalchemy import text
from sqlalchemy.engine import Engine

FEATURE_COLS = [
    "pts_rolling_3gw",
    "pts_rolling_5gw",
    "minutes_rolling_3gw",
    "goals_rolling_5gw",
    "assists_rolling_5gw",
    "clean_sheets_rolling_5gw",
    "saves_rolling_3gw",
    "bonus_rolling_3gw",
    "bps_rolling_3gw",
    "ict_rolling_3gw",
    "xg_rolling_5gw",
    "xa_rolling_5gw",
    "xgi_rolling_5gw",
    "position_encoded",
    "was_home",
    "price_current",
    "ownership_log",
    "shots_per_90",
    "shots_on_target_per_90",
    "on_off_diff",
    "gk_save_pct",
]

# (source column in player_gw_stats, output feature name, window size)
_ROLLING_SPECS = [
    ("total_points", "pts_rolling_3gw", 3),
    ("total_points", "pts_rolling_5gw", 5),
    ("minutes", "minutes_rolling_3gw", 3),
    ("goals_scored", "goals_rolling_5gw", 5),
    ("assists", "assists_rolling_5gw", 5),
    ("clean_sheets", "clean_sheets_rolling_5gw", 5),
    ("saves", "saves_rolling_3gw", 3),
    ("bonus", "bonus_rolling_3gw", 3),
    ("bps", "bps_rolling_3gw", 3),
    ("ict_index", "ict_rolling_3gw", 3),
    ("expected_goals", "xg_rolling_5gw", 5),
    ("expected_assists", "xa_rolling_5gw", 5),
    ("expected_goal_involvements", "xgi_rolling_5gw", 5),
]

# FBref-sourced features not available from this schema -- confirmed
# acceptable as NaN (combined feature importance ~5.9%).
_UNAVAILABLE_FEATURES = [
    "shots_per_90",
    "shots_on_target_per_90",
    "on_off_diff",
    "gk_save_pct",
]


def _load_players(engine: Engine, season: str) -> pd.DataFrame:
    query = text(
        """
        SELECT id AS player_id, position_encoded, team_id
        FROM ml.players
        WHERE season = :season
        """
    )
    return pd.read_sql(query, engine, params={"season": season})


def _load_history(engine: Engine, season: str, target_gameweek: int) -> pd.DataFrame:
    query = text(
        """
        SELECT player_id, gameweek, minutes, goals_scored, assists, clean_sheets,
               saves, bonus, bps, ict_index, expected_goals, expected_assists,
               expected_goal_involvements, total_points, value, selected
        FROM ml.player_gw_stats
        WHERE season = :season AND gameweek < :target_gameweek
        ORDER BY player_id, gameweek
        """
    )
    return pd.read_sql(
        query, engine, params={"season": season, "target_gameweek": target_gameweek}
    )


def _load_target_fixtures(engine: Engine, season: str, target_gameweek: int) -> pd.DataFrame:
    query = text(
        """
        SELECT home_team_id, away_team_id, kickoff_time
        FROM ml.fixtures
        WHERE season = :season AND gameweek = :target_gameweek
        ORDER BY kickoff_time
        """
    )
    return pd.read_sql(
        query, engine, params={"season": season, "target_gameweek": target_gameweek}
    )


def _rolling_features(history: pd.DataFrame) -> pd.DataFrame:
    """Mean of each player's last N completed gameweeks, as-of their latest row."""
    if history.empty:
        # Must match the non-empty branch's out_cols below (value/selected
        # included, all numeric dtype) -- otherwise build_features's
        # price_current/ownership_log assignment crashes (KeyError on 'value'
        # if the columns are missing, or a np.log1p TypeError if they're left
        # as default object dtype) whenever a season has no completed
        # gameweeks yet (e.g. predicting for GW1 itself).
        cols = ["player_id"] + [spec[1] for spec in _ROLLING_SPECS] + ["value", "selected"]
        return pd.DataFrame(columns=cols, dtype="float64")

    grouped = history.groupby("player_id")
    for src_col, out_col, window in _ROLLING_SPECS:
        history[out_col] = grouped[src_col].transform(
            lambda s: s.rolling(window, min_periods=1).mean()
        )

    # Each player's most recent history row carries the rolling mean over
    # their last min(window, available) games -- exactly what we want.
    latest_idx = history.groupby("player_id")["gameweek"].idxmax()
    latest = history.loc[latest_idx].set_index("player_id")

    out_cols = [spec[1] for spec in _ROLLING_SPECS] + ["value", "selected"]
    return latest[out_cols]


def _was_home_by_team(fixtures: pd.DataFrame) -> dict:
    """team_id -> bool. First fixture (by kickoff_time) wins on a double gameweek."""
    was_home = {}
    for _, row in fixtures.iterrows():
        was_home.setdefault(row["home_team_id"], True)
        was_home.setdefault(row["away_team_id"], False)
    return was_home


def build_features(engine: Engine, season: str, target_gameweek: int) -> pd.DataFrame:
    """
    Build the 21 model features for every player in `season`, as of the
    gameweek that is about to be predicted (`target_gameweek`).

    Returns a DataFrame indexed by player_id with columns in FEATURE_COLS
    order, ready for model.predict(df[feature_cols]). Read-only.
    """
    players = _load_players(engine, season)
    if players.empty:
        raise ValueError(
            f"No player data found for season {season} -- check the season string "
            "is correct and data has been ingested"
        )

    history = _load_history(engine, season, target_gameweek)
    fixtures = _load_target_fixtures(engine, season, target_gameweek)

    rolling = _rolling_features(history)

    df = players.set_index("player_id")
    df = df.join(rolling, how="left")

    was_home_map = _was_home_by_team(fixtures)
    df["was_home"] = df["team_id"].map(was_home_map)

    df["price_current"] = df["value"] / 10
    df["ownership_log"] = np.log1p(df["selected"])

    for col in _UNAVAILABLE_FEATURES:
        df[col] = np.nan

    return df[FEATURE_COLS]
