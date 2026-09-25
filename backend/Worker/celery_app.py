"""
celery_app.py — Celery application instance (Redis running locally via
Docker) plus the Beat schedule for the three automation tasks in
Worker/tasks.py.

On Linux (Ubuntu dev, the e2-micro server), run the worker with Beat
embedded:
    celery -A Worker.celery_app worker -B --pool=solo --loglevel=info
--pool=solo keeps it to one task process (prefork's parent + child would
be two ~110 MB copies; these tasks are light and run one at a time). -B
forks Beat off the worker, so the two share most of their memory. Only
ever pass -B to ONE worker -- two Beats fire every schedule twice.

On Windows, Celery refuses -B ("-B option does not work on Windows"), and
the prefork pool needs os.fork, so run two processes:
    celery -A Worker.celery_app worker --pool=solo --loglevel=info
    celery -A Worker.celery_app beat --loglevel=info

Beat only schedules tasks onto the broker; the worker executes them. Both
must be running for the schedule below to fire.

The Redis URL comes from the environment, resolved by _redis_url()
below in deliberately the same shape as Game_logic/db_utils.py's
_database_url(): walk up for the nearest .env, prefer a TEST_ override,
and RAISE if neither is set rather than falling back to a default.

That last part is the point. This was previously hardcoded to
redis://localhost:6379/0, which meant any other environment either
connected to the wrong Redis or failed opaquely, with no way to change
it short of editing this file. A localhost default would have preserved
exactly that failure mode -- a misconfigured production deploy would
silently look for a Redis that isn't there instead of saying so -- so
Redis is configured no more leniently than the database is.

Broker and result backend share the one URL, which is what the
hardcoded pair did. Split them only if a deployment genuinely needs
different Redis instances for queueing and results; nothing here
assumes they differ.
"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))

from celery import Celery
from celery.schedules import crontab

_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)


def _redis_url() -> str:
    """Find and load the nearest .env, then return the Redis URL.

    TEST_REDIS_URL wins over REDIS_URL, mirroring how every db_utils.py
    in this project prefers TEST_DATABASE_URL -- so a test run can point
    at a separate Redis (or a separate database index) without touching
    the dev configuration. backend/Tests/conftest.py loads both .env and
    .env.test into os.environ before any test imports this module, which
    is what makes the TEST_ override reachable here.
    """
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    url = os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")
    if not url:
        looked = "\n  ".join(str(d / ".env") for d in _SEARCH_DIRS[:4])
        raise RuntimeError(
            "REDIS_URL not set (Celery broker/result backend). "
            "Looked for a .env in:\n  " + looked
        )
    return url


_REDIS_URL = _redis_url()
_SIM_FAST_BEAT_SECONDS = os.getenv("SIMULATION_BEAT_FAST_SECONDS")


def _beat_interval(seconds: float) -> float:
    """Allow the simulation compose stack to exercise real Beat ticks fast."""
    if os.getenv("ENVIRONMENT") == "simulation" and _SIM_FAST_BEAT_SECONDS:
        return float(_SIM_FAST_BEAT_SECONDS)
    return seconds

app = Celery(
    "fpl_worker",
    broker=_REDIS_URL,
    backend=_REDIS_URL,
    include=["Worker.tasks"],
)

# .apply_async()/.delay() used to be called from inside code that had
# ALREADY committed to the database and treated scheduling as best-effort
# (contest creation booking its polls, inside an HTTP request). Nothing in
# the app does that any more -- live polling is poll_due_fixtures on Beat --
# but these bounds still keep any publish from hanging. Celery's DEFAULT result
# backend retry policy is max_retries=20 at ~5s per attempt, so with Redis
# unreachable those try/except blocks did not run for ~100 SECONDS -- the
# HTTP request just hung -- and the attempt then ended in "Retry limit
# exceeded ... The Celery application must be restarted", poisoning the app
# instance so every later .apply_async() in that process failed too.
# Verified by hand against a live uvicorn with Redis stopped. Bounding the
# retries turns that into a ~2s failure the existing except blocks handle.
#
# Deliberately scoped: only socket_connect_timeout is set on the BROKER,
# not socket_timeout -- the worker's blocking BRPOP consume loop reads on
# that same socket, and a short read timeout would break long-polling.
_FAIL_FAST_RETRY_POLICY = {
    "max_retries": 2,
    "interval_start": 0,
    "interval_step": 0.2,
    "interval_max": 0.5,
}

app.conf.result_backend_transport_options = {
    "socket_connect_timeout": 2,
    "socket_timeout": 2,
    "retry_policy": _FAIL_FAST_RETRY_POLICY,
}
app.conf.broker_transport_options = {"socket_connect_timeout": 2}
if os.getenv("ENVIRONMENT") == "simulation" and os.getenv("SIMULATION_REDIS_VISIBILITY_TIMEOUT"):
    app.conf.broker_transport_options["visibility_timeout"] = int(os.getenv("SIMULATION_REDIS_VISIBILITY_TIMEOUT", "10"))
    app.conf.result_backend_transport_options["visibility_timeout"] = int(
        os.getenv("SIMULATION_REDIS_VISIBILITY_TIMEOUT", "10")
    )
app.conf.task_publish_retry_policy = _FAIL_FAST_RETRY_POLICY

# Memory bounds. The concurrency and per-child limits only apply to the
# prefork pool (Linux); --pool=solo on Windows is already one process.
# Prefork's default concurrency is the CPU count, each child a full copy
# of the app, which is far more than five light Beat tasks need.
app.conf.worker_concurrency = int(os.getenv("CELERY_CONCURRENCY", "1"))
app.conf.worker_max_tasks_per_child = 50
app.conf.worker_max_memory_per_child = 300_000  # KiB: recycle a child past ~300 MB
app.conf.worker_prefetch_multiplier = 1
# Results are only ever read back right away (the .get() calls in
# test_celery_wiring.py), so they don't need Celery's 1-day default in Redis.
app.conf.result_expires = 3600

app.conf.beat_schedule = {
    "lock-expired-gameweeks": {
        "task": "lock_expired_gameweeks",
        "schedule": _beat_interval(300.0),  # every 5 minutes
    },
    "carry-forward-selections": {
        # Same 5-minute cadence as lock-expired-gameweeks: this is what
        # keeps the Dashboard's pitch/bench view populated for the next
        # gameweek before a manager has touched Starting XI, so it should
        # be at least as prompt as locking itself.
        "task": "carry_forward_selections",
        "schedule": _beat_interval(300.0),
    },
    "lock-dream11-contests": {
        # Same 5-minute cadence as lock-expired-gameweeks: this is the only
        # thing that ever locks a Dream11 contest, so the interval bounds how
        # long a contest stays joinable/submittable after its fixture starts.
        "task": "lock_dream11_contests",
        "schedule": _beat_interval(300.0),
    },
    "refresh-active-gameweeks": {
        "task": "refresh_active_gameweeks",
        "schedule": _beat_interval(900.0),  # every 15 minutes
    },
    "finalize-dream11-contests": {
        # Every 15 minutes: voids contests on postponed/abandoned fixtures,
        # then finalizes any poll-due-fixtures' 'final' checkpoint left
        # open. Gates on ml.fixtures.finished, which refresh-fixtures writes.
        "task": "finalize_dream11_contests",
        "schedule": _beat_interval(900.0),
    },
    "poll-due-fixtures": {
        # Every minute, for both game modes: runs each fixture's halftime
        # (kickoff+50), fulltime (kickoff+115) and final (finished) checkpoint
        # the moment it is due. Nothing is booked ahead in Redis. With no
        # match on, one small query and no API call.
        "task": "poll_due_fixtures",
        "schedule": _beat_interval(60.0),
    },
    "refresh-fixtures": {
        # Ticks every 15 minutes but only calls FPL while a fixture is in
        # play or once a day (Data/live_poll.py's fixtures_refresh_reason).
        # Its finished flag is what makes a fixture's 'final' checkpoint due.
        "task": "refresh_fixtures",
        "schedule": _beat_interval(900.0),
    },
    "refresh-player-prices": {
        # 01:30 UTC (02:30 UK summer time), after FPL's overnight price
        # changes: now_cost for GW mode's buy/sell prices, plus new players.
        "task": "refresh_player_prices",
        "schedule": crontab(hour=1, minute=30),
    },
    #
    # schedule-predictions-weekly is disabled (not deleted) along with the
    # schedule_predictions / run_ml_pipeline tasks in Worker/tasks.py:
    # predictions are backfilled, so the ML pipeline no longer runs, and
    # dropping it keeps xgboost out of the worker and Beat. Re-enable all
    # three together.
    #
    # "schedule-predictions-weekly": {
    #     # Tuesday 06:00 UTC -- FPL deadlines are typically Fri/Sat, so this
    #     # gives several days' lead time before the next gameweek locks.
    #     "task": "schedule_predictions",
    #     "schedule": crontab(hour=6, minute=0, day_of_week=2),
    # },
}

if os.getenv("ENVIRONMENT") == "simulation" and os.getenv("SIMULATION_BEAT_LOCK_ONLY") == "1":
    app.conf.beat_schedule = {
        "lock-expired-gameweeks": app.conf.beat_schedule["lock-expired-gameweeks"],
    }
