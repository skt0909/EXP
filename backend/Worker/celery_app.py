"""
celery_app.py — Celery application instance (Redis running locally via
Docker) plus the Beat schedule for the three automation tasks in
Worker/tasks.py.

Run a worker with:
    celery -A Worker.celery_app worker --loglevel=info --pool=solo
(--pool=solo is required on Windows -- the default prefork pool needs
os.fork, which Windows doesn't have.)

Beat is a SEPARATE process from the worker -- it only schedules tasks
onto the broker on the configured intervals, it doesn't execute them
itself. Run it alongside the worker above, in its own terminal:
    celery -A Worker.celery_app beat --loglevel=info
Both processes must be running for the schedule below to actually fire
(worker with no beat = tasks never get triggered on a schedule, only
via manual .delay()/.apply_async() calls; beat with no worker = tasks
get scheduled but never picked up and run). This is a manual
dev-environment step for now -- not automated/supervised.

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

# .apply_async() is called from inside code that has ALREADY committed to
# the database and treats scheduling as best-effort: Game_logic/dream11.py's
# create_contest (inside an HTTP request), Worker/tasks.py's
# schedule_fixture_polls. Celery's DEFAULT result
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

app.conf.beat_schedule = {
    "lock-expired-gameweeks": {
        "task": "lock_expired_gameweeks",
        "schedule": _beat_interval(300.0),  # every 5 minutes
    },
    "lock-dream11-contests": {
        # Same 5-minute cadence as lock-expired-gameweeks: this is the only
        # thing that ever locks a Dream11 contest, so the interval bounds how
        # long a contest stays joinable/submittable after its fixture starts.
        "task": "lock_dream11_contests",
        "schedule": _beat_interval(300.0),
    },
    "revert-free-hits": {
        # Every 15 minutes: this only fires once a gameweek's last fixture
        # is comfortably over (see free_hit_revert.FREE_HIT_REVERT_BUFFER_HOURS),
        # so the cadence just bounds how long a user keeps seeing their
        # free-hit squad after it should have reverted -- there is no
        # deadline being raced here, unlike the two lock tasks above.
        "task": "revert_free_hits",
        "schedule": _beat_interval(900.0),
    },
    "refresh-active-gameweeks": {
        "task": "refresh_active_gameweeks",
        "schedule": _beat_interval(900.0),  # every 15 minutes
    },
    "finalize-dream11-contests": {
        # Every 15 minutes. Deliberately the same cadence as
        # refresh-fixtures below, and necessarily NOT faster: this task
        # gates on ml.fixtures.finished, which is exactly what that task
        # writes, so finalization can only ever be as current as the
        # fixture refresh feeding it.
        "task": "finalize_dream11_contests",
        "schedule": _beat_interval(900.0),
    },
    "refresh-fixtures": {
        # Every 15 minutes, matching schedule-fixture-polls below, which
        # reads the rows this writes and is therefore only ever as fresh
        # as this is. Without this entry ml.fixtures only ever changed
        # when someone ran fpl_ingest.py by hand, so played matches sat at
        # finished=FALSE indefinitely and later gameweeks never got their
        # scores at all.
        "task": "refresh_fixtures",
        "schedule": _beat_interval(900.0),
    },
    "schedule-fixture-polls": {
        "task": "schedule_fixture_polls",
        "schedule": _beat_interval(900.0),  # every 15 minutes -- catches newly-ingested upcoming fixtures without excessive overhead
    },
    "schedule-predictions-weekly": {
        # Tuesday 06:00 UTC -- FPL deadlines are typically Fri/Sat, so this
        # gives several days' lead time before the next gameweek locks.
        # Flagged per the task brief: change day_of_week/hour here if a
        # different lead time is wanted.
        "task": "schedule_predictions",
        "schedule": crontab(hour=6, minute=0, day_of_week=2),
    },
}

if os.getenv("ENVIRONMENT") == "simulation" and os.getenv("SIMULATION_BEAT_LOCK_ONLY") == "1":
    app.conf.beat_schedule = {
        "lock-expired-gameweeks": app.conf.beat_schedule["lock-expired-gameweeks"],
    }
