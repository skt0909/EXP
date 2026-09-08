"""
test_beat_scheduling.py — tests for the pure functions backing the
Celery Beat tasks, and Worker/celery_app.py's beat_schedule config.

Those functions used to share one module, Game_logic/scheduling.py. That
file has been split up and no longer exists; they now live in
deadlines.py, gameweek_lock.py, gameweek_finalize.py, free_hit_revert.py,
dream11_locking.py and Predict/prediction_scheduling.py, with
Worker/beat_registry.py as the index of which module owns what.

Deliberately imports only those pure-function modules plus scoring and
standings and Worker.celery_app, never Worker.tasks -- same reasoning as every
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

from conftest import TEST_SEASON, bearer_headers
from main import app as fastapi_app
from GameEngine.free_hit_revert import revert_expired_free_hits
from GameEngine.gameweek_finalize import find_active_gameweeks, refresh_active_gameweeks
from GameEngine.gameweek_lock import lock_expired_gameweeks
from Shared.deadlines import resolve_gameweek_deadline, deadline_has_passed
# Relocated out of scheduling.py to their real domains -- see
# Worker/beat_registry.py for the full map of what Beat calls.
from dream11_locking import lock_started_contests
from prediction_scheduling import find_next_gameweek_needing_predictions

client = TestClient(fastapi_app)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 -- small enough to keep as a lambda, used throughout


@pytest.fixture
def make_user(make_user, engine):
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
    # Creation delegated to conftest's make_user (same name, received as an
    # argument -- pytest resolves it to the parent fixture). The prefix is
    # passed so the rows this file creates are still named as they were.
    # The teardown below stays here because it is specific to this file.
    factory = make_user

    def _make():
        return factory("beat")

    yield _make

    with engine.begin() as conn:
        for uid in factory.created:
            conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM free_hit_squads WHERE user_id = :uid"), {"uid": uid})
            conn.execute(
                text("DELETE FROM squad_players WHERE user_squad_id IN "
                     "(SELECT id FROM user_squads WHERE user_id = :uid)"),
                {"uid": uid},
            )
            conn.execute(text("DELETE FROM user_squads WHERE user_id = :uid"), {"uid": uid})


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
    assert deadline == earliest - timedelta(minutes=90)


def test_deadline_resolution_no_fixtures_returns_none(engine):
    deadline = resolve_gameweek_deadline(engine, TEST_SEASON, 999)
    assert deadline is None


def test_deadline_has_passed_reflects_kickoff_relative_to_now(engine, make_team, make_fixture):
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(hours=1))
    make_fixture(fpl_id=2002, gameweek=2, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(hours=2))

    assert deadline_has_passed(engine, TEST_SEASON, 1) is True
    assert deadline_has_passed(engine, TEST_SEASON, 2) is False


def test_deadline_has_passed_uses_earliest_kickoff_not_latest(engine, make_team, make_fixture):
    """The deadline is MIN(kickoff_time): once the first match of a
    gameweek starts, the whole gameweek is shut, even though later
    fixtures in it are still ahead."""
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(hours=1))
    make_fixture(fpl_id=2002, gameweek=1, home_team_id=away, away_team_id=home, kickoff_time=NOW() + timedelta(days=2))

    assert deadline_has_passed(engine, TEST_SEASON, 1) is True


def test_deadline_has_passed_no_fixtures_is_false_not_locked(engine):
    """An un-ingested gameweek must read as OPEN -- treating "unknown"
    as locked would freeze a gameweek nobody could then play."""
    assert deadline_has_passed(engine, TEST_SEASON, 999) is False


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
        json={"name": "Refresh League", "season": TEST_SEASON, "league_type": "private", "scoring_type": "classic"}, headers=bearer_headers(user)
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


# ---------------------------------------------------------------- lock_started_contests

def _seed_contest(engine, user_id, fixture_id, is_locked=False):
    """dream11.contests FKs to ml.fixtures ON DELETE CASCADE, and conftest's
    autouse wipe drops TEST_SEASON fixtures after every test -- so contests
    seeded here clean themselves up, no explicit teardown needed."""
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO dream11.contests (fixture_id, name, code, created_by, max_members, is_locked) "
                "VALUES (:f, :n, :c, :u, 50, :locked) RETURNING id"
            ),
            {"f": fixture_id, "n": "BeatTest", "c": uuid.uuid4().hex[:9].upper(), "u": user_id, "locked": is_locked},
        ).scalar()


def _contest_is_locked(engine, contest_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT is_locked FROM dream11.contests WHERE id = :cid"), {"cid": contest_id}
        ).scalar()


def test_lock_started_contests_locks_contest_whose_fixture_kicked_off(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    fixture_id = make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(minutes=5))
    contest_id = _seed_contest(engine, user, fixture_id)

    summary = lock_started_contests(engine)

    assert contest_id in summary["locked"]
    assert _contest_is_locked(engine, contest_id) is True


def test_lock_started_contests_leaves_future_kickoff_unlocked(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1003, name="Home2", short_name="H2")
    away = make_team(fpl_id=1004, name="Away2", short_name="A2")
    fixture_id = make_fixture(fpl_id=2002, gameweek=2, home_team_id=home, away_team_id=away, kickoff_time=NOW() + timedelta(days=1))
    contest_id = _seed_contest(engine, user, fixture_id)

    summary = lock_started_contests(engine)

    assert contest_id not in summary["locked"]
    assert _contest_is_locked(engine, contest_id) is False


def test_lock_started_contests_no_kickoff_time_skipped_not_errored(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1005, name="Home3", short_name="H3")
    away = make_team(fpl_id=1006, name="Away3", short_name="A3")
    fixture_id = make_fixture(fpl_id=2003, gameweek=3, home_team_id=home, away_team_id=away, kickoff_time=None)
    contest_id = _seed_contest(engine, user, fixture_id)

    summary = lock_started_contests(engine)  # must not raise

    assert contest_id in summary["skipped_no_kickoff"]
    assert _contest_is_locked(engine, contest_id) is False


def test_lock_started_contests_leaves_already_locked_alone(engine, make_user, make_team, make_fixture):
    user = make_user()
    home = make_team(fpl_id=1007, name="Home4", short_name="H4")
    away = make_team(fpl_id=1008, name="Away4", short_name="A4")
    fixture_id = make_fixture(fpl_id=2004, gameweek=4, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(hours=2))
    contest_id = _seed_contest(engine, user, fixture_id, is_locked=True)

    summary = lock_started_contests(engine)

    assert contest_id not in summary["locked"]  # never in the unlocked set to begin with
    assert _contest_is_locked(engine, contest_id) is True


def test_lock_started_contests_actually_blocks_joining_afterwards(engine, make_user, make_team, make_fixture):
    """The point of the locker: end-to-end, a kicked-off contest stops
    accepting joins. Covers the wiring between this function and the
    is_locked check in dream11.py's join_contest."""
    creator = make_user()
    joiner = make_user()
    home = make_team(fpl_id=1009, name="Home5", short_name="H5")
    away = make_team(fpl_id=1010, name="Away5", short_name="A5")
    fixture_id = make_fixture(fpl_id=2005, gameweek=5, home_team_id=home, away_team_id=away, kickoff_time=NOW() - timedelta(minutes=1))
    contest_id = _seed_contest(engine, creator, fixture_id)
    with engine.connect() as conn:
        code = conn.execute(text("SELECT code FROM dream11.contests WHERE id = :cid"), {"cid": contest_id}).scalar()

    joined = client.post(
        "/dream11/contests/join", json={"code": code}, headers=bearer_headers(joiner)
    )
    assert joined.status_code == 200, joined.json()

    lock_started_contests(engine)

    second = make_user()
    resp = client.post(
        "/dream11/contests/join", json={"code": code}, headers=bearer_headers(second)
    )
    assert resp.status_code == 422
    assert "contest is locked and can no longer be joined" in resp.json()["detail"]


# ---------------------------------------------------------------- revert_expired_free_hits

def _seed_squad_with_free_hit(engine, user_id, pre_ids, post_ids, pre_budget, post_budget, gameweek):
    """Puts the user in the state a played Free Hit leaves behind: the
    post-chip squad active, the pre-chip squad recorded in
    free_hit_squads and already deactivated in squad_players (exactly
    what transfers.py's DEACTIVATE_SQUAD_PLAYER_STMT does to it).
    """
    with engine.begin() as conn:
        user_squad_id = conn.execute(
            text("INSERT INTO user_squads (user_id, season, budget_remaining) VALUES (:u, :s, :b) RETURNING id"),
            {"u": user_id, "s": TEST_SEASON, "b": post_budget},
        ).scalar()
        for pid in pre_ids:
            conn.execute(
                text("INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                     "VALUES (:usid, :pid, 50, FALSE)"),
                {"usid": user_squad_id, "pid": pid},
            )
        for pid in post_ids:
            conn.execute(
                text("INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                     "VALUES (:usid, :pid, 50, TRUE)"),
                {"usid": user_squad_id, "pid": pid},
            )
        for pid in pre_ids:
            conn.execute(
                text("INSERT INTO free_hit_squads (user_id, season, gameweek, player_id, purchase_price, budget_remaining) "
                     "VALUES (:u, :s, :gw, :pid, 50, :b)"),
                {"u": user_id, "s": TEST_SEASON, "gw": gameweek, "pid": pid, "b": pre_budget},
            )
    return user_squad_id


def _active_player_ids(engine, user_squad_id):
    with engine.connect() as conn:
        return sorted(
            r.player_id
            for r in conn.execute(
                text("SELECT player_id FROM squad_players WHERE user_squad_id = :usid AND is_active = TRUE"),
                {"usid": user_squad_id},
            )
        )


def _budget(engine, user_squad_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT budget_remaining FROM user_squads WHERE id = :usid"), {"usid": user_squad_id}
        ).scalar()


def test_revert_free_hit_restores_squad_and_budget_once_gameweek_is_over(
    engine, make_user, make_team, make_fixture
):
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time=NOW() - timedelta(hours=6), finished=True)
    usid = _seed_squad_with_free_hit(
        engine, user, pre_ids=[101, 102, 103], post_ids=[201, 202, 203],
        pre_budget=40, post_budget=5, gameweek=1,
    )

    summary = revert_expired_free_hits(engine)

    assert (user, TEST_SEASON, 1) in summary["reverted"]
    assert summary["failed"] == []
    assert _active_player_ids(engine, usid) == [101, 102, 103]
    assert _budget(engine, usid) == 40


def test_revert_free_hit_does_not_fire_while_gameweek_is_still_being_played(
    engine, make_user, make_team, make_fixture
):
    """The free-hit squad must stay active through its own gameweek --
    reverting at the deadline (the way locking works) would swap a
    manager's team out from under them mid-match."""
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time=NOW() - timedelta(minutes=30), finished=False)
    usid = _seed_squad_with_free_hit(
        engine, user, pre_ids=[101, 102, 103], post_ids=[201, 202, 203],
        pre_budget=40, post_budget=5, gameweek=1,
    )

    summary = revert_expired_free_hits(engine)

    assert (user, TEST_SEASON, 1) not in summary["reverted"]
    assert _active_player_ids(engine, usid) == [201, 202, 203]
    assert _budget(engine, usid) == 5


def test_revert_free_hit_waits_for_the_last_fixture_of_the_gameweek(
    engine, make_user, make_team, make_fixture
):
    """One finished early match doesn't end the gameweek -- a Monday
    night fixture still to come keeps the free-hit squad in place."""
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time=NOW() - timedelta(days=2), finished=True)
    make_fixture(fpl_id=2002, gameweek=1, home_team_id=away, away_team_id=home,
                 kickoff_time=NOW() + timedelta(days=1), finished=False)
    usid = _seed_squad_with_free_hit(
        engine, user, pre_ids=[101, 102, 103], post_ids=[201, 202, 203],
        pre_budget=40, post_budget=5, gameweek=1,
    )

    summary = revert_expired_free_hits(engine)

    assert (user, TEST_SEASON, 1) not in summary["reverted"]
    assert _active_player_ids(engine, usid) == [201, 202, 203]


def test_revert_free_hit_is_idempotent(engine, make_user, make_team, make_fixture):
    """Beat retries and manual Tools/fpl_sim.py runs both re-enter this;
    a second pass must not re-revert (which would resurrect the pre-chip
    squad over a legitimate later transfer)."""
    user = make_user()
    home = make_team(fpl_id=1001, name="Home1", short_name="H1")
    away = make_team(fpl_id=1002, name="Away1", short_name="A1")
    make_fixture(fpl_id=2001, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time=NOW() - timedelta(hours=6), finished=True)
    usid = _seed_squad_with_free_hit(
        engine, user, pre_ids=[101, 102, 103], post_ids=[201, 202, 203],
        pre_budget=40, post_budget=5, gameweek=1,
    )

    revert_expired_free_hits(engine)
    second = revert_expired_free_hits(engine)

    assert (user, TEST_SEASON, 1) not in second["reverted"]
    assert _active_player_ids(engine, usid) == [101, 102, 103]

    with engine.connect() as conn:
        pending = conn.execute(
            text("SELECT COUNT(*) FROM free_hit_squads WHERE user_id = :u AND reverted_at IS NULL"),
            {"u": user},
        ).scalar()
    assert pending == 0


def test_revert_free_hit_skips_gameweek_with_no_fixtures_ingested(engine, make_user):
    """No fixtures means the gameweek's end is unknown -- same stance as
    an unknown deadline never being 'passed'."""
    user = make_user()
    usid = _seed_squad_with_free_hit(
        engine, user, pre_ids=[101, 102, 103], post_ids=[201, 202, 203],
        pre_budget=40, post_budget=5, gameweek=1,
    )

    summary = revert_expired_free_hits(engine)

    assert (user, TEST_SEASON, 1) not in summary["reverted"]
    assert _active_player_ids(engine, usid) == [201, 202, 203]


# ---------------------------------------------------------------- beat_schedule config

def test_beat_schedule_contains_every_task_with_correct_intervals():
    from celery.schedules import crontab
    from Worker.celery_app import app as celery_app

    schedule = celery_app.conf.beat_schedule

    assert schedule["lock-expired-gameweeks"]["task"] == "lock_expired_gameweeks"
    assert schedule["lock-expired-gameweeks"]["schedule"] == 300.0

    assert schedule["lock-dream11-contests"]["task"] == "lock_dream11_contests"
    assert schedule["lock-dream11-contests"]["schedule"] == 300.0

    assert schedule["refresh-active-gameweeks"]["task"] == "refresh_active_gameweeks"
    assert schedule["refresh-active-gameweeks"]["schedule"] == 900.0

    assert schedule["revert-free-hits"]["task"] == "revert_free_hits"
    assert schedule["revert-free-hits"]["schedule"] == 900.0

    assert schedule["schedule-predictions-weekly"]["task"] == "schedule_predictions"
    entry_schedule = schedule["schedule-predictions-weekly"]["schedule"]
    assert isinstance(entry_schedule, crontab)
    assert entry_schedule.hour == {6}
    assert entry_schedule.minute == {0}
    assert entry_schedule.day_of_week == {2}  # Tuesday

    assert schedule["schedule-fixture-polls"]["task"] == "schedule_fixture_polls"
    assert schedule["schedule-fixture-polls"]["schedule"] == 900.0

    # Without this entry ml.fixtures only changed when someone ran
    # fpl_ingest.py by hand, so played matches sat at finished=FALSE
    # indefinitely -- and schedule-fixture-polls above, which reads these
    # rows, was only ever as fresh as the last manual run.
    assert schedule["refresh-fixtures"]["task"] == "refresh_fixtures"
    assert schedule["refresh-fixtures"]["schedule"] == 900.0

    # The guarantee that a Dream11 contest gets frozen at all: the
    # kickoff+115min checkpoint also tries, but it fires before FPL sets
    # fixtures.finished, and its one-off ETA may never have been booked if
    # the broker was down at contest creation.
    assert schedule["finalize-dream11-contests"]["task"] == "finalize_dream11_contests"
    assert schedule["finalize-dream11-contests"]["schedule"] == 900.0


def test_every_beat_task_name_is_actually_registered():
    """Each beat_schedule entry names a task by string. A typo, or an entry
    for a task nobody wrote, fails only at the first tick in production --
    so it is checked here instead."""
    from Worker.celery_app import app as celery_app
    import Worker.tasks  # noqa: F401 -- importing is what registers the tasks

    scheduled = {entry["task"] for entry in celery_app.conf.beat_schedule.values()}
    missing = scheduled - set(celery_app.tasks)

    assert not missing, f"beat_schedule names tasks that are not registered: {sorted(missing)}"
