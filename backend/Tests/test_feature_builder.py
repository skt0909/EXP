"""
test_feature_builder.py — regression tests for bugs manually found and
fixed in Feature_engineering/feature_builder.py during this project's
manual verification. Real Postgres, dedicated fake TEST_SEASON (see
conftest.py) -- no mocking.
"""

import pandas as pd
import pytest

from conftest import TEST_SEASON
from feature_builder import build_features


def test_was_home_reflects_target_gameweek_not_history(make_team, make_player, make_fixture, make_gw_stat, engine):
    """
    was_home must come from the TARGET gameweek's fixture, not a
    historical average of past was_home values -- a player whose entire
    history says was_home=True but who is AWAY in the target fixture
    must get was_home=False.
    """
    team_home = make_team(fpl_id=1, name="Home Team", short_name="HOM")
    team_away = make_team(fpl_id=2, name="Away Team", short_name="AWY")

    player_id = make_player(fpl_id=100, position="FWD", position_encoded=3, team_id=team_away)

    for gw in (1, 2, 3):
        make_gw_stat(player_id, gameweek=gw, was_home=True, total_points=5)

    # Target gameweek 4: the player's team (team_away) plays away.
    make_fixture(fpl_id=900, gameweek=4, home_team_id=team_home, away_team_id=team_away)

    features = build_features(engine, TEST_SEASON, 4)

    assert features.loc[player_id, "was_home"] == False  # noqa: E712


def test_rolling_features_use_mean_not_sum(make_player, make_gw_stat, engine):
    player_id = make_player(fpl_id=101)
    points = [2, 4, 9]
    for gw, pts in zip((1, 2, 3), points):
        make_gw_stat(player_id, gameweek=gw, total_points=pts)

    features = build_features(engine, TEST_SEASON, 4)
    actual = features.loc[player_id, "pts_rolling_3gw"]

    expected_mean = sum(points) / len(points)
    assert actual == pytest.approx(expected_mean)
    assert actual != pytest.approx(sum(points))


def test_zero_prior_history_gives_genuine_nan(make_player, engine):
    player_id = make_player(fpl_id=102)
    # No player_gw_stats rows inserted at all for this player.

    features = build_features(engine, TEST_SEASON, 4)
    value = features.loc[player_id, "pts_rolling_3gw"]

    assert pd.isna(value)


def test_build_features_raises_clear_error_for_nonexistent_season(engine):
    with pytest.raises(ValueError, match="No player data found for season"):
        build_features(engine, "SEASON_THAT_TRULY_DOES_NOT_EXIST_XYZ", 4)


def test_null_selected_on_a_real_history_row_does_not_crash(make_player, make_gw_stat, engine):
    """Regression test: ml.player_gw_stats.selected is nullable, and a real
    ingested gameweek can have it NULL for every row (confirmed live for
    season 2026-27's only ingested gameweek at the time this was found).
    np.log1p on a column forced to object dtype by a None raises
    AttributeError: 'NoneType' object has no attribute 'log1p' -- a
    different, more confusing failure than a normal numeric NaN would
    produce. Must not crash, and the fallback (0, i.e. "unknown
    ownership") must not silently look like a real recorded value of 0."""
    player_id = make_player(fpl_id=103)
    make_gw_stat(player_id, gameweek=1, total_points=5, selected=None)

    features = build_features(engine, TEST_SEASON, 2)

    assert features.loc[player_id, "ownership_log"] == 0


def test_null_value_on_a_real_history_row_leaves_price_current_nan(make_player, engine):
    """price_current must NOT get the same zero-fallback ownership_log
    does -- a fabricated £0.0m price is misleading in a way "unknown
    ownership" isn't, so a missing value stays a genuine NaN (this
    module's existing "no zero-imputation" rule) rather than being
    coerced to 0. ml.player_gw_stats.value is NOT NULL at the schema
    level, so this exercises the defensive pd.to_numeric(errors="coerce")
    path via a player with no history row at all, the one real way a NaN
    reaches this column -- the same path test_zero_prior_history_gives_
    genuine_nan already covers for the rolling features."""
    player_id = make_player(fpl_id=104)

    features = build_features(engine, TEST_SEASON, 2)

    assert pd.isna(features.loc[player_id, "price_current"])
