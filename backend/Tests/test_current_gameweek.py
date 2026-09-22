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
