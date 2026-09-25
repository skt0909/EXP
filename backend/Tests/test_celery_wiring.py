"""
test_celery_wiring.py — Track B: verifies the actual Celery/Redis task-queue
plumbing, as opposed to test_full_season_scenario.py (Track A) and every
other file in this suite, which deliberately bypass Celery entirely by
calling the plain wrapped functions directly (see e.g. GameEngine/
gameweek_lock.py's own module docstring: "no broker, no eager mode, and no
Celery import is needed").

test_worker.py's own docstring says the quiet part out loud: its retry test
uses task_always_eager=True specifically because a real worker+broker round
trip "would need a real worker process, which this test deliberately
avoids," and notes the full retry sequence was previously only verified by
hand against a live worker (one specific task id, logged once, never
re-checked automatically). THIS file is what closes that gap permanently.

WHAT THIS FILE DOES NOT USE, DELIBERATELY: no make_team/make_player/
make_fixture/make_user/TestClient/bearer_headers -- no domain modeling, no
HTTP layer. The one thing borrowed from conftest.py is the plain `engine`
fixture (generic DB access, not a domain builder) and the TEST_SEASON
constant, used only to seed the single minimal row test_b needs to observe
a real side effect. Every other row this file writes uses raw SQL, not
Track A's squad/league/dream11 builders.

TASK-NAME MAP (see the earlier investigation for why these differ from the
pure functions they wrap -- intentional, not a bug, avoiding a Python name
collision between the @app.task wrapper and the imported function):

    pure function (GameEngine/Game_logic)      Celery task name
    --------------------------------------     --------------------------
    lock_expired_gameweeks                     lock_expired_gameweeks
    refresh_active_gameweeks                   refresh_active_gameweeks
    lock_started_contests                      lock_dream11_contests
    finalize_dream11_contest (singular)        no dedicated task -- only
                                                reachable via the plural
                                                Beat sweep, finalize_dream11
                                                _contests, or inline inside
                                                the one-off poll_and_score_
                                                dream11 task.

REDIS. This is the one file in the suite that needs a REAL, reachable Redis
-- see Tests/README.md for the local `docker run -p 6379:6379 redis:7`
prerequisite (CI already provides one as a service container). The whole
module is skipped, with a visible reason (not a silent pass), if
TEST_REDIS_URL/REDIS_URL isn't set or isn't actually reachable.

NOT EAGER. task_always_eager=False is asserted, not just assumed --
eager mode would silently reduce every test below back to Track A's
in-process function-call approach and defeat the entire point of this file.
Uses celery.contrib.testing.worker.start_worker directly against the REAL
Worker.celery_app.app singleton (the exact object Worker/tasks.py's tasks
are registered on) rather than pytest-celery's fixtures: pytest-celery
>=1.0's celery_worker/celery_app fixtures are Docker-container-based (they
pulled in the `docker` and `pytest-docker-tools` packages as transitive
dependencies when trial-installed for this file), which is a materially
heavier ask than "a locally reachable Redis" and auto-registers a pytest11
entry point that would affect the whole suite just by being installed.
celery.contrib.testing.worker ships with celery itself -- no new dependency.
"""

import os
import time
import uuid
from unittest.mock import patch

import pytest
from celery.contrib.testing.worker import start_worker
from celery.signals import before_task_publish
from sqlalchemy import text

from conftest import TEST_SEASON
from Worker.celery_app import app as celery_app
import Worker.tasks as tasks_module
from Worker.tasks import (
    finalize_dream11_contests,
    lock_dream11_contests,
    lock_expired_gameweeks,
    refresh_active_gameweeks,
)

# --- module-wide skip guard --------------------------------------------
#
# A visible SKIPPED result (with a reason shown under -v/-rs), not a
# silent pass: pytest.mark.skipif applied via `pytestmark` marks every
# test in this module, and pytest reports each one individually as
# skipped with the reason string below.

TEST_REDIS_URL = os.getenv("TEST_REDIS_URL") or os.getenv("REDIS_URL")


def _redis_reachable(url: str | None) -> bool:
    if not url:
        return False
    try:
        import redis
        redis.Redis.from_url(url, socket_connect_timeout=1).ping()
        return True
    except Exception:
        return False


_REDIS_UP = _redis_reachable(TEST_REDIS_URL)

pytestmark = pytest.mark.skipif(
    not _REDIS_UP,
    reason=(
        f"Redis unreachable at {TEST_REDIS_URL!r} -- this file needs a real "
        "Redis to exercise actual broker/worker plumbing. Start one locally "
        "with `docker run -p 6379:6379 redis:7` (see Tests/README.md) or run "
        "in CI, where a redis:7 service container is already provided."
    ),
)


# --- fixtures ------------------------------------------------------------


@pytest.fixture(autouse=True)
def _not_eager():
    """Asserted, not assumed. task_always_eager=True would make every task
    below execute synchronously in-process with no broker round trip at
    all -- silently collapsing this file back into Track A's approach and
    defeating its entire purpose. Explicitly forced False (not just
    asserted) because test_worker.py's own eager-mode test only resets it
    to False on its happy path -- if that test ever fails partway through,
    the flag could be left True for whatever runs after it in the same
    session, and this file must not silently inherit that.
    """
    celery_app.conf.task_always_eager = False
    yield
    assert celery_app.conf.task_always_eager is False


@pytest.fixture
def real_worker():
    """A real in-process worker THREAD, consuming from the REAL broker
    (TEST_REDIS_URL), bound to the actual Worker.celery_app.app singleton --
    the same object Worker/tasks.py's @app.task-decorated functions are
    already registered on, so no task re-registration is needed the way a
    fresh pytest-celery/celery.contrib.pytest `celery_app` fixture would
    require. perform_ping_check is left off: this file's own tests already
    substantively verify a real round trip, so the extra built-in ping
    task adds an untested app-registration edge case without adding real
    coverage.
    """
    with start_worker(celery_app, pool="solo", perform_ping_check=False, shutdown_timeout=10.0) as w:
        yield w


# --- (a) + (b): a task actually gets enqueued, then a real worker executes it


def test_lock_expired_gameweeks_enqueues_and_executes_for_real(engine, real_worker):
    """(a) The .apply_async() call really publishes to the broker with the
    correct task name/args -- observed via celery's own before_task_publish
    signal, which fires synchronously as part of the actual publish path
    (not a mock of it).

    (b) A real worker then picks the message up off the real broker and
    executes it; the result is polled via .get(timeout=...) rather than
    assumed instant, and the genuine DB side effect (gw_selections.is_locked
    flipping) is checked afterward, not inferred from the task's return
    value alone.
    """
    published = []

    def _capture(sender=None, headers=None, body=None, **kwargs):
        published.append((sender, headers))

    before_task_publish.connect(_capture, weak=False)

    # Minimal raw-SQL setup, no domain builder fixtures: one fixture whose
    # deadline has already passed, one gw_selections row still unlocked.
    with engine.begin() as conn:
        team_id = conn.execute(
            text("INSERT INTO ml.teams (fpl_id, season, name, short_name) VALUES (81234, :s, 'CeleryWiringFC', 'CWF') RETURNING id"),
            {"s": TEST_SEASON},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, away_team_id, kickoff_time, finished) "
                "VALUES (81234, :s, 77, :t, :t, now() - interval '3 hours', FALSE)"
            ),
            {"s": TEST_SEASON, "t": team_id},
        )
        user_id = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, 'not_a_real_hash') RETURNING id"),
            {"e": f"celery_wiring_{uuid.uuid4().hex[:12]}@example.com", "u": f"celery_wiring_{uuid.uuid4().hex[:12]}"},
        ).scalar()
        conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, tactic, is_locked) "
                "VALUES (:u, :s, 77, 'balanced', FALSE)"
            ),
            {"u": user_id, "s": TEST_SEASON},
        )

    try:
        async_result = lock_expired_gameweeks.apply_async()

        # (a) -- the publish already happened by the time apply_async() returns.
        assert len(published) == 1
        sender, headers = published[0]
        assert headers["task"] == "lock_expired_gameweeks"
        assert headers["argsrepr"] == "()"

        # (b) -- polled, not assumed instant. NOTE: [TEST_SEASON, 77], a
        # list, not the (TEST_SEASON, 77) tuple the pure function actually
        # returns in-process -- a real round trip through Celery's JSON
        # result backend turns tuples into lists, something Track A's
        # direct in-process calls never expose since nothing there ever
        # serializes. This is exactly the kind of divergence this file
        # exists to catch.
        result = async_result.get(timeout=15)
        assert [TEST_SEASON, 77] in result["locked"]

        with engine.connect() as conn:
            is_locked = conn.execute(
                text("SELECT is_locked FROM gw_selections WHERE user_id = :u"), {"u": user_id}
            ).scalar()
        assert is_locked is True
    finally:
        before_task_publish.disconnect(_capture)
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM gw_selections WHERE user_id = :u"), {"u": user_id})
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


# --- (c) Beat schedule registers the correct task names and intervals ---


def test_beat_schedule_registers_the_expected_tasks_and_intervals():
    """No worker or broker needed for this one -- app.conf.beat_schedule is
    plain Python data, read directly. Confirms the specific mapping
    documented in this file's own module docstring stays true: if a future
    edit renames a task or changes its schedule entry without updating the
    other, this is what catches it."""
    schedule = celery_app.conf.beat_schedule

    assert schedule["lock-expired-gameweeks"]["task"] == "lock_expired_gameweeks"
    assert schedule["lock-expired-gameweeks"]["schedule"] == 300.0

    assert schedule["lock-dream11-contests"]["task"] == "lock_dream11_contests"
    assert schedule["lock-dream11-contests"]["schedule"] == 300.0
    assert schedule["poll-due-fixtures"]["task"] == "poll_due_fixtures"
    assert schedule["poll-due-fixtures"]["schedule"] == 60.0

    assert schedule["refresh-active-gameweeks"]["task"] == "refresh_active_gameweeks"
    assert schedule["refresh-active-gameweeks"]["schedule"] == 900.0

    assert schedule["finalize-dream11-contests"]["task"] == "finalize_dream11_contests"
    assert schedule["finalize-dream11-contests"]["schedule"] == 900.0

    # Every task name referenced above must actually be a registered task --
    # a schedule entry pointing at a name nothing implements would only ever
    # surface as a silent no-op in production (Beat happily schedules
    # messages nothing consumes).
    for entry in schedule.values():
        assert entry["task"] in celery_app.tasks, f"beat_schedule references unregistered task {entry['task']!r}"


# --- (d) retry behavior when a task raises ------------------------------


def test_task_retries_through_a_real_worker_after_raising(real_worker, monkeypatch):
    """Forces the underlying pure function to raise twice, then succeed.
    default_retry_delay is monkeypatched to 0 (same technique
    test_worker.py's eager-mode test already uses) purely so this doesn't
    take 60s-per-attempt real time -- the retry mechanism being exercised
    is the real one: self.retry(exc=exc) inside a non-eager task raises
    Retry, the real worker catches it and republishes the message back onto
    the real broker with the countdown, and this same worker thread picks
    it up again. Eager mode cannot do this at all (test_worker.py's own
    docstring: eager "does NOT loop through actual retry attempts, since
    real retries depend on broker-driven message redelivery").
    """
    monkeypatch.setattr(lock_expired_gameweeks, "default_retry_delay", 0)

    calls = []

    def _flaky(engine):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError(f"deliberate failure #{len(calls)}")
        return {"locked": [], "skipped_no_deadline": []}

    monkeypatch.setattr(tasks_module, "_lock_expired_gameweeks", _flaky)

    async_result = lock_expired_gameweeks.apply_async()
    result = async_result.get(timeout=15)

    assert result == {"locked": [], "skipped_no_deadline": []}
    # Proves real redelivery happened -- not a single synchronous attempt.
    assert len(calls) == 3


# --- (e) serialization sanity for a non-trivial argument type ------------

# Registered on the REAL app at import time (before any worker fixture
# starts), so it's already in the task registry a1 worker picks up when it
# starts. A throwaway task local to this test file, not added to
# Worker/tasks.py -- this is queue-infrastructure plumbing, not a task this
# project's product code needs.
@celery_app.task(name="test_celery_wiring.echo")
def _echo(value):
    return value


def test_uuid_argument_survives_a_real_enqueue_execute_round_trip(real_worker):
    """A raw uuid.UUID instance, not a pre-stringified one -- the point is
    to prove Celery's actual configured serializer (kombu's JSON encoder,
    confirmed separately to support uuid.UUID via a {"__type__": "uuid",
    ...} envelope) survives a REAL publish -> broker -> worker -> result
    round trip with the type intact, not just the value."""
    original = uuid.uuid4()

    async_result = _echo.apply_async(args=[original])
    returned = async_result.get(timeout=15)

    assert isinstance(returned, uuid.UUID)
    assert returned == original

