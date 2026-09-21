"""Phase 3: transfers under the tactical rules.

Replaces the hit/banking tests in test_transfers.py that encoded rules this
game no longer has. What changed:

  * the bank caps at FREE_TRANSFER_BANK_CAP (2), not 5
  * there are NO paid transfers and NO hits -- a transfer beyond the
    allowance is REJECTED with a 422 rather than charged 4 points
  * chips are gone, so nothing bypasses the allowance any more

Budget and the 3-per-club limit are unchanged and still enforced; the tests
for those live in test_transfers.py and were left alone.

Every replaced test is listed in PHASE3_REPORT.md beside the test here that
covers the same ground.
"""
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON
from main import app
from Shared.rules import FREE_TRANSFER_BANK_CAP

client = TestClient(app)

GAMEWEEK = 4
BASE = 7300
SHAPE = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3


@pytest.fixture
def squad(engine, make_team, make_player, make_fixture, make_user, auth_headers):
    """An active 15 plus 5 spare midfielders to transfer in, all cheap enough
    that budget never interferes with an allowance test."""
    user_id = make_user()
    teams = [make_team(fpl_id=6300 + i, name=f"X{i}", short_name=f"X{i:02d}")
             for i in range(len(SHAPE) + 5)]
    for i, position in enumerate(SHAPE):
        make_player(fpl_id=BASE + i, position=position, team_id=teams[i], cost_start=50)
    spares = []
    for j in range(5):
        pid = BASE + 100 + j
        make_player(fpl_id=pid, position="MID", team_id=teams[len(SHAPE) + j], cost_start=50)
        spares.append(pid)

    make_fixture(fpl_id=8300, gameweek=GAMEWEEK, home_team_id=teams[0],
                 away_team_id=teams[1], kickoff_time=_future(engine))

    with engine.begin() as conn:
        squad_id = conn.execute(text(
            "INSERT INTO user_squads (user_id, season, budget_remaining) "
            "VALUES (:u, :s, 500) RETURNING id"),
            {"u": user_id, "s": TEST_SEASON}).scalar()
        for i in range(len(SHAPE)):
            conn.execute(text(
                "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                "VALUES (:sq, :p, 50, TRUE)"), {"sq": squad_id, "p": BASE + i})

    ids = [BASE + i for i in range(len(SHAPE))]
    yield {"user_id": user_id, "headers": auth_headers(user_id),
           "mid": ids[7:12], "spares": spares}


def _future(engine, days=3):
    with engine.connect() as conn:
        return conn.execute(text("SELECT now() + make_interval(days => :d)"),
                            {"d": days}).scalar()


def _post(squad, pairs, gameweek=GAMEWEEK):
    return client.post("/transfers", json={
        "season": TEST_SEASON, "gameweek": gameweek,
        "transfers": [{"player_out_id": o, "player_in_id": i} for o, i in pairs],
    }, headers=squad["headers"])


# ---- the allowance ---------------------------------------------------------

def test_one_free_transfer_in_the_first_gameweek(squad):
    resp = client.get(f"/transfers/used?season={TEST_SEASON}&gameweek=1",
                      headers=squad["headers"])
    assert resp.status_code == 200
    assert resp.json()["free_transfers_remaining"] == 1


def test_the_bank_caps_at_two_not_five(squad):
    # An empty history replays the recurrence to the cap. Under the classic
    # rules this reached 5 by gameweek 6; the tactical cap is 2.
    resp = client.get(f"/transfers/used?season={TEST_SEASON}&gameweek=6",
                      headers=squad["headers"])
    assert resp.json()["free_transfers_remaining"] == FREE_TRANSFER_BANK_CAP == 2


def test_a_single_transfer_within_the_allowance_is_accepted_and_is_free(squad):
    resp = _post(squad, [(squad["mid"][0], squad["spares"][0])])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["transfers"]) == 1
    assert body["transfers"][0]["is_free"] is True


def test_two_transfers_are_accepted_once_the_bank_has_reached_two(squad):
    resp = _post(squad, [(squad["mid"][0], squad["spares"][0]),
                         (squad["mid"][1], squad["spares"][1])], gameweek=6)
    assert resp.status_code == 200, resp.text
    assert [t["is_free"] for t in resp.json()["transfers"]] == [True, True]


# ---- no paid transfers, no hits -------------------------------------------

def test_a_transfer_beyond_the_allowance_is_rejected_not_charged(squad):
    # Two transfers in gameweek 1, where the allowance is 1. Under the classic
    # rules this succeeded and cost 4 points; now it is refused outright.
    resp = _post(squad, [(squad["mid"][0], squad["spares"][0]),
                         (squad["mid"][1], squad["spares"][1])], gameweek=1)
    assert resp.status_code == 422
    assert any("no paid transfers" in e for e in resp.json()["detail"])


def test_the_rejection_names_how_many_were_available(squad):
    resp = _post(squad, [(squad["mid"][0], squad["spares"][0]),
                         (squad["mid"][1], squad["spares"][1])], gameweek=1)
    detail = " ".join(resp.json()["detail"])
    assert "only 1 free transfer" in detail


def test_a_rejected_batch_writes_nothing(engine, squad):
    _post(squad, [(squad["mid"][0], squad["spares"][0]),
                  (squad["mid"][1], squad["spares"][1])], gameweek=1)
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM transfers WHERE user_id = :u AND season = :s"),
            {"u": squad["user_id"], "s": TEST_SEASON}).scalar()
    assert n == 0


def test_spending_the_allowance_then_transferring_again_is_rejected(squad):
    # Gameweek 1 deliberately: its allowance is 1, so the second request has
    # nothing left. By gameweek 4 the bank has reached 2 and both would fit.
    assert _post(squad, [(squad["mid"][0], squad["spares"][0])], gameweek=1).status_code == 200
    second = _post(squad, [(squad["mid"][1], squad["spares"][1])], gameweek=1)
    assert second.status_code == 422
    assert any("only 0 free transfer" in e for e in second.json()["detail"])


def test_no_transfer_row_is_ever_stamped_paid(engine, squad):
    _post(squad, [(squad["mid"][0], squad["spares"][0])])
    with engine.connect() as conn:
        paid = conn.execute(text(
            "SELECT count(*) FROM transfers WHERE user_id = :u AND season = :s "
            "AND is_free = FALSE"), {"u": squad["user_id"], "s": TEST_SEASON}).scalar()
    assert paid == 0


# ---- the rules that did NOT change ----------------------------------------

def test_the_deadline_still_locks_transfers(engine, squad):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ml.fixtures SET kickoff_time = now() - make_interval(hours => 2) "
            "WHERE season = :s AND gameweek = :g"), {"s": TEST_SEASON, "g": GAMEWEEK})
    resp = _post(squad, [(squad["mid"][0], squad["spares"][0])])
    assert resp.status_code == 422


def test_transferring_in_a_player_you_already_own_is_still_rejected(squad):
    resp = _post(squad, [(squad["mid"][0], squad["mid"][1])])
    assert resp.status_code == 422
