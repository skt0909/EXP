"""
test_worker.py — regression test for Worker/tasks.py's run_ml_pipeline
error handling: a nonexistent season must surface the clear ValueError
from feature_builder's guard (not the old cryptic KeyError), routed
through Celery's retry mechanism.
"""

import pytest
from celery.exceptions import Retry

from Worker.celery_app import app as celery_app
from Worker.tasks import run_ml_pipeline

NONEXISTENT_SEASON = "SEASON_THAT_TRULY_DOES_NOT_EXIST_XYZ"


def test_run_ml_pipeline_nonexistent_season_raises_clear_error(monkeypatch):
    """
    Scope note: Celery's task_always_eager mode (used here so this test
    needs no real running worker/broker) executes a task exactly once per
    .apply() call -- it does NOT loop through actual retry attempts, since
    real retries depend on broker-driven message redelivery, which eager
    mode doesn't do. Confirmed empirically while writing this test (timed
    at ~0.2s, single attempt), not assumed.

    So this test verifies what eager mode can actually verify: a single
    execution correctly raises Retry wrapping the NEW clear ValueError
    (not the old KeyError). The full "3 retries, 60s apart, ends FAILURE"
    sequence was already verified against a real worker + Redis broker in
    this project's manual testing (task id
    b1310ecb-1a46-46e6-be46-d6ce0677b355 in Worker's log: 4 total
    attempts, 60s apart, final state FAILURE) -- not re-asserted here
    since that would need a real worker process, which this test
    deliberately avoids per the task instructions.
    """
    celery_app.conf.task_always_eager = True
    celery_app.conf.task_eager_propagates = True
    monkeypatch.setattr(run_ml_pipeline, "default_retry_delay", 0)

    with pytest.raises(Retry) as exc_info:
        run_ml_pipeline.apply(args=(NONEXISTENT_SEASON, 4)).get()

    assert isinstance(exc_info.value.exc, ValueError)
    assert "No player data found for season" in str(exc_info.value.exc)

    celery_app.conf.task_always_eager = False
