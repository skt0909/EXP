"""Phase 3: the tactical selection endpoints, against a real database.

Covers what the pure validator tests cannot: that POST /gw_selection persists
tactic, starting_xi (slot + is_bonus) and tactical_swaps in ONE transaction,
that GET /gw_selection plays all of it back, that the deadline lock still
rejects a late submission, and that the DEFERRED constraint triggers added by
migration d58b3f10a7c2 refuse bad data at COMMIT.

The rules themselves are tested without a database in
backend/Tests/unit/test_selection_rules.py. This file deliberately does not
re-test them one by one; it checks the wiring and the two things only Postgres
can answer -- the triggers, and the single-transaction delete-and-reinsert.

REPLACES the captain/vice/chip selection tests in test_starting_xi.py that
could not survive the migration, listed in PHASE3_REPORT.md.
"""
import uuid
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON
from main import app
from Gameplay.starting_xi import LOCKED_DETAIL

client = TestClient(app)

GAMEWEEK = 7
# 2 GK / 5 DEF / 5 MID / 3 FWD, laid out so the position of an id is obvious.
SQUAD_SHAPE = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
BASE_FPL_ID = 7700


@pytest.fixture
def squad(engine, make_team, make_player, make_fixture, make_user, auth_headers):
    """A user with an active 15, every club playing once this gameweek.

    Each player gets his own club so the 3-per-club rule can never interfere,
    and every club's fixture kicks off at the same time -- a test that needs a
    later kickoff moves one fixture itself.
    """
    user_id = make_user()
    players, teams = [], []
    for i, position in enumerate(SQUAD_SHAPE):
        team = make_team(fpl_id=6600 + i, name=f"T{i}", short_name=f"T{i:02d}")
        teams.append(team)
        players.append(make_player(fpl_id=BASE_FPL_ID + i, position=position,
                                   team_id=team, cost_start=50))

    # One fixture per club, all kicking off together.
    for i in range(0, len(teams) - 1, 2):
        make_fixture(fpl_id=8800 + i, gameweek=GAMEWEEK,
                     home_team_id=teams[i], away_team_id=teams[i + 1],
                     kickoff_time=_now_plus(engine, days=3))
    make_fixture(fpl_id=8899, gameweek=GAMEWEEK,
                 home_team_id=teams[-1], away_team_id=teams[0],
                 kickoff_time=_now_plus(engine, days=3))

    with engine.begin() as conn:
        squad_id = conn.execute(text(
            "INSERT INTO user_squads (user_id, season, budget_remaining) "
            "VALUES (:u, :s, 0) RETURNING id"), {"u": user_id, "s": TEST_SEASON}).scalar()
        for i in range(len(SQUAD_SHAPE)):
            conn.execute(text(
                "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                "VALUES (:sq, :p, 50, TRUE)"), {"sq": squad_id, "p": BASE_FPL_ID + i})

    ids = [BASE_FPL_ID + i for i in range(len(SQUAD_SHAPE))]
    yield {
        "user_id": user_id,
        "headers": auth_headers(user_id),
        "gk": ids[0:2], "def": ids[2:7], "mid": ids[7:12], "fwd": ids[12:15],
        "all": ids,
    }

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM gw_selections WHERE user_id = :u"), {"u": user_id})


def _now_plus(engine, **kw):
    """A timestamp relative to the DATABASE's clock, not Python's -- every
    deadline comparison in this app runs as SQL now()."""
    delta = timedelta(**kw)
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT now() + make_interval(secs => :s)"),
            {"s": delta.total_seconds()},
        ).scalar()


def _payload(squad, **kw):
    """1-4-4-2, Balanced, Bonus = the first two midfielders."""
    base = dict(
        season=TEST_SEASON,
        gameweek=GAMEWEEK,
        tactic="balanced",
        player_ids=squad["gk"][:1] + squad["def"][:4] + squad["mid"][:4] + squad["fwd"][:2],
        bench_order=[squad["gk"][1], squad["def"][4], squad["mid"][4], squad["fwd"][2]],
        bonus_player_ids=squad["mid"][:2],
        swaps=[],
    )
    base.update(kw)
    return base


# ---- POST, the happy path --------------------------------------------------

def test_a_valid_selection_is_accepted_and_echoed_back(squad):
    resp = client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["tactic"] == "balanced"
    assert body["bonus_player_ids"] == squad["mid"][:2]
    assert len(body["player_ids"]) == 11
    assert len(body["bench_order"]) == 4


def test_the_tactic_and_bonus_flags_are_persisted(engine, squad):
    client.post("/gw_selection", json=_payload(squad, tactic="defence",
                                               bonus_player_ids=squad["def"][:2]),
                headers=squad["headers"])
    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT id, tactic FROM gw_selections "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": squad["user_id"], "s": TEST_SEASON, "g": GAMEWEEK}).first()
        assert row.tactic == "defence"
        bonus = [r.player_id for r in conn.execute(text(
            "SELECT player_id FROM starting_xi WHERE gw_selection_id = :i AND is_bonus "
            "ORDER BY position_slot"), {"i": row.id})]
        assert bonus == squad["def"][:2]


def test_slots_one_to_fifteen_are_written_in_submission_order(engine, squad):
    payload = _payload(squad)
    client.post("/gw_selection", json=payload, headers=squad["headers"])
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT sx.player_id, sx.position_slot, sx.role FROM starting_xi sx "
            "JOIN gw_selections gs ON gs.id = sx.gw_selection_id "
            "WHERE gs.user_id = :u AND gs.season = :s AND gs.gameweek = :g "
            "ORDER BY sx.position_slot"),
            {"u": squad["user_id"], "s": TEST_SEASON, "g": GAMEWEEK}).all()
    assert [r.position_slot for r in rows] == list(range(1, 16))
    assert [r.player_id for r in rows] == payload["player_ids"] + payload["bench_order"]
    # `role` is GENERATED from the slot, so this also pins the bench map.
    assert [r.role for r in rows[10:]] == ["starter", "auto_gk", "auto_outfield", "tactical", "tactical"]


def test_resubmitting_replaces_the_previous_selection_rather_than_appending(engine, squad):
    client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    resp = client.post("/gw_selection",
                       json=_payload(squad, tactic="attack", bonus_player_ids=squad["fwd"][:2]),
                       headers=squad["headers"])
    assert resp.status_code == 200, resp.text
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM starting_xi sx JOIN gw_selections gs ON gs.id = sx.gw_selection_id "
            "WHERE gs.user_id = :u AND gs.season = :s AND gs.gameweek = :g"),
            {"u": squad["user_id"], "s": TEST_SEASON, "g": GAMEWEEK}).scalar()
    assert n == 15          # not 30


# ---- swaps -----------------------------------------------------------------

def _late_kickoff(engine, squad, player_id, days=5):
    """Move the club of `player_id` to a later kickoff, so a swap into him is
    legal. Updating ml.fixtures.kickoff_time is allowed -- the identity trigger
    guards only the team ids and fpl_id."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ml.fixtures SET kickoff_time = :k "
            "WHERE season = :s AND gameweek = :g AND (home_team_id = ("
            "  SELECT team_id FROM ml.players WHERE season = :s AND fpl_id = :p) "
            "OR away_team_id = ("
            "  SELECT team_id FROM ml.players WHERE season = :s AND fpl_id = :p))"),
            {"k": _now_plus(engine, days=days), "s": TEST_SEASON,
             "g": GAMEWEEK, "p": player_id})


def test_a_legal_swap_is_persisted(engine, squad):
    tactical_mid = squad["mid"][4]          # bench slot 14
    _late_kickoff(engine, squad, tactical_mid)
    payload = _payload(squad, swaps=[{"player_out_id": squad["mid"][2],
                                      "player_in_id": tactical_mid}])
    resp = client.post("/gw_selection", json=payload, headers=squad["headers"])
    assert resp.status_code == 200, resp.text
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT ts.player_out_id, ts.player_in_id FROM tactical_swaps ts "
            "JOIN gw_selections gs ON gs.id = ts.gw_selection_id "
            "WHERE gs.user_id = :u AND gs.season = :s AND gs.gameweek = :g"),
            {"u": squad["user_id"], "s": TEST_SEASON, "g": GAMEWEEK}).all()
    assert [(r.player_out_id, r.player_in_id) for r in rows] == [
        (squad["mid"][2], tactical_mid)]


def test_three_swaps_are_rejected(engine, squad):
    for pid in (squad["mid"][4], squad["fwd"][2], squad["def"][4]):
        _late_kickoff(engine, squad, pid)
    payload = _payload(squad, swaps=[
        {"player_out_id": squad["mid"][3], "player_in_id": squad["mid"][4]},
        {"player_out_id": squad["fwd"][1], "player_in_id": squad["fwd"][2]},
        {"player_out_id": squad["def"][3], "player_in_id": squad["def"][4]},
    ])
    resp = client.post("/gw_selection", json=payload, headers=squad["headers"])
    assert resp.status_code == 422
    assert any("at most 2" in e for e in resp.json()["detail"])


def test_resubmitting_without_swaps_clears_the_previous_ones(engine, squad):
    tactical_mid = squad["mid"][4]
    _late_kickoff(engine, squad, tactical_mid)
    client.post("/gw_selection",
                json=_payload(squad, swaps=[{"player_out_id": squad["mid"][3],
                                             "player_in_id": tactical_mid}]),
                headers=squad["headers"])
    client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM tactical_swaps ts JOIN gw_selections gs ON gs.id = ts.gw_selection_id "
            "WHERE gs.user_id = :u AND gs.season = :s AND gs.gameweek = :g"),
            {"u": squad["user_id"], "s": TEST_SEASON, "g": GAMEWEEK}).scalar()
    assert n == 0


# ---- validation reaches the endpoint --------------------------------------

def test_an_invalid_selection_returns_every_error_at_once(squad):
    resp = client.post("/gw_selection", json=_payload(
        squad, tactic="attack", bonus_player_ids=[squad["mid"][0]]),
        headers=squad["headers"])
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and len(detail) >= 2


def test_five_two_three_is_rejected_by_the_endpoint(squad):
    # The one formation decision D1 removes.
    resp = client.post("/gw_selection", json=_payload(
        squad,
        player_ids=squad["gk"][:1] + squad["def"][:5] + squad["mid"][:2] + squad["fwd"][:3],
        bench_order=[squad["gk"][1], squad["mid"][2], squad["mid"][3], squad["mid"][4]],
        bonus_player_ids=squad["mid"][:2]),
        headers=squad["headers"])
    assert resp.status_code == 422
    assert any("MID" in e for e in resp.json()["detail"])


# ---- the deadline lock -----------------------------------------------------

def test_a_submission_after_the_deadline_is_rejected(engine, squad):
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ml.fixtures SET kickoff_time = now() - make_interval(hours => 2) "
            "WHERE season = :s AND gameweek = :g"), {"s": TEST_SEASON, "g": GAMEWEEK})
    resp = client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    assert resp.status_code == 422
    assert resp.json()["detail"] == LOCKED_DETAIL


# ---- GET -------------------------------------------------------------------

def test_get_plays_back_everything_that_was_submitted(engine, squad):
    tactical_mid = squad["mid"][4]
    _late_kickoff(engine, squad, tactical_mid)
    payload = _payload(squad, tactic="defence", bonus_player_ids=squad["def"][:2],
                       swaps=[{"player_out_id": squad["mid"][2], "player_in_id": tactical_mid}])
    client.post("/gw_selection", json=payload, headers=squad["headers"])

    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                      headers=squad["headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["has_selection"] is True
    assert body["tactic"] == "defence"
    assert body["player_ids"] == payload["player_ids"]
    assert body["bench_order"] == payload["bench_order"]
    assert sorted(body["bonus_player_ids"]) == sorted(squad["def"][:2])
    assert body["swaps"] == [{"player_out_id": squad["mid"][2], "player_in_id": tactical_mid}]


def test_get_reports_no_selection_rather_than_404(squad):
    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek=33",
                      headers=squad["headers"])
    assert resp.status_code == 200
    body = resp.json()
    assert body["has_selection"] is False
    assert body["tactic"] is None
    assert body["swaps"] == []


# ---- fixture_windows, added for the Starting XI eligibility panel ----------
#
# Gameplay/lineup.py's _fixture_windows() imports last_fixture_end from
# selection_rules.py -- the SAME function validate_selection uses for the
# swap-timing rule -- rather than re-deriving "when a player's gameweek
# ends". These tests exist to catch the two definitions drifting apart, not
# just to check the new field's shape.

def test_fixture_windows_present_for_every_squad_player_with_a_normal_fixture(squad):
    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                      headers=squad["headers"])
    windows = resp.json()["fixture_windows"]
    assert set(int(k) for k in windows) == set(squad["all"])
    # squad's own fixture builder puts every club's single kickoff 3 days out
    # and FIXTURE_DURATION_MIN is 115 minutes -- last_end is first_kickoff
    # plus exactly that, for a single fixture.
    sample = windows[str(squad["mid"][0])]
    first = datetime.fromisoformat(sample["first_kickoff"])
    last = datetime.fromisoformat(sample["last_end"])
    assert last - first == timedelta(minutes=115)


def test_fixture_windows_blank_gameweek_player_is_simply_absent(engine, squad):
    blank = squad["fwd"][2]
    with engine.begin() as conn:
        conn.execute(text(
            "DELETE FROM ml.fixtures WHERE season = :s AND gameweek = :g AND ("
            "home_team_id = (SELECT team_id FROM ml.players WHERE season = :s AND fpl_id = :p) "
            "OR away_team_id = (SELECT team_id FROM ml.players WHERE season = :s AND fpl_id = :p))"),
            {"s": TEST_SEASON, "g": GAMEWEEK, "p": blank})

    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                      headers=squad["headers"])
    windows = resp.json()["fixture_windows"]
    assert str(blank) not in windows
    # Everyone else is untouched.
    assert str(squad["fwd"][0]) in windows


def test_fixture_windows_double_gameweek_uses_earliest_kickoff_and_latest_end(engine, squad, make_team, make_fixture):
    double_gw_player = squad["def"][3]
    # A second fixture for the same club, later than the one `squad` already
    # seeded -- last_end must track THIS one, not the first.
    second_opponent = make_team(fpl_id=9601, name="SecondOpponent", short_name="SOP")
    with engine.connect() as conn:
        team_id = conn.execute(text(
            "SELECT team_id FROM ml.players WHERE season = :s AND fpl_id = :p"),
            {"s": TEST_SEASON, "p": double_gw_player}).scalar()
    later_kickoff = _now_plus(engine, days=6)
    make_fixture(fpl_id=8950, gameweek=GAMEWEEK, home_team_id=team_id,
                away_team_id=second_opponent, kickoff_time=later_kickoff)

    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                      headers=squad["headers"])
    window = resp.json()["fixture_windows"][str(double_gw_player)]
    first = datetime.fromisoformat(window["first_kickoff"])
    last_end = datetime.fromisoformat(window["last_end"])

    # first_kickoff is the EARLIER of his two fixtures (the one `squad` seeded
    # at +3 days), not the later one just added.
    assert first < later_kickoff
    # last_end tracks the LATER fixture's end, not the earlier one's.
    assert last_end == later_kickoff + timedelta(minutes=115)


def test_deadline_passed_reflects_shared_deadlines_check(engine, squad):
    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                      headers=squad["headers"])
    assert resp.json()["deadline_passed"] is False  # squad's fixtures kick off 3 days out

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ml.fixtures SET kickoff_time = :k WHERE season = :s AND gameweek = :g"),
            {"k": _now_plus(engine, hours=-2), "s": TEST_SEASON, "g": GAMEWEEK})

    resp = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                      headers=squad["headers"])
    assert resp.json()["deadline_passed"] is True


def test_a_pair_fixture_windows_marks_eligible_is_accepted_by_post(engine, squad):
    """The drift check, accept side: derive eligibility from fixture_windows
    exactly as the frontend's computeSwapEligibility does (incoming's
    first_kickoff strictly after outgoing's last_end), then confirm
    POST /gw_selection agrees."""
    tactical_mid = squad["mid"][4]
    _late_kickoff(engine, squad, tactical_mid)
    outgoing = squad["mid"][2]

    windows = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                        headers=squad["headers"]).json()["fixture_windows"]
    incoming_kickoff = datetime.fromisoformat(windows[str(tactical_mid)]["first_kickoff"])
    outgoing_end = datetime.fromisoformat(windows[str(outgoing)]["last_end"])
    assert incoming_kickoff > outgoing_end  # eligible per the client-side rule

    payload = _payload(squad, swaps=[{"player_out_id": outgoing, "player_in_id": tactical_mid}])
    resp = client.post("/gw_selection", json=payload, headers=squad["headers"])
    assert resp.status_code == 200, resp.text


def test_a_pair_fixture_windows_marks_ineligible_is_rejected_by_post(squad):
    """The drift check, reject side: squad's own fixture builder kicks every
    club off at the SAME instant, so by the strictly-after rule no pair is
    eligible without _late_kickoff -- confirm the server agrees."""
    tactical_mid = squad["mid"][4]  # untouched: same kickoff as everyone else
    outgoing = squad["mid"][2]

    windows = client.get(f"/gw_selection?season={TEST_SEASON}&gameweek={GAMEWEEK}",
                        headers=squad["headers"]).json()["fixture_windows"]
    incoming_kickoff = datetime.fromisoformat(windows[str(tactical_mid)]["first_kickoff"])
    outgoing_end = datetime.fromisoformat(windows[str(outgoing)]["last_end"])
    assert incoming_kickoff <= outgoing_end  # ineligible per the client-side rule

    payload = _payload(squad, swaps=[{"player_out_id": outgoing, "player_in_id": tactical_mid}])
    resp = client.post("/gw_selection", json=payload, headers=squad["headers"])
    assert resp.status_code == 422
    assert any("must be after" in e for e in resp.json()["detail"])


# ---- the DEFERRED triggers, which only Postgres can answer -----------------

def _selection_id(engine, squad):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT id FROM gw_selections WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": squad["user_id"], "s": TEST_SEASON, "g": GAMEWEEK}).scalar()


def test_the_trigger_refuses_three_bonus_rows_at_commit(engine, squad):
    client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    sel_id = _selection_id(engine, squad)
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE starting_xi SET is_bonus = TRUE WHERE gw_selection_id = :i "
                "AND player_id = :p"), {"i": sel_id, "p": squad["mid"][2]})
    assert "exactly 2 Bonus Players" in str(exc.value)


def test_the_trigger_refuses_a_bonus_player_on_the_bench(engine, squad):
    client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    sel_id = _selection_id(engine, squad)
    # ck_starting_xi_bonus_is_starter is a plain CHECK, so it fires immediately.
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE starting_xi SET is_bonus = TRUE WHERE gw_selection_id = :i "
                "AND position_slot = 15"), {"i": sel_id})
    assert "ck_starting_xi_bonus_is_starter" in str(exc.value)


def test_the_trigger_refuses_a_swap_whose_incoming_player_is_not_a_tactical_sub(engine, squad):
    client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    sel_id = _selection_id(engine, squad)
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO tactical_swaps (gw_selection_id, player_out_id, player_in_id) "
                "VALUES (:i, :out, :inn)"),
                {"i": sel_id, "out": squad["mid"][3], "inn": squad["def"][4]})
    assert "must be a Tactical Sub" in str(exc.value)


def test_the_trigger_refuses_a_swap_whose_outgoing_player_is_a_bonus_player(engine, squad):
    client.post("/gw_selection", json=_payload(squad), headers=squad["headers"])
    sel_id = _selection_id(engine, squad)
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO tactical_swaps (gw_selection_id, player_out_id, player_in_id) "
                "VALUES (:i, :out, :inn)"),
                {"i": sel_id, "out": squad["mid"][0], "inn": squad["mid"][4]})
    assert "non-Bonus starter" in str(exc.value)
