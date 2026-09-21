"""Phase 4b: GET /team backed by the tactical engine.

Per-player points are now COMPUTED at request time for this manager and
gameweek, not read from ml.player_gw_stats.total_points. The consistency test
below is the one that matters most: the dashboard's total must equal the
number the scoring job committed to gw_scores, because the dashboard is
explaining that number rather than producing a second opinion.
"""
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, text

from conftest import TEST_SEASON
from main import app
from Results.scoring_job import score_gameweek_tactical
from Shared.rules import RULES_VERSION
from Tests.test_scoring_job_tactical import _seed_stats, _squad_for

client = TestClient(app)

GAMEWEEK = 13
BASE = 9400


@pytest.fixture
def epoch(engine):
    made = []

    def _set(first_gameweek):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ruleset_epochs (season, rules_version, first_gameweek) "
                "VALUES (:s, :rv, :fg) ON CONFLICT DO NOTHING"),
                {"s": TEST_SEASON, "rv": RULES_VERSION, "fg": first_gameweek})
        made.append(True)

    yield _set
    if made:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE ruleset_epochs DISABLE TRIGGER enforce_ruleset_epochs_immutability"))
            conn.execute(text("DELETE FROM ruleset_epochs WHERE season = :s"), {"s": TEST_SEASON})
            conn.execute(text("ALTER TABLE ruleset_epochs ENABLE TRIGGER enforce_ruleset_epochs_immutability"))


def _get(user_id, auth_headers, gameweek=GAMEWEEK):
    resp = client.get(f"/team?season={TEST_SEASON}&gameweek={gameweek}",
                      headers=auth_headers(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


def _all_players(body):
    out = list(body["bench"])
    for pos in ("GK", "DEF", "MID", "FWD"):
        out.extend(body["lineup"][pos])
    return {p["player_id"]: p for p in out}


# ---- a hand-computed gameweek per tactic ----------------------------------

def test_balanced_manager_sees_engine_points_and_a_rule_breakdown(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    """Bonus MID scores a goal with creativity 40.
    general = 2 (appearance) + 5 (MID goal) = 7
    tactical = 1 (goal_or_assist) + 3 (creativity tier 40+) = 4"""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs,
                {7: dict(minutes=90, goals_scored=1, creativity=40)}, gameweek=GAMEWEEK)

    body = _get(uid, auth_headers)
    players = _all_players(body)
    star = players[pairs[7][0]]

    assert body["tactic"] == "balanced"
    assert star["general_points"] == 7
    assert star["tactical_points"] == 4
    assert star["is_bonus"] is True
    assert star["role"] == "starter"
    assert star["counted"] is True

    rules = {r["rule"]: r["points"] for r in star["general_breakdown"]}
    assert rules == {"appearance_60_plus": 2, "goals": 5}
    trules = {r["rule"]: r["points"] for r in star["tactical_breakdown"]}
    assert trules == {"balanced_goal_or_assist": 1, "balanced_creativity_tier": 3}

    # 10 other starters at 2 each, plus this one at 7.
    assert body["general_points"] == 27
    assert body["tactical_points"] == 4
    assert body["sub_bonus"] == 0


def test_defence_manager_with_a_swap_and_an_auto_sub(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    """Mirrors the 4a golden manager B: 41 general, 5 tactical, 1 sub bonus."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 100,
                       "defence", (2, 3), swaps=[(10, 11)], gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, {
        2: dict(minutes=90, clean_sheets=1, defensive_contributions=10),
        4: dict(minutes=0),
        6: dict(minutes=90, goals_scored=1),
        11: dict(minutes=90, goals_scored=1),
    }, gameweek=GAMEWEEK)

    body = _get(uid, auth_headers)
    players = _all_players(body)

    assert body["general_points"] == 41
    assert body["tactical_points"] == 5
    assert body["sub_bonus"] == 1

    assert players[pairs[10][0]]["role"] == "swapped_out"
    assert players[pairs[11][0]]["role"] == "swapped_in"
    assert players[pairs[4][0]]["role"] == "auto_sub_replaced"
    assert players[pairs[4][0]]["counted"] is False
    assert players[pairs[6][0]]["role"] == "auto_sub_cover"
    # The legacy flags still carry the same meaning for the current frontend.
    assert players[pairs[6][0]]["is_autosubbed_in"] is True
    assert players[pairs[4][0]]["is_autosubbed_out"] is True

    assert body["swaps"] == [{
        "player_out_id": pairs[10][0], "player_in_id": pairs[11][0],
        "general_out": 2, "general_in": 7, "sub_bonus": 1,
    }]


def test_attack_manager_with_a_bonus_no_show(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    """Mirrors the 4a golden manager C: 30 general, 6 tactical."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 200,
                       "attack", (12, 13), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, {
        12: dict(minutes=0),
        13: dict(minutes=90, goals_scored=2),
    }, gameweek=GAMEWEEK)

    body = _get(uid, auth_headers)
    players = _all_players(body)

    assert body["general_points"] == 30
    assert body["tactical_points"] == 6

    # A Bonus Player who did not appear loses Bonus status for the gameweek.
    noshow = players[pairs[12][0]]
    assert noshow["is_bonus"] is True          # he was NAMED as one
    assert noshow["tactical_points"] == 0      # but earned nothing
    assert noshow["tactical_breakdown"] == []


# ---- consistency with what the job actually wrote -------------------------

def test_the_dashboard_total_equals_gw_scores_total_points(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    """The dashboard EXPLAINS gw_scores; it must not disagree with it."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 300,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs,
                {7: dict(minutes=90, goals_scored=1, creativity=40),
                 9: dict(minutes=45, assists=1)}, gameweek=GAMEWEEK)

    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert uid in summary["scored"]

    with engine.connect() as conn:
        row = conn.execute(text(
            "SELECT raw_points, tactical_points, sub_bonus, total_points FROM gw_scores "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": uid, "s": TEST_SEASON, "g": GAMEWEEK}).first()

    body = _get(uid, auth_headers)
    assert body["general_points"] == row.raw_points
    assert body["tactical_points"] == row.tactical_points
    assert body["sub_bonus"] == row.sub_bonus
    assert body["total"] == row.total_points
    assert body["gw_points"] == row.total_points


# ---- provisional -----------------------------------------------------------

def test_provisional_is_true_while_a_fixture_is_unfinished(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 400,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)
    home = make_team(fpl_id=94001, name="ProvH", short_name="PVH")
    away = make_team(fpl_id=94002, name="ProvA", short_name="PVA")
    make_fixture(fpl_id=94010, gameweek=GAMEWEEK, home_team_id=home, away_team_id=away,
                 kickoff_time="2030-01-01T12:00:00+00:00", finished=False)

    assert _get(uid, auth_headers)["provisional"] is True

    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE ml.fixtures SET finished = TRUE WHERE season = :s AND gameweek = :g"),
            {"s": TEST_SEASON, "g": GAMEWEEK})

    assert _get(uid, auth_headers)["provisional"] is False


# ---- scored / not scored ---------------------------------------------------

def test_a_gameweek_before_the_epoch_is_reported_as_not_scored(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers, epoch
):
    epoch(GAMEWEEK + 3)
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 500,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)

    body = _get(uid, auth_headers)
    assert body["scored"] is False
    assert "precedes" in body["scored_reason"]


def test_a_fresh_user_still_gets_200_and_a_reason(engine, make_user, auth_headers):
    """The fresh-user case from Phase 1 must keep working: HTTP 200, no lineup,
    zeroes rather than an error."""
    uid = make_user()
    body = _get(uid, auth_headers)
    assert body["has_lineup"] is False
    assert body["scored"] is False
    assert body["scored_reason"] == "no selection was submitted for this gameweek"
    assert body["tactic"] is None
    # F3: an unscored gameweek returns null points, not 0. A fresh user has no
    # selection, which is one of the two unscored cases.
    assert body["general_points"] is None
    assert body["total"] is None
    assert body["score_source"] is None
    assert body["lineup"]["GK"] == [] and body["bench"] == []


# ---- the legacy keys are still present ------------------------------------

def test_every_legacy_key_is_still_present(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 600,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)
    body = _get(uid, auth_headers)

    for key in ("user_id", "username", "team_name", "season", "gameweek", "deadline",
                "has_lineup", "chip_used", "captain_multiplier", "gw_points",
                "gw_average", "season_total", "has_score", "raw_points",
                "captain_bonus", "transfer_hits", "hit_deductions", "final_total",
                "live_status", "overall_rank", "overall_rank_total",
                "team_value_available", "team_value", "bank", "lineup", "bench"):
        assert key in body, f"legacy key {key} disappeared"

    # Captain / vice / chip are present but inert until the frontend phase.
    assert body["chip_used"] is None
    # F6: an INTEGER 1, not null -- a client doing points * multiplier keeps
    # working and gets the right answer.
    assert body["captain_multiplier"] == 1
    assert body["captain_bonus"] == 0
    assert body["transfer_hits"] == 0
    assert body["hit_deductions"] == 0
    for p in _all_players(body).values():
        assert p["is_captain"] is False
        assert p["is_vice_captain"] is False


# ---- cost ------------------------------------------------------------------

def test_report_query_count_and_response_time(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers, capsys
):
    """Report only -- no threshold asserted beyond a sanity ceiling, because
    the number is a fact to record rather than a target to hit."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 700,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)

    counter = {"n": 0, "statements": []}

    def count(conn, cursor, statement, params, context, executemany):
        counter["n"] += 1
        counter["statements"].append(" ".join(statement.split())[:70])

    # The APP's engine, not this test's fixture engine. get_engine() is
    # lru_cached, so the endpoint and this listener see the same object --
    # attaching to the fixture engine instead silently counts zero, which is
    # how the first version of this test reported "0 SQL queries".
    from Shared.db_utils import get_engine
    app_engine = get_engine()
    event.listen(app_engine, "before_cursor_execute", count)
    try:
        t0 = time.perf_counter()
        body = _get(uid, auth_headers)
        elapsed_ms = (time.perf_counter() - t0) * 1000
    finally:
        event.remove(app_engine, "before_cursor_execute", count)

    with capsys.disabled():
        print(f"\n    GET /team: {counter['n']} SQL queries, {elapsed_ms:.1f} ms "
              f"(fpl_game_test, one manager, gameweek {GAMEWEEK})")

    assert body["has_lineup"] is True
    assert counter["n"] > 0, "the listener attached to the wrong engine"
    assert counter["n"] < 30, "the dashboard should not be issuing dozens of queries"


# ---- F2: score_source says where the numbers came from --------------------

def test_score_source_is_live_before_the_job_runs_and_committed_after(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 800,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)

    before = _get(uid, auth_headers)
    assert before["score_source"] == "live"
    assert before["has_score"] is False

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    after = _get(uid, auth_headers)
    assert after["score_source"] == "committed"
    assert after["has_score"] is True


# ---- F3: an unscored gameweek returns nulls, not numbers ------------------

def test_a_pre_epoch_gameweek_returns_null_points_but_keeps_the_lineup(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers, epoch
):
    """Showing points for a gameweek that will never be scored invites a
    client to render them as real. The shape stays so the lineup still draws."""
    epoch(GAMEWEEK + 3)
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 900,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs,
                {7: dict(minutes=90, goals_scored=1, creativity=40)}, gameweek=GAMEWEEK)

    body = _get(uid, auth_headers)
    assert body["scored"] is False

    for key in ("general_points", "tactical_points", "sub_bonus", "total",
                "raw_points", "final_total", "gw_points"):
        assert body[key] is None, f"{key} should be null for an unscored gameweek"

    # The lineup structure survives: 11 starters and 4 bench, still named.
    players = _all_players(body)
    assert len(players) == 15
    for p in players.values():
        assert p["points"] is None
        assert p["general_points"] is None
        assert p["tactical_points"] is None
        assert p["name"]                      # still identifiable
        assert p["position"] in ("GK", "DEF", "MID", "FWD")


def test_a_scored_gameweek_still_returns_numbers(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    """The control for the test above."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 950,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)
    body = _get(uid, auth_headers)
    assert body["scored"] is True
    assert body["general_points"] == 22
    assert all(p["points"] is not None for p in _all_players(body).values())


# ---- F6: captain_multiplier stays an integer -------------------------------

def test_captain_multiplier_is_the_integer_one_and_is_arithmetically_inert(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    """A null here would break any client doing points * multiplier. 1 keeps
    that arithmetic correct and captaincy removed at the same time."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 1000,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs,
                {7: dict(minutes=90, goals_scored=1)}, gameweek=GAMEWEEK)

    body = _get(uid, auth_headers)
    assert body["captain_multiplier"] == 1
    assert isinstance(body["captain_multiplier"], int)
    assert not isinstance(body["captain_multiplier"], bool)
    assert body["chip_used"] is None

    for p in _all_players(body).values():
        assert p["is_captain"] is False
        assert p["is_vice_captain"] is False
        assert p["points"] * body["captain_multiplier"] == p["points"]


# ---- H: team_value_available means a finance row exists -------------------

def test_team_value_available_is_false_for_a_fresh_user(engine, make_user, auth_headers):
    uid = make_user()
    body = _get(uid, auth_headers)
    assert body["team_value_available"] is False


def test_team_value_available_is_true_once_a_finance_row_exists(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, auth_headers
):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, BASE + 1100,
                       "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)

    assert _get(uid, auth_headers)["team_value_available"] is False

    # The scoring job writes the finance snapshot.
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    body = _get(uid, auth_headers)
    assert body["team_value_available"] is True
    assert body["bank"] == 3.5           # budget_remaining 35 tenths
    assert body["team_value"] == 75.0    # 15 players at 50 tenths
