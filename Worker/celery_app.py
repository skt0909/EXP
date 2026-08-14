"""
celery_app.py — Celery application instance for manual-trigger testing of
the ML prediction pipeline (Redis running locally via Docker, no Celery
Beat schedule yet -- that's a later step once run_ml_pipeline itself is
verified correct).

Run a worker with:
    celery -A Worker.celery_app worker --loglevel=info --pool=solo
(--pool=solo is required on Windows -- the default prefork pool needs
os.fork, which Windows doesn't have.)
"""

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))

from celery import Celery

app = Celery(
    "fpl_worker",
    broker="redis://localhost:6379/0",
    backend="redis://localhost:6379/0",
    include=["Worker.tasks"],
)
