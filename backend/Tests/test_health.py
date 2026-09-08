"""
test_health.py — two independent pre-deployment fixes, tested separately:

  1. CORS is no longer allow_origins=["*"] -- Context_assembler/main.py's
     _allowed_origins() reads ALLOWED_ORIGINS from the environment (same
     walk-up-and-raise convention as Data/auth.py's _jwt_secret() and
     Shared/db_utils.py's _database_url()), and the middleware only
     answers requests from origins on that list.

  2. GET /health and GET /health/scheduled-tasks make two previously
     invisible facts queryable: can this process reach the database, and
     when did each Beat-scheduled task last succeed or fail. Backed by
     Worker/task_health.py's public.task_heartbeats table (migration
     d4a8f209c1e6).

These share a file because they touch the same module (main.py) and
were shipped in the same session, not because they depend on each other
-- confirmed independent: different functions, different tables, and
each set of tests below passes with the other fix reverted.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

import main
from main import _allowed_origins, app
from Worker.task_health import get_all_task_heartbeats, record_task_heartbeat

client = TestClient(app)

VITE_DEV_ORIGIN = "http://localhost:5173"


# ------------------------------------------------------------------ CORS


def test_allowed_origins_parses_a_comma_separated_list(monkeypatch):
    """The one behaviour worth unit-testing in isolation: multiple origins
    in one ALLOWED_ORIGINS value split and trim correctly. Does NOT test
    the raise-when-unset branch -- same precedent as _jwt_secret() and
    _database_url() elsewhere in this suite, neither of which tests that
    branch either, because it would mean fighting the real .env this
    process already loaded rather than testing the function."""
    monkeypatch.setenv("ALLOWED_ORIGINS", " http://a.example.com ,http://b.example.com")
    _allowed_origins.cache_clear()
    try:
        assert _allowed_origins() == ["http://a.example.com", "http://b.example.com"]
    finally:
        _allowed_origins.cache_clear()  # don't leak this value into later tests


def test_cors_allows_the_configured_vite_origin():
    """http://localhost:5173 is Vite's default dev-server origin (confirmed
    by actually starting `npm run dev`), and is what ALLOWED_ORIGINS is set
    to for local dev. The middleware must echo it back -- that echo is what
    tells a real browser the response may be read."""
    resp = client.get("/health", headers={"Origin": VITE_DEV_ORIGIN})

    assert resp.status_code == 200
    assert resp.headers.get("access-control-allow-origin") == VITE_DEV_ORIGIN


def test_cors_rejects_an_arbitrary_origin():
    """THE REGRESSION this fix closes. Under the old allow_origins=["*"],
    this exact request would have come back with
    access-control-allow-origin: *, and a real browser would have handed
    the JSON response to a script running on evil.example.com. The server
    still answers the request (CORS is a browser-side control, not a
    request-level firewall), but without the header a browser refuses to
    expose the response body to that origin's JavaScript."""
    resp = client.get("/health", headers={"Origin": "http://evil.example.com"})

    assert resp.status_code == 200  # the server itself is not the enforcement point
    assert "access-control-allow-origin" not in resp.headers


def test_cors_preflight_from_an_arbitrary_origin_is_refused():
    """The preflight OPTIONS request a real cross-origin fetch() sends
    first. Starlette's CORSMiddleware answers a disallowed origin's
    preflight with 400 and no allow-origin header, which is what stops the
    browser from ever sending the real request."""
    resp = client.options(
        "/health",
        headers={"Origin": "http://evil.example.com", "Access-Control-Request-Method": "GET"},
    )

    assert resp.status_code == 400
    assert "access-control-allow-origin" not in resp.headers


# ------------------------------------------------------------------ GET /health


def test_health_reports_database_reachable():
    resp = client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "database": "reachable"}


def test_health_returns_503_when_database_unreachable(monkeypatch):
    """Same FakeEngine pattern test_context_assembler.py uses for /chat's
    DB-failure fallback -- confirms this endpoint answers instead of
    hanging or 500ing when the one thing it checks is actually down."""
    class FakeEngine:
        def connect(self):
            raise OperationalError("mocked connect failure", None, Exception("mocked"))

    monkeypatch.setattr(main, "get_engine", lambda: FakeEngine())

    resp = client.get("/health")

    assert resp.status_code == 503
    assert "unreachable" in resp.json()["detail"]


# ------------------------------------------------------------------ Worker/task_health.py


@pytest.fixture
def clean_heartbeats(engine):
    """task_heartbeats isn't part of conftest's autouse ml-schema wipe (it's
    public, not ml, and it isn't gameplay data), so this suite owns its own
    cleanup -- before AND after, same defensive-both-ends stance
    conftest's _clean_ml_test_data takes, so a prior failed run can't leave
    a row that makes an unrelated test's "never ran" assertion false."""
    names = (
        "_test_task_a", "_test_task_b",
        "lock_expired_gameweeks", "lock_dream11_contests", "refresh_active_gameweeks",
        "revert_free_hits", "finalize_dream11_contests", "refresh_fixtures",
        "schedule_fixture_polls", "schedule_predictions",
    )

    def _wipe():
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM public.task_heartbeats WHERE task_name = ANY(:names)"),
                {"names": list(names)},
            )

    _wipe()
    yield
    _wipe()


def test_record_task_heartbeat_success_and_failure_are_independent(engine, clean_heartbeats):
    """THE DESIGN THIS TABLE EXISTS FOR: a success does not erase a prior
    failure's record, and a failure does not erase a prior success's
    timestamp. Losing either direction would collapse "ran fine 30s ago,
    failed once 2 minutes ago" and "hasn't succeeded in days" into the same
    row -- exactly the ambiguity a single last_run_at column would have."""
    record_task_heartbeat(engine, "_test_task_a", success=True)
    record_task_heartbeat(engine, "_test_task_a", success=False, error="boom")

    row = next(r for r in get_all_task_heartbeats(engine) if r.task_name == "_test_task_a")
    assert row.last_success_at is not None, "the failure call erased the earlier success"
    assert row.last_failure_at is not None
    assert row.last_error == "boom"

    record_task_heartbeat(engine, "_test_task_a", success=True)
    row = next(r for r in get_all_task_heartbeats(engine) if r.task_name == "_test_task_a")
    assert row.last_failure_at is not None, "the later success erased the earlier failure"
    assert row.last_error == "boom", "last_error must not be cleared by an unrelated success"


def test_record_task_heartbeat_upserts_not_duplicates(engine, clean_heartbeats):
    record_task_heartbeat(engine, "_test_task_b", success=True)
    record_task_heartbeat(engine, "_test_task_b", success=True)

    with engine.connect() as conn:
        n = conn.execute(
            text("SELECT count(*) FROM public.task_heartbeats WHERE task_name = '_test_task_b'")
        ).scalar()
    assert n == 1


def test_record_task_heartbeat_truncates_a_very_long_error(engine, clean_heartbeats):
    """last_error is a health-check field, not a log -- the full traceback
    already goes to logger.exception beside every call site in
    Worker/tasks.py. This just confirms the truncation the module promises
    actually happens, so one runaway exception message can't inflate the
    row indefinitely."""
    record_task_heartbeat(engine, "_test_task_a", success=False, error="x" * 5000)

    row = next(r for r in get_all_task_heartbeats(engine) if r.task_name == "_test_task_a")
    assert len(row.last_error) <= 2000


# ------------------------------------------------------------------ GET /health/scheduled-tasks


def test_scheduled_tasks_lists_every_beat_task_even_with_no_heartbeat_yet(clean_heartbeats):
    """Every task in Worker/celery_app.py's beat_schedule appears, even one
    that has never once recorded a heartbeat -- a freshly deployed
    environment must show "never run" for all eight, not silently omit
    them until their first tick."""
    resp = client.get("/health/scheduled-tasks")
    assert resp.status_code == 200

    names = {t["task_name"] for t in resp.json()["tasks"]}
    assert names == {
        "lock_expired_gameweeks", "lock_dream11_contests", "refresh_active_gameweeks",
        "revert_free_hits", "finalize_dream11_contests", "refresh_fixtures",
        "schedule_fixture_polls", "schedule_predictions",
    }

    by_name = {t["task_name"]: t for t in resp.json()["tasks"]}
    never_run = by_name["lock_expired_gameweeks"]
    assert never_run["last_success_at"] is None
    assert never_run["stale"] is None, "no baseline yet -- 'stale' must not guess"
    assert never_run["expected_interval_seconds"] == 300.0

    cron_task = by_name["schedule_predictions"]
    assert cron_task["expected_interval_seconds"] is None, "a weekly crontab has no single interval"
    assert cron_task["stale"] is None


def test_scheduled_tasks_reports_a_fresh_success_as_not_stale(engine, clean_heartbeats):
    record_task_heartbeat(engine, "lock_dream11_contests", success=True)

    resp = client.get("/health/scheduled-tasks")
    row = next(t for t in resp.json()["tasks"] if t["task_name"] == "lock_dream11_contests")

    assert row["last_success_at"] is not None
    assert row["seconds_since_success"] < 5
    assert row["stale"] is False


def test_scheduled_tasks_flags_a_genuinely_stale_task(engine, clean_heartbeats):
    """THE REGRESSION this endpoint exists to catch: a task whose interval
    is 5 minutes but whose last success was an hour ago -- exactly what
    Beat silently dying looks like from the outside."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public.task_heartbeats (task_name, last_success_at) "
                "VALUES ('lock_expired_gameweeks', :ts)"
            ),
            {"ts": datetime.now(timezone.utc) - timedelta(hours=1)},
        )

    resp = client.get("/health/scheduled-tasks")
    row = next(t for t in resp.json()["tasks"] if t["task_name"] == "lock_expired_gameweeks")

    assert row["expected_interval_seconds"] == 300.0
    assert row["seconds_since_success"] > 3000
    assert row["stale"] is True


def test_scheduled_tasks_a_task_just_inside_its_grace_window_is_not_yet_stale(engine, clean_heartbeats):
    """STALE_INTERVAL_MULTIPLIER is 2.0 -- a task running a little late
    (worker busy, a slow prior task) gets one full extra cycle before this
    calls it stale, so a single missed-by-a-bit tick doesn't read as an
    incident. 900s interval (refresh_fixtures): 1200s since success is
    under the 1800s threshold."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO public.task_heartbeats (task_name, last_success_at) "
                "VALUES ('refresh_fixtures', :ts)"
            ),
            {"ts": datetime.now(timezone.utc) - timedelta(seconds=1200)},
        )

    resp = client.get("/health/scheduled-tasks")
    row = next(t for t in resp.json()["tasks"] if t["task_name"] == "refresh_fixtures")

    assert row["stale"] is False


def test_scheduled_tasks_a_failure_with_no_prior_success_has_no_stale_verdict(engine, clean_heartbeats):
    """A task that has failed every time it ran has never succeeded, so
    there is no success timestamp to measure staleness from -- reported as
    'never succeeded, last failure at ...', not coerced into stale=True or
    stale=False, either of which would overstate what is actually known."""
    record_task_heartbeat(engine, "finalize_dream11_contests", success=False, error="ValueError: boom")

    resp = client.get("/health/scheduled-tasks")
    row = next(t for t in resp.json()["tasks"] if t["task_name"] == "finalize_dream11_contests")

    assert row["last_success_at"] is None
    assert row["last_failure_at"] is not None
    assert row["last_error"] == "ValueError: boom"
    assert row["stale"] is None


# ------------------------------------------------------------------ actually wired into the real tasks


def test_a_real_beat_task_records_its_own_heartbeat(engine, clean_heartbeats):
    """Not a mock of the wiring -- the actual lock_expired_gameweeks task
    function, run the same way test_worker.py runs Celery tasks
    (task_always_eager, no broker needed), confirming the call sites added
    to Worker/tasks.py actually fire rather than merely existing in the
    diff."""
    from Worker.celery_app import app as celery_app
    from Worker.tasks import lock_expired_gameweeks

    celery_app.conf.task_always_eager = True
    try:
        lock_expired_gameweeks.apply().get()
    finally:
        celery_app.conf.task_always_eager = False

    row = next(r for r in get_all_task_heartbeats(engine) if r.task_name == "lock_expired_gameweeks")
    assert row.last_success_at is not None
