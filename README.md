# FPL Points Predictor

A three-stage, read-only-after-ingestion pipeline that predicts Fantasy
Premier League points and tiers the results before they reach a user or LLM.

See [ARCHITECTURE.md](ARCHITECTURE.md) for a full system reference: API surface, data model, scheduled tasks, and known gaps.

## Pipeline

All backend code lives under `backend/`.

1. **`backend/Data/`** — loads FPL API data into a 7-table Postgres schema
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

On top of that pipeline the backend implements two games, served by the
FastAPI app in `backend/Context_assembler/main.py` with a React frontend in
`frontend/`:

- **Season-long FPL** — squad selection, starting XI and chips, transfers,
  scoring and mini-leagues.
- **Dream11 contests** — single-fixture contests with their own `dream11`
  schema, credit-based pricing and point weightings. See **[DREAM11.md](DREAM11.md)**.

### Where the game code lives

The season-long game is split by responsibility, one directory per box. The
dependency direction is **Gameplay → GameEngine → Results**, with everything
free to depend on `Shared/` and `Data/` and nothing depending back on them —
the graph is acyclic, and it is meant to stay that way.

| Directory | Holds | Depends on |
|---|---|---|
| `backend/Shared/` | Pure rules and primitives with no project imports at all: `rules.py` (the constant table), `deadlines.py`, `db_utils.py` | nothing |
| `backend/Data/` | Identity and reference data: `auth.py`, `players.py`, plus ingestion — `fpl_ingest.py`, `live_poll.py`, and the trained `model.json` | `Shared` |
| `backend/Gameplay/` | What a manager does: `squad_selection.py`, `starting_xi.py`, `transfers.py`, `transfer_drafts.py`, `chips.py`, `lineup.py` | `Shared`, `Data`, `GameEngine` |
| `backend/GameEngine/` | What the clock does, on Celery Beat: `gameweek_lock.py`, `gameweek_finalize.py`, `free_hit_revert.py` | `Shared`, `Results` |
| `backend/Results/` | What came out: `scoring.py`, `standings.py`, `leagues.py`, `team_dashboard.py` | `Shared`, `Data` |
| `backend/Game_logic/` | Dream11 only — `dream11.py`, `dream11_locking.py`, `dream11_scoring.py` — plus `fixtures.py`, which is half Dream11 and awaits that audit | `Shared`, `Data` |

`backend/Worker/beat_registry.py` is the index of which of these Beat
actually calls.

**This layout dates from the 2026 reorganisation.** Before it, all of the
above lived in one `backend/Game_logic/` directory, and ingestion lived in
`backend/Data_ingestion/`. Older files that record a moment in time —
migration docstrings especially — still name those paths, deliberately; see
[backend/Migrations/README.md](backend/Migrations/README.md).

## Setup

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in the project root (not tracked in git — see
`.env.example` for the full list including `JWT_SECRET`, `GROQ_API_KEY`
and `API_FOOTBALL_DATA_KEY`):
```
DATABASE_URL=postgresql://user:password@host:port/dbname
REDIS_URL=redis://localhost:6379/0
ALLOWED_ORIGINS=http://localhost:5173
```

All three are **required** and have no built-in default — the app raises on
startup naming the `.env` paths it searched rather than quietly falling back
to localhost (or, for `ALLOWED_ORIGINS`, to a wildcard), so a misconfigured
environment fails loudly instead of connecting somewhere unintended or
accepting requests from anywhere. `REDIS_URL` is the Celery broker *and*
result backend (see `backend/Worker/celery_app.py`); it is only needed for
the worker, Beat, and anything that queues a task. `ALLOWED_ORIGINS` is the
FastAPI app's CORS allowlist (see `backend/Context_assembler/main.py`) — a
comma-separated list of origins the browser is allowed to call the API
from; `http://localhost:5173` is Vite's default dev-server origin
(`npm run dev` under `frontend/`), confirmed by actually starting it.
