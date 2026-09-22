"""
prediction_scheduling.py — which gameweek the ML pipeline should predict
next.

Relocated verbatim from Game_logic/scheduling.py. It lived there only
because Celery Beat calls it, which is a deployment grouping rather than
a domain one: this function asks a question about ml.ml_predictions
coverage and has nothing to do with deadlines, locking, scoring or the
Free Hit chip it used to sit among.

It belongs on the Predict side because that is what it schedules, but it
is deliberately NOT in predictor.py -- this is a pure DB query about
pipeline coverage, not model code, and keeping it separate means
Worker/tasks.py can ask "what needs predicting?" without loading a model
or its dependencies.

Finding the gameweek is all this does. Worker/tasks.py's
schedule_predictions task is what actually calls run_ml_pipeline with
the answer, because that call is celery-dependent -- the same split
every other Beat-backed function in this project uses.
"""

import logging

from sqlalchemy import text

from Shared.seasons import real_season_sql

logger = logging.getLogger(__name__)

# Earliest gameweek that has fixtures, has not kicked off, and has no
# predictions yet. MIN(kickoff_time) > NOW() keeps the comparison in SQL
# so the DB clock is authoritative.
NEXT_GAMEWEEK_NEEDING_PREDICTIONS_QUERY = text(
    f"""
    SELECT f.season, f.gameweek
    FROM ml.fixtures f
    WHERE {real_season_sql('f.season')}
      AND NOT EXISTS (
        SELECT 1 FROM ml.ml_predictions mp WHERE mp.season = f.season AND mp.gameweek = f.gameweek
    )
    GROUP BY f.season, f.gameweek
    HAVING MIN(f.kickoff_time) > NOW()
    ORDER BY MIN(f.kickoff_time) ASC
    LIMIT 1
    """
)


def find_next_gameweek_needing_predictions(engine) -> tuple[str, int] | None:
    """Earliest (season, gameweek) with a future kickoff_time and no
    ml.ml_predictions rows yet. None if nothing upcoming needs it."""
    with engine.connect() as conn:
        row = conn.execute(NEXT_GAMEWEEK_NEEDING_PREDICTIONS_QUERY).first()
    return (row.season, row.gameweek) if row is not None else None
