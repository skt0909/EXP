"""
tasks.py — Celery tasks for the ML prediction pipeline and gameweek
scoring.

run_ml_pipeline reuses the exact same building blocks as
Predict/predict_gameweek.ipynb and Context_assembler/main.py:
build_features -> predict_points (Predict/predictor.py) -> build_tiers,
then writes the result into ml.ml_predictions (tier_or_label column added
via ALTER TABLE -- no source DDL file exists in this repo to keep in sync,
confirmed with the user). Re-running for the same (season, gameweek) is
idempotent: existing rows for that season/gameweek/model_version are
deleted before the new ones are inserted, in the same transaction.

compute_gw_scores wraps Game_logic/scoring.py's score_gameweek -- pure
function, all the actual scoring/autosub/captaincy logic lives there,
not here. Unlike run_ml_pipeline, a failure scoring one user does NOT
fail the whole task -- score_gameweek isolates per-user errors
internally and returns them in its summary; this task's own
try/except+retry only covers a failure before any per-user work starts
(e.g. the initial DB query itself failing).

compute_league_standings wraps Game_logic/standings.py's
compute_league_standings (imported here under the alias
compute_standings to avoid shadowing this task's own name) -- same
per-league isolation as compute_gw_scores' per-user isolation; run
compute_gw_scores first for a given (season, gameweek) since this task
reads gw_scores, it doesn't compute it.

On failure, the full exception is logged and the task retries up to 3
times (60s apart) via Celery's built-in retry mechanism -- for transient
blips (e.g. a momentary DB connection drop). Once retries are exhausted,
Celery re-raises the original exception and marks the task FAILED --
never swallowed, since Celery's own retry/monitoring depends on failures
being visible as failures.

Manual-trigger only -- no Celery Beat schedule yet.
"""

import logging
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))

from sqlalchemy import text

from Worker.celery_app import app
from Worker.db_utils import get_engine
from feature_builder import build_features
from predictor import predict_points
from tier_builder import build_tiers
from Game_logic.scoring import score_gameweek
from Game_logic.standings import compute_league_standings as compute_standings

logger = logging.getLogger(__name__)

MODEL_VERSION = "xgboost_v1"


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="run_ml_pipeline")
def run_ml_pipeline(self, season: str, target_gameweek: int) -> dict:
    try:
        engine = get_engine()

        features = build_features(engine, season, target_gameweek)
        predictions = predict_points(features)
        tiers = build_tiers(engine, season, features, predictions)

        rows = tiers[["player_id", "predicted_points", "tier_or_label"]].copy()
        rows["season"] = season
        rows["gameweek"] = target_gameweek
        rows["model_version"] = MODEL_VERSION

        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM ml.ml_predictions "
                    "WHERE season = :season AND gameweek = :gameweek AND model_version = :mv"
                ),
                {"season": season, "gameweek": target_gameweek, "mv": MODEL_VERSION},
            )
            rows.to_sql("ml_predictions", conn, schema="ml", if_exists="append", index=False)

        return {
            "season": season,
            "gameweek": target_gameweek,
            "rows_written": len(rows),
            "tier_distribution": tiers["tier_or_label"].value_counts().to_dict(),
        }
    except Exception as exc:
        logger.exception(
            "run_ml_pipeline failed for season=%s, target_gameweek=%s (attempt %d/%d)",
            season, target_gameweek, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="compute_gw_scores")
def compute_gw_scores(self, season: str, gameweek: int) -> dict:
    try:
        engine = get_engine()
        summary = score_gameweek(engine, season, gameweek)

        if summary["failed"]:
            failures = "; ".join(f"user_id={uid}: {err}" for uid, err in summary["failed"])
            logger.warning(
                "compute_gw_scores: season=%s gameweek=%s -- scored %d user(s), %d FAILED: %s",
                season, gameweek, len(summary["scored"]), len(summary["failed"]), failures,
            )
        else:
            logger.info(
                "compute_gw_scores: season=%s gameweek=%s -- scored %d user(s), 0 failed",
                season, gameweek, len(summary["scored"]),
            )

        return summary
    except Exception as exc:
        logger.exception(
            "compute_gw_scores failed for season=%s, gameweek=%s (attempt %d/%d)",
            season, gameweek, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="compute_league_standings")
def compute_league_standings(self, season: str, gameweek: int) -> dict:
    try:
        engine = get_engine()
        summary = compute_standings(engine, season, gameweek)

        if summary["failed"]:
            failures = "; ".join(f"league_id={lid}: {err}" for lid, err in summary["failed"])
            logger.warning(
                "compute_league_standings: season=%s gameweek=%s -- processed %d league(s), %d FAILED: %s",
                season, gameweek, len(summary["processed"]), len(summary["failed"]), failures,
            )
        else:
            logger.info(
                "compute_league_standings: season=%s gameweek=%s -- processed %d league(s), 0 failed",
                season, gameweek, len(summary["processed"]),
            )

        return summary
    except Exception as exc:
        logger.exception(
            "compute_league_standings failed for season=%s, gameweek=%s (attempt %d/%d)",
            season, gameweek, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)
