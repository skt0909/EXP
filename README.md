# FPL Points Predictor

A three-stage, read-only-after-ingestion pipeline that predicts Fantasy
Premier League points and tiers the results before they reach a user or LLM.

## Pipeline

1. **`Data_ingestion/`** — loads FPL API data into a 7-table Postgres schema
   (`ml.players`, `ml.teams`, `ml.fixtures`, `ml.player_gw_stats`,
   `ml.season_stats`, `ml.player_gw_features`, `ml.ml_predictions`) via
   `fpl_ingest.py`. Also holds the trained model: `model.json` (the
   canonical, portable xgboost booster dump) and `model_metadata.json`
   (feature list, validation metrics, feature importances). `FPL_Model_1
   (1).ipynb` is the original Colab training notebook.

2. **`Feature_engineering/`** — `feature_builder.py`'s
   `build_features(engine, season, target_gameweek)` computes the 21 model
   features per player directly from raw `ml.player_gw_stats`/`players`/
   `fixtures` rows, leaving genuine `NaN` where a player has no rolling
   history or where FBref-sourced stats aren't ingested (the model was
   trained with `missing=nan`, so this is intentional, not a bug).

3. **`Predict/`** — `predict_gameweek.ipynb` (generated + executed by
   `build_notebook.py`) runs `build_features` output through `model.json`,
   compares predictions against real results, and finishes with
   `tier_builder.py`'s `build_tiers(...)`, which turns raw
   `predicted_points` into either a percentile tier (Elite/Strong/Average/
   Weak) for players who are both known and available, or a plain status
   label (`New/Insufficient Data`, `Doubtful/Injured/Unavailable`) for
   everyone else.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in the project root (not tracked in git):
```
DATABASE_URL=postgresql://user:password@host:port/dbname
```
