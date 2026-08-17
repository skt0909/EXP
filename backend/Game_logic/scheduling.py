"""
scheduling.py — pure functions backing the three Celery Beat tasks
(lock_expired_gameweeks, refresh_active_gameweeks, schedule_predictions
in Worker/tasks.py). Kept celery-independent, same reason
Game_logic/scoring.py and Game_logic/standings.py are: testable without
a broker or the celery package's import chain, and the Celery task
wrappers stay thin (retry/logging only), matching every other task in
this project.

Deadline for a (season, gameweek) is MIN(ml.fixtures.kickoff_time) for
that pair -- no buffer, no stored column, derived fresh every call.
Returns None if that gameweek's fixtures haven't been ingested yet
(MIN over zero/all-NULL rows), which is a legitimate, expected state,
not an error: gw_selections can exist for a gameweek before its
fixtures (and therefore its kickoff times) have been loaded.

lock_expired_gameweeks only ever flips is_locked FALSE -> TRUE (the
UPDATE's own WHERE clause enforces this), which
enforce_selection_lock_fn's trigger allows -- that trigger only raises
when OLD.is_locked was already TRUE, so this can never conflict with it.

refresh_active_gameweeks' "active" definition, per the confirmed
design: a fixed time window after deadline (default 5 days), not
ml.fixtures.finished or ml.player_gw_stats.is_live. Neither of those
columns is currently a reliable recency signal under this project's
ingestion pipeline (Data_ingestion/fpl_ingest.py) -- is_live is
hardcoded FALSE by that script's ingest_gameweek_stats, and
ingest_fixtures refuses to write any row for a gameweek until every
fixture in it is already finished, so finished/updated_at flip
together in one atomic batch write per gameweek rather than
incrementally as individual matches complete. A gameweek's real match
window (Fri/Sat kickoff through Monday, typically) plus FPL's usual
24-48h bonus-point confirmation lag comfortably fits inside 5 days.

schedule_predictions' "next gameweek needing predictions" is the
earliest (season, gameweek) with a future kickoff_time and no
ml.ml_predictions rows yet -- this module only finds it;
Worker/tasks.py's schedule_predictions task is the one that actually
calls run_ml_pipeline with the result, since that call itself is
celery-dependent.
"""

import logging

from sqlalchemy import text

from Game_logic.scoring import score_gameweek
from Game_logic.standings import compute_league_standings

logger = logging.getLogger(__name__)

DEFAULT_ACTIVE_WINDOW_DAYS = 5

GAMEWEEK_DEADLINE_QUERY = text(
    "SELECT MIN(kickoff_time) FROM ml.fixtures WHERE season = :season AND gameweek = :gameweek"
)

# Split into two queries rather than one with a CASE, so the >NOW()
# comparison happens in SQL (no Python/DB clock-skew risk) and the
# "no deadline yet" set is trivially just "the LEFT JOIN found nothing."
EXPIRED_UNLOCKED_GAMEWEEKS_QUERY = text(
    """
    SELECT gs.season, gs.gameweek
    FROM (SELECT DISTINCT season, gameweek FROM gw_selections WHERE is_locked = FALSE) gs
    JOIN (
        SELECT season, gameweek, MIN(kickoff_time) AS deadline
        FROM ml.fixtures GROUP BY season, gameweek
    ) f ON f.season = gs.season AND f.gameweek = gs.gameweek
    WHERE f.deadline IS NOT NULL AND f.deadline <= NOW()
    """
)

UNLOCKED_NO_DEADLINE_QUERY = text(
    """
    SELECT gs.season, gs.gameweek
    FROM (SELECT DISTINCT season, gameweek FROM gw_selections WHERE is_locked = FALSE) gs
    LEFT JOIN (
        SELECT season, gameweek, MIN(kickoff_time) AS deadline
        FROM ml.fixtures GROUP BY season, gameweek
    ) f ON f.season = gs.season AND f.gameweek = gs.gameweek
    WHERE f.deadline IS NULL
    """
)

LOCK_GAMEWEEK_STMT = text(
    "UPDATE gw_selections SET is_locked = TRUE WHERE season = :season AND gameweek = :gameweek AND is_locked = FALSE"
)

# See module docstring for why this is a fixed window, not
# finished/is_live -- both computed in SQL so DB clock is authoritative.
ACTIVE_GAMEWEEKS_QUERY = text(
    """
    SELECT season, gameweek
    FROM ml.fixtures
    GROUP BY season, gameweek
    HAVING MIN(kickoff_time) IS NOT NULL
       AND MIN(kickoff_time) <= NOW()
       AND MIN(kickoff_time) > NOW() - make_interval(days => :window_days)
    """
)

NEXT_GAMEWEEK_NEEDING_PREDICTIONS_QUERY = text(
    """
    SELECT f.season, f.gameweek
    FROM ml.fixtures f
    WHERE NOT EXISTS (
        SELECT 1 FROM ml.ml_predictions mp WHERE mp.season = f.season AND mp.gameweek = f.gameweek
    )
    GROUP BY f.season, f.gameweek
    HAVING MIN(f.kickoff_time) > NOW()
    ORDER BY MIN(f.kickoff_time) ASC
    LIMIT 1
    """
)


def resolve_gameweek_deadline(engine, season: str, gameweek: int):
    """Returns the tz-aware deadline datetime, or None if this
    gameweek's fixtures haven't been ingested yet."""
    with engine.connect() as conn:
        return conn.execute(GAMEWEEK_DEADLINE_QUERY, {"season": season, "gameweek": gameweek}).scalar()


def lock_expired_gameweeks(engine) -> dict:
    """Locks every (season, gameweek) with an unlocked gw_selections row
    whose deadline has passed. Skips (with a warning, not an error) any
    pair whose deadline can't be computed yet.

    Returns {"locked": [(season, gameweek), ...], "skipped_no_deadline": [(season, gameweek), ...]}.
    """
    with engine.connect() as conn:
        expired = conn.execute(EXPIRED_UNLOCKED_GAMEWEEKS_QUERY).all()
        no_deadline = conn.execute(UNLOCKED_NO_DEADLINE_QUERY).all()

    for p in no_deadline:
        logger.warning(
            "lock_expired_gameweeks: no fixtures/deadline yet for season=%s gameweek=%s -- skipping",
            p.season, p.gameweek,
        )

    locked = []
    for p in expired:
        with engine.begin() as conn:
            conn.execute(LOCK_GAMEWEEK_STMT, {"season": p.season, "gameweek": p.gameweek})
        locked.append((p.season, p.gameweek))

    return {
        "locked": locked,
        "skipped_no_deadline": [(p.season, p.gameweek) for p in no_deadline],
    }


def find_active_gameweeks(engine, window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS) -> list[tuple[str, int]]:
    with engine.connect() as conn:
        rows = conn.execute(ACTIVE_GAMEWEEKS_QUERY, {"window_days": window_days}).all()
    return [(r.season, r.gameweek) for r in rows]


def refresh_active_gameweeks(engine, window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS) -> dict:
    """Runs score_gameweek then compute_league_standings for every
    currently-active (season, gameweek) pair -- the automatic version of
    what the manually-triggered compute_gw_scores/compute_league_standings
    tasks already do; no new scoring logic here.

    Returns {"refreshed": [(season, gameweek), ...], "failed": [(season, gameweek, error_message), ...]}.
    """
    active = find_active_gameweeks(engine, window_days)

    refreshed = []
    failed = []
    for season, gameweek in active:
        try:
            score_gameweek(engine, season, gameweek)
            compute_league_standings(engine, season, gameweek)
            refreshed.append((season, gameweek))
        except Exception as e:
            logger.error(
                "refresh_active_gameweeks: failed for season=%s gameweek=%s: %s: %s",
                season, gameweek, type(e).__name__, e,
            )
            failed.append((season, gameweek, str(e)))

    return {"refreshed": refreshed, "failed": failed}


def find_next_gameweek_needing_predictions(engine) -> tuple[str, int] | None:
    """Earliest (season, gameweek) with a future kickoff_time and no
    ml.ml_predictions rows yet. None if nothing upcoming needs it."""
    with engine.connect() as conn:
        row = conn.execute(NEXT_GAMEWEEK_NEEDING_PREDICTIONS_QUERY).first()
    return (row.season, row.gameweek) if row is not None else None
