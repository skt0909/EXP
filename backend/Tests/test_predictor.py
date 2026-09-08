"""
test_predictor.py — unit tests for Predict/predictor.py: booster loading/
caching and predict_points() output shape. Uses the real model.json (a
small, already-trained xgboost model checked into the repo) rather than
mocking xgboost, since the whole point is to catch a broken model file or
a FEATURE_COLS/DMatrix mismatch -- something a mock would hide.
"""

import numpy as np
import pandas as pd
import xgboost as xgb

from feature_builder import FEATURE_COLS
import predictor
from predictor import get_booster, predict_points


def _make_features(n=3, all_nan=False):
    rng = np.random.default_rng(seed=0)
    data = {}
    for col in FEATURE_COLS:
        if all_nan:
            data[col] = [np.nan] * n
        elif col == "was_home":
            data[col] = rng.integers(0, 2, size=n).astype(float)
        elif col == "position_encoded":
            data[col] = rng.integers(0, 4, size=n).astype(float)
        else:
            data[col] = rng.uniform(0, 10, size=n)
    return pd.DataFrame(data, index=pd.Index(range(100, 100 + n), name="player_id"))


def test_get_booster_returns_xgb_booster():
    booster = get_booster()
    assert isinstance(booster, xgb.Booster)


def test_get_booster_is_cached_singleton():
    first = get_booster()
    second = get_booster()
    assert first is second


def test_predict_points_returns_expected_shape():
    features = _make_features(n=4)
    result = predict_points(features)

    assert list(result.columns) == ["predicted_points"]
    assert len(result) == 4
    assert list(result.index) == list(features.index)


def test_predict_points_values_are_finite():
    features = _make_features(n=5)
    result = predict_points(features)

    assert result["predicted_points"].notna().all()
    assert np.isfinite(result["predicted_points"]).all()


def test_predict_points_handles_all_missing_features():
    """The model was trained with missing=nan -- a player with zero rolling
    history (all-NaN feature row, e.g. a brand-new signing) must still
    produce a real numeric prediction, not a NaN or a crash."""
    features = _make_features(n=1, all_nan=True)
    result = predict_points(features)

    assert len(result) == 1
    assert np.isfinite(result["predicted_points"].iloc[0])


def test_predict_points_single_row_matches_batch_prediction():
    """Predicting one row alone should give the same result as predicting
    it as part of a larger batch -- rows must not influence each other."""
    features = _make_features(n=3)
    batch_result = predict_points(features)

    single_row = features.iloc[[1]]
    single_result = predict_points(single_row)

    assert single_result["predicted_points"].iloc[0] == batch_result["predicted_points"].iloc[1]
