"""
predictor.py — load model.json and run predictions against a features
DataFrame. Extracted so predict_gameweek.ipynb, Worker/tasks.py, and (later)
Context_assembler/main.py can share one implementation instead of each
reimplementing booster loading + DMatrix + predict.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import xgboost as xgb

from feature_builder import FEATURE_COLS

MODEL_JSON = Path(__file__).resolve().parent.parent / "Data" / "model.json"

_booster: xgb.Booster | None = None


def get_booster() -> xgb.Booster:
    """Load model.json once, reuse across calls."""
    global _booster
    if _booster is None:
        _booster = xgb.Booster()
        _booster.load_model(str(MODEL_JSON))
    return _booster


def predict_points(features: pd.DataFrame) -> pd.DataFrame:
    """features: the DataFrame from feature_builder.build_features.
    Returns a DataFrame indexed by player_id with a predicted_points column."""
    booster = get_booster()
    dmatrix = xgb.DMatrix(features[FEATURE_COLS], feature_names=FEATURE_COLS, missing=np.nan)
    predicted = booster.predict(dmatrix)
    return pd.DataFrame({"predicted_points": predicted}, index=features.index)
