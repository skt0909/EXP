"""
test_tier_builder.py — regression tests for Predict/tier_builder.py's
tier/label assignment rules. Real Postgres for player info (web_name,
status), dedicated fake TEST_SEASON -- but `features`/`predictions` are
built in-memory (tier_builder only needs pts_rolling_3gw + predicted_points),
so these tests can pin exact values without depending on feature_builder.
"""

import numpy as np
import pandas as pd

from conftest import TEST_SEASON
from tier_builder import build_tiers


def _features(player_ids, has_history_map):
    return pd.DataFrame(
        {"pts_rolling_3gw": [1.0 if has_history_map[pid] else np.nan for pid in player_ids]},
        index=pd.Index(player_ids, name="player_id"),
    )


def _predictions(player_ids, points_map):
    return pd.DataFrame(
        {"predicted_points": [points_map[pid] for pid in player_ids]},
        index=pd.Index(player_ids, name="player_id"),
    )


def test_no_history_gets_new_insufficient_data_label_regardless_of_points(make_player, engine):
    player_id = make_player(fpl_id=200, status="a")
    features = _features([player_id], {player_id: False})
    predictions = _predictions([player_id], {player_id: 15.0})  # deliberately Elite-looking

    tiers = build_tiers(engine, TEST_SEASON, features, predictions).set_index("player_id")

    assert tiers.loc[player_id, "tier_or_label"] == "New/Insufficient Data"


def test_unavailable_status_wins_over_no_history(make_player, engine):
    """status != 'a' takes priority over has_history==False when both apply, per spec."""
    player_id = make_player(fpl_id=201, status="i")
    features = _features([player_id], {player_id: False})
    predictions = _predictions([player_id], {player_id: 8.0})

    tiers = build_tiers(engine, TEST_SEASON, features, predictions).set_index("player_id")

    assert tiers.loc[player_id, "tier_or_label"] == "Doubtful/Injured/Unavailable"


def test_percentile_boundaries_computed_only_among_eligible_players(make_player, engine):
    eligible_ids = [make_player(fpl_id=300 + i, status="a") for i in range(10)]
    ineligible_id = make_player(fpl_id=399, status="u")  # unavailable -- ineligible despite having history

    all_ids = eligible_ids + [ineligible_id]
    has_history = {pid: True for pid in all_ids}
    features = _features(all_ids, has_history)

    points_map = {pid: float(i + 1) for i, pid in enumerate(eligible_ids)}  # 1..10
    points_map[ineligible_id] = 1000.0  # extreme value that must NOT skew eligible percentiles
    predictions = _predictions(all_ids, points_map)

    tiers = build_tiers(engine, TEST_SEASON, features, predictions).set_index("player_id")

    expected = {
        eligible_ids[9]: "Elite",    # points=10 -> rank 1.0
        eligible_ids[8]: "Strong",   # points=9  -> rank 0.9
        eligible_ids[7]: "Strong",   # points=8  -> rank 0.8
        eligible_ids[6]: "Strong",   # points=7  -> rank 0.7
        eligible_ids[5]: "Average",  # points=6  -> rank 0.6
        eligible_ids[4]: "Average",  # points=5  -> rank 0.5
        eligible_ids[3]: "Average",  # points=4  -> rank 0.4
        eligible_ids[2]: "Average",  # points=3  -> rank 0.3
        eligible_ids[1]: "Weak",     # points=2  -> rank 0.2
        eligible_ids[0]: "Weak",     # points=1  -> rank 0.1
    }
    for pid, expected_tier in expected.items():
        assert tiers.loc[pid, "tier_or_label"] == expected_tier, f"player_id {pid} expected {expected_tier}"

    # If the extreme ineligible value had leaked into the ranking, the top
    # eligible player (points=10) would no longer register as rank 1.0/Elite.
    assert tiers.loc[ineligible_id, "tier_or_label"] == "Doubtful/Injured/Unavailable"
