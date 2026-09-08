"""
test_transfers.py — FastAPI TestClient tests for Gameplay/transfers.py:
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

from conftest import TEST_SEASON, bearer_headers
from main import app  # shared FastAPI app -- transfers' router is mounted on it
# Both from Shared/rules.py, the pure-rules module: the banking
# recurrence, driven directly with hand-built histories (reaching the cap
# through the API would cost five gameweeks of HTTP calls per case), and
# the real per-hit cost, so the worked examples below assert -4/-8/-12
# against the constant scoring actually charges rather than a literal.
from Shared.rules import HIT_COST, _free_transfers_available

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


def _add_historical_transfers(engine, user_id, season, gameweek, n, is_free=False, id_offset=91000):
    with engine.begin() as conn:
        for i in range(n):
            conn.execute(
                text(
                    "INSERT INTO transfers (user_id, season, gameweek, player_in_id, player_out_id, price_in, price_out, is_free) "
                    "VALUES (:u, :s, :gw, :pin, :pout, 50, 50, :free)"
                ),
                {
                    "u": user_id,
                    "s": season,
                    "gw": gameweek,
                    "pin": id_offset + i,
                    "pout": id_offset + 100 + i,
                    "free": is_free,
                },
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
            text("SELECT budget_remaining FROM user_squads WHERE user_id = :u AND season = :s"),
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_def, "player_in_id": cand_def},
                {"player_out_id": out_mid, "player_in_id": cand_mid},
            ],
        }, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 200
    body = resp.json()
    # budget: 100 (start) + 60+60 (sold) - 50-40 (bought) = 130
    assert body["budget_remaining"] == 130
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_def, "player_in_id": cand_def}],
        }, headers=bearer_headers(test_user)
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


def _squad_player_rows(engine, user_squad_id, player_id):
    """Every row for this (squad, player) pair -- the duplicate-detection
    counterpart to _squad_player_row's .first()."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT id, is_active, sell_price, purchase_price FROM squad_players "
                    "WHERE user_squad_id = :usid AND player_id = :pid ORDER BY id"
                ),
                {"usid": user_squad_id, "pid": player_id},
            )
        )


def test_selling_then_rebuying_the_same_player_reactivates_the_original_row(
    engine, make_team, make_player, test_user
):
    """Ordinary FPL usage: sell a player in one gameweek, buy them back in
    a later one. uq_squad_players_active is UNIQUE (user_squad_id,
    player_id) with no is_active predicate and selling only flips the row
    inactive, so a blind INSERT on the way back in collides with the
    seller's own leftover row and 500s. The rebuy must reactivate that
    single row, not add a second.
    """
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    usid = squad["user_squad_id"]
    original = squad["by_position"]["MID"][0]
    replacement = _add_candidate(
        make_team, make_player, fpl_id=8500, position="MID", team_fpl_id=80500, cost=55
    )

    # GW1: sell `original`, bring in `replacement`.
    sell = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [{"player_out_id": original, "player_in_id": replacement}],
        }, headers=bearer_headers(test_user)
    )
    assert sell.status_code == 200

    sold_rows = _squad_player_rows(engine, usid, original)
    assert len(sold_rows) == 1
    assert sold_rows[0].is_active is False
    assert sold_rows[0].sell_price == 60  # credited back what was paid
    original_row_id = sold_rows[0].id

    # GW2: change our mind and buy `original` back.
    rebuy = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 2,
            "transfers": [{"player_out_id": replacement, "player_in_id": original}],
        }, headers=bearer_headers(test_user)
    )
    assert rebuy.status_code == 200, rebuy.text

    rebought = _squad_player_rows(engine, usid, original)
    assert len(rebought) == 1, "rebuy must reactivate the original row, not insert a duplicate"
    assert rebought[0].id == original_row_id, "same physical row, reactivated in place"
    assert rebought[0].is_active is True
    assert rebought[0].sell_price is None, "stale sell_price from the earlier sale must be cleared"
    assert rebought[0].purchase_price == 60  # repriced at ml.players.cost_start now

    # The player they were swapped for is the one now sitting inactive.
    replacement_rows = _squad_player_rows(engine, usid, replacement)
    assert len(replacement_rows) == 1
    assert replacement_rows[0].is_active is False

    # Squad is still 15 strong, and both legs are on record in transfers.
    with engine.connect() as conn:
        active = conn.execute(
            text("SELECT COUNT(*) FROM squad_players WHERE user_squad_id = :usid AND is_active = TRUE"),
            {"usid": usid},
        ).scalar()
    assert active == 15
    assert len(_transfer_rows(engine, test_user, TEST_SEASON, 1)) == 1
    assert len(_transfer_rows(engine, test_user, TEST_SEASON, 2)) == 1


def test_rebuying_a_sold_player_keeps_budget_arithmetic_correct(
    engine, make_team, make_player, test_user
):
    """The upsert must not disturb the budget maths -- a round trip at
    different prices should net out to exactly the price difference."""
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    start_budget = squad["budget_remaining"]
    original = squad["by_position"]["FWD"][0]
    replacement = _add_candidate(
        make_team, make_player, fpl_id=8600, position="FWD", team_fpl_id=80600, cost=45
    )

    client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [{"player_out_id": original, "player_in_id": replacement}],
        }, headers=bearer_headers(test_user)
    )
    # Sold at 60, bought at 45 -> +15.
    assert _user_squad_row(engine, test_user, TEST_SEASON).budget_remaining == start_budget + 15

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 2,
            "transfers": [{"player_out_id": replacement, "player_in_id": original}],
        }, headers=bearer_headers(test_user)
    )
    assert resp.status_code == 200

    # Sold at 45, rebought at 60 -> back to where we started.
    usq = _user_squad_row(engine, test_user, TEST_SEASON)
    assert usq.budget_remaining == start_budget


def test_passed_deadline_rejects_transfers_with_no_gw_selection_row(
    engine, make_team, make_player, make_fixture, test_user
):
    """The is_locked flag can only exist on a submitted selection, and
    Beat's lock_expired_gameweeks only locks gameweeks that already have
    one -- so a user who never picked an XI has nothing to lock. Without
    the ml.fixtures deadline check this batch is accepted long after
    kickoff. Deliberately seeds NO gw_selections row at all.
    """
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    home = make_team(fpl_id=97001, name="DeadlineHome", short_name="DH")
    away = make_team(fpl_id=97002, name="DeadlineAway", short_name="DA")
    make_fixture(fpl_id=97010, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time="2000-01-01T12:00:00+00:00")

    out_def = squad["by_position"]["DEF"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=8300, position="DEF", team_fpl_id=80300, cost=50)

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_def, "player_in_id": cand_def}],
        }, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    assert "gameweek 1 is locked and can no longer be modified" in resp.json()["detail"]
    assert len(_transfer_rows(engine, test_user, TEST_SEASON, 1)) == 0
    assert _squad_player_row(engine, squad["user_squad_id"], out_def).is_active is True


def test_future_deadline_still_allows_transfers(engine, make_team, make_player, make_fixture, test_user):
    """Guards the fix's other direction: an ingested fixture whose
    kickoff is still ahead must NOT read as locked."""
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    home = make_team(fpl_id=97101, name="FutureHome", short_name="FH")
    away = make_team(fpl_id=97102, name="FutureAway", short_name="FA")
    make_fixture(fpl_id=97110, gameweek=1, home_team_id=home, away_team_id=away,
                 kickoff_time="2999-01-01T12:00:00+00:00")

    out_def = squad["by_position"]["DEF"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=8400, position="DEF", team_fpl_id=80400, cost=50)

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_def, "player_in_id": cand_def}],
        }, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 200
    assert len(_transfer_rows(engine, test_user, TEST_SEASON, 1)) == 1


def test_self_swap_rejected(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    fwd = squad["by_position"]["FWD"][0]

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": fwd, "player_in_id": fwd}],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": gk, "player_in_id": cand_a},
                {"player_out_id": gk, "player_in_id": cand_b},
            ],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": gk1, "player_in_id": cand},
                {"player_out_id": gk2, "player_in_id": cand},
            ],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": gk, "player_in_id": cand_mid}],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_mid_a, "player_in_id": cand_a},
                {"player_out_id": out_mid_b, "player_in_id": cand_b},
            ],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_fwd, "player_in_id": cand_exact}],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_fwd, "player_in_id": cand_over}],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_def, "player_in_id": cand_def},
                {"player_out_id": out_mid, "player_in_id": cand_mid},
                {"player_out_id": out_fwd, "player_in_id": cand_fwd},
            ],
        }, headers=bearer_headers(test_user)
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
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": fwd_self, "player_in_id": fwd_self},  # self-swap
                {"player_out_id": gk, "player_in_id": cand_mid_cheap},  # position mismatch (GK -> MID)
                {"player_out_id": mid_expensive_out, "player_in_id": cand_mid_pricey},  # over budget
            ],
        }, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("player_out_id and player_in_id must differ" in e for e in errors)
    assert any("must swap the same position" in e for e in errors)
    assert any("insufficient budget" in e for e in errors)
    assert len(errors) >= 3


def test_transfers_used_no_transfers_yet_reports_full_free_slot(test_user):
    resp = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["free_transfers_used"] == 0
    assert body["free_transfers_remaining"] == 1
    assert body["chip_active"] is False
    assert body["total_transfers_this_gameweek"] == 0


def test_transfers_used_reflects_committed_transfers(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    out_def = squad["by_position"]["DEF"][0]
    out_mid = squad["by_position"]["MID"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=8100, position="DEF", team_fpl_id=80100, cost=50)
    cand_mid = _add_candidate(make_team, make_player, fpl_id=8101, position="MID", team_fpl_id=80101, cost=40)

    client.post(
        "/transfers",
        json={
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [
                {"player_out_id": out_def, "player_in_id": cand_def},
                {"player_out_id": out_mid, "player_in_id": cand_mid},
            ],
        }, headers=bearer_headers(test_user)
    )

    resp = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["free_transfers_used"] == 1
    assert body["free_transfers_remaining"] == 0
    assert body["chip_active"] is False
    assert body["total_transfers_this_gameweek"] == 2


def test_transfers_used_wildcard_reports_uncapped_free(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk_ids = squad["by_position"]["GK"]
    _upsert_gw_selection(
        engine, test_user, TEST_SEASON, 1, captain_id=gk_ids[0], vice_captain_id=gk_ids[1], chip_used="wildcard"
    )

    resp = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(test_user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["chip_active"] is True
    assert body["free_transfers_remaining"] == 0
    assert body["free_transfers_used"] == 0


# ---------------------------------------------------------------- free-transfer banking
#
# One free transfer is earned per gameweek, unused ones roll over, and the
# bank caps at 5:
#
#     available = min(5, max(0, available_prev - used_prev) + 1)
#
# The recurrence is tested directly against hand-built histories, because
# reaching the cap through the API would take five gameweeks of HTTP calls
# per case; the endpoint tests below then confirm the wiring, and the hit
# arithmetic is asserted end-to-end through scoring.


@pytest.mark.parametrize(
    "used_by_gameweek, gameweek, expected",
    [
        # Nothing spent yet -- the bank climbs one per gameweek and stops at 5.
        ({}, 1, 1),
        ({}, 2, 2),
        ({}, 3, 3),
        ({}, 4, 4),
        ({}, 5, 5),
        ({}, 6, 5),
        ({}, 12, 5),
        ({}, 38, 5),
        # Spending exactly the allowance each week never banks anything.
        ({1: 1, 2: 1, 3: 1}, 4, 1),
        # Bank to 3, spend 2, keep 1, earn 1.
        ({1: 0, 2: 0, 3: 2}, 4, 2),
        # Overspending does not leave a debt: gameweek 2 had 2 available and
        # made 5, so gameweek 3 starts from 0 + 1, not from -3 + 1.
        ({2: 5}, 3, 1),
        # At the cap, spending all 5 drops straight back to 1.
        ({1: 0, 2: 0, 3: 0, 4: 0, 5: 5}, 6, 1),
        # At the cap, spending 1 leaves 4, +1 back to the cap.
        ({1: 0, 2: 0, 3: 0, 4: 0, 5: 1}, 6, 5),
    ],
)
def test_banking_recurrence(used_by_gameweek, gameweek, expected):
    assert _free_transfers_available(used_by_gameweek, gameweek) == expected


def test_banking_reaches_exactly_five_and_never_exceeds_it_across_a_season():
    """The cap is a ceiling, not a one-off clamp -- it has to hold for
    every subsequent gameweek too, which a single-gameweek assertion
    wouldn't catch."""
    values = [_free_transfers_available({}, gw) for gw in range(1, 39)]

    assert values[:6] == [1, 2, 3, 4, 5, 5]
    assert max(values) == 5
    assert all(v == 5 for v in values[4:])


def test_unused_gameweeks_bank_through_the_api(engine, make_team, make_player, test_user):
    """Three consecutive gameweeks with no transfers made, read back
    through GET /transfers/used rather than the pure function."""
    _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)

    for gameweek, expected in [(1, 1), (2, 2), (3, 3), (4, 4), (5, 5), (6, 5)]:
        body = client.get(
            "/transfers/used",
            params={"season": TEST_SEASON, "gameweek": gameweek}, headers=bearer_headers(test_user)
        ).json()
        assert body["free_transfers_remaining"] == expected, f"gameweek {gameweek}"


def test_spending_the_bank_rolls_the_remainder_not_a_flat_reset(
    engine, make_team, make_player, test_user
):
    """Bank to 3 across two idle gameweeks, spend 2 of them, and arrive at
    the next gameweek with 1 banked + 1 earned = 2 -- not a flat 1."""
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    mids = squad["by_position"]["MID"]
    for i in range(2):
        _add_candidate(
            make_team, make_player, fpl_id=8700 + i, position="MID", team_fpl_id=80700 + i, cost=60
        )

    # Gameweeks 1 and 2 pass untouched -> gameweek 3 opens with 3.
    gw3 = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 3}, headers=bearer_headers(test_user)
    ).json()
    assert gw3["free_transfers_remaining"] == 3

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 3,
            "transfers": [
                {"player_out_id": mids[0], "player_in_id": 8700},
                {"player_out_id": mids[1], "player_in_id": 8701},
            ],
        }, headers=bearer_headers(test_user)
    )
    assert resp.status_code == 200
    assert [t["is_free"] for t in resp.json()["transfers"]] == [True, True]  # both covered

    after = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 3}, headers=bearer_headers(test_user)
    ).json()
    assert after["free_transfers_used"] == 2
    assert after["free_transfers_remaining"] == 1  # 3 available, 2 spent

    gw4 = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 4}, headers=bearer_headers(test_user)
    ).json()
    assert gw4["free_transfers_remaining"] == 2  # 1 carried + 1 earned


def _make_n_transfers(test_user, gameweek, out_ids, in_ids):
    return client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": gameweek,
            "transfers": [
                {"player_out_id": o, "player_in_id": i} for o, i in zip(out_ids, in_ids)
            ],
        }, headers=bearer_headers(test_user)
    )


@pytest.mark.parametrize(
    "transfers_made, expected_paid, expected_points_cost",
    [
        (3, 1, 4),
        (4, 2, 8),
        (5, 3, 12),
    ],
)
def test_worked_hit_examples_with_two_free_transfers(
    engine, make_team, make_player, test_user, transfers_made, expected_paid, expected_points_cost
):
    """The rules doc's worked examples, verbatim: with 2 free transfers
    available, 3 made costs -4, 4 costs -8, 5 costs -12.

    Two free transfers is what gameweek 2 has after an untouched gameweek
    1, so this needs no setup beyond transferring in gameweek 2.
    """
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    mids = squad["by_position"]["MID"]
    candidates = [
        _add_candidate(
            make_team, make_player, fpl_id=8800 + i, position="MID", team_fpl_id=80800 + i, cost=60
        )
        for i in range(transfers_made)
    ]

    assert client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(test_user)
    ).json()["free_transfers_remaining"] == 2

    resp = _make_n_transfers(test_user, 2, mids[:transfers_made], candidates)
    assert resp.status_code == 200

    flags = [t["is_free"] for t in resp.json()["transfers"]]
    assert flags == [True, True] + [False] * expected_paid

    # What scoring.py will actually charge: HIT_COST per non-free row.
    paid = sum(1 for r in _transfer_rows(engine, test_user, TEST_SEASON, 2) if r.is_free is False)
    assert paid == expected_paid
    assert paid * HIT_COST == expected_points_cost  # -4 / -8 / -12


def test_five_banked_and_five_made_costs_nothing(engine, make_team, make_player, test_user):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    mids = squad["by_position"]["MID"]
    candidates = [
        _add_candidate(
            make_team, make_player, fpl_id=8900 + i, position="MID", team_fpl_id=80900 + i, cost=60
        )
        for i in range(5)
    ]

    # Gameweeks 1-5 untouched -> gameweek 6 sits at the cap.
    assert client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 6}, headers=bearer_headers(test_user)
    ).json()["free_transfers_remaining"] == 5

    resp = _make_n_transfers(test_user, 6, mids[:5], candidates)
    assert resp.status_code == 200
    assert all(t["is_free"] for t in resp.json()["transfers"])

    paid = sum(1 for r in _transfer_rows(engine, test_user, TEST_SEASON, 6) if r.is_free is False)
    assert paid == 0

    # And the bank drops to 1 for the next gameweek, not to 0 or back to 5.
    assert client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 7}, headers=bearer_headers(test_user)
    ).json()["free_transfers_remaining"] == 1


def test_five_banked_and_six_made_costs_exactly_four_points(
    engine, make_team, make_player, test_user
):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    outs = squad["by_position"]["MID"] + squad["by_position"]["DEF"][:1]  # 5 MID + 1 DEF
    candidates = [
        _add_candidate(
            make_team, make_player, fpl_id=9100 + i, position="MID", team_fpl_id=81100 + i, cost=60
        )
        for i in range(5)
    ] + [
        _add_candidate(
            make_team, make_player, fpl_id=9200, position="DEF", team_fpl_id=81200, cost=60
        )
    ]

    resp = _make_n_transfers(test_user, 6, outs, candidates)
    assert resp.status_code == 200
    assert [t["is_free"] for t in resp.json()["transfers"]] == [True] * 5 + [False]

    paid = sum(1 for r in _transfer_rows(engine, test_user, TEST_SEASON, 6) if r.is_free is False)
    assert paid == 1
    assert paid * 4 == 4


def test_normal_gameweek_rejects_transfer_after_twenty_already_made(
    engine, make_team, make_player, test_user
):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    _add_historical_transfers(engine, test_user, TEST_SEASON, 1, 20)
    out_def = squad["by_position"]["DEF"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=9400, position="DEF", team_fpl_id=81400, cost=60)

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_def, "player_in_id": cand_def}],
        }, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 422
    assert any("maximum 20 transfers per gameweek exceeded" in e for e in resp.json()["detail"])


def test_wildcard_gameweek_allows_more_than_twenty_transfers(
    engine, make_team, make_player, test_user
):
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    gk_ids = squad["by_position"]["GK"]
    _upsert_gw_selection(
        engine, test_user, TEST_SEASON, 1, captain_id=gk_ids[0], vice_captain_id=gk_ids[1], chip_used="wildcard"
    )
    _add_historical_transfers(engine, test_user, TEST_SEASON, 1, 20)
    out_def = squad["by_position"]["DEF"][0]
    cand_def = _add_candidate(make_team, make_player, fpl_id=9401, position="DEF", team_fpl_id=81401, cost=60)

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON,
            "gameweek": 1,
            "transfers": [{"player_out_id": out_def, "player_in_id": cand_def}],
        }, headers=bearer_headers(test_user)
    )

    assert resp.status_code == 200
    assert resp.json()["transfers"][0]["is_free"] is True


def test_a_chip_gameweek_preserves_the_bank(engine, make_team, make_player, test_user):
    """Wildcard/Free Hit transfers are free, but they do not spend the
    manager's saved free-transfer bank for the following gameweek."""
    squad = _seed_full_squad(engine, make_team, make_player, test_user, TEST_SEASON, cost=60)
    mids = squad["by_position"]["MID"]
    gk_ids = squad["by_position"]["GK"]
    candidates = [
        _add_candidate(
            make_team, make_player, fpl_id=9300 + i, position="MID", team_fpl_id=81300 + i, cost=60
        )
        for i in range(3)
    ]
    _upsert_gw_selection(
        engine, test_user, TEST_SEASON, 3, captain_id=gk_ids[0], vice_captain_id=gk_ids[1],
        chip_used="wildcard",
    )

    # Gameweek 3 would otherwise hold 3 banked transfers.
    resp = _make_n_transfers(test_user, 3, mids[:3], candidates)
    assert resp.status_code == 200
    assert all(t["is_free"] for t in resp.json()["transfers"])

    gw4 = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 4}, headers=bearer_headers(test_user)
    ).json()
    assert gw4["free_transfers_remaining"] == 4
