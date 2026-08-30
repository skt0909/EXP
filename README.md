# FPL Points Predictor

A three-stage, read-only-after-ingestion pipeline that predicts Fantasy
Premier League points and tiers the results before they reach a user or LLM.

## Pipeline

All backend code lives under `backend/`.

1. **`backend/Data_ingestion/`** — loads FPL API data into a 7-table Postgres schema
   (`ml.players`, `ml.teams`, `ml.fixtures`, `ml.player_gw_stats`,
   `ml.season_stats`, `ml.player_gw_features`, `ml.ml_predictions`) via
   `fpl_ingest.py`. Also holds the trained model: `model.json` (the
   canonical, portable xgboost booster dump) and `model_metadata.json`
   (feature list, validation metrics, feature importances). `backend/FPL_Model_1
   (1).ipynb` is the original Colab training notebook.

2. **`backend/Feature_engineering/`** — `feature_builder.py`'s
   `build_features(engine, season, target_gameweek)` computes the 21 model
   features per player directly from raw `ml.player_gw_stats`/`players`/
   `fixtures` rows, leaving genuine `NaN` where a player has no rolling
   history or where FBref-sourced stats aren't ingested (the model was
   trained with `missing=nan`, so this is intentional, not a bug).

3. **`backend/Predict/`** — `predict_gameweek.ipynb` runs `build_features`
   output through `model.json`, compares predictions against real results,
   and finishes with
   `tier_builder.py`'s `build_tiers(...)`, which turns raw
   `predicted_points` into either a percentile tier (Elite/Strong/Average/
   Weak) for players who are both known and available, or a plain status
   label (`New/Insufficient Data`, `Doubtful/Injured/Unavailable`) for
   everyone else.

   The notebook is **build output, not source** — it is gitignored, so a
   fresh clone will not have it. Generate it on demand (needs the database
   reachable, since `build_notebook.py` executes every cell against real
   data as it writes):
   ```
   cd backend/Predict
   python build_notebook.py
   ```
   Regenerate it rather than editing it, and regenerate it after any change
   to `build_notebook.py`, `feature_builder.py` or `model.json`. It was
   previously tracked and drifted silently — nothing in CI executes it, so
   a stale import inside it stayed broken until someone opened it by hand.

## Games

On top of that pipeline, `backend/Game_logic/` implements two games, served by
the FastAPI app in `backend/Context_assembler/main.py` with a React frontend in
`frontend/`:

- **Season-long FPL** — squad selection, starting XI and chips, transfers,
  scoring and mini-leagues.
- **Dream11 contests** — single-fixture contests with their own `dream11`
  schema, credit-based pricing and point weightings. See **[DREAM11.md](DREAM11.md)**.

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in the project root (not tracked in git):
```
DATABASE_URL=postgresql://user:password@host:port/dbname
REDIS_URL=redis://localhost:6379/0
```

Both are **required** and have no built-in default — the app raises on
startup naming the `.env` paths it searched rather than quietly falling back
to localhost, so a misconfigured environment fails loudly instead of
connecting somewhere unintended. `REDIS_URL` is the Celery broker *and*
result backend (see `backend/Worker/celery_app.py`); it is only needed for
the worker, Beat, and anything that queues a task.
