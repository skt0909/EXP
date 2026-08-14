"""
test_starting_xi.py — FastAPI TestClient tests for
Game_logic/starting_xi.py: formation-boundary validation (GK/DEF/MID/FWD,
tested right at their limits, not just "clearly wrong"), bench_order
validation (count/duplicates/overlap-with-XI/exact-GK-count), the
combined player_ids+bench_order == active-15-man-squad check,
captain/vice validation, the chip pre-check (restricted chips capped at
1/season, wildcard at 2/season -- matching the DB migration applied to
enforce_chip_limit_fn), the lock-trigger-to-422 translation, and the
delete-existing-chip-row-first behavior on resubmission.

Seeds a real user_squads/squad_players "active squad" directly via SQL
(bypassing squad_selection.py entirely) so each test can shape exactly
the position mix it needs, including mixes a real squad_selection
submission would never allow (e.g. 6 DEF-position players, or both GKs
in the starting XI) -- that's fine here since starting_xi.py only cares
about ml.players/squad_players state, not how it got there.
"""

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app  # shared FastAPI app -- starting_xi's router is mounted on it

client = TestClient(app)

# 1 GK + 4 DEF + 4 MID + 2 FWD = 11, valid under GK==1 / DEF 3-5 / MID 2-5 / FWD 1-3.
VALID_XI_POSITIONS = ["GK"] + ["DEF"] * 4 + ["MID"] * 4 + ["FWD"] * 2
# Complements VALID_XI_POSITIONS to a realistic 2GK/5DEF/5MID/3FWD 15-man
# squad. GK deliberately placed 3rd (not 1st/last) so tests can't
# accidentally pass by assuming "GK is always slot 12 or 15."
DEFAULT_BENCH_POSITIONS = ["DEF", "MID", "GK", "FWD"]


@pytest.fixture
def test_user(engine):
    with engine.begin() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": "pytest_gw_selection_test_user@example.com", "u": "pytest_gw_selection_test_user", "p": "not_a_real_hash"},
        ).scalar()
    yield uid
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})  # cascades


def _seed_squad(engine, make_team, make_player, user_id, season, xi_positions, bench_positions, id_offset=9000, cost=50):
    """Seeds one ml.players + active squad_players row per entry in
    xi_positions + bench_positions, all under a single user_squads row
    for (user_id, season). Call this ONCE per test -- user_squads has a
    UNIQUE(user_id, season) constraint, so a second call for the same
    user/season would collide; tests needing several gameweeks reuse the
    same seeded squad across multiple /gw_selection calls instead of
    reseeding.

    Returns (xi_ids, bench_ids).
    """
    positions = list(xi_positions) + list(bench_positions)
    team_ids: dict[int, int] = {}
    fpl_ids = []
    for i, pos in enumerate(positions):
        team_ids[i] = make_team(fpl_id=id_offset + i, name=f"Club{i}", short_name=f"C{i}")
        fid = id_offset + i
        make_player(fpl_id=fid, position=pos, team_id=team_ids[i], cost_start=cost)
        fpl_ids.append(fid)

    with engine.begin() as conn:
        squad_id = conn.execute(
            text("INSERT INTO user_squads (user_id, season, budget_remaining) VALUES (:u, :s, 1000) RETURNING id"),
            {"u": user_id, "s": season},
        ).scalar()
        for fid in fpl_ids:
            conn.execute(
                text(
                    "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                    "VALUES (:usid, :pid, :cost, TRUE)"
                ),
                {"usid": squad_id, "pid": fid, "cost": cost},
            )

    xi_ids = fpl_ids[: len(xi_positions)]
    bench_ids = fpl_ids[len(xi_positions):]
    return xi_ids, bench_ids


def _chip_rows(engine, user_id, season, chip_type):
    with engine.connect() as conn:
        return list(
            conn.execute(
                text("SELECT * FROM chips WHERE user_id = :u AND season = :s AND chip_type = :c"),
                {"u": user_id, "s": season, "c": chip_type},
            )
        )


def _base_payload(user_id, xi_ids, bench_ids, gameweek=1, chip_used=None):
    return {
        "user_id": user_id,
        "season": TEST_SEASON,
        "gameweek": gameweek,
        "player_ids": xi_ids,
        "bench_order": bench_ids,
        "captain_id": xi_ids[0],
        "vice_captain_id": xi_ids[1],
        "chip_used": chip_used,
    }


def test_valid_selection_succeeds_and_persists(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, bench_ids))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body["player_ids"]) == set(xi_ids)
    assert body["bench_order"] == bench_ids
    assert body["captain_id"] == xi_ids[0]
    assert body["vice_captain_id"] == xi_ids[1]

    with engine.connect() as conn:
        rows = list(
            conn.execute(
                text(
                    "SELECT sx.player_id, sx.position_slot, sx.is_captain, sx.is_vice_captain FROM starting_xi sx "
                    "JOIN gw_selections gs ON gs.id = sx.gw_selection_id "
                    "WHERE gs.user_id = :u AND gs.season = :s AND gs.gameweek = 1"
                ),
                {"u": test_user, "s": TEST_SEASON},
            )
        )
    assert len(rows) == 15
    assert {r.player_id for r in rows} == set(xi_ids) | set(bench_ids)
    assert next(r for r in rows if r.player_id == xi_ids[0]).is_captain is True
    assert next(r for r in rows if r.player_id == xi_ids[1]).is_vice_captain is True

    # Starting XI in submitted order at slots 1-11.
    for slot, pid in enumerate(xi_ids, start=1):
        row = next(r for r in rows if r.player_id == pid)
        assert row.position_slot == slot

    # Bench in submitted order at slots 12-15 -- DEFAULT_BENCH_POSITIONS
    # puts GK 3rd (slot 14), not slot 12 or 15.
    for slot, pid in enumerate(bench_ids, start=12):
        row = next(r for r in rows if r.player_id == pid)
        assert row.position_slot == slot
    bench_gk_id = bench_ids[2]  # DEFAULT_BENCH_POSITIONS[2] == "GK"

    # The real test: identify the bench GK by joining to ml.players.position,
    # not by assuming a slot number -- must land on slot 14 here, and the
    # query must not hardcode "12" or "15" to find it.
    with engine.connect() as conn:
        gk_rows = list(
            conn.execute(
                text(
                    "SELECT sx.position_slot, sx.player_id FROM starting_xi sx "
                    "JOIN gw_selections gs ON gs.id = sx.gw_selection_id "
                    "JOIN ml.players ml ON ml.fpl_id = sx.player_id AND ml.season = gs.season "
                    "WHERE gs.user_id = :u AND gs.season = :s AND gs.gameweek = 1 "
                    "AND sx.position_slot > 11 AND ml.position = 'GK'"
                ),
                {"u": test_user, "s": TEST_SEASON},
            )
        )
    assert len(gk_rows) == 1
    assert gk_rows[0].player_id == bench_gk_id
    assert gk_rows[0].position_slot == 14


@pytest.mark.parametrize(
    "positions, expected_error",
    [
        (["GK", "GK"] + ["DEF"] * 3 + ["MID"] * 4 + ["FWD"] * 2, "expected exactly 1 GK, got 2"),
        (["GK"] + ["DEF"] * 6 + ["MID"] * 2 + ["FWD"] * 2, "DEF count must be between 3 and 5, got 6"),
        (["GK"] + ["DEF"] * 5 + ["MID"] * 5, "FWD count must be between 1 and 3, got 0"),
        (["GK"] + ["DEF"] * 3 + ["MID"] * 3 + ["FWD"] * 4, "FWD count must be between 1 and 3, got 4"),
    ],
    ids=["2-GK", "6-DEF", "0-FWD", "4-FWD"],
)
def test_formation_boundary_violations_rejected(engine, make_team, make_player, test_user, positions, expected_error):
    assert len(positions) == 11
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, positions, DEFAULT_BENCH_POSITIONS
    )

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, bench_ids))

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert expected_error in errors


def test_bench_order_wrong_count_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, bench_ids[:3]))

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert "bench_order must contain exactly 4 players, got 3" in errors


def test_bench_order_duplicate_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    dup_bench = [bench_ids[0], bench_ids[0], bench_ids[2], bench_ids[3]]  # bench_ids[1] dropped, [0] repeated

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, dup_bench))

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert f"duplicate player_id(s) in bench_order: [{bench_ids[0]}]" in errors


def test_bench_order_overlap_with_starting_xi_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    overlapping_bench = [xi_ids[0], bench_ids[1], bench_ids[2], bench_ids[3]]  # bench_ids[0] dropped

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, overlapping_bench))

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert f"player_id(s) cannot appear in both player_ids and bench_order: [{xi_ids[0]}]" in errors


def test_bench_order_missing_gk_rejected(engine, make_team, make_player, test_user):
    # Both squad GKs placed in the XI (also trips the XI's own "exactly 1
    # GK" rule -- expected and fine, collect-all means both errors surface).
    xi_positions = ["GK", "GK"] + ["DEF"] * 4 + ["MID"] * 3 + ["FWD"] * 2  # 11
    bench_positions = ["DEF", "MID", "MID", "FWD"]  # 4, 0 GK
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, xi_positions, bench_positions
    )

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, bench_ids))

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert "bench_order must contain exactly 1 GK, got 0" in errors


def test_bench_order_two_gks_rejected(engine, make_team, make_player, test_user):
    # 0 GK in the XI (also trips the XI's own "exactly 1 GK" rule) --
    # both squad GKs land on the bench instead.
    xi_positions = ["DEF"] * 5 + ["MID"] * 5 + ["FWD"]  # 11, 0 GK
    bench_positions = ["FWD", "FWD", "GK", "GK"]  # 4, 2 GK
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, xi_positions, bench_positions
    )

    resp = client.post("/gw_selection", json=_base_payload(test_user, xi_ids, bench_ids))

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert "bench_order must contain exactly 1 GK, got 2" in errors


def test_captain_and_vice_captain_same_player_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    payload = _base_payload(test_user, xi_ids, bench_ids)
    payload["vice_captain_id"] = xi_ids[0]

    resp = client.post("/gw_selection", json=payload)

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert "captain_id and vice_captain_id must be different players" in errors


def test_captain_not_in_submitted_xi_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    bogus_captain = 424242
    payload = _base_payload(test_user, xi_ids, bench_ids)
    payload["captain_id"] = bogus_captain

    resp = client.post("/gw_selection", json=payload)

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert f"captain_id {bogus_captain} is not in the submitted starting XI" in errors


def test_vice_captain_not_in_submitted_xi_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    bogus_vice = 434343
    payload = _base_payload(test_user, xi_ids, bench_ids)
    payload["vice_captain_id"] = bogus_vice

    resp = client.post("/gw_selection", json=payload)

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert f"vice_captain_id {bogus_vice} is not in the submitted starting XI" in errors


def test_locked_gameweek_resubmission_returns_clean_422_not_500(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    payload = _base_payload(test_user, xi_ids, bench_ids)

    resp1 = client.post("/gw_selection", json=payload)
    assert resp1.status_code == 200

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE gw_selections SET is_locked = TRUE WHERE user_id = :u AND season = :s AND gameweek = :gw"),
            {"u": test_user, "s": TEST_SEASON, "gw": 1},
        )

    resp2 = client.post(
        "/gw_selection", json={**payload, "captain_id": xi_ids[1], "vice_captain_id": xi_ids[0]}
    )

    assert resp2.status_code == 422
    assert resp2.json()["detail"] == "This gameweek's selection is locked and can no longer be changed"


def test_resubmitting_same_wildcard_does_not_consume_second_use(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    payload = _base_payload(test_user, xi_ids, bench_ids, chip_used="wildcard")

    resp1 = client.post("/gw_selection", json=payload)
    assert resp1.status_code == 200

    # Resubmit the *same* gameweek with the *same* chip -- captain/vice swapped
    # so it's a real resubmission, not a byte-identical duplicate request.
    resp2 = client.post(
        "/gw_selection", json={**payload, "captain_id": xi_ids[1], "vice_captain_id": xi_ids[0]}
    )
    assert resp2.status_code == 200

    rows = _chip_rows(engine, test_user, TEST_SEASON, "wildcard")
    assert len(rows) == 1
    assert rows[0].gameweek_used == 1


def test_third_wildcard_attempt_rejected(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )

    def _payload(gw):
        return _base_payload(test_user, xi_ids, bench_ids, gameweek=gw, chip_used="wildcard")

    assert client.post("/gw_selection", json=_payload(1)).status_code == 200
    assert client.post("/gw_selection", json=_payload(2)).status_code == 200

    resp3 = client.post("/gw_selection", json=_payload(3))
    assert resp3.status_code == 422
    errors = resp3.json()["detail"]
    assert "wildcard has already been used twice this season" in errors

    rows = _chip_rows(engine, test_user, TEST_SEASON, "wildcard")
    assert len(rows) == 2  # the rejected 3rd attempt must not have been persisted


def test_switching_chip_from_bench_boost_to_null_frees_the_row(engine, make_team, make_player, test_user):
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, VALID_XI_POSITIONS, DEFAULT_BENCH_POSITIONS
    )
    payload = _base_payload(test_user, xi_ids, bench_ids, chip_used="bench_boost")

    resp1 = client.post("/gw_selection", json=payload)
    assert resp1.status_code == 200
    assert len(_chip_rows(engine, test_user, TEST_SEASON, "bench_boost")) == 1

    resp2 = client.post("/gw_selection", json={**payload, "chip_used": None})
    assert resp2.status_code == 200
    assert len(_chip_rows(engine, test_user, TEST_SEASON, "bench_boost")) == 0  # row freed, not just orphaned

    # Prove it's truly freed (not silently blocked) by using bench_boost again
    # on a different gameweek -- would 422 as "already used" if the delete
    # hadn't actually happened.
    resp3 = client.post("/gw_selection", json={**payload, "gameweek": 2, "chip_used": "bench_boost"})
    assert resp3.status_code == 200
    assert len(_chip_rows(engine, test_user, TEST_SEASON, "bench_boost")) == 1


def test_multiple_simultaneous_violations_all_reported_together(engine, make_team, make_player, test_user):
    # 0 FWD (formation violation) + captain==vice (duplicate violation) +
    # a chip value that isn't a real chip (invalid-chip violation).
    positions = ["GK"] + ["DEF"] * 5 + ["MID"] * 5  # 11 players, 0 FWD
    xi_ids, bench_ids = _seed_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, positions, DEFAULT_BENCH_POSITIONS
    )
    payload = _base_payload(test_user, xi_ids, bench_ids, chip_used="not_a_real_chip")
    payload["vice_captain_id"] = xi_ids[0]

    resp = client.post("/gw_selection", json=payload)

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert "FWD count must be between 1 and 3, got 0" in errors
    assert "captain_id and vice_captain_id must be different players" in errors
    assert any("chip_used must be one of" in e for e in errors)
    assert len(errors) >= 3
