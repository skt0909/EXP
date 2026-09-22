"""GET /gameweeks/current — the season leak, and the scored-based rule.

Every page reads this endpoint to decide which gameweek to show
(frontend/src/config/gameweek.jsx), so it is live production logic. These
tests were written BEFORE the fixes, and each bug-proving test is noted where
it asserted the broken behaviour first.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from main import app

client = TestClient(app)

SIM_SEASON = "SIM38OK"


@pytest.fixture
def sim_fixtures(engine):
    """A SIM season fixture whose deadline is sooner than anything real.

    Seeded with raw SQL because conftest's make_fixture hardcodes TEST_SEASON,
    and TEST_SEASON ('9999-00') matches the real-season pattern -- so it could
    never demonstrate the leak.
    """
    created = []

    def _seed(days_ahead, gameweek=1):
        with engine.begin() as conn:
            team = conn.execute(text(
                "INSERT INTO ml.teams (fpl_id, season, name, short_name) "
                "VALUES (:f, :s, 'SimTeam', 'SIM') RETURNING id"),
                {"f": 77001, "s": SIM_SEASON}).scalar()
            conn.execute(text(
                "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, "
                "away_team_id, kickoff_time, finished) "
                "VALUES (:f, :s, :g, :t, :t, now() + make_interval(days => :d), FALSE)"),
                {"f": 77010, "s": SIM_SEASON, "g": gameweek, "t": team, "d": days_ahead})
        created.append(True)

    yield _seed

    if created:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ml.fixtures WHERE season = :s"), {"s": SIM_SEASON})
            conn.execute(text("DELETE FROM ml.teams WHERE season = :s"), {"s": SIM_SEASON})


def _current(user_id):
    resp = client.get("/gameweeks/current", headers=bearer_headers(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---- A: the season leak ----------------------------------------------------

def test_a_simulation_season_never_wins_the_current_gameweek(
    engine, make_user, make_team, make_fixture, sim_fixtures
):
    """THE BUG, proven before the fix: CURRENT_GAMEWEEK_QUERY ranked deadlines
    across every season in ml.fixtures with no season filter, so a SIM
    season's fixture -- which carries a real timestamp -- could win and the
    whole app would show a simulation gameweek.

    Before the fix this returned season='SIM38OK'.
    """
    uid = make_user()
    sim_fixtures(days_ahead=2)          # soonest deadline of all

    home = make_team(fpl_id=78001, name="RealH", short_name="RLH")
    away = make_team(fpl_id=78002, name="RealA", short_name="RLA")
    make_fixture(fpl_id=78010, gameweek=7, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=9))

    body = _current(uid)
    assert body["found"] is True
    assert body["season"] != SIM_SEASON, \
        "a simulation season won the current-gameweek race"
    assert body["season"] == TEST_SEASON
    assert body["gameweek"] == 7


def _future(engine, days):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT now() + make_interval(days => :d)"), {"d": days}).scalar()


# ---- B: the scored-based rule ---------------------------------------------

@pytest.fixture
def scored(engine):
    """Mark a gameweek scored, and clean up."""
    def _mark(gameweek, season=TEST_SEASON):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO gameweeks (season, gameweek, scored_at) "
                "VALUES (:s, :g, now()) "
                "ON CONFLICT (season, gameweek) DO UPDATE SET scored_at = now()"),
                {"s": season, "g": gameweek})

    yield _mark

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gameweeks WHERE season = :s"), {"s": TEST_SEASON})


def test_the_earliest_unscored_gameweek_is_current(
    engine, make_user, make_team, make_fixture, scored
):
    """gw5 scored, gw6 not -> current is 6.

    Before the rewrite this returned 6 only by accident of the deadlines; the
    rule now says so directly.
    """
    uid = make_user()
    home = make_team(fpl_id=78101, name="H1", short_name="H01")
    away = make_team(fpl_id=78102, name="A1", short_name="A01")
    make_fixture(fpl_id=78110, gameweek=5, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=-9), finished=True)
    make_fixture(fpl_id=78111, gameweek=6, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=-2), finished=True)
    scored(5)

    body = _current(uid)
    assert body["season"] == TEST_SEASON
    assert body["gameweek"] == 6


def test_a_locked_but_unscored_gameweek_is_still_current(
    engine, make_user, make_team, make_fixture, scored
):
    """THE BUG, proven before the fix: the deadline rule moved on the instant a
    deadline passed, so while gw8 was being played every page already showed
    gw9. Under the scored rule gw8 stays current until it is actually scored.

    Before the fix this returned gameweek 9.
    """
    uid = make_user()
    home = make_team(fpl_id=78201, name="H2", short_name="H02")
    away = make_team(fpl_id=78202, name="A2", short_name="A02")
    # gw8 kicked off yesterday: deadline long past, not finished, not scored.
    make_fixture(fpl_id=78210, gameweek=8, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=-1), finished=False)
    make_fixture(fpl_id=78211, gameweek=9, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=6), finished=False)
    scored(7)

    body = _current(uid)
    assert body["gameweek"] == 8, "a locked, unplayed-out gameweek must stay current"


def test_the_season_start_falls_back_to_the_deadline_rule(
    engine, make_user, make_team, make_fixture
):
    """No scored history at all: the earliest unscored gameweek is simply the
    first one, which is also what the deadline rule would say. The two rules
    must agree here, and neither may return nothing."""
    uid = make_user()
    home = make_team(fpl_id=78301, name="H3", short_name="H03")
    away = make_team(fpl_id=78302, name="A3", short_name="A03")
    make_fixture(fpl_id=78310, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=3))
    make_fixture(fpl_id=78311, gameweek=2, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=10))

    body = _current(uid)
    assert body["found"] is True
    assert body["gameweek"] == 1


def test_no_fixtures_and_no_scored_history_reports_not_found(engine, make_user):
    """No real-season fixtures at all: found=False rather than a 404, the same
    'unstarted state is not an error' stance as GET /squad."""
    uid = make_user()
    body = _current(uid)
    assert body["found"] is False
    assert body["season"] is None and body["gameweek"] is None


def test_a_fully_scored_season_does_not_report_nothing(
    engine, make_user, make_team, make_fixture, scored
):
    """End of season: every gameweek scored, so the scored rule selects no row.
    The deadline fallback must still answer -- returning nothing would strand
    every page that reads this endpoint."""
    uid = make_user()
    home = make_team(fpl_id=78401, name="H4", short_name="H04")
    away = make_team(fpl_id=78402, name="A4", short_name="A04")
    make_fixture(fpl_id=78410, gameweek=37, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=-12), finished=True)
    make_fixture(fpl_id=78411, gameweek=38, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=-5), finished=True)
    scored(37)
    scored(38)

    body = _current(uid)
    assert body["found"] is True
    assert body["gameweek"] == 38, "the most recent past gameweek, read-only"


def test_a_gameweeks_row_with_a_null_scored_at_still_counts_as_unscored(
    engine, make_user, make_team, make_fixture, scored
):
    """scored_at is nullable, so 'no row' and 'a row with NULL' are two ways of
    saying the same thing. The query must not treat the row's existence as the
    signal."""
    uid = make_user()
    home = make_team(fpl_id=78501, name="H5", short_name="H05")
    away = make_team(fpl_id=78502, name="A5", short_name="A05")
    make_fixture(fpl_id=78510, gameweek=3, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=-4), finished=True)
    make_fixture(fpl_id=78511, gameweek=4, home_team_id=home, away_team_id=away,
                 kickoff_time=_future(engine, days=2))
    scored(3)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gameweeks (season, gameweek) VALUES (:s, 4)"), {"s": TEST_SEASON})

    body = _current(uid)
    assert body["gameweek"] == 4
