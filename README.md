# Pitchside

A fantasy football app with a tactical twist: pick a tactic, choose two Bonus Players, and plan in-game substitutions. It also includes single-match contests you can play with friends.

**Status:** MVP, deployed and tested with a small private group of users. Non-commercial portfolio project.

**Live:** <YOUR-URL> · **Demo video:** <VIDEO-LINK> · **User guide:** [User Guide.pdf](frontend/public/docs/User%20Guide.pdf)

<!-- Add a screenshot here: ![Pitchside screenshot](docs/screenshot.png) -->

## What it does

- **Tactic mode (season-long).** Build a 15-man squad, set a lineup each gameweek, and choose Attack, Defence or Balanced. Your two Bonus Players earn extra Tactical Points, and planned Tactical Subs can earn a Sub Bonus. Includes free-transfer banking and classic or head-to-head mini-leagues.
- **Quick 11 mode (single match).** Build 11 players on a credit budget, invite friends with a code, and see results after the final whistle.
- **AI assistant.** Chat advice built on player form tiers from an XGBoost model.

## Highlights

- Deployed on a 1 GB Google Cloud VM (nginx, systemd, Postgres, Redis, Celery).
- Automatic scoring after every match, driven by a database-backed live poller.
- Database triggers enforce game rules as a second line of defence behind the application code.
- 900+ automated tests and 31 hand-written migrations.
- JWT auth, rate limiting, and honest, documented known gaps.

## Tech stack

FastAPI · PostgreSQL · Celery + Redis · React + Vite · XGBoost · Groq · nginx · systemd · Google Cloud

## Documentation

| Doc | What it covers |
|---|---|
| [User guide](frontend/public/docs/User%20Guide.pdf) | Getting started for players |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | System reference: API, data model, scheduled tasks, known gaps |
| [docs/ML_LAYERS.md](docs/ML_LAYERS.md) | The prediction pipeline: features, model, tiers |
| [docs/DREAM11.md](docs/DREAM11.md) | Quick 11 contests (code name: Dream11): schema, pricing, scoring |
| [docs/DEPLOY_PLAN.md](docs/DEPLOY_PLAN.md) | Server setup and deployment |
| [deploy/STAGING.md](deploy/STAGING.md) · [deploy/PRODUCTION.md](deploy/PRODUCTION.md) | Staging setup and manual production frontend releases |
| [docs/IMPLEMENTATION_PLAN.md](docs/IMPLEMENTATION_PLAN.md) | Tactic mode rulebook and conversion plan (historical) |
| [docs/STATUS.md](docs/STATUS.md) | Status snapshot from 22 Sep 2026 (historical) |
| [docs/history/](docs/history/) | Phase reports (point-in-time records) |
| [docs/stitch_mockups/](docs/stitch_mockups/) | UI mockups |
| [frontend/README.md](frontend/README.md) | Frontend setup and commands |

Some docs predate recent changes. Each file's header carries its last-verified date, and ARCHITECTURE.md is the most current.

## Roadmap

Built for the MVP: Tactic mode, Quick 11 mode, leagues, AI chat, automatic scoring and a live deployment.

Deliberately left for later:

- **Reliability:** uptime alerts on `/health/scheduled-tasks`, and backoff if the data source blocks the server's IP.
- **Game balance:** re-tune Tactical Points after about 20 gameweeks of 2026-27 data.
- **Features:** public contest browsing.
- **Data:** the Quick 11 scoring categories that have no data source yet.

## Disclaimer, privacy and licence

Not affiliated with, endorsed by or connected to the Premier League or Fantasy Premier League. Personal, non-commercial use only; no official logos or crests are used. The app stores account details, picks and scores for its small test group, and accounts can be deleted from within the app.

**Licence:** <CHOOSE ONE, e.g. MIT>

## Running locally

```
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```

Create a `.env` in the project root (not tracked in git; see `.env.example` for the full list, including `JWT_SECRET`, `GROQ_API_KEY` and `RATE_LIMIT_STORAGE_URL`):

```
DATABASE_URL=postgresql://user:password@host:port/dbname
REDIS_URL=redis://localhost:6379/0
ALLOWED_ORIGINS=http://localhost:5173
```

`DATABASE_URL`, `REDIS_URL` and `ALLOWED_ORIGINS` are **required** and have no built-in default. The app raises on startup rather than quietly falling back to localhost or a wildcard, so a misconfigured environment fails loudly. `REDIS_URL` is the Celery broker and result backend; it is only needed for the worker and Beat. `ALLOWED_ORIGINS` is the CORS allowlist, a comma-separated list of origins the browser may call the API from (`http://localhost:5173` is Vite's default).

Apply migrations with `alembic upgrade heads`, then start the API and the frontend (`npm run dev` under `frontend/`).

### Redis and the Celery worker

Run Redis with a memory cap and no persistence:

```
docker run -d --name redis -p 6379:6379 redis:7-alpine redis-server --maxmemory 64mb --maxmemory-policy noeviction --save "" --appendonly no
```

`noeviction` is deliberate: an LRU policy would silently drop queued tasks, while `noeviction` makes a full Redis refuse writes loudly. Nothing in Redis needs to survive a restart, because live-poll progress is tracked in Postgres.

Run the worker and Beat from `backend/`. On Linux, as one process with Beat embedded:

```
celery -A Worker.celery_app worker -B --pool=solo --loglevel=info
```

On Windows, Celery refuses `-B`, so run two processes:

```
celery -A Worker.celery_app worker --pool=solo --loglevel=info
celery -A Worker.celery_app beat --loglevel=info
```

Never pass `-B` to more than one worker. For server deployment, see [docs/DEPLOY_PLAN.md](docs/DEPLOY_PLAN.md).

## How the code is organised

All backend code lives under `backend/`. The prediction pipeline feeds the games, which are served by the FastAPI app in `backend/Context_assembler/main.py`, with a React frontend in `frontend/`.

### Prediction pipeline

- **`backend/Data/`**: loads FPL API data into the `ml` schema (8 tables) via `fpl_ingest.py`, and holds the trained model (`model.json`, the portable xgboost booster dump, and `model_metadata.json`).
- **`backend/Feature_engineering/`**: `build_features(engine, season, target_gameweek)` computes the 21 model features per player from raw stats. Missing values are left as `NaN` on purpose, since the model was trained with `missing=nan`.
- **`backend/Predict/`**: runs the features through `model.json`, then `tier_builder.py` turns raw predictions into a tier (Elite, Strong, Average, Weak) or a status label (New/Insufficient Data, Doubtful/Injured/Unavailable). Predictions are written by `backfill_predictions.py`, run by a daily timer on the server.

### Game code

The dependency direction is **Gameplay → GameEngine → Results**, with everything free to depend on `Shared/` and `Data/`, and nothing depending back on them. The graph is acyclic and is meant to stay that way.

| Directory | Holds | Depends on |
|---|---|---|
| `backend/Shared/` | Pure rules and primitives: `rules.py` (the constant table), `deadlines.py`, `seasons.py`, `db_utils.py`, `rate_limit.py` | nothing |
| `backend/Data/` | Identity and reference data (`auth.py`, `players.py`, `scoring_rules.py`) plus ingestion (`fpl_ingest.py`, `live_poll.py`) | `Shared` |
| `backend/Gameplay/` | What a manager does: `squad_selection.py`, `starting_xi.py`, `transfers.py`, `transfer_drafts.py`, `lineup.py` | `Shared`, `Data`, `GameEngine` |
| `backend/GameEngine/` | What the clock does, on Celery Beat: `gameweek_lock.py`, `gameweek_finalize.py`, `selection_carry_forward.py` | `Shared`, `Results` |
| `backend/Results/` | What came out: `tactical_scoring.py`, `scoring_job.py`, `standings.py`, `leagues.py`, `team_dashboard.py` | `Shared`, `Data` |
| `backend/Game_logic/` | Quick 11 mode (code name Dream11): `dream11.py`, `dream11_locking.py`, `dream11_scoring.py`, plus `fixtures.py` | `Shared`, `Data` |

`backend/Worker/beat_registry.py` is the index of which of these Beat actually calls.
