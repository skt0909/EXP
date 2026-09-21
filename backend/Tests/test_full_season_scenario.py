"""
test_full_season_scenario.py — the one file that runs BOTH game modes plus
the ML/chat layer against each other's output, in one continuous HTTP-level
season narrative.

Every other scenario file stays single-mode: test_gameweek_lifecycle.py
walks Classic FPL alone, test_dream11.py exercises Dream11 alone. Per-module
unit tests already cover formation/budget/chip-boundary rules, autosub
mechanics, and pricing math in isolation -- this file deliberately does NOT
re-prove any of that. Its assertions are weighted toward cross-module wiring
those files structurally cannot reach: a player who exists in both games at
once, the real Beat-equivalent lock/finalize functions run back to back with
real HTTP traffic around them, and the fpl_id<->internal-id translation
followed all the way into a mocked Groq prompt.

MAINTENANCE CHECKLIST -- read this before touching any of the following,
and re-derive the specific affected assertion by hand rather than leaving a
vague "this might need updating" note:

  * Scoring rules -- Shared/rules.py or Game_logic/dream11_scoring.py
    (clean-sheet minute thresholds, assist point values, captain
    multipliers, hit/deduction amounts). The centerpiece phase below
    hand-computes Classic vs. Dream11 point totals from the CURRENT values
    of these constants (60-minute vs. 54-minute clean-sheet threshold, 3
    vs. 20 points per assist). A rule change means those numbers must be
    re-derived by hand before this test is edited.
  * Schema/migrations -- any change to classic or dream11 tables, FKs, or
    cascade behavior. Teardown here relies on ml.fixtures deletion
    cascading to dream11.contests -> members/teams/prices; a schema change
    could silently orphan rows or break the second-run-clean check.
  * conftest.py fixtures -- any signature change to make_team, make_player,
    make_fixture, make_gw_stat, make_user, bearer_headers. This file
    depends on their exact current behavior, including the hardcoded
    TEST_SEASON inside them.
  * API contracts -- request/response shape, status codes, or field names
    for /auth/register, /auth/login, /auth/me, /chat, /team,
    /dream11/contests/{id}/team, transfer endpoints, or squad-selection
    endpoints.
  * Named constants -- MODEL_VERSION, is_chat_available,
    CHAT_AVAILABILITY_MESSAGE in Context_assembler/main.py, or the tier
    vocabulary used by ml.ml_predictions.

THE SHARED FIXTURE. Classic FPL's gameweek 1 and the Dream11 contest are
deliberately backed by the SAME ml.fixtures row: a teamed fixture whose
gameweek is 1. That is what lets one ml.player_gw_stats row (for
DUAL_ID_PLAYER, seeded once in the centerpiece phase below) be read by BOTH
scoring engines at once -- Results/scoring.py sums by (player_id, season,
gameweek) with no fixture filter, while Game_logic/dream11_scoring.py filters
on this exact fixture_id. Crossing gameweek 1's deadline (a plain UPDATE of
this fixture's kickoff_time) therefore also puts the Dream11 fixture's own
kickoff in the past -- which is harmless, since nothing flips
dream11.contests.is_locked except explicitly calling lock_started_contests,
done later and separately from the classic lock.

NO MOCKS except Groq (an external API) -- same policy as
test_gameweek_lifecycle.py and test_dream11.py. Deadlines are crossed by
UPDATE-ing ml.fixtures.kickoff_time into the past, never by a Python clock
library, since every deadline comparison happens in Postgres.
"""

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
import main
from main import app as fastapi_app
from GameEngine.gameweek_finalize import refresh_active_gameweeks
from GameEngine.gameweek_lock import lock_expired_gameweeks
from Results.scoring_job import score_gameweek_tactical  # noqa: F401 -- imported for readers tracing the wiring, called via refresh_active_gameweeks
from dream11_locking import lock_started_contests
from dream11_scoring import (
    finalize_dream11_contest,
    find_contests_needing_finalization,
    score_dream11_contest,
)

client = TestClient(fastapi_app)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 -- matches test_gameweek_lifecycle.py's convention

# ---------------------------------------------------------------- ids
#
# Every numeric range below is disjoint from every other, so nothing here
# can collide with itself. Ranges are NOT required to avoid other test
# files' numbers -- TEST_SEASON is wiped clean before and after this whole
# function by conftest's autouse fixture, and tests run serially.

# Manager A's classic squad: 2 GK / 5 DEF / 5 MID / 3 FWD.
SQUAD_A_POSITIONS = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
SQUAD_A_OFFSET = 9600
SQUAD_A_TEAM_OFFSET = 8600
PLAYER_COST = 60  # tenths of GBP -- 15 * 60 = 900 of the 1000 cap, 100 in the bank

XI_FPL_IDS_A = [9600, 9602, 9603, 9604, 9605, 9607, 9608, 9609, 9610, 9612, 9613]
BENCH_FPL_IDS_A = [9606, 9611, 9601, 9614]  # bench GK deliberately third, same guard as precedent
CAPTAIN_FPL_ID_A = 9607
VICE_FPL_ID_A = 9608

# The player who lives in both games at once: a DEF in manager_a's classic
# XI whose CLUB is the Dream11 fixture's home team, not its own individual
# club like every other squad-A player.
DUAL_ID_FPL = 9602

# Manager B's classic squad -- same shape, its own disjoint range, no dual
# player. Exists only so the league table has two real rows to rank.
SQUAD_B_POSITIONS = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
SQUAD_B_OFFSET = 9620
SQUAD_B_TEAM_OFFSET = 8620

XI_FPL_IDS_B = [9620, 9622, 9623, 9624, 9625, 9627, 9628, 9629, 9630, 9632, 9633]
BENCH_FPL_IDS_B = [9626, 9631, 9621, 9634]
CAPTAIN_FPL_ID_B = 9627
VICE_FPL_ID_B = 9628

# Transfer candidates: gameweek 1's swap (manager_a) and gameweek 2's
# free-hit swap (manager_a), on their own clubs.
CANDIDATE_GW1_DEF = 9700  # replaces 9604, priced to trigger the half-profit sell-price rule
CANDIDATE_GW1_MID = 9701  # replaces 9609, priced flat (no rise)
CANDIDATE_GW2_DEF = 9702  # free-hit swap, gameweek 2
CANDIDATE_GW2_MID = 9703
CANDIDATE_TEAM_OFFSET = 8700

# Classic-only gameweek 2 fixtures (team-less, same reasoning as
# test_gameweek_lifecycle.py's _gw_fixtures -- nothing on the classic path
# joins a fixture back to a team).
GW2_FIXTURE_OFFSET = 7900

# Dream11: one teamed fixture at gameweek 1 -- see module docstring. Pool is
# 2 GK / 5 DEF / 5 MID / 3 FWD per side, 30 total, DUAL_ID_FPL taking the
# home side's first DEF slot instead of a fresh id.
DREAM11_FIXTURE_FPL = 7800
DREAM11_HOME_TEAM_FPL = 8800
DREAM11_AWAY_TEAM_FPL = 8801
DREAM11_HOME_REST_IDS = list(range(9900, 9914))  # 14 ids: 2 GK, 4 DEF, 5 MID, 3 FWD
DREAM11_HOME_REST_POSITIONS = ["GK", "GK"] + ["DEF"] * 4 + ["MID"] * 5 + ["FWD"] * 3
DREAM11_AWAY_IDS = list(range(9920, 9935))  # 15 ids: full side
DREAM11_AWAY_POSITIONS = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3

# Manager A's Dream11 team: DUAL_ID_FPL captains it, mixed home/away so
# neither club exceeds MAX_PLAYERS_PER_CLUB (7).
D11_TEAM_A = {
    "player_ids": [9900, 9602, 9902, 9922, 9923, 9906, 9907, 9928, 9929, 9911, 9932],
    "captain_id": 9602,
    "vice_captain_id": 9906,
}
# Manager B's Dream11 team: independent picks from the same pool -- Dream11
# is not a draft, both managers may (and here do, on a couple of players)
# own the same player.
D11_TEAM_B = {
    "player_ids": [9920, 9903, 9904, 9924, 9925, 9908, 9909, 9927, 9928, 9913, 9933],
    "captain_id": 9920,
    "vice_captain_id": 9927,
}

# Pricing history: exactly two distinct rolling-points values in the whole
# pool, so the min-max normalisation lands on exact numbers -- DUAL_ID_FPL
# at the ceiling (11.0), one away DEF at the floor (6.0), everyone else
# un-seeded (no history -> floor by definition, per _compute_prices).
ROLLING_POINTS = {DUAL_ID_FPL: 90, 9922: 30}

CHAT_GAMEWEEK = 1
CHAT_OMITTED_FPL = 9603  # in manager_a's final GW1 XI, deliberately given no ml.ml_predictions row


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def make_user(make_user, engine):
    """Delegates creation to conftest's make_user. manager_a is registered
    over real HTTP in Phase 1 rather than through this fixture, but its id
    is appended to the same tracked list (see the test body) so the one
    teardown loop below covers it too.

    Teardown deletes every classic-schema row this file writes, leaving
    users/transfers behind -- enforce_transfers_immutability_fn blocks
    deleting through a transferred user, the same permanent-by-design
    situation test_gameweek_lifecycle.py documents. mini_leagues/
    league_members/leaderboard_snapshots are also left alone, matching
    test_leagues.py and test_gameweek_lifecycle.py exactly:
    leaderboard_snapshots' enforce_snapshot_immutability_fn trigger blocks
    deleting through them once compute_league_standings has written a row,
    which this file's own scoring phase guarantees it has. No Dream11
    teardown is needed at all: conftest's autouse ml.fixtures wipe (see
    _clean_ml_test_data) cascades to dream11.contests and everything under
    it.
    """
    factory = make_user

    def _make():
        return factory("full_season")

    _make.created = factory.created
    yield _make

    # Each user's cleanup gets its OWN transaction, deliberately -- NOT one
    # shared transaction wrapping the whole loop. A shared transaction means
    # any single statement failing (e.g. a DB trigger firing) rolls back
    # every other user's already-issued deletes in the same transaction too,
    # leaving orphaned rows that conftest's autouse wipe won't catch (it
    # only clears ml.* tables) -- and those orphans then silently break
    # unrelated tests doing broad (season, gameweek) scans, like
    # score_gameweek or lock_expired_gameweeks. This happened once here: a
    # mini_leagues delete hit the leaderboard_snapshots immutability
    # trigger, rolled back sibling deletes in the same shared transaction,
    # and left stale gw_selections rows that broke 3 apparently-unrelated
    # tests until traced back to this loop. Per-user transactions structurally
    # prevent that recurring -- one user's failure can now only roll back
    # that user's own deletes, never a sibling's.
    for uid in factory.created:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM free_hit_squads WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM chips WHERE user_id = :uid"), {"uid": uid})
            conn.execute(
                text(
                    "DELETE FROM squad_players WHERE user_squad_id IN "
                    "(SELECT id FROM user_squads WHERE user_id = :uid)"
                ),
                {"uid": uid},
            )
            conn.execute(text("DELETE FROM user_squads WHERE user_id = :uid"), {"uid": uid})


# ---------------------------------------------------------------- setup helpers


def _build_squad(make_team, make_player, offset, team_offset, positions, dual_fpl=None, dual_team_id=None):
    """Creates one 15-player classic squad, one club each, WITHOUT
    submitting it. If dual_fpl is given, that one fpl_id is assigned to
    dual_team_id instead of getting its own club.

    Returns {fpl_id: internal ml.players.id}.
    """
    internal_ids = {}
    for i, position in enumerate(positions):
        fpl_id = offset + i
        if fpl_id == dual_fpl:
            team_id = dual_team_id
        else:
            team_id = make_team(fpl_id=team_offset + i, name=f"Club{fpl_id}", short_name=f"C{fpl_id}")
        internal_ids[fpl_id] = make_player(
            fpl_id=fpl_id, position=position, team_id=team_id, cost_start=PLAYER_COST
        )
    return internal_ids


def _add_candidate(make_team, make_player, fpl_id, position, cost=PLAYER_COST):
    team_id = make_team(
        fpl_id=CANDIDATE_TEAM_OFFSET + (fpl_id - CANDIDATE_GW1_DEF),
        name=f"CandidateClub{fpl_id}", short_name=f"CC{fpl_id}",
    )
    return make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=cost)


def _xi_payload(gameweek, xi, bench, captain, vice, chip_used=None):
    return {
        "season": TEST_SEASON,
        "gameweek": gameweek,
        "player_ids": list(xi),
        "bench_order": list(bench),
        "captain_id": captain,
        "vice_captain_id": vice,
        "chip_used": chip_used,
    }


def _replace(ids, out_id, in_id):
    return [in_id if pid == out_id else pid for pid in ids]


def _gw_fixtures(make_fixture, gameweek, first_kickoff, second_kickoff, id_offset=GW2_FIXTURE_OFFSET):
    make_fixture(fpl_id=id_offset + gameweek * 2, gameweek=gameweek, home_team_id=None,
                 away_team_id=None, kickoff_time=first_kickoff)
    make_fixture(fpl_id=id_offset + gameweek * 2 + 1, gameweek=gameweek, home_team_id=None,
                 away_team_id=None, kickoff_time=second_kickoff)


def _advance_deadline(engine, gameweek, hours_ago=2):
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ml.fixtures SET kickoff_time = now() - make_interval(hours => :h) "
                "WHERE season = :s AND gameweek = :gw"
            ),
            {"h": hours_ago, "s": TEST_SEASON, "gw": gameweek},
        )


def _finish_gameweek(engine, gameweek):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ml.fixtures SET finished = TRUE WHERE season = :s AND gameweek = :gw"),
            {"s": TEST_SEASON, "gw": gameweek},
        )


def _active_player_ids(engine, user_id):
    with engine.connect() as conn:
        return sorted(
            r.player_id for r in conn.execute(
                text(
                    "SELECT sp.player_id FROM squad_players sp "
                    "JOIN user_squads us ON us.id = sp.user_squad_id "
                    "WHERE us.user_id = :u AND us.season = :s AND sp.is_active = TRUE"
                ),
                {"u": user_id, "s": TEST_SEASON},
            )
        )


def _budget_remaining(engine, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT budget_remaining FROM user_squads WHERE user_id = :u AND season = :s"),
            {"u": user_id, "s": TEST_SEASON},
        ).scalar()


def _gw_score_row(engine, user_id, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT raw_points, final_points, transfer_hits, hit_deductions, total_points, season_total "
                "FROM gw_scores WHERE user_id = :u AND season = :s AND gameweek = :gw"
            ),
            {"u": user_id, "s": TEST_SEASON, "gw": gameweek},
        ).first()


def _seed_prior_points(engine, internal_id, gameweek, total_points):
    """One completed-gameweek row so the mean-of-last-5 dream11.py prices on
    is exactly total_points -- same trick as test_dream11.py."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ml.player_gw_stats (player_id, season, gameweek, minutes, goals_scored, "
                "assists, clean_sheets, saves, bonus, bps, ict_index, expected_goals, expected_assists, "
                "expected_goal_involvements, total_points, value, selected, was_home) "
                "VALUES (:pid, :s, :gw, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, :pts, 50, 1000, TRUE) "
                "ON CONFLICT DO NOTHING"
            ),
            {"pid": internal_id, "s": TEST_SEASON, "gw": gameweek, "pts": total_points},
        )


def _dream11_prices(engine, contest_id):
    with engine.connect() as conn:
        return {
            r.player_id: float(r.credit_price)
            for r in conn.execute(
                text("SELECT player_id, credit_price FROM dream11.player_prices WHERE contest_id = :cid"),
                {"cid": contest_id},
            )
        }


def _member_points(engine, contest_id, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :c AND user_id = :u"),
            {"c": contest_id, "u": user_id},
        ).scalar()


# ================================================================== the test


def test_full_season_scenario(engine, make_user, make_team, make_player, make_fixture, make_gw_stat):
    # ---------------------------------------------------------------- setup

    # The Dream11 fixture's two clubs, created first so DUAL_ID_FPL can be
    # assigned to the home club instead of getting its own.
    dream11_home_team_id = make_team(
        fpl_id=DREAM11_HOME_TEAM_FPL, name="ScenarioHome", short_name="SHM"
    )
    dream11_away_team_id = make_team(
        fpl_id=DREAM11_AWAY_TEAM_FPL, name="ScenarioAway", short_name="SAW"
    )

    internal_a = _build_squad(
        make_team, make_player, SQUAD_A_OFFSET, SQUAD_A_TEAM_OFFSET, SQUAD_A_POSITIONS,
        dual_fpl=DUAL_ID_FPL, dual_team_id=dream11_home_team_id,
    )
    internal_b = _build_squad(make_team, make_player, SQUAD_B_OFFSET, SQUAD_B_TEAM_OFFSET, SQUAD_B_POSITIONS)

    internal_a[CANDIDATE_GW1_DEF] = _add_candidate(make_team, make_player, CANDIDATE_GW1_DEF, "DEF", cost=65)
    internal_a[CANDIDATE_GW1_MID] = _add_candidate(make_team, make_player, CANDIDATE_GW1_MID, "MID", cost=PLAYER_COST)
    internal_a[CANDIDATE_GW2_DEF] = _add_candidate(make_team, make_player, CANDIDATE_GW2_DEF, "DEF")
    internal_a[CANDIDATE_GW2_MID] = _add_candidate(make_team, make_player, CANDIDATE_GW2_MID, "MID")

    # One real match backs BOTH classic gameweek 1 AND the Dream11 contest --
    # see the module docstring for why that is deliberate.
    dream11_fixture_id = make_fixture(
        fpl_id=DREAM11_FIXTURE_FPL, gameweek=1, home_team_id=dream11_home_team_id,
        away_team_id=dream11_away_team_id, kickoff_time=NOW() + timedelta(days=2),
    )

    dream11_pool_internal = {DUAL_ID_FPL: internal_a[DUAL_ID_FPL]}
    for fpl_id, position in zip(DREAM11_HOME_REST_IDS, DREAM11_HOME_REST_POSITIONS):
        dream11_pool_internal[fpl_id] = make_player(
            fpl_id=fpl_id, position=position, team_id=dream11_home_team_id, cost_start=0
        )
    for fpl_id, position in zip(DREAM11_AWAY_IDS, DREAM11_AWAY_POSITIONS):
        dream11_pool_internal[fpl_id] = make_player(
            fpl_id=fpl_id, position=position, team_id=dream11_away_team_id, cost_start=0
        )

    for fpl_id, points in ROLLING_POINTS.items():
        _seed_prior_points(engine, dream11_pool_internal[fpl_id], gameweek=0, total_points=points)

    _gw_fixtures(make_fixture, gameweek=2, first_kickoff=NOW() + timedelta(days=9),
                 second_kickoff=NOW() + timedelta(days=10))

    # ---------------------------------------------------------------- Phase 1: registration

    register_email = f"scenario_{uuid.uuid4().hex[:12]}@example.com"
    register_password = "ScenarioPass123!"
    register_resp = client.post(
        "/auth/register",
        json={"email": register_email, "username": f"scenario_{uuid.uuid4().hex[:8]}",
              "password": register_password, "team_name": "Scenario United"},
    )
    assert register_resp.status_code == 200, register_resp.json()
    manager_a = register_resp.json()["user_id"]
    make_user.created.append(manager_a)  # so this file's teardown covers it too

    login_resp = client.post("/auth/login", json={"email": register_email, "password": register_password})
    assert login_resp.status_code == 200
    login_token = login_resp.json()["access_token"]

    me_resp = client.get("/auth/me", headers={"Authorization": f"Bearer {login_token}"})
    assert me_resp.status_code == 200
    assert me_resp.json()["id"] == manager_a
    assert me_resp.json()["email"] == register_email

    manager_b = make_user()

    # ---------------------------------------------------------------- Phase 2: chat gate, pre-season

    with patch("main.call_groq") as mock_groq:
        gate_resp = client.post(
            "/chat", json={"season": TEST_SEASON, "gameweek": CHAT_GAMEWEEK, "message": "who should I captain?"},
            headers=bearer_headers(manager_a),
        )
    assert gate_resp.status_code == 200
    assert gate_resp.json()["response"] == main.CHAT_AVAILABILITY_MESSAGE
    mock_groq.assert_not_called()

    # ---------------------------------------------------------------- Phase 3: classic setup

    select_a = client.post(
        "/squad/select", json={"season": TEST_SEASON, "player_ids": XI_FPL_IDS_A + BENCH_FPL_IDS_A},
        headers=bearer_headers(manager_a),
    )
    assert select_a.status_code == 200, select_a.json()
    assert _budget_remaining(engine, manager_a) == 100

    select_b = client.post(
        "/squad/select", json={"season": TEST_SEASON, "player_ids": XI_FPL_IDS_B + BENCH_FPL_IDS_B},
        headers=bearer_headers(manager_b),
    )
    assert select_b.status_code == 200, select_b.json()

    league_resp = client.post(
        "/leagues",
        json={"name": "Scenario League", "season": TEST_SEASON, "league_type": "private",
              "scoring_type": "classic", "max_members": 10},
        headers=bearer_headers(manager_a),
    )
    assert league_resp.status_code == 200, league_resp.json()
    league_id = league_resp.json()["league_id"]
    league_code = league_resp.json()["code"]

    join_league_resp = client.post("/leagues/join", json={"code": league_code}, headers=bearer_headers(manager_b))
    assert join_league_resp.status_code == 200, join_league_resp.json()

    xi1_a = client.post(
        "/gw_selection",
        json=_xi_payload(1, XI_FPL_IDS_A, BENCH_FPL_IDS_A, CAPTAIN_FPL_ID_A, VICE_FPL_ID_A, chip_used="triple_captain"),
        headers=bearer_headers(manager_a),
    )
    assert xi1_a.status_code == 200, xi1_a.json()

    xi1_b = client.post(
        "/gw_selection",
        json=_xi_payload(1, XI_FPL_IDS_B, BENCH_FPL_IDS_B, CAPTAIN_FPL_ID_B, VICE_FPL_ID_B),
        headers=bearer_headers(manager_b),
    )
    assert xi1_b.status_code == 200, xi1_b.json()

    chips_a = client.get(
        "/chips/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(manager_a)
    ).json()
    assert chips_a["triple_captain_available"] is False

    # ---------------------------------------------------------------- Phase 4: transfer with a hit

    # 9604's price rises 60 -> 90 before the transfer, so price_out exercises
    # the half-profit-rounded-down rule (60 + (90-60)//2 = 75); the second
    # swap is priced flat (no rise) so only ONE of the two hits pricing math.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ml.players SET now_cost = 90 WHERE season = :s AND fpl_id = 9604"),
            {"s": TEST_SEASON},
        )

    transfer_resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [
                {"player_out_id": 9604, "player_in_id": CANDIDATE_GW1_DEF},
                {"player_out_id": 9609, "player_in_id": CANDIDATE_GW1_MID},
            ],
        },
        headers=bearer_headers(manager_a),
    )
    assert transfer_resp.status_code == 200, transfer_resp.json()
    body = transfer_resp.json()
    assert [t["is_free"] for t in body["transfers"]] == [True, False]
    assert body["transfers"][0]["price_out"] == 75  # half of the 30-tenths rise, rounded down
    assert body["transfers"][0]["price_in"] == 65
    assert body["transfers"][1]["price_out"] == 60  # flat, no rise
    assert body["transfers"][1]["price_in"] == 60
    assert body["budget_remaining"] == 110  # 100 prior + (75 - 65) + (60 - 60)

    xi_a_post_transfer = _replace(_replace(XI_FPL_IDS_A, 9604, CANDIDATE_GW1_DEF), 9609, CANDIDATE_GW1_MID)
    resubmit_a = client.post(
        "/gw_selection",
        json=_xi_payload(1, xi_a_post_transfer, BENCH_FPL_IDS_A, CAPTAIN_FPL_ID_A, VICE_FPL_ID_A, chip_used="triple_captain"),
        headers=bearer_headers(manager_a),
    )
    assert resubmit_a.status_code == 200, resubmit_a.json()

    # ---------------------------------------------------------------- Phase 5: Dream11 setup

    contest_resp = client.post(
        "/dream11/contests",
        json={"fixture_id": dream11_fixture_id, "name": "Scenario Contest", "max_members": 10},
        headers=bearer_headers(manager_a),
    )
    assert contest_resp.status_code == 200, contest_resp.json()
    contest_id = contest_resp.json()["contest_id"]
    contest_code = contest_resp.json()["code"]
    assert len(contest_code) == 7
    assert contest_resp.json()["pool_size"] == 30

    prices = _dream11_prices(engine, contest_id)
    assert all(6.0 <= p <= 11.0 for p in prices.values())
    assert prices[dream11_pool_internal[DUAL_ID_FPL]] == 11.0  # the pool's rolling-points ceiling
    assert prices[dream11_pool_internal[9922]] == 6.0  # the pool's rolling-points floor

    join_resp = client.post("/dream11/contests/join", json={"code": contest_code}, headers=bearer_headers(manager_b))
    assert join_resp.status_code == 200, join_resp.json()

    submit_a = client.post(
        f"/dream11/contests/{contest_id}/team", json=D11_TEAM_A, headers=bearer_headers(manager_a)
    )
    assert submit_a.status_code == 200, submit_a.json()

    submit_b = client.post(
        f"/dream11/contests/{contest_id}/team", json=D11_TEAM_B, headers=bearer_headers(manager_b)
    )
    assert submit_b.status_code == 200, submit_b.json()

    # ---------------------------------------------------------------- Phase 6: deadline crossing (classic)

    _advance_deadline(engine, gameweek=1, hours_ago=2)  # also moves the shared Dream11 fixture's kickoff

    locked_transfer = client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9605, "player_in_id": CANDIDATE_GW1_DEF}]},
        headers=bearer_headers(manager_a),
    )
    assert locked_transfer.status_code == 422

    lock_summary = lock_expired_gameweeks(engine)
    assert (TEST_SEASON, 1) in lock_summary["locked"]

    # ---------------------------------------------------------------- Phase 7: one stats row, two engines

    make_gw_stat(
        player_id=internal_a[DUAL_ID_FPL], gameweek=1, fixture_id=dream11_fixture_id,
        minutes=57, assists=1, clean_sheets=1, goals_conceded=0, goals_scored=0, total_points=4,
    )
    # The captain needs minutes so the triple-captain multiplier actually
    # applies; total_points=2 with no other component field set takes
    # _component_score's "no signal -> return total_points literally" path.
    make_gw_stat(player_id=internal_a[CAPTAIN_FPL_ID_A], gameweek=1, minutes=90, total_points=2)
    make_gw_stat(player_id=internal_b[CAPTAIN_FPL_ID_B], gameweek=1, minutes=90, total_points=1)

    _finish_gameweek(engine, gameweek=1)

    refresh_summary = refresh_active_gameweeks(engine)
    assert (TEST_SEASON, 1) in refresh_summary["refreshed"]

    dream11_score_summary = score_dream11_contest(engine, contest_id)
    assert dream11_score_summary["skipped_finalized"] is False
    assert manager_a in dream11_score_summary["scored"]
    assert manager_b in dream11_score_summary["scored"]

    # Classic: 57 minutes fails the 60-minute clean-sheet threshold, but
    # earns the 1-point sub-60 appearance point; assist worth 3.
    # points = 1 (appearance) + 1*3 (assist) = 4.
    row_a = _gw_score_row(engine, manager_a, 1)
    assert row_a.raw_points == 4 + 2  # DUAL_ID_FPL (4) + captain filler (2)
    assert row_a.final_points == row_a.raw_points + (3 - 1) * 2  # triple captain: (3x - 1) * captain's own 2
    assert row_a.transfer_hits == 1
    assert row_a.hit_deductions == 4
    assert row_a.total_points == row_a.final_points - 4
    assert row_a.total_points == 6

    row_b = _gw_score_row(engine, manager_b, 1)
    assert row_b.total_points == 2  # captain filler (1) + (2x - 1) * 1, no hits

    # Dream11: 57 minutes PASSES the 54-minute threshold (recomputed from
    # goals_conceded + minutes, never from FPL's precomputed column), and an
    # assist is worth 20, not 3 -- raw = 20 (assist) + 4 (DEF clean sheet) = 24.
    team_a_after_score = client.get(
        f"/dream11/contests/{contest_id}/team", params={"user_id": manager_a}, headers=bearer_headers(manager_a)
    ).json()
    dual_line = next(p for p in team_a_after_score["players"] if p["player_id"] == DUAL_ID_FPL)
    assert dual_line["points"] == 24  # per-player breakdown, BEFORE the captain multiplier
    assert dual_line["is_captain"] is True

    # The API never stores a per-player POST-multiplier number -- the 2x is
    # applied once, at the team-total level (dream11.py's captain_bonus =
    # points_by_fpl_id[captain] * (CAPTAIN_MULTIPLIER - 1.0)). DUAL_ID_FPL is
    # the only player on either roster with any seeded stats, so the team's
    # displayed total is entirely this one doubled contribution: 24 raw +
    # 24 captain_bonus = 48 -- the number that actually reaches the UI.
    assert team_a_after_score["captain_bonus"] == 24
    assert team_a_after_score["live_total_points"] == 48

    # Same underlying row, same instant, two different -- both correct --
    # totals: 4 for Classic, 24 for Dream11, purely from the assist-weight
    # and clean-sheet-threshold divergence between the two rulebooks.
    assert dual_line["points"] != 4

    team_a_classic = client.get(
        "/team", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(manager_a)
    ).json()
    assert team_a_classic["final_total"] == 6

    # ---------------------------------------------------------------- Phase 8: standings, lock, finalize

    table = client.get(f"/leagues/{league_id}/table", headers=bearer_headers(manager_a)).json()
    rows_by_user = {r["user_id"]: r for r in table["rows"]}
    assert rows_by_user[manager_a]["season_points"] == 6
    assert rows_by_user[manager_b]["season_points"] == 2
    assert rows_by_user[manager_a]["rank"] < rows_by_user[manager_b]["rank"]

    dream11_lock_summary = lock_started_contests(engine)
    assert contest_id in dream11_lock_summary["locked"]

    post_lock_submit = client.post(
        f"/dream11/contests/{contest_id}/team", json=D11_TEAM_B, headers=bearer_headers(manager_b)
    )
    assert post_lock_submit.status_code == 422

    needing_finalization = find_contests_needing_finalization(engine)
    assert contest_id in needing_finalization

    finalize_result = finalize_dream11_contest(engine, contest_id)
    assert finalize_result["finalized"] is True

    leaderboard = client.get(
        f"/dream11/contests/{contest_id}/leaderboard", headers=bearer_headers(manager_a)
    ).json()
    leaderboard_by_user = {r["user_id"]: r for r in leaderboard["rows"]}
    assert leaderboard_by_user[manager_a]["total_points"] == _member_points(engine, contest_id, manager_a)
    assert leaderboard_by_user[manager_b]["total_points"] == _member_points(engine, contest_id, manager_b)

    # A one-way door: re-scoring a finalized contest is refused, not silently
    # re-applied, inside the same continuous run that produced the score.
    rescored = score_dream11_contest(engine, contest_id)
    assert rescored["skipped_finalized"] is True

    # ---------------------------------------------------------------- Phase 9: Free Hit, gameweek 2

    pre_free_hit_squad = _active_player_ids(engine, manager_a)

    free_hit_activate = client.post(
        "/gw_selection",
        json=_xi_payload(2, xi_a_post_transfer, BENCH_FPL_IDS_A, CAPTAIN_FPL_ID_A, VICE_FPL_ID_A, chip_used="free_hit"),
        headers=bearer_headers(manager_a),
    )
    assert free_hit_activate.status_code == 200, free_hit_activate.json()

    free_hit_transfer = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 2,
            "transfers": [
                {"player_out_id": CANDIDATE_GW1_DEF, "player_in_id": CANDIDATE_GW2_DEF},
                {"player_out_id": CANDIDATE_GW1_MID, "player_in_id": CANDIDATE_GW2_MID},
            ],
        },
        headers=bearer_headers(manager_a),
    )
    assert free_hit_transfer.status_code == 200, free_hit_transfer.json()
    assert all(t["is_free"] for t in free_hit_transfer.json()["transfers"])  # FREE_CHIPS, uncapped

    xi_a_free_hit = _replace(_replace(xi_a_post_transfer, CANDIDATE_GW1_DEF, CANDIDATE_GW2_DEF),
                              CANDIDATE_GW1_MID, CANDIDATE_GW2_MID)
    resubmit_free_hit = client.post(
        "/gw_selection",
        json=_xi_payload(2, xi_a_free_hit, BENCH_FPL_IDS_A, CAPTAIN_FPL_ID_A, VICE_FPL_ID_A, chip_used="free_hit"),
        headers=bearer_headers(manager_a),
    )
    assert resubmit_free_hit.status_code == 200, resubmit_free_hit.json()

    _advance_deadline(engine, gameweek=2, hours_ago=2)
    lock_expired_gameweeks(engine)

    make_gw_stat(player_id=internal_a[CAPTAIN_FPL_ID_A], gameweek=2, minutes=90, total_points=3)
    _finish_gameweek(engine, gameweek=2)

    refresh_again = refresh_active_gameweeks(engine)
    assert (TEST_SEASON, 2) in refresh_again["refreshed"]

    # Idempotent re-scoring: gameweek 1's total must be unchanged by this
    # second pass, since nothing about its own data changed.
    assert _gw_score_row(engine, manager_a, 1).total_points == 6

    row_a_gw2 = _gw_score_row(engine, manager_a, 2)
    assert row_a_gw2.transfer_hits == 0  # free_hit -- every transfer this gameweek is free
    assert row_a_gw2.hit_deductions == 0

    revert_result = revert_expired_free_hits(engine)
    assert (manager_a, TEST_SEASON, 2) in revert_result["reverted"]
    assert _active_player_ids(engine, manager_a) == pre_free_hit_squad
    assert _budget_remaining(engine, manager_a) == 110

    # ---------------------------------------------------------------- Phase 10: ML predictions + chat

    predicted_xi_internal = {fpl_id: internal_a[fpl_id] for fpl_id in xi_a_post_transfer if fpl_id != CHAT_OMITTED_FPL}
    tiers = ["Elite", "Strong", "Average", "Weak", "Elite", "Strong", "Average", "Weak", "Strong", "Average"]
    with engine.begin() as conn:
        for (fpl_id, internal_id), tier in zip(predicted_xi_internal.items(), tiers):
            conn.execute(
                text(
                    "INSERT INTO ml.ml_predictions (player_id, season, gameweek, predicted_points, "
                    "tier_or_label, model_version) VALUES (:pid, :s, :gw, :pp, :tier, :mv)"
                ),
                {
                    "pid": internal_id, "s": TEST_SEASON, "gw": CHAT_GAMEWEEK,
                    "pp": 5.5, "tier": tier, "mv": main.MODEL_VERSION,
                },
            )

    captured = {}

    def fake_call_groq(prompt, *args, **kwargs):
        captured["prompt"] = prompt
        return "mocked scenario advice"

    with patch("main.call_groq", side_effect=fake_call_groq) as mock_groq:
        chat_resp = client.post(
            "/chat",
            json={"season": TEST_SEASON, "gameweek": CHAT_GAMEWEEK, "message": "Should I captain differently?"},
            headers=bearer_headers(manager_a),
        )
    assert chat_resp.status_code == 200
    assert chat_resp.json()["response"] == "mocked scenario advice"
    mock_groq.assert_called_once()

    prompt = captured["prompt"]
    assert f"TestPlayer{CHAT_OMITTED_FPL}" in prompt
    assert "New/Insufficient Data" in prompt.split(f"TestPlayer{CHAT_OMITTED_FPL}")[1].split("\n")[0]
    assert f"TestPlayer{CAPTAIN_FPL_ID_A}" in prompt
    assert "[CURRENT CAPTAIN]" in prompt.split(f"TestPlayer{CAPTAIN_FPL_ID_A}")[1].split("\n")[0]
    assert f"TestPlayer{VICE_FPL_ID_A}" in prompt
    assert "[CURRENT VICE-CAPTAIN]" in prompt.split(f"TestPlayer{VICE_FPL_ID_A}")[1].split("\n")[0]
    assert "5.5" not in prompt  # the raw predicted_points value must never leak into the prompt
