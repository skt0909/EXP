"""
test_squad_selection.py — FastAPI TestClient tests for
Gameplay/squad_selection.py: formation/budget/club-cap validation
(individually and combined), player-id resolution against ml.players,
the resubmission upsert path, and the deliberate non-check of
ml.players.status (injured/unavailable players are still selectable --
this endpoint has no availability rule).

Uses TEST_SEASON's dedicated ml.players/ml.teams rows (wiped autouse by
conftest.py's _clean_ml_test_data) plus a dedicated fake public.users row
per test (cascades to user_squads/squad_players on teardown).
"""

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON, bearer_headers
from main import app  # shared FastAPI app -- squad_selection's router is mounted on it
from Shared.rules import BUDGET_CAP, MAX_PER_CLUB

client = TestClient(app)

POSITIONS_15 = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3


def _build_roster(
    make_team, make_player, *, positions=None, costs=60, teams=None, statuses=None, id_offset=9000
):
    """Seed one ml.teams row per distinct team index and one ml.players row
    per slot, returning the fpl_ids in the same order as `positions`.

    teams: list[int] of team indices, one per slot (default: every slot on
    its own team, so club-cap never trips unless the caller shares an index).
    costs: either a single int applied to every slot, or a list[int].
    id_offset: base fpl_id/team fpl_id for this roster -- bump it when a
    single test seeds more than one roster, so the two don't collide on
    ml.players'/ml.teams' (fpl_id, season) unique constraint.
    """
    positions = positions if positions is not None else POSITIONS_15
    n = len(positions)
    teams = teams if teams is not None else list(range(n))
    statuses = statuses if statuses is not None else ["a"] * n
    cost_list = [costs] * n if isinstance(costs, int) else costs

    team_ids: dict[int, int] = {}
    fpl_ids = []
    for i in range(n):
        team_idx = teams[i]
        if team_idx not in team_ids:
            team_ids[team_idx] = make_team(
                fpl_id=id_offset + team_idx, name=f"Club{team_idx}", short_name=f"C{team_idx}"
            )
        fpl_id = id_offset + i
        make_player(
            fpl_id=fpl_id,
            position=positions[i],
            team_id=team_ids[team_idx],
            status=statuses[i],
            cost_start=cost_list[i],
        )
        fpl_ids.append(fpl_id)
    return fpl_ids


@pytest.fixture
def test_user(engine):
    with engine.begin() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_squad_test_user@example.com", "u": "pytest_squad_test_user", "p": "not_a_real_hash"},
        ).scalar()
    yield uid
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})  # cascades


def _squad_players_rows(engine, user_id, season):
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT sp.player_id, sp.purchase_price FROM squad_players sp "
                    "JOIN user_squads us ON us.id = sp.user_squad_id "
                    "WHERE us.user_id = :uid AND us.season = :season"
                ),
                {"uid": user_id, "season": season},
            )
        )


def test_valid_squad_succeeds_and_persists_all_rows(engine, make_team, make_player, test_user):
    fpl_ids = _build_roster(make_team, make_player, costs=60)  # 15 * 60 = 900

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["budget_remaining"] == BUDGET_CAP - 900
    assert len(body["players"]) == 15
    assert {p["purchase_price"] for p in body["players"]} == {60}

    rows = _squad_players_rows(engine, test_user, TEST_SEASON)
    assert len(rows) == 15
    assert {r.player_id for r in rows} == set(fpl_ids)
    assert all(r.purchase_price == 60 for r in rows)


@pytest.mark.parametrize(
    "swap_index, from_position, to_position",
    [
        (0, "GK", "DEF"),   # 1 GK, 6 DEF, 5 MID, 3 FWD
        (2, "DEF", "MID"),  # 2 GK, 4 DEF, 6 MID, 3 FWD
        (7, "MID", "FWD"),  # 2 GK, 5 DEF, 4 MID, 4 FWD
        (12, "FWD", "MID"),  # 2 GK, 5 DEF, 6 MID, 2 FWD
    ],
)
def test_wrong_formation_count_reported_for_each_position(
    make_team, make_player, test_user, swap_index, from_position, to_position
):
    positions = list(POSITIONS_15)
    assert positions[swap_index] == from_position
    positions[swap_index] = to_position
    fpl_ids = _build_roster(make_team, make_player, positions=positions, costs=60)

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any(f"{from_position}" in e and "expected" in e for e in errors)


def test_budget_exactly_at_cap_succeeds(make_team, make_player, test_user):
    costs = [66] * 14 + [76]  # sums to exactly 1000
    assert sum(costs) == BUDGET_CAP
    fpl_ids = _build_roster(make_team, make_player, costs=costs)

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 200
    assert resp.json()["budget_remaining"] == 0


def test_budget_one_over_cap_fails(make_team, make_player, test_user):
    costs = [66] * 14 + [77]  # sums to 1001
    assert sum(costs) == BUDGET_CAP + 1
    fpl_ids = _build_roster(make_team, make_player, costs=costs)

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("exceeds budget cap" in e for e in errors)


def test_four_players_from_one_club_fails(make_team, make_player, test_user):
    teams = list(range(15))
    for idx in (2, 3, 4, 5):  # 4 of the 5 DEF slots share a club
        teams[idx] = 100
    fpl_ids = _build_roster(make_team, make_player, teams=teams, costs=60)

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any(f"max {MAX_PER_CLUB} players per club" in e for e in errors)


def test_duplicate_player_id_fails(make_team, make_player, test_user):
    fpl_ids = _build_roster(make_team, make_player, costs=60)
    fpl_ids_with_dup = fpl_ids[:-1] + [fpl_ids[0]]  # last slot duplicates the first

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids_with_dup}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("duplicate player_id" in e for e in errors)


def test_unknown_fpl_id_fails(make_team, make_player, test_user):
    fpl_ids = _build_roster(make_team, make_player, costs=60)
    fpl_ids[-1] = 999999  # never seeded into ml.players

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("not found in ml.players" in e and "999999" in e for e in errors)


def test_injured_or_unavailable_player_is_still_accepted(make_team, make_player, test_user):
    """Regression lock: squad_selection has no ml.players.status rule by
    design (only existence + position/team/price are validated). If this
    test starts failing because status is being checked, that's a
    deliberate scope change, not a bug fix -- confirm with the user first.
    """
    statuses = ["i"] * 15  # every player "injured"
    fpl_ids = _build_roster(make_team, make_player, costs=60, statuses=statuses)

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 200
    assert len(resp.json()["players"]) == 15


def test_resubmission_replaces_roster_not_appends(engine, make_team, make_player, test_user):
    first_ids = _build_roster(make_team, make_player, costs=60, id_offset=9000)
    resp1 = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": first_ids}, headers=bearer_headers(test_user)
    )
    assert resp1.status_code == 200
    squad_id_1 = resp1.json()["squad_id"]

    # Second roster: entirely different fpl_ids/teams, zero overlap with the first.
    second_ids = _build_roster(make_team, make_player, costs=65, id_offset=9100)

    resp2 = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": second_ids}, headers=bearer_headers(test_user)
    )
    assert resp2.status_code == 200
    squad_id_2 = resp2.json()["squad_id"]
    assert squad_id_2 == squad_id_1  # same (user_id, season) -> same row, upserted not duplicated

    rows = _squad_players_rows(engine, test_user, TEST_SEASON)
    assert len(rows) == 15
    assert {r.player_id for r in rows} == set(second_ids)
    assert not set(first_ids) & {r.player_id for r in rows}


def test_multiple_simultaneous_violations_all_reported_together(make_team, make_player, test_user):
    positions = list(POSITIONS_15)
    positions[0] = "DEF"  # formation violation: 1 GK, 6 DEF
    costs = [66] * 14 + [77]  # budget violation: sums to 1001
    fpl_ids = _build_roster(make_team, make_player, positions=positions, costs=costs)
    # Append (not swap-in) the duplicate so all 15 original costs/positions
    # stay intact -- swapping would drop a real player and could mask the
    # budget/formation violations being tested alongside it.
    fpl_ids_with_dup = fpl_ids + [fpl_ids[0]]

    resp = client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": fpl_ids_with_dup}, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("duplicate player_id" in e for e in errors)
    assert any("exceeds budget cap" in e for e in errors)
    assert any("expected 2 GK" in e for e in errors)
    assert len(errors) >= 3


def test_get_squad_returns_current_roster_with_decimal_prices(make_team, make_player, test_user):
    fpl_ids = _build_roster(make_team, make_player, costs=65)  # 15 * 65 = 975
    client.post("/squad/select", json={"season": TEST_SEASON, "player_ids": fpl_ids}, headers=bearer_headers(test_user))

    resp = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["budget_remaining"] == (BUDGET_CAP - 975) / 10
    assert len(body["players"]) == 15
    assert {p["player_id"] for p in body["players"]} == set(fpl_ids)
    assert all(p["price"] == 6.5 for p in body["players"])
    assert all({"name", "position", "club"} <= p.keys() for p in body["players"])


def test_get_squad_no_squad_yet_returns_empty_not_error(test_user):
    resp = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["players"] == []
    assert body["budget_remaining"] == 0.0


def test_get_squad_reflects_resubmission_not_stale_roster(make_team, make_player, test_user):
    original_ids = _build_roster(make_team, make_player, costs=60, id_offset=9000)
    client.post("/squad/select", json={"season": TEST_SEASON, "player_ids": original_ids}, headers=bearer_headers(test_user))

    replacement_ids = _build_roster(make_team, make_player, costs=60, id_offset=9100)
    client.post("/squad/select", json={"season": TEST_SEASON, "player_ids": replacement_ids}, headers=bearer_headers(test_user))

    resp = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    ids = {p["player_id"] for p in resp.json()["players"]}
    assert ids == set(replacement_ids)
    assert ids.isdisjoint(original_ids)
