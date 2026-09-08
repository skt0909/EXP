"""
test_fpl_live_polling.py — tests for the classic-FPL live-polling gap
closed this round:

  Data/fpl_ingest.py:
    ingest_upcoming_fixtures -- the new gate-free incremental path that
    writes future-dated, unfinished fixture rows (ingest_fixtures itself
    still refuses those -- see its own docstring/comments for why).

  Data/live_poll.py:
    find_fixtures_needing_poll_schedule / mark_fixture_polls_scheduled --
    pure functions backing Worker/tasks.py's schedule_fixture_polls Beat
    task, same celery-independent testing philosophy as every other
    Beat-backing function (test_beat_scheduling.py).

  Worker/tasks.py:
    poll_and_score_fpl_fixture / schedule_fixture_polls -- thin task
    wrappers. poll_and_score_fpl_fixture is exercised the same way
    test_worker.py exercises run_ml_pipeline: celery_app.conf.
    task_always_eager = True, then .apply(...).get(), no real broker
    needed. Both fpl_ingest.fetch_json (live API) and the task's own
    poll_fixture_checkpoint/score_gameweek calls are monkeypatched --
    same "mock only external APIs / celery internals, never the DB"
    precedent as test_dream11.py's poll_fixture_checkpoint tests.

Uses TEST_SEASON's dedicated ml.players/ml.fixtures/ml.teams rows
(wiped autouse by conftest.py's _clean_ml_test_data); ml.fixture_poll_
schedule rows cascade-delete with their ml.fixtures row automatically
(ON DELETE CASCADE), so no separate cleanup is needed for that table.
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from sqlalchemy import text

from conftest import TEST_SEASON
import fpl_ingest
import live_poll
from live_poll import find_fixtures_needing_poll_schedule, mark_fixture_polls_scheduled

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 -- matches test_beat_scheduling.py's own convention


@pytest.fixture(autouse=True)
def _cold_fetch_cache():
    """fetch_json_cached memoizes for the life of the PROCESS, which under
    pytest means across every test in this file. A test that monkeypatches
    fetch_json would otherwise be served an earlier test's payload through
    the cached wrapper -- the same staleness the cache split exists to
    prevent, just aimed at the suite instead of at a gameweek.

    Scoped to this file rather than conftest deliberately: it is the only
    one that reaches LiveSource._bootstrap(), and an autouse fixture in
    conftest would import pandas for every pure-unit test in the repo.
    """
    fpl_ingest.fetch_json_cached.cache_clear()
    yield
    fpl_ingest.fetch_json_cached.cache_clear()


def _fake_fetch_json(fixtures_payload):
    """fpl_ingest.fetch_json monkeypatch: bootstrap-static/ is faked just
    enough for LiveSource.check_season()'s year-derivation to land on
    TEST_SEASON ("9999-00": year=9999, str(9999+1)[-2:]="00"); fixtures/
    returns the payload under test. Any other path is a test bug."""
    def _fake(path):
        if path == "bootstrap-static/":
            return {"events": [{"deadline_time": "9999-08-01T00:00:00Z"}]}
        if path == "fixtures/":
            return fixtures_payload
        raise AssertionError(f"unexpected fetch_json path in test: {path!r}")

    return _fake


class _FakeFinishedSource:
    """Minimal duck-typed Source for exercising ingest_fixtures without
    the network -- only what ingest_fixtures actually calls: .season and
    .fixtures(gameweek)."""

    label = "fake"

    def __init__(self, season):
        self.season = season

    def fixtures(self, gameweek):
        return pd.DataFrame([{
            "id": 5200, "event": gameweek, "team_h": 301, "team_a": 302,
            "kickoff_time": "2020-01-01T15:00:00Z",
            "team_h_score": 2, "team_a_score": 1, "finished": True,
        }])


def _fixture_row(engine, fpl_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT gameweek, home_team_id, away_team_id, finished, kickoff_time, "
                "home_score, away_score FROM ml.fixtures WHERE fpl_id = :fid AND season = :s"
            ),
            {"fid": fpl_id, "s": TEST_SEASON},
        ).first()


# ---------------------------------------------------------------- ingest_upcoming_fixtures

def test_ingest_upcoming_fixtures_writes_future_unfinished_fixture(engine, make_team, monkeypatch):
    home_id = make_team(fpl_id=101, name="Home", short_name="HOM")
    away_id = make_team(fpl_id=102, name="Away", short_name="AWY")
    kickoff = (NOW() + timedelta(days=3)).isoformat()

    payload = [{
        "id": 5001, "event": 10, "team_h": 101, "team_a": 102,
        "kickoff_time": kickoff, "team_h_score": None, "team_a_score": None, "finished": False,
    }]
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json(payload))

    fpl_ingest.ingest_upcoming_fixtures(engine, TEST_SEASON)

    row = _fixture_row(engine, 5001)
    assert row is not None
    assert row.gameweek == 10
    assert row.home_team_id == home_id
    assert row.away_team_id == away_id
    assert row.finished is False


def test_ingest_upcoming_fixtures_rerun_does_not_touch_identity_fields(engine, make_team, monkeypatch):
    home_id = make_team(fpl_id=201, name="Home2", short_name="HM2")
    away_id = make_team(fpl_id=202, name="Away2", short_name="AW2")
    make_team(fpl_id=203, name="Other", short_name="OTH")  # a decoy team the re-run payload tries to reassign to

    payload1 = [{
        "id": 5100, "event": 11, "team_h": 201, "team_a": 202,
        "kickoff_time": (NOW() + timedelta(days=2)).isoformat(),
        "team_h_score": None, "team_a_score": None, "finished": False,
    }]
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json(payload1))
    fpl_ingest.ingest_upcoming_fixtures(engine, TEST_SEASON)

    # Re-run: same fpl_id, but the feed now claims a DIFFERENT home team
    # and the match has finished -- identity must not move, mutable
    # fields (finished/scores/kickoff_time) must.
    payload2 = [{
        "id": 5100, "event": 11, "team_h": 203, "team_a": 202,
        "kickoff_time": (NOW() + timedelta(days=1)).isoformat(),
        "team_h_score": 1, "team_a_score": 0, "finished": True,
    }]
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json(payload2))
    fpl_ingest.ingest_upcoming_fixtures(engine, TEST_SEASON)

    row = _fixture_row(engine, 5100)
    assert row.home_team_id == home_id  # unchanged despite payload2 claiming team 203
    assert row.away_team_id == away_id
    assert row.finished is True  # mutable field DID update
    assert row.home_score == 1


def test_ingest_upcoming_fixtures_no_data_is_a_no_op_not_an_error(engine, make_team, monkeypatch):
    make_team(fpl_id=301, name="H", short_name="HHH")
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json([]))

    fpl_ingest.ingest_upcoming_fixtures(engine, TEST_SEASON)  # must not raise


def test_ingest_fixtures_existing_path_still_works(engine, make_team):
    """Regression check: ingest_upcoming_fixtures being added alongside
    ingest_fixtures in the same module didn't break the original,
    all-finished-gated path."""
    make_team(fpl_id=301, name="H3", short_name="H3T")
    make_team(fpl_id=302, name="A3", short_name="A3T")
    source = _FakeFinishedSource(TEST_SEASON)

    fpl_ingest.ingest_fixtures(engine, source, gameweek=12)

    row = _fixture_row(engine, 5200)
    assert row is not None
    assert row.finished is True


def test_ingest_fixtures_still_refuses_unfinished_gameweek(engine, make_team):
    """The gate itself must still be intact -- ingest_upcoming_fixtures is
    an ADDITIONAL path, not a replacement."""
    make_team(fpl_id=301, name="H3b", short_name="H3B")
    make_team(fpl_id=302, name="A3b", short_name="A3B")

    class _FakeUnfinishedSource:
        label = "fake"

        def __init__(self, season):
            self.season = season

        def fixtures(self, gameweek):
            return pd.DataFrame([{
                "id": 5201, "event": gameweek, "team_h": 301, "team_a": 302,
                "kickoff_time": "2020-01-01T15:00:00Z",
                "team_h_score": None, "team_a_score": None, "finished": False,
            }])

    with pytest.raises(SystemExit, match="not finished"):
        fpl_ingest.ingest_fixtures(engine, _FakeUnfinishedSource(TEST_SEASON), gameweek=13)


# ---------------------------------------------------------------- find_fixtures_needing_poll_schedule

def test_finds_new_upcoming_fixture_needing_schedule(engine, make_team, make_fixture):
    home = make_team(fpl_id=401, name="H4", short_name="H4T")
    away = make_team(fpl_id=402, name="A4", short_name="A4T")
    fixture_id = make_fixture(
        fpl_id=6001, gameweek=1, home_team_id=home, away_team_id=away,
        kickoff_time=NOW() + timedelta(days=1), finished=False,
    )

    needing = find_fixtures_needing_poll_schedule(engine)

    assert fixture_id in {r.id for r in needing}


def test_does_not_reschedule_an_already_scheduled_fixture(engine, make_team, make_fixture):
    home = make_team(fpl_id=411, name="H5", short_name="H5T")
    away = make_team(fpl_id=412, name="A5", short_name="A5T")
    fixture_id = make_fixture(
        fpl_id=6002, gameweek=1, home_team_id=home, away_team_id=away,
        kickoff_time=NOW() + timedelta(days=1), finished=False,
    )

    mark_fixture_polls_scheduled(engine, fixture_id)
    needing = find_fixtures_needing_poll_schedule(engine)

    assert fixture_id not in {r.id for r in needing}


def test_ignores_already_finished_fixtures(engine, make_team, make_fixture):
    home = make_team(fpl_id=421, name="H6", short_name="H6T")
    away = make_team(fpl_id=422, name="A6", short_name="A6T")
    fixture_id = make_fixture(
        fpl_id=6003, gameweek=1, home_team_id=home, away_team_id=away,
        kickoff_time=NOW() + timedelta(days=1), finished=True,
    )

    needing = find_fixtures_needing_poll_schedule(engine)

    assert fixture_id not in {r.id for r in needing}


def test_ignores_fixtures_with_no_kickoff_time_yet(engine, make_team, make_fixture):
    home = make_team(fpl_id=431, name="H7", short_name="H7T")
    away = make_team(fpl_id=432, name="A7", short_name="A7T")
    fixture_id = make_fixture(
        fpl_id=6004, gameweek=1, home_team_id=home, away_team_id=away,
        kickoff_time=None, finished=False,
    )

    needing = find_fixtures_needing_poll_schedule(engine)

    assert fixture_id not in {r.id for r in needing}


def test_ignores_fixtures_whose_kickoff_is_already_past(engine, make_team, make_fixture):
    home = make_team(fpl_id=441, name="H8", short_name="H8T")
    away = make_team(fpl_id=442, name="A8", short_name="A8T")
    fixture_id = make_fixture(
        fpl_id=6005, gameweek=1, home_team_id=home, away_team_id=away,
        kickoff_time=NOW() - timedelta(hours=1), finished=False,
    )

    needing = find_fixtures_needing_poll_schedule(engine)

    assert fixture_id not in {r.id for r in needing}


# ---------------------------------------------------------------- Worker.tasks

def test_poll_and_score_fpl_fixture_calls_poll_then_score_in_order(engine, make_team, make_fixture, monkeypatch):
    from Worker.celery_app import app as celery_app
    from Worker import tasks

    home = make_team(fpl_id=451, name="H9", short_name="H9T")
    away = make_team(fpl_id=452, name="A9", short_name="A9T")
    fixture_id = make_fixture(
        fpl_id=6010, gameweek=7, home_team_id=home, away_team_id=away,
        kickoff_time=NOW() + timedelta(hours=1), finished=False,
    )

    call_order = []

    def fake_poll(engine_arg, fid, checkpoint):
        call_order.append(("poll", fid, checkpoint))
        return {"updated": [], "already_settled": [], "unresolved": []}

    def fake_score(engine_arg, season, gameweek):
        call_order.append(("score", season, gameweek))
        return {"scored": [], "failed": []}

    monkeypatch.setattr(tasks, "poll_fixture_checkpoint", fake_poll)
    monkeypatch.setattr(tasks, "score_gameweek", fake_score)

    celery_app.conf.task_always_eager = True
    try:
        result = tasks.poll_and_score_fpl_fixture.apply(args=(fixture_id, "halftime")).get()
    finally:
        celery_app.conf.task_always_eager = False

    assert call_order == [("poll", fixture_id, "halftime"), ("score", TEST_SEASON, 7)]
    assert result == {"poll": {"updated": [], "already_settled": [], "unresolved": []}, "score": {"scored": [], "failed": []}}


def test_schedule_fixture_polls_schedules_both_checkpoints_and_marks_done(engine, make_team, make_fixture, monkeypatch):
    from Worker.celery_app import app as celery_app
    from Worker import tasks

    home = make_team(fpl_id=461, name="H10", short_name="H10")
    away = make_team(fpl_id=462, name="A10", short_name="A10")
    kickoff = NOW() + timedelta(days=1)
    fixture_id = make_fixture(
        fpl_id=6020, gameweek=8, home_team_id=home, away_team_id=away,
        kickoff_time=kickoff, finished=False,
    )

    scheduled_calls = []
    monkeypatch.setattr(
        tasks.poll_and_score_fpl_fixture, "apply_async",
        lambda args, eta: scheduled_calls.append((args, eta)),
    )

    celery_app.conf.task_always_eager = True
    try:
        result = tasks.schedule_fixture_polls.apply(args=()).get()
    finally:
        celery_app.conf.task_always_eager = False

    assert fixture_id in result["scheduled"]
    assert len(scheduled_calls) == 2
    checkpoints = {call_args[1] for call_args, _eta in scheduled_calls}
    assert checkpoints == {"halftime", "fulltime"}
    etas = {call_args[1]: eta for call_args, eta in scheduled_calls}
    assert etas["halftime"] == kickoff + timedelta(minutes=tasks.HALFTIME_OFFSET_MINUTES)
    assert etas["fulltime"] == kickoff + timedelta(minutes=tasks.FULLTIME_OFFSET_MINUTES)

    # Marked scheduled -- a second run must not re-schedule it.
    assert fixture_id not in {r.id for r in find_fixtures_needing_poll_schedule(engine)}


# ------------------------------------------------- fetch_json caching
#
# THESE ARE THE ONLY TESTS IN THE SUITE THAT EXERCISE THE REAL fetch_json,
# and that is the whole point of them.
#
# fetch_json was @lru_cache'd for years while 499 tests stayed green,
# because every other test in the repo monkeypatches fetch_json ITSELF --
# and replacing the function also replaces the cache that was the bug. The
# mock was standing exactly where the defect was.
#
# So these patch the layer UNDERNEATH instead (fpl_ingest.requests) and let
# the real fetch_json run. Any future test of caching behaviour has to do
# the same, or it proves nothing.


class _RecordingGet:
    """Stand-in for requests.get that returns a DIFFERENT payload on each
    call, so "fetched again" and "served from cache" are distinguishable.

    Repeats the final payload once exhausted, so a caller making more
    requests than expected fails on the assertion about .urls rather than
    on an IndexError.
    """

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.urls = []

    def __call__(self, url, **kwargs):
        self.urls.append(url)
        payload = self.payloads[min(len(self.urls) - 1, len(self.payloads) - 1)]
        return SimpleNamespace(raise_for_status=lambda: None, json=lambda: payload)


def _patch_http(monkeypatch, get):
    """Swap the requests module out of fpl_ingest's namespace only, rather
    than mutating the real requests.get for the whole process."""
    monkeypatch.setattr(fpl_ingest, "requests", SimpleNamespace(get=get))


def test_fetch_json_returns_fresh_data_for_a_repeated_path(monkeypatch):
    """THE REGRESSION.

    A gameweek in progress is polled repeatedly on the SAME path --
    event/{gw}/live/ -- by every fixture in it, at both checkpoints. With
    fetch_json cached, the first poll populated the entry and every later
    poll was served that payload without making a request at all.

    Under the old code this test fails twice over: one URL recorded instead
    of two, and `second` carrying the 45-minute payload.
    """
    get = _RecordingGet(
        {"elements": [{"id": 1, "stats": {"minutes": 45, "goals_scored": 0}}]},
        {"elements": [{"id": 1, "stats": {"minutes": 90, "goals_scored": 2}}]},
    )
    _patch_http(monkeypatch, get)

    first = fpl_ingest.fetch_json("event/7/live/")
    second = fpl_ingest.fetch_json("event/7/live/")

    assert len(get.urls) == 2, "the second poll must make a real request, not read a cache"
    assert first["elements"][0]["stats"]["minutes"] == 45
    assert second["elements"][0]["stats"]["minutes"] == 90, (
        "the second poll received the FIRST poll's payload -- fetch_json is cached again"
    )
    assert first is not second, "a shared object would also let one caller mutate another's payload"


def test_fetch_json_cached_still_memoizes_reference_data(monkeypatch):
    """The other half of the split: bootstrap-static/ is read three times
    per CLI run (check_season, teams, players) and must still cost one
    request. If this starts failing, the cached path was removed rather
    than narrowed."""
    get = _RecordingGet({"events": [{"deadline_time": "9999-08-01T00:00:00Z"}]})
    _patch_http(monkeypatch, get)

    a = fpl_ingest.fetch_json_cached("bootstrap-static/")
    b = fpl_ingest.fetch_json_cached("bootstrap-static/")

    assert len(get.urls) == 1, "the cached variant should have made exactly one request"
    assert a is b


def test_two_checkpoints_on_one_gameweek_settle_the_later_payload(
    engine, make_team, make_player, make_fixture, monkeypatch
):
    """End to end, through the path that actually corrupted data.

    Half-time then full-time on one fixture, with the live endpoint
    reporting different stats between the two. The settled row must hold
    the FULL-TIME numbers.

    Under the old code the full-time poll re-read the half-time payload and
    wrote 45 minutes / 0 goals with is_live=FALSE -- and
    enforce_gw_stats_immutability_fn then made that permanent.
    """
    home = make_team(fpl_id=7401, name="PollHome", short_name="PLH")
    away = make_team(fpl_id=7402, name="PollAway", short_name="PLA")
    internal_id = make_player(fpl_id=7410, position="MID", team_id=home)
    fixture_id = make_fixture(
        fpl_id=7400, gameweek=7, home_team_id=home, away_team_id=away, kickoff_time=NOW()
    )

    get = _RecordingGet(
        {"elements": [{"id": 7410, "stats": {"minutes": 45, "goals_scored": 0, "total_points": 1}}]},
        {"elements": [{"id": 7410, "stats": {"minutes": 90, "goals_scored": 2, "total_points": 13}}]},
    )
    _patch_http(monkeypatch, get)

    live_poll.poll_fixture_checkpoint(engine, fixture_id, "halftime")
    live_poll.poll_fixture_checkpoint(engine, fixture_id, "fulltime")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT is_live, minutes, goals_scored, total_points FROM ml.player_gw_stats "
                "WHERE player_id = :p AND season = :s AND gameweek = 7"
            ),
            {"p": internal_id, "s": TEST_SEASON},
        ).first()

    assert len(get.urls) == 2, "full-time must re-fetch, not reuse the half-time payload"
    assert row.is_live is False, "full-time settles the row"
    assert (row.minutes, row.goals_scored, row.total_points) == (90, 2, 13), (
        "the settled row holds half-time's stats -- a stale payload was sealed as final"
    )


# ------------------------------------------- defensive_contribution(s)


def test_live_poll_reads_the_singular_defensive_contribution_field(
    engine, make_team, make_player, make_fixture, monkeypatch
):
    """event/N/live/ calls it `defensive_contribution` -- SINGULAR -- while
    our column, Results/scoring.py and the migration all say
    `defensive_contributions`. live_poll read the plural name straight off
    the payload, got None for every player, and the `or 0` beside it turned
    that into a hard zero. So the DefCon rule never fired on anything this
    poller ingested, silently.

    fpl_ingest.py hit the identical bug on its own path and fixed it with
    SOURCE_STAT_KEYS; this asserts live_poll now applies the same map. The
    payload deliberately carries ONLY the singular key, so reading the
    plural one scores 0 and fails here.
    """
    home = make_team(fpl_id=7501, name="DefconHome", short_name="DCH")
    away = make_team(fpl_id=7502, name="DefconAway", short_name="DCA")
    internal_id = make_player(fpl_id=7510, position="DEF", team_id=home)
    fixture_id = make_fixture(
        fpl_id=7500, gameweek=9, home_team_id=home, away_team_id=away, kickoff_time=NOW()
    )

    get = _RecordingGet(
        {"elements": [{"id": 7510, "stats": {
            "minutes": 90,
            "defensive_contribution": 12,   # the real API's key
            "total_points": 6,
        }}]}
    )
    _patch_http(monkeypatch, get)

    live_poll.poll_fixture_checkpoint(engine, fixture_id, "fulltime")

    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT defensive_contributions, minutes FROM ml.player_gw_stats "
                "WHERE player_id = :p AND season = :s AND gameweek = 9"
            ),
            {"p": internal_id, "s": TEST_SEASON},
        ).first()

    assert row.defensive_contributions == 12, (
        "read as 0 -- live_poll is reading the plural column name off the payload again"
    )
    assert row.minutes == 90, "the unmapped columns must still read straight through"
    # The caching fix must survive this change: one poll, one real request.
    assert len(get.urls) == 1


# ------------------------------------------------- refresh_current_season_fixtures


def test_current_live_season_is_derived_from_the_first_deadline(monkeypatch):
    """A Beat task cannot hardcode a season without needing an edit every
    August, so it derives one from the API -- which only ever serves the
    season in progress."""
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json([]))

    assert fpl_ingest.current_live_season() == TEST_SEASON


def test_current_live_season_is_none_between_seasons(monkeypatch):
    """The API is up but carries no events yet -- not an error, just
    nothing to refresh."""
    monkeypatch.setattr(fpl_ingest, "fetch_json", lambda path: {"events": []})

    assert fpl_ingest.current_live_season() is None


def test_refresh_current_season_fixtures_flips_finished_on_a_played_fixture(
    engine, make_team, monkeypatch
):
    """THE REGRESSION for the stuck-finished bug.

    ml.fixtures is upserted correctly -- `finished = EXCLUDED.finished` is
    in the ON CONFLICT DO UPDATE SET -- but nothing ever ran it a second
    time: ingest_upcoming_fixtures was reachable only from the CLI's
    main(). A row first written while its match was in progress therefore
    kept finished=FALSE permanently, which is exactly the state the dev
    database was found in.

    So this test is about the SECOND call, not the first.
    """
    make_team(fpl_id=401, name="RefreshHome", short_name="RFH")
    make_team(fpl_id=402, name="RefreshAway", short_name="RFA")

    # First run: match under way. Scores are in, FPL has not confirmed it
    # finished yet (it lags until bonus points settle).
    in_progress = [{
        "id": 5300, "event": 3, "team_h": 401, "team_a": 402,
        "kickoff_time": "9999-08-01T15:00:00Z",
        "team_h_score": 2, "team_a_score": 1, "finished": False,
    }]
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json(in_progress))

    assert fpl_ingest.refresh_current_season_fixtures(engine) == {
        "season": TEST_SEASON, "refreshed": True,
    }
    assert _fixture_row(engine, 5300).finished is False

    # Second run: FPL has now flagged it finished. Under the old code this
    # run simply never happened.
    monkeypatch.setattr(
        fpl_ingest, "fetch_json", _fake_fetch_json([dict(in_progress[0], finished=True)])
    )
    fpl_ingest.refresh_current_season_fixtures(engine)

    row = _fixture_row(engine, 5300)
    assert row.finished is True, "finished never flipped -- the refresh is not updating existing rows"
    assert (row.home_score, row.away_score) == (2, 1)


def test_refresh_current_season_fixtures_is_a_no_op_between_seasons(engine, monkeypatch):
    monkeypatch.setattr(fpl_ingest, "fetch_json", lambda path: {"events": []})

    result = fpl_ingest.refresh_current_season_fixtures(engine)

    assert result["refreshed"] is False
    assert result["season"] is None


def test_refresh_current_season_fixtures_converts_systemexit_to_a_normal_error(
    engine, monkeypatch
):
    """fpl_ingest is a script and raises SystemExit for operator errors --
    here, no teams ingested yet. SystemExit derives from BaseException, so
    a Celery task's `except Exception` would not catch it and the worker
    would take the exit instead of logging a failed task."""
    payload = [{
        "id": 5310, "event": 3, "team_h": 401, "team_a": 402,
        "kickoff_time": "9999-08-01T15:00:00Z",
        "team_h_score": None, "team_a_score": None, "finished": False,
    }]
    monkeypatch.setattr(fpl_ingest, "fetch_json", _fake_fetch_json(payload))
    # No make_team calls -- ml.teams is empty for TEST_SEASON, which is
    # what ingest_upcoming_fixtures raises SystemExit on.

    with pytest.raises(RuntimeError, match="fixture refresh aborted"):
        fpl_ingest.refresh_current_season_fixtures(engine)
