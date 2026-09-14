"""
main.py — FastAPI context assembler for FPL chat-style advice.

Looks up a user's real starting XI for a gameweek from the public schema
(starting_xi / gw_selections / user_squads / users), maps FPL ids to
internal ml.players ids, and reads already-computed predictions/tiers from
ml.ml_predictions (written by Worker.tasks.run_ml_pipeline) -- this
endpoint does NOT call Feature_engineering.feature_builder or run any
predictions itself anymore. That live-compute pipeline now lives solely in
the Celery task; /chat just reads its output.

If ml.ml_predictions is missing a row for a player in the starting XI
(the scheduled task hasn't run yet, or ran for a different gameweek),
that player is treated as "New/Insufficient Data" and a warning is logged
-- not an error, since a stale table shouldn't break chat for players who
do have data.

Pipeline per request:
  chat-availability gate -> starting_xi lookup -> ml.ml_predictions lookup
  -> assemble a tier-only prompt (no raw predicted_points ever reaches
  Groq, captain/vice-captain flagged) -> call_groq.

Read-only against the database.

If a user has no gw_selections/starting_xi rows for the requested
season/gameweek, that's a 200 with a "please select your squad" message,
not an error -- fail fast. A starting XI is always <= 11 players, so it
naturally respects MAX_PLAYERS (kept as a defensive cap regardless).
"""

import logging
import os
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path

# Explicit dotted imports (from Game_logic.db_utils import ...) still need
# the repo root on sys.path to resolve Game_logic as a package -- that's
# not implied by running this file directly (e.g. `uvicorn main:app` from
# within this directory only puts Context_assembler/ on sys.path, which is
# what groq_client's own bare import below needs). Confirmed by actually
# booting uvicorn both with and without this line before settling on it.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from Shared.db_utils import get_engine
from Data.auth import CurrentUser, get_current_user, router as auth_router
from Data.players import router as players_router
from Data.scoring_rules import router as scoring_rules_router
from Game_logic.fixtures import router as fixtures_router
from Gameplay.squad_selection import router as squad_router
from Gameplay.starting_xi import router as starting_xi_router
from Gameplay.chips import router as chips_router
from Gameplay.lineup import router as lineup_router
from Gameplay.transfers import router as transfers_router
from Gameplay.transfer_drafts import router as transfer_drafts_router
from Results.leagues import router as leagues_router
from Game_logic.dream11 import router as dream11_router
from Results.team_dashboard import router as team_dashboard_router
from Worker.task_health import get_all_task_heartbeats
from groq_client import call_groq, GroqError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)


@lru_cache(maxsize=1)
def _allowed_origins() -> list[str]:
    """Find and load the nearest .env, then return ALLOWED_ORIGINS as a
    list, comma-separated in the file (e.g.
    "http://localhost:5173,http://localhost:4173").

    Same convention as Data/auth.py's _jwt_secret() and
    Shared/db_utils.py's _database_url(): walk up for the nearest .env,
    and RAISE rather than fall back to a hardcoded default if the
    variable isn't set. This used to be allow_origins=["*"], which was
    fine when nothing on this API was worth stealing a session for --
    that stopped being true once real JWT auth landed, and a wildcard
    origin alongside a bearer token is exactly the combination CORS
    exists to prevent.

    The "sensible local-dev default" lives in .env (see .env.example),
    not in this function -- a Python-level localhost fallback would mean
    a production deploy that forgot to set ALLOWED_ORIGINS silently
    opened itself to every origin, the same failure mode REDIS_URL's
    hardcoded default caused before Worker/celery_app.py's _redis_url()
    was written to raise instead. Never repeat that here.
    """
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    raw = os.getenv("ALLOWED_ORIGINS")
    if not raw:
        looked = "\n  ".join(str(d / ".env") for d in _SEARCH_DIRS[:4])
        raise RuntimeError("ALLOWED_ORIGINS not set. Looked for a .env in:\n  " + looked)
    return [origin.strip() for origin in raw.split(",") if origin.strip()]

DB_UNAVAILABLE_MESSAGE = "Having trouble reaching player data right now -- please try again shortly."
GROQ_UNAVAILABLE_MESSAGE = "Having trouble generating advice right now -- please try again in a moment."

# Must match Worker/tasks.py's MODEL_VERSION -- confirmed via
# `SELECT DISTINCT model_version FROM ml.ml_predictions` that this is the
# actual value the Celery task writes.
MODEL_VERSION = "xgboost_v1"

MAX_PLAYERS = 15

INSTRUCTIONS = """You are helping a Fantasy Premier League manager make squad decisions.

You are given each player's tier, not a raw predicted score. Tiers reflect
recent form ranking, not exact point forecasts:
- Elite: top 10% of predicted performers this gameweek
- Strong: next 25%
- Average: middle 40%
- Weak: bottom 25%
- New/Insufficient Data: this player has no games played yet this season --
  you have no form read on them. Say so plainly. Do not guess or invent a
  performance expectation for them.
- Doubtful/Injured/Unavailable: do not recommend starting this player.

The underlying prediction model is known to underestimate big, explosive
performances (goals, hauls, clean sheet bonuses) -- treat a tier as a
FLOOR on expected performance, not a ceiling. A "Strong" player could
easily have a huge game; an "Elite" player is not guaranteed a big haul,
just the most likely to perform well among this group.

One player in the context below may be marked [CURRENT CAPTAIN] or
[CURRENT VICE-CAPTAIN] -- that's who the manager has already picked for
this gameweek. If asked about captaincy, compare your suggestion against
that current pick explicitly, don't just rank players in a vacuum.

Never state a specific predicted-points number or invent one, even if
asked directly -- you were not given one and do not have one."""

app = FastAPI(title="FPL Context Assembler")

# The frontend/ dev server runs on its own Vite port, not served by
# FastAPI, so without this the browser blocks the fetch() call.
# allow_origins is an explicit list from ALLOWED_ORIGINS (see
# _allowed_origins() above), not a wildcard -- this API carries a bearer
# token on every authenticated request, and allow_origins=["*"] alongside
# that is exactly the hole CORS exists to close.
app.add_middleware(
    CORSMiddleware, allow_origins=_allowed_origins(), allow_methods=["*"], allow_headers=["*"]
)

app.include_router(auth_router)
app.include_router(players_router)
app.include_router(scoring_rules_router)
app.include_router(fixtures_router)
app.include_router(squad_router)
app.include_router(starting_xi_router)
app.include_router(chips_router)
app.include_router(lineup_router)
app.include_router(transfers_router)
app.include_router(transfer_drafts_router)
app.include_router(leagues_router)
app.include_router(team_dashboard_router)
app.include_router(dream11_router)


# ------------------------------------------------------------------ health
#
# Neither endpoint requires a credential -- a health check that needs a
# valid JWT to answer "is the service up" is not a health check, it's a
# second thing that can be down. Same "public by decision" reasoning as
# GET /players.

class HealthResponse(BaseModel):
    status: str
    database: str


@app.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    """At minimum: can this process reach the database. Nothing here checks
    Redis/Celery -- a worker or Beat being down does not make the API
    itself unhealthy, and conflating the two would make this endpoint fail
    for a problem this process cannot fix by restarting. See
    GET /health/scheduled-tasks for the Beat-side question."""
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
    except SQLAlchemyError as e:
        logger.error("health check: database unreachable: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=503, detail="database unreachable") from e
    return HealthResponse(status="ok", database="reachable")


class TaskHeartbeatResponse(BaseModel):
    task_name: str
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_error: str | None = None
    seconds_since_success: float | None = None
    # None for a task never yet run, OR one on a crontab schedule (e.g.
    # weekly) rather than a plain interval -- "expected interval" isn't a
    # single number for a cron, and guessing at one would be worse than
    # admitting this endpoint doesn't compute it.
    expected_interval_seconds: float | None = None
    # None whenever expected_interval_seconds is None: there is nothing to
    # compare seconds_since_success against, so "stale" isn't a yes/no this
    # endpoint can answer for that task.
    stale: bool | None = None


class ScheduledTasksHealthResponse(BaseModel):
    tasks: list[TaskHeartbeatResponse]


# A missed run gets one full cycle of slack before this calls it stale --
# a task that runs a little late (worker busy, a slow prior task) is not
# the same incident as Beat having stopped entirely, and 1x would flag
# the former as often as the latter.
STALE_INTERVAL_MULTIPLIER = 2.0


@app.get("/health/scheduled-tasks", response_model=ScheduledTasksHealthResponse)
def health_scheduled_tasks() -> ScheduledTasksHealthResponse:
    """Per Beat-scheduled task: when did it last succeed, when did it last
    fail, and -- for tasks on a plain interval -- has it gone stale against
    that interval. This is what makes "Beat silently stopped" detectable at
    all: previously nothing recorded that any of these eight tasks had ever
    run, so there was no fact to check.

    Deliberately not a monitor: this returns the current numbers on
    request, once. Nothing here polls on a schedule, retains history
    beyond the single last success/failure, or notifies anyone -- turning
    "stale" into a page or an email is the real Observability project's
    job, not this endpoint's.

    Worker.celery_app is imported LOCALLY, inside this handler, not at
    module level -- same reasoning as Game_logic/dream11.py's create_contest
    importing Worker.tasks locally: nothing in the API's import graph
    otherwise reaches into Worker, and a module-level import here would
    mean every server boot (and every test that imports this module)
    resolves REDIS_URL and constructs a Celery app just to read a
    dictionary. Constructing that object doesn't connect to Redis by
    itself -- only dispatching a task or polling a queue would -- so this
    stays safe to call even while Redis itself is down, which is exactly
    when you most want a health check to still answer.
    """
    from Worker.celery_app import app as celery_app

    engine = get_engine()
    heartbeats_by_name = {r.task_name: r for r in get_all_task_heartbeats(engine)}

    tasks = []
    for entry in celery_app.conf.beat_schedule.values():
        task_name = entry["task"]
        schedule = entry["schedule"]
        interval_seconds = float(schedule) if isinstance(schedule, (int, float)) else None

        row = heartbeats_by_name.get(task_name)
        seconds_since_success = float(row.seconds_since_success) if row and row.seconds_since_success is not None else None

        stale = (
            seconds_since_success > interval_seconds * STALE_INTERVAL_MULTIPLIER
            if interval_seconds is not None and seconds_since_success is not None
            else None
        )

        tasks.append(
            TaskHeartbeatResponse(
                task_name=task_name,
                last_success_at=row.last_success_at if row else None,
                last_failure_at=row.last_failure_at if row else None,
                last_error=row.last_error if row else None,
                seconds_since_success=seconds_since_success,
                expected_interval_seconds=interval_seconds,
                stale=stale,
            )
        )

    return ScheduledTasksHealthResponse(tasks=sorted(tasks, key=lambda t: t.task_name))


STARTING_XI_QUERY = text(
    """
    SELECT sx.player_id AS fpl_id, ml.id AS internal_id
    FROM starting_xi sx
    JOIN gw_selections gs ON gs.id = sx.gw_selection_id
    JOIN ml.players ml ON ml.fpl_id = sx.player_id AND ml.season = gs.season
    WHERE gs.user_id = :user_id AND gs.season = :season AND gs.gameweek = :gameweek
    """
)

CAPTAIN_QUERY = text(
    """
    SELECT captain_id, vice_captain_id
    FROM gw_selections
    WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek
    """
)

PREDICTIONS_QUERY = text(
    """
    SELECT player_id, tier_or_label
    FROM ml.ml_predictions
    WHERE season = :season AND gameweek = :gameweek
      AND player_id = ANY(:player_ids)
      AND model_version = :model_version
    """
)

# player_id/position/web_name and price are read directly here -- not via
# feature_builder, per the "no longer import Feature_engineering" rule.
PLAYER_INFO_QUERY = text(
    "SELECT id AS player_id, web_name, position FROM ml.players WHERE season = :season"
)

PRICE_QUERY = text(
    """
    SELECT DISTINCT ON (player_id) player_id, value
    FROM ml.player_gw_stats
    WHERE season = :season AND gameweek < :gameweek
    ORDER BY player_id, gameweek DESC
    """
)

CHAT_AVAILABILITY_MESSAGE = (
    "Chat advice will be available once Gameweek 1 results are in -- "
    "check back after the first matches finish."
)

# Distinct from CHAT_AVAILABILITY_MESSAGE above: that one is a season-wide
# gate (no GW1 data exists for anyone yet); this one is per-user (GW1 data
# exists, but *this* caller has no starting_xi/gw_selections row for the
# requested season+gameweek yet). Named the same way for the same reason --
# so tests assert against the constant, not a string literal that could
# drift between main.py and the test file.
NO_SQUAD_MESSAGE = "Create your team for this gameweek to get chat advice on your squad."


def is_chat_available(engine, season: str) -> bool:
    """False until GW1 data exists -- before that, every player would show
    New/Insufficient Data (no rolling history), making chat technically
    functional but practically useless."""
    with engine.connect() as conn:
        result = conn.execute(
            text("SELECT COUNT(*) FROM ml.player_gw_stats WHERE season = :season AND gameweek = 1"),
            {"season": season},
        ).scalar()
    return result > 0


class ChatRequest(BaseModel):
    season: str
    gameweek: int
    message: str


class ChatResponse(BaseModel):
    response: str


def _build_prompt(
    context_df: pd.DataFrame,
    captain_internal_id: int | None,
    vice_captain_internal_id: int | None,
    message: str,
) -> str:
    lines = [INSTRUCTIONS, "", "Player context (your current starting XI):"]
    for player_id, row in context_df.iterrows():
        price = f"£{row['price_current']}m" if pd.notna(row["price_current"]) else "price unknown"
        tag = ""
        if player_id == captain_internal_id:
            tag = " [CURRENT CAPTAIN]"
        elif player_id == vice_captain_internal_id:
            tag = " [CURRENT VICE-CAPTAIN]"
        lines.append(
            f"- {row['web_name']} (player_id {player_id}, {row['position']}, "
            f"{price}): {row['tier_or_label']}{tag}"
        )
    lines.append("")
    lines.append(f"User question: {message}")
    return "\n".join(lines)


@app.post("/chat", response_model=ChatResponse)
def chat(
    req: ChatRequest,
    current_user: CurrentUser = Depends(get_current_user),
) -> ChatResponse:
    user_id = current_user.id
    engine = get_engine()

    try:
        if not is_chat_available(engine, req.season):
            return ChatResponse(response=CHAT_AVAILABILITY_MESSAGE)

        squad = pd.read_sql(
            STARTING_XI_QUERY,
            engine,
            params={"user_id": user_id, "season": req.season, "gameweek": req.gameweek},
        )

        if squad.empty:
            return ChatResponse(response=NO_SQUAD_MESSAGE)

        if len(squad) > MAX_PLAYERS:
            raise HTTPException(
                status_code=400,
                detail=f"too many players -- max {MAX_PLAYERS} per request, got {len(squad)}",
            )

        fpl_to_internal = dict(zip(squad["fpl_id"], squad["internal_id"]))
        player_ids = squad["internal_id"].tolist()

        selection = pd.read_sql(
            CAPTAIN_QUERY,
            engine,
            params={"user_id": user_id, "season": req.season, "gameweek": req.gameweek},
        )
        captain_internal_id = fpl_to_internal.get(int(selection.iloc[0]["captain_id"]))
        vice_captain_internal_id = fpl_to_internal.get(int(selection.iloc[0]["vice_captain_id"]))

        predictions = pd.read_sql(
            PREDICTIONS_QUERY,
            engine,
            params={
                "season": req.season,
                "gameweek": req.gameweek,
                "player_ids": player_ids,
                "model_version": MODEL_VERSION,
            },
        ).set_index("player_id")

        tier_map = predictions["tier_or_label"].to_dict()
        missing_ids = [pid for pid in player_ids if pid not in tier_map]
        if missing_ids:
            logger.warning(
                "ml.ml_predictions missing row(s) for player_id %s (season=%s, gameweek=%s, "
                "model_version=%s) -- table may be stale or not yet populated for this "
                "gameweek. Treating as New/Insufficient Data.",
                missing_ids, req.season, req.gameweek, MODEL_VERSION,
            )
            for pid in missing_ids:
                tier_map[pid] = "New/Insufficient Data"

        player_info = pd.read_sql(
            PLAYER_INFO_QUERY, engine, params={"season": req.season}
        ).set_index("player_id")

        price_rows = pd.read_sql(
            PRICE_QUERY, engine, params={"season": req.season, "gameweek": req.gameweek}
        ).set_index("player_id")
        price_rows["price_current"] = price_rows["value"] / 10

        context_df = player_info.loc[player_ids].copy()
        context_df["tier_or_label"] = pd.Series(tier_map)
        context_df = context_df.join(price_rows["price_current"])
    except OperationalError as e:
        logger.error("Database connection failure: %s: %s", type(e).__name__, e)
        return ChatResponse(response=DB_UNAVAILABLE_MESSAGE)
    except SQLAlchemyError as e:
        logger.error("Database query failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    prompt = _build_prompt(
        context_df.loc[player_ids], captain_internal_id, vice_captain_internal_id, req.message
    )

    logger.info("Assembled prompt:\n%s", prompt)

    try:
        answer = call_groq(prompt)
    except GroqError as e:
        logger.error("Groq API call failed: %s: %s", type(e).__name__, e)
        return ChatResponse(response=GROQ_UNAVAILABLE_MESSAGE)

    return ChatResponse(response=answer)
