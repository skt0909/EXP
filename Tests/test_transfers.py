"""
test_transfers.py — FastAPI TestClient tests for Game_logic/transfers.py:
valid multi-transfer batches (squad_players/user_squads state changes),
the free-transfer-1-per-gameweek/wildcard-uncapped rule, position-match
and cumulative (not just per-pair) club-cap enforcement, budget-boundary
enforcement, the explicit gw_selections.is_locked pre-check (no trigger
ties transfers to that table, unlike starting_xi.py), and
multi-violation error collection.

Seeds a real user_squads/squad_players "active squad" directly via SQL
(same approach as test_starting_xi.py), plus standalone ml.players
"candidate" rows (never added to squad_players) to serve as incoming
transfer targets.
"""

import uuid

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app  # shared FastAPI app -- transfers' router is mounted on it

client = TestClient(app)

FULL_SQUAD_POSITIONS = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3  # 15


@pytest.fixture
def test_user(engine):
    """enforce_transfers_immutability_fn blocks DELETE on transfers even
    when triggered by a parent users row's ON DELETE CASCADE -- so once a
    test user has made any transfer, `DELETE FROM users` for them fails
    outright and the whole teardown rolls back. That's consistent with
    "transfers are permanent history" as a real design property, but it
    means this fixture can't clean up users/transfers the way
    test_squad_selection.py/test_starting_xi.py's fixtures do.

    Fix: give every test run a unique identity (never collides with a
    prior run's leftover row) and only clean up what's actually
    deletable -- squad_players/user_squads/gw_selections -- leaving the
    users row and any transfers rows behind permanently, by design.
    """
    unique = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
            {"e": f"pytest_transfers_{unique}@example.com", "u": f"pytest_transfers_{unique}", "p": "not_a_real_hash"},
        ).scalar()
    yield uid
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": uid})
        conn.execute(
            text("DELETE FROM squad_players WHERE user_squad_id IN (SELECT id FROM user_squads WHERE user_id = :uid)"),
            {"uid": uid},
        )
        conn.execute(text("DELETE FROM user_squads WHERE user_id = :uid"), {"uid": uid})


def _seed_full_squad(engine, make_team, make_player, user_id, season, id_offset=9000, cost=60, team_overrides=None):
    """Seeds a 2GK/5DEF/5MID/3FWD squad, one team per player by default
    (team_overrides: {squad_index: team_index} to put specific players on
    a shared team, for club-cap tests). budget_remaining starts at
    1000 - cost*15 to match what squad_selection.py would have left.

    Returns {"user_squad_id", "budget_remaining", "by_position": {...}}.
    """
    team_overrides = team_overrides or {}
    team_ids: dict[int, int] = {}
    fpl_ids = []
    for i, pos in enumerate(FULL_SQUAD_POSITIONS):
        team_idx = team_overrides.get(i, i)
        if team_idx not in team_ids:
            team_ids[team_idx] = make_team(
                fpl_id=id_offset + 1000 + team_idx, name=f"Club{team_idx}", short_name=f"C{team_idx}"
            )
        fid = id_offset + i
        make_player(fpl_id=fid, position=pos, team_id=team_ids[team_idx], cost_start=cost)
        fpl_ids.append(fid)

    budget_remaining = 1000 - cost * len(FULL_SQUAD_POSITIONS)
    with engine.begin() as conn:
        user_squad_id = conn.execute(
            text("INSERT INTO user_squads (user_id, season, budget_remaining) VALUES (:u, :s, :b) RETURNING id"),
            {"u": user_id, "s": season, "b": budget_remaining},
        ).scalar()
        for fid in fpl_ids:
            conn.execute(
                text(
                    "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                    "VALUES (:usid, :pid, :cost, TRUE)"
                ),
                {"usid": user_squad_id, "pid": fid, "cost": cost},
            )

    return {
        "user_squad_id": user_squad_id,
        "budget_remaining": budget_remaining,
        "by_position": {
            "GK": fpl_ids[0:2],
            "DEF": fpl_ids[2:7],
            "MID": fpl_ids[7:12],
            "FWD": fpl_ids[12:15],
        },
        "team_ids": team_ids,  # squad_index/team_override key -> real ml.teams.id, for club-cap tests
    }


def _add_candidate(make_team, make_player, fpl_id, position, team_fpl_id, cost):
    """Seeds an ml.players row NOT in anyone's squad_players -- a valid
    transfer target on a brand new team."""
    team_id = make_team(fpl_id=team_fpl_id, name=f"Candidate{team_fpl_id}", short_name=f"X{team_fpl_id}")
    make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=cost)
    return fpl_id


def _add_candidate_to_team(make_player, fpl_id, position, team_id, cost):
    """Seeds an ml.players row on an EXISTING team_id (e.g. one shared
    with squad members already seeded by _seed_full_squad), for tests
    that need a candidate to land on a specific, already-populated club."""
    make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=cost)
    return fpl_id


def _upsert_gw_selection(engine, user_id, season, gameweek, captain_id, vice_captain_id, chip_used=None, is_locked=False):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id, chip_used, is_locked) "
                "VALUES (:u, :s, :gw, :cap, :vc, :chip, :locked)"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "cap": captain_id, "vc": vice_captain_id, "chip": chip_used, "locked": is_locked},
        )


def _transfer_rows(engine, user_id, season, gameweek):
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT player_out_id, player_in_id, price_out, price_in, is_free FROM transfers "
                    "WHERE user_id = :u AND season = :s AND gameweek = :gw ORDER BY id"
                ),
                {"u": user_id, "s": season, "gw": gameweek},
            )
        )


def _squad_player_row(engine, user_squad_id, player_id):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT is_active, sell_price, purchase_price FROM squad_players "
                "WHERE user_squad_id = :usid AND player_id = :pid"
            ),
            {"usid": user_squad_id, "pid": player_id},
        ).first()


def _user_squad_row(engine, user_id, season):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT budget_remaining, total_transfers FROM user_squads WHERE user_id = :u AND season = :s"),
            {"u": user_id, "s": season},
        ).first()


def test_valid_multi_transfer_batch_succeeds_and_updates_state(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)  # budget_remaining=100
    out_def = squad["by_position"]["DEF"][0]
    out_mid = squad["by_position"]["MID"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=8100, position="DEF", team_fpl_id=80100, cost=50)
    cand_mid = _add_candidate(make_team, make_player, fpl_id=8101, position="MID", team_fpl_id=80101, cost=40)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_def, "player_in_id": cand_def},
                {"player_out_id": out_mid, "player_in_id": cand_mid},
            ],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    # budget: 100 (start) + 60+60 (sold) - 50-40 (bought) = 130
    assert body["budget_remaining"] == 130
    assert body["total_transfers"] == 2
    assert body["transfers"][0]["is_free"] is True
    assert body["transfers"][1]["is_free"] is False

    out_def_row = _squad_player_row(engine, squad["user_squad_id"], out_def)
    assert out_def_row.is_active is False
    assert out_def_row.sell_price == 60
    out_mid_row = _squad_player_row(engine, squad["user_squad_id"], out_mid)
    assert out_mid_row.is_active is False
    assert out_mid_row.sell_price == 60

    cand_def_row = _squad_player_row(engine, squad["user_squad_id"], cand_def)
    assert cand_def_row.is_active is True
    assert cand_def_row.purchase_price == 50
    cand_mid_row = _squad_player_row(engine, squad["user_squad_id"], cand_mid)
    assert cand_mid_row.is_active is True
    assert cand_mid_row.purchase_price == 40

    rows = _transfer_rows(engine, test_user, TEST_SEASON, 1)
    assert len(rows) == 2
    assert {(r.player_out_id, r.player_in_id) for r in rows} == {(out_def, cand_def), (out_mid, cand_mid)}


def test_locked_gameweek_rejects_whole_batch_no_partial_writes(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk_ids = squad["by_position"]["GK"]
    _upsert_gw_selection(engine, test_user, TEST_SEASON, 1, captain_id=gk_ids[0], vice_captain_id=gk_ids[1], is_locked=True)

    out_def = squad["by_position"]["DEF"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=8100, position="DEF", team_fpl_id=80100, cost=50)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_def, "player_in_id": cand_def}],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert "gameweek 1 is locked and can no longer be modified" in errors

    assert len(_transfer_rows(engine, test_user, TEST_SEASON, 1)) == 0
    out_def_row = _squad_player_row(engine, squad["user_squad_id"], out_def)
    assert out_def_row.is_active is True
    assert out_def_row.sell_price is None
    usq = _user_squad_row(engine, test_user, TEST_SEASON)
    assert usq.budget_remaining == squad["budget_remaining"]
    assert usq.total_transfers == 0


def test_self_swap_rejected(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    fwd = squad["by_position"]["FWD"][0]

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": fwd, "player_in_id": fwd}],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("player_out_id and player_in_id must differ" in e for e in errors)


def test_duplicate_player_out_id_rejected(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk = squad["by_position"]["GK"][0]
    cand_a = _add_candidate(make_team, make_player, fpl_id=8100, position="GK", team_fpl_id=80100, cost=40)
    cand_b = _add_candidate(make_team, make_player, fpl_id=8101, position="GK", team_fpl_id=80101, cost=40)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": gk, "player_in_id": cand_a},
                {"player_out_id": gk, "player_in_id": cand_b},
            ],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert f"duplicate player_out_id(s) in the same batch: [{gk}]" in errors


def test_duplicate_player_in_id_rejected(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk1, gk2 = squad["by_position"]["GK"]
    cand = _add_candidate(make_team, make_player, fpl_id=8100, position="GK", team_fpl_id=80100, cost=40)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": gk1, "player_in_id": cand},
                {"player_out_id": gk2, "player_in_id": cand},
            ],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert f"duplicate player_in_id(s) in the same batch: [{cand}]" in errors


def test_position_mismatch_rejected(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk = squad["by_position"]["GK"][0]
    cand_mid = _add_candidate(make_team, make_player, fpl_id=8100, position="MID", team_fpl_id=80100, cost=40)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": gk, "player_in_id": cand_mid}],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("must swap the same position" in e and f"{gk} (GK)" in e and f"{cand_mid} (MID)" in e for e in errors)


def test_cumulative_club_cap_violation_rejected(engine, make_team, make_player, test_user):
    """Two of the squad's 5 MID players already share a club (count=2).
    The batch transfers OUT two *other* MID players and transfers IN two
    new MID candidates from that same club. Checked pair-by-pair against
    the original squad, each swap alone would only bring that club to 3
    (fine); only the cumulative effect of both together (2 existing + 2
    incoming = 4) actually violates the cap."""
    squad = _seed_full_squad(
        engine, make_team, make_player, test_user, TEST_SEASON, cost=60,
        team_overrides={7: 50, 8: 50},  # squad indices 7,8 are the first 2 MID slots -> shared "team 50"
    )
    mid_ids = squad["by_position"]["MID"]  # 5 players; mid_ids[0], mid_ids[1] are on team 50
    out_mid_a, out_mid_b = mid_ids[2], mid_ids[3]  # NOT on team 50
    shared_team_id = squad["team_ids"][50]  # the real ml.teams.id backing "team 50"

    cand_a = _add_candidate_to_team(make_player, fpl_id=8100, position="MID", team_id=shared_team_id, cost=40)
    cand_b = _add_candidate_to_team(make_player, fpl_id=8101, position="MID", team_id=shared_team_id, cost=40)
    # cand_a/cand_b are seeded directly onto shared_team_id -- the exact
    # same physical ml.teams row mid_ids[0]/mid_ids[1] already belong to --
    # so the pre-transfer club count for that team is genuinely 2, and this
    # batch genuinely pushes it to 4.

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_mid_a, "player_in_id": cand_a},
                {"player_out_id": out_mid_b, "player_in_id": cand_b},
            ],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("max 3 players per club exceeded after transfer(s)" in e for e in errors)


def test_budget_boundary_exact_and_over(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)  # budget_remaining=100
    out_fwd = squad["by_position"]["FWD"][0]

    cand_exact = _add_candidate(make_team, make_player, fpl_id=8100, position="FWD", team_fpl_id=80100, cost=160)
    resp_exact = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_fwd, "player_in_id": cand_exact}],
        },
    )
    assert resp_exact.status_code == 200
    assert resp_exact.json()["budget_remaining"] == 0


def test_budget_one_over_boundary_fails(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)  # budget_remaining=100
    out_fwd = squad["by_position"]["FWD"][0]

    cand_over = _add_candidate(make_team, make_player, fpl_id=8100, position="FWD", team_fpl_id=80100, cost=161)
    resp_over = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_fwd, "player_in_id": cand_over}],
        },
    )
    assert resp_over.status_code == 422
    errors = resp_over.json()["detail"]
    assert any("insufficient budget" in e and "-1" in e for e in errors)


def test_wildcard_chip_makes_every_transfer_free(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk_ids = squad["by_position"]["GK"]
    _upsert_gw_selection(
        engine, test_user, TEST_SEASON, 1, captain_id=gk_ids[0], vice_captain_id=gk_ids[1], chip_used="wildcard"
    )

    out_def = squad["by_position"]["DEF"][0]
    out_mid = squad["by_position"]["MID"][0]
    out_fwd = squad["by_position"]["FWD"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=8100, position="DEF", team_fpl_id=80100, cost=50)
    cand_mid = _add_candidate(make_team, make_player, fpl_id=8101, position="MID", team_fpl_id=80101, cost=50)
    cand_fwd = _add_candidate(make_team, make_player, fpl_id=8102, position="FWD", team_fpl_id=80102, cost=50)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_def, "player_in_id": cand_def},
                {"player_out_id": out_mid, "player_in_id": cand_mid},
                {"player_out_id": out_fwd, "player_in_id": cand_fwd},
            ],
        },
    )

    assert resp.status_code == 200
    body = resp.json()
    assert all(t["is_free"] is True for t in body["transfers"])

    rows = _transfer_rows(engine, test_user, TEST_SEASON, 1)
    assert len(rows) == 3
    assert all(r.is_free is True for r in rows)


def test_multiple_simultaneous_violations_all_reported_together(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)  # budget_remaining=100
    fwd_self = squad["by_position"]["FWD"][0]
    gk = squad["by_position"]["GK"][0]
    mid_expensive_out = squad["by_position"]["MID"][0]

    cand_mid_cheap = _add_candidate(make_team, make_player, fpl_id=8100, position="MID", team_fpl_id=80100, cost=40)
    cand_mid_pricey = _add_candidate(make_team, make_player, fpl_id=8101, position="MID", team_fpl_id=80101, cost=999)

    resp = client.post(
        "/transfers",
        json={
            "user_id": test_user,
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": fwd_self, "player_in_id": fwd_self},  # self-swap
                {"player_out_id": gk, "player_in_id": cand_mid_cheap},  # position mismatch (GK -> MID)
                {"player_out_id": mid_expensive_out, "player_in_id": cand_mid_pricey},  # over budget
            ],
        },
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("player_out_id and player_in_id must differ" in e for e in errors)
    assert any("must swap the same position" in e for e in errors)
    assert any("insufficient budget" in e for e in errors)
    assert len(errors) >= 3
