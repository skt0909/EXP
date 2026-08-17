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
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))

from celery import Celery
from celery.schedules import crontab

app = Celery(
    "fpl_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=["Worker.tasks"],
)

app.conf.beat_schedule = {
    "lock-expired-gameweeks": {
        "task": "lock_expired_gameweeks",
        "schedule": 300.0,  # every 5 minutes
    },
    "refresh-active-gameweeks": {
        "task": "refresh_active_gameweeks",
        "schedule": 900.0,  # every 15 minutes
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
