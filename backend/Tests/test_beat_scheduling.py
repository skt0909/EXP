"""
test_beat_scheduling.py — tests for Game_logic/scheduling.py (the pure
functions backing the three Celery Beat tasks) and Worker/celery_app.py's
beat_schedule config.

Deliberately imports only from Game_logic.scheduling/scoring/standings
and Worker.celery_app, never Worker.tasks -- same reasoning as every
other Game_logic pure-function test file (test_scoring.py,
test_leagues.py): keeps this file runnable independent of whether the
Celery task wrappers themselves are exercised. Worker.celery_app IS
imported for the beat_schedule test specifically, since that config
only exists there; celery is installed in this environment so this is
safe to do directly (confirmed before writing this file).
"""

import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app as fastapi_app
from scheduling import (
    resolve_gameweek_deadline,
    lock_expired_gameweeks,
    find_active_gameweeks,
    refresh_active_gameweeks,
    find_next_gameweek_needing_predictions,
)

client = TestClient(fastapi_app)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 -- small enough to keep as a lambda, used throughout


@pytest.fixture
def make_user(engine):
    """Tracks every user created this test and cleans up their
    gw_selections (cascades starting_xi) + gw_scores at teardown --
    without this, an orphaned bare gw_selections row (no starting_xi,
    which the lock tests create on purpose) for TEST_SEASON/gameweek 1
    gets picked up by score_gameweek in a LATER test file (test_scoring.py
    uses the same season/gameweek), since gw_selections isn't part of
    conftest's autouse ml-schema wipe. users/mini_leagues/league_members/
    leaderboard_snapshots are left alone -- same permanent-by-design
    reasoning as test_leagues.py/test_transfers.py (leaderboard_snapshots'
    immutability trigger blocks deleting through it anyway)."""
    created = []

    def _make():
        unique = uuid.uuid4().hex[:12]
        with engine.begin() as conn:
            uid = conn.execute(
                text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
                {"e": f"pytest_beat_{unique}@example.com", "u": f"pytest_beat_{unique}", "p": "not_a_real_hash"},
            ).scalar()
        created.append(uid)
        return uid

    yield _make

    with engine.begin() as conn:
        for uid in created:
            conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": uid})


def _seed_gw_selection_row(engine, user_id, season, gameweek, is_locked=False, captain_id=1, vice_captain_id=2):
    """Minimal gw_selections row -- no starting_xi -- for lock tests,
    which never read starting_xi at all."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id, is_locked) "
                "VALUES (:u, :s, :gw, :cap, :vc, :locked)"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "cap": captain_id, "vc": vice_captain_id, "locked": is_locked},
        )


def _is_locked(engine, season, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT is_locked FROM gw_selections WHERE season = :s AND gameweek = :gw"),
            {"s": season, "gw": gameweek},
        ).scalar()


def _seed_player(make_team, make_player, make_gw_stat, fpl_id, position, gameweek, minutes, total_points):
    team_id = make_team(fpl_id=fpl_id + 50000, name=f"T{fpl_id}", short_name=f"T{fpl_id}")
    internal_id = make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=0)
    make_gw_stat(player_id=internal_id, gameweek=gameweek, minutes=minutes, total_points=total_points)
    return fpl_id


# Same 11-player shape used throughout test_scoring.py -- all played, all
# worth 3 points, no autosub complexity needed for these tests.
SIMPLE_XI_POSITIONS = ["GK"] + ["DEF"] * 4 + ["MID"] * 4 + ["FWD"] * 2
SIMPLE_BENCH_POSITIONS = ["GK", "DEF", "MID", "FWD"]


def _build_full_squad(make_team, make_player, make_gw_stat, gameweek, id_offset):
    fpl_ids = []
    for i, pos in enumerate(SIMPLE_XI_POSITIONS + SIMPLE_BENCH_POSITIONS):
        fid = id_offset + i
        _seed_player(make_team, make_player, make_gw_stat, fid, pos, gameweek, minutes=90, total_points=3)
        fpl_ids.append(fid)
    return fpl_ids[:11], fpl_ids[11:]


def _seed_full_gw_selection(engine, user_id, season, gameweek, xi_ids, bench_ids):
    with engine.begin() as conn:
        gw_selection_id = conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id) "
                "VALUES (:u, :s, :gw, :cap, :vc) RETURNING id"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "cap": xi_ids[0], "vc": xi_ids[1]},
        ).scalar()
        for slot, pid in enumerate(xi_ids, start=1):
            conn.execute(
                text(
                    "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain) "
                    "VALUES (:gsid, :pid, :slot, :cap, :vc)"
                ),
                {"gsid": gw_selection_id, "pid": pid, "slot": slot, "cap": pid == xi_ids[0], "vc": pid == xi_ids[1]},
            )
        for slot, pid in enumerate(bench_ids, start=12):
            conn.execute(
                text(
                    "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain) "
                    "VALUES (:gsid, :pid, :slot, FALSE, FALSE)"
                ),
                {"gsid": gw_selection_id, "pid": pid, "slot": slot},
            )


def _gw_scores_exists(engine, user_id, season, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT 1 FROM gw_scores WHERE user_id = :u AND season = :s AND gameweek = :gw"),
            {"u": user_id, "s": season, "gw": gameweek},
        ).first() is not None


def _snapshot_exists(engine, league_id, user_id, season, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT 1 FROM leaderboard_snapshots "
                "WHERE league_id = :lid AND user_id = :u AND season = :s AND gameweek = :gw"
            ),
            {"lid": league_id, "u": user_id, "s": season, "gw": gameweek},
        ).first() is not None


def _seed_prediction(engine, make_team, make_player, fpl_id, season, gameweek):
    team_id = make_team(fpl_id=fpl_id + 60000, name=f"P{fpl_id}", short_name=f"P{fpl_id}")
    internal_id = make_player(fpl_id=fpl_id, team_id=team_id)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ml.ml_predictions (player_id, season, gameweek, predicted_points, model_version) "
                "VALUES (:pid, :s, :gw, 3.5, 'xgboost_v1')"
            ),
            {"pid": internal_id, "s": season, "gw": gameweek},
        )


# ---------------------------------------------------------------- deadline resolution

def test_deadline_resolution_returns_min_kickoff_time(engine, make_team, make_fixture):
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    earliest = NOW() + timedelta(days=1)
    later = NOW() + timedelta(days=3)

    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=later)
    make_fixture(fpl_id=2002, gameweek=1, home_team_id=away, away_team_id=home, kickoff_time=earliest)

    deadline = resolve_gameweek_deadline(engine, TEST_SEASON, 1)
    assert deadline == earliest


def test_deadline_resolution_no_fixtures_returns_none(engine):
    deadline = resolve_gameweek_deadline(engine, TEST_SEASON, 999)
    assert deadline is None


# ---------------------------------------------------------------- lock_expired_gameweeks

def test_lock_expired_gameweeks_locks_expired_deadline(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(hours=1))
    _seed_gw_selection_row(engine, user, TEST_SEASON, 1, is_locked=False)

    summary = lock_expired_gameweeks(engine)

    assert (TEST_SEASON, 1) in summary["locked"]
    assert _is_locked(engine, TEST_SEASON, 1) is True


def test_lock_expired_gameweeks_leaves_future_deadline_unlocked(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=2, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(days=2))
    _seed_gw_selection_row(engine, user, TEST_SEASON, 2, is_locked=False)

    summary = lock_expired_gameweeks(engine)

    assert (TEST_SEASON, 2) not in summary["locked"]
    assert _is_locked(engine, TEST_SEASON, 2) is False


def test_lock_expired_gameweeks_leaves_already_locked_alone(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=3, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(days=1))
    _seed_gw_selection_row(engine, user, TEST_SEASON, 3, is_locked=True)

    summary = lock_expired_gameweeks(engine)

    assert (TEST_SEASON, 3) not in summary["locked"]  # never selected -- already locked, not in the unlocked set
    assert _is_locked(engine, TEST_SEASON, 3) is True


def test_lock_expired_gameweeks_no_fixtures_yet_skipped_not_errored(engine, make_user):
    user = make_user()
    _seed_gw_selection_row(engine, user, TEST_SEASON, 4, is_locked=False)  # no fixtures seeded at all

    summary = lock_expired_gameweeks(engine)  # must not raise

    assert (TEST_SEASON, 4) in summary["skipped_no_deadline"]
    assert (TEST_SEASON, 4) not in summary["locked"]
    assert _is_locked(engine, TEST_SEASON, 4) is False


def test_lock_expired_gameweeks_does_not_touch_a_different_season(engine, make_user, make_team, make_fixture):
    other_season = "7777-00"  # dedicated, never touched elsewhere -- cleaned up explicitly below
    try:
        user = make_user()
        home = make_team(fpl_id=1001, name="Home1", short_name="H1")
        away = make_team(fpl_id=1002, name="Away1", short_name="A1")
        # TEST_SEASON gw5: expired -- should lock.
        make_fixture(fpl_id=2001, gameweek=5, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(hours=1))
        _seed_gw_selection_row(engine, user, TEST_SEASON, 5, is_locked=False)

        # other_season gw5 (same gameweek NUMBER, different season): also
        # expired, seeded directly since make_fixture is scoped to TEST_SEASON.
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, away_team_id, kickoff_time, finished) "
                    "VALUES (9001, :s, 5, :home, :away, :ko, FALSE)"
                ),
                {"s": other_season, "home": home, "away": away, "ko": NOW() - timedelta(hours=1)},
            )
        _seed_gw_selection_row(engine, user, other_season, 5, is_locked=False)

        summary = lock_expired_gameweeks(engine)

        assert (TEST_SEASON, 5) in summary["locked"]
        assert (other_season, 5) in summary["locked"]  # both legitimately expired -- both should lock independently
        assert _is_locked(engine, TEST_SEASON, 5) is True
        assert _is_locked(engine, other_season, 5) is True
    finally:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM gw_selections WHERE season = :s"), {"s": other_season})
            conn.execute(text("DELETE FROM ml.fixtures WHERE season = :s"), {"s": other_season})


# ---------------------------------------------------------------- refresh_active_gameweeks

def test_find_active_gameweeks_identifies_window_correctly(engine, make_team, make_fixture):
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(days=2))  # active
    make_fixture(fpl_id=2002, gameweek=2, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(days=10))  # too old
    make_fixture(fpl_id=2003, gameweek=3, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(days=2))  # not started yet

    active = find_active_gameweeks(engine, window_days=5)

    assert (TEST_SEASON, 1) in active
    assert (TEST_SEASON, 2) not in active
    assert (TEST_SEASON, 3) not in active


def test_refresh_active_gameweeks_only_refreshes_active_ones(engine, make_user, make_team, make_player, make_gw_stat, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(days=2))  # active
    make_fixture(fpl_id=2002, gameweek=2, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(days=10))  # inactive

    xi1, bench1 = _build_full_squad(make_team, make_player, make_gw_stat, gameweek=1, id_offset=9000)
    _seed_full_gw_selection(engine, user, TEST_SEASON, 1, xi1, bench1)

    xi2, bench2 = _build_full_squad(make_team, make_player, make_gw_stat, gameweek=2, id_offset=9100)
    _seed_full_gw_selection(engine, user, TEST_SEASON, 2, xi2, bench2)

    league_resp = client.post(
        "/leagues",
        json={"user_id": user, "name": "Refresh League", "season": TEST_SEASON, "league_type": "private", "scoring_type": "classic"},
    )
    assert league_resp.status_code == 200
    league_id = league_resp.json()["league_id"]

    summary = refresh_active_gameweeks(engine, window_days=5)

    assert (TEST_SEASON, 1) in summary["refreshed"]
    assert (TEST_SEASON, 2) not in summary["refreshed"]

    assert _gw_scores_exists(engine, user, TEST_SEASON, 1) is True
    assert _gw_scores_exists(engine, user, TEST_SEASON, 2) is False

    assert _snapshot_exists(engine, league_id, user, TEST_SEASON, 1) is True
    assert _snapshot_exists(engine, league_id, user, TEST_SEASON, 2) is False


# ---------------------------------------------------------------- schedule_predictions

def test_finds_earliest_upcoming_gameweek_without_predictions(engine, make_team, make_player, make_fixture):
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")

    # gw1: sooner, but already has predictions -> must be skipped.
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(days=1))
    _seed_prediction(engine, make_team, make_player, fpl_id=3001, season=TEST_SEASON, gameweek=1)

    # gw2: later, no predictions yet -> this is the one that should be picked.
    make_fixture(fpl_id=2002, gameweek=2, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(days=8))

    result = find_next_gameweek_needing_predictions(engine)

    assert result == (TEST_SEASON, 2)


def test_schedule_predictions_no_op_when_nothing_needs_predicting(engine, make_team, make_player, make_fixture):
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(days=1))
    _seed_prediction(engine, make_team, make_player, fpl_id=3001, season=TEST_SEASON, gameweek=1)

    result = find_next_gameweek_needing_predictions(engine)  # must not raise

    assert result is None


# ---------------------------------------------------------------- beat_schedule config

def test_beat_schedule_contains_all_three_tasks_with_correct_intervals():
    from celery.schedules import crontab
    from Worker.celery_app import app as celery_app

    schedule = celery_app.conf.beat_schedule

    assert schedule["lock-expired-gameweeks"]["task"] == "lock_expired_gameweeks"
    assert schedule["lock-expired-gameweeks"]["schedule"] == 300.0

    assert schedule["refresh-active-gameweeks"]["task"] == "refresh_active_gameweeks"
    assert schedule["refresh-active-gameweeks"]["schedule"] == 900.0

    assert schedule["schedule-predictions-weekly"]["task"] == "schedule_predictions"
    entry_schedule = schedule["schedule-predictions-weekly"]["schedule"]
    assert isinstance(entry_schedule, crontab)
    assert entry_schedule.hour == {6}
    assert entry_schedule.minute == {0}
    assert entry_schedule.day_of_week == {2}  # Tuesday
