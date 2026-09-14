"""
test_players.py — FastAPI TestClient tests for Data/players.py:
response shape, x10 -> decimal price conversion, and season filtering.

Uses TEST_SEASON's dedicated ml.players/ml.teams rows (wiped autouse by
conftest.py's _clean_ml_test_data), plus a second, distinct fake season
seeded and torn down manually to prove season filtering doesn't leak.
"""

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app

client = TestClient(app)

OTHER_SEASON = "8888-00"


@pytest.fixture(autouse=True)
def _clean_other_season(engine):
    def _wipe():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ml.players WHERE season = :s"), {"s": OTHER_SEASON})
            conn.execute(text("DELETE FROM ml.teams WHERE season = :s"), {"s": OTHER_SEASON})

    _wipe()
    yield
    _wipe()


def _make_team_for_season(engine, season, fpl_id, name, short_name):
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ml.teams (fpl_id, season, name, short_name) "
                "VALUES (:fpl_id, :season, :name, :short_name) RETURNING id"
            ),
            {"fpl_id": fpl_id, "season": season, "name": name, "short_name": short_name},
        ).scalar()


def test_returns_correct_shape(make_team, make_player):
    team_id = make_team(fpl_id=1, name="Test FC", short_name="TFC")
    make_player(fpl_id=101, position="MID", team_id=team_id, web_name="Testman", cost_start=85, status="a")

    resp = client.get("/players", params={"season": TEST_SEASON})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    player = body[0]
    assert player == {
        "id": 101,
        "name": "Testman",
        "position": "MID",
        "club": "TFC",
        "price": 8.5,
        "status": "a",
        "points": 0,
        "season_points": 0,
        "next_opponent": None,
        "next_opponent_is_home": None,
    }


def test_season_points_sums_every_ingested_gameweek_independent_of_the_gameweek_param(
    make_team, make_player, make_gw_stat
):
    """season_points must (a) sum across every gameweek this player has a
    row for, and (b) stay the same regardless of which :gameweek was
    requested -- unlike `points`, which is scoped to exactly that one
    gameweek. Both read from the same request/response to prove they can't
    drift apart."""
    team_id = make_team(fpl_id=1, name="Test FC", short_name="TFC")
    internal_id = make_player(fpl_id=101, team_id=team_id, web_name="Testman")
    make_gw_stat(player_id=internal_id, gameweek=1, total_points=5)
    make_gw_stat(player_id=internal_id, gameweek=2, total_points=8)
    make_gw_stat(player_id=internal_id, gameweek=3, total_points=2)

    resp_gw2 = client.get("/players", params={"season": TEST_SEASON, "gameweek": 2})
    assert resp_gw2.status_code == 200
    player_gw2 = resp_gw2.json()[0]
    assert player_gw2["points"] == 8  # gameweek 2 only
    assert player_gw2["season_points"] == 15  # 5 + 8 + 2

    resp_gw1 = client.get("/players", params={"season": TEST_SEASON, "gameweek": 1})
    player_gw1 = resp_gw1.json()[0]
    assert player_gw1["points"] == 5  # gameweek 1 only
    assert player_gw1["season_points"] == 15  # unchanged by which gameweek was asked for

    resp_no_gw = client.get("/players", params={"season": TEST_SEASON})
    player_no_gw = resp_no_gw.json()[0]
    assert player_no_gw["points"] == 0  # no gameweek requested -- the existing, unchanged behavior
    assert player_no_gw["season_points"] == 15  # season total needs no gameweek param at all


def test_price_converted_from_x10_scale(make_team, make_player):
    team_id = make_team(fpl_id=1, name="Test FC", short_name="TFC")
    make_player(fpl_id=101, team_id=team_id, cost_start=45)
    make_player(fpl_id=102, team_id=team_id, cost_start=120)

    resp = client.get("/players", params={"season": TEST_SEASON})

    assert resp.status_code == 200
    prices = {p["id"]: p["price"] for p in resp.json()}
    assert prices == {101: 4.5, 102: 12.0}


def test_filters_by_season_does_not_leak_other_season(engine, make_team, make_player):
    team_id = make_team(fpl_id=1, name="Test FC", short_name="TFC")
    make_player(fpl_id=101, team_id=team_id, cost_start=60)

    other_team_id = _make_team_for_season(engine, OTHER_SEASON, fpl_id=1, name="Other FC", short_name="OFC")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ml.players (fpl_id, season, fpl_name, web_name, position, "
                "position_encoded, team_id, status, cost_start) VALUES "
                "(:fpl_id, :season, :name, :name, :position, :pos_enc, :team_id, :status, :cost_start)"
            ),
            {
                "fpl_id": 999,
                "season": OTHER_SEASON,
                "name": "OtherSeasonPlayer",
                "position": "FWD",
                "pos_enc": 3,
                "team_id": other_team_id,
                "status": "a",
                "cost_start": 70,
            },
        )

    resp = client.get("/players", params={"season": TEST_SEASON})

    assert resp.status_code == 200
    ids = {p["id"] for p in resp.json()}
    assert ids == {101}
    assert 999 not in ids


def test_next_opponent_resolves_for_home_and_away_players(make_team, make_player, make_fixture):
    home_team_id = make_team(fpl_id=1, name="Home FC", short_name="HFC")
    away_team_id = make_team(fpl_id=2, name="Away FC", short_name="AFC")
    make_player(fpl_id=101, team_id=home_team_id, web_name="Homer")
    make_player(fpl_id=102, team_id=away_team_id, web_name="Awayer")

    make_fixture(
        fpl_id=1001, gameweek=5, home_team_id=home_team_id, away_team_id=away_team_id,
        kickoff_time="2026-01-10T15:00:00Z", finished=False,
    )

    resp = client.get("/players", params={"season": TEST_SEASON})

    assert resp.status_code == 200
    by_id = {p["id"]: p for p in resp.json()}
    assert by_id[101]["next_opponent"] == "AFC"
    assert by_id[101]["next_opponent_is_home"] is True
    assert by_id[102]["next_opponent"] == "HFC"
    assert by_id[102]["next_opponent_is_home"] is False


def test_next_opponent_picks_earliest_of_multiple_unfinished_fixtures(make_team, make_player, make_fixture):
    team_id = make_team(fpl_id=1, name="Test FC", short_name="TFC")
    opponent_soon = make_team(fpl_id=2, name="Soon FC", short_name="SOO")
    opponent_later = make_team(fpl_id=3, name="Later FC", short_name="LAT")
    make_player(fpl_id=101, team_id=team_id, web_name="Testman")

    make_fixture(
        fpl_id=1001, gameweek=6, home_team_id=team_id, away_team_id=opponent_later,
        kickoff_time="2026-02-01T15:00:00Z", finished=False,
    )
    make_fixture(
        fpl_id=1002, gameweek=5, home_team_id=team_id, away_team_id=opponent_soon,
        kickoff_time="2026-01-10T15:00:00Z", finished=False,
    )

    resp = client.get("/players", params={"season": TEST_SEASON})

    assert resp.status_code == 200
    player = next(p for p in resp.json() if p["id"] == 101)
    assert player["next_opponent"] == "SOO"
    assert player["next_opponent_is_home"] is True


def test_next_opponent_null_when_no_unfinished_fixture(make_team, make_player, make_fixture):
    team_id = make_team(fpl_id=1, name="Test FC", short_name="TFC")
    opponent_id = make_team(fpl_id=2, name="Opponent FC", short_name="OPP")
    make_player(fpl_id=101, team_id=team_id, web_name="Testman")

    # Only a finished fixture exists -- next gameweek's fixtures haven't
    # been ingested yet -- should resolve to null, not error.
    make_fixture(
        fpl_id=1001, gameweek=4, home_team_id=team_id, away_team_id=opponent_id,
        kickoff_time="2025-12-01T15:00:00Z", finished=True,
    )

    resp = client.get("/players", params={"season": TEST_SEASON})

    assert resp.status_code == 200
    player = next(p for p in resp.json() if p["id"] == 101)
    assert player["next_opponent"] is None
    assert player["next_opponent_is_home"] is None
