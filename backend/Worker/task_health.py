"""
task_health.py — records that a Beat-scheduled task ran, and reads that
record back for a health check.

Deliberately small and deliberately not a monitoring system: two SQL
statements and a read query, nothing that polls, notifies, or decides
anything on its own. See migration d4a8f209c1e6 for why the table has
two independent timestamps rather than one, and why this lives in
public rather than ml or dream11.

record_task_heartbeat is called from Worker/tasks.py at the end of each
of the eight Beat-scheduled tasks -- both on success and from inside
their existing except block, so a task that runs but keeps failing is
still visibly attempting to run, not indistinguishable from one Beat
never fires at all. The tasks that are NOT on Beat's schedule
(compute_gw_scores, compute_league_standings -- manually triggered) do
not call this: there is no "expected interval"
for a one-off task to go stale against, so a heartbeat for one would
have nothing to compare itself to.

Pure functions taking an engine, same shape as every other Game_logic/
GameEngine module in this project -- testable against a real test
database without Celery installed or running.
"""

from sqlalchemy import text

UPSERT_SUCCESS_STMT = text(
    """
    INSERT INTO public.task_heartbeats (task_name, last_success_at)
    VALUES (:task_name, now())
    ON CONFLICT (task_name) DO UPDATE SET last_success_at = EXCLUDED.last_success_at
    """
)

# last_success_at is NOT touched here -- see module docstring on why the
# two timestamps must move independently.
UPSERT_FAILURE_STMT = text(
    """
    INSERT INTO public.task_heartbeats (task_name, last_failure_at, last_error)
    VALUES (:task_name, now(), :error)
    ON CONFLICT (task_name) DO UPDATE SET
        last_failure_at = EXCLUDED.last_failure_at,
        last_error = EXCLUDED.last_error
    """
)

# now() - last_*_at computed in SQL, against the database's own clock --
# same reasoning as Shared/deadlines.py: comparing a stored timestamp
# against Python's clock would introduce skew between whatever process
# is asking (here, the FastAPI process) and whatever process last wrote
# the row (a Celery worker, possibly on a different host).
ALL_HEARTBEATS_QUERY = text(
    """
    SELECT task_name, last_success_at, last_failure_at, last_error,
           EXTRACT(EPOCH FROM (now() - last_success_at)) AS seconds_since_success
    FROM public.task_heartbeats
    ORDER BY task_name
    """
)


def record_task_heartbeat(engine, task_name: str, success: bool, error: str | None = None) -> None:
    """Upsert this task's last-run outcome. Call once per task execution,
    success or failure -- never both for the same run."""
    with engine.begin() as conn:
        if success:
            conn.execute(UPSERT_SUCCESS_STMT, {"task_name": task_name})
        else:
            # Truncated: this is a health-check field, not a log -- the
            # full traceback is already in whatever logger.exception call
            # sits beside record_task_heartbeat's failure-path call in
            # Worker/tasks.py.
            conn.execute(UPSERT_FAILURE_STMT, {"task_name": task_name, "error": str(error)[:2000]})


def get_all_task_heartbeats(engine) -> list:
    """Every task_heartbeats row. Each exposes .task_name, .last_success_at,
    .last_failure_at, .last_error, .seconds_since_success (None if the task
    has never succeeded)."""
    with engine.connect() as conn:
        return conn.execute(ALL_HEARTBEATS_QUERY).all()
