"""
test_gameweek_lifecycle.py — cross-module gameweek sequencing: deadline
boundary behaviour, and the wiring between squad selection and the
transfers endpoint across a gameweek boundary (lock/reopen, banking).

This file used to also cover the classic scorer's Beat-lock wiring and the
Free Hit chip's end-to-end revert chain. Both are gone: the classic scorer
(Results/scoring.py) was deleted in Phase 4c and the Free Hit chip
(GameEngine/free_hit_revert.py, the free_hit_squads table) was removed
with it. Their tests are deleted, not skipped, per the MERGE GATE in
IMPLEMENTATION_PLAN.md -- there is nothing left for them to cover. The
tactical selection/scoring lifecycle is exercised end-to-end by
test_gw_selection_tactical.py, test_transfers_tactical.py and
test_scoring_job_tactical.py instead.

Imports only the Game_logic pure-function modules and the FastAPI app, never
Worker.tasks -- same reasoning as test_beat_scheduling.py and
test_scoring.py. Every Beat task in Worker/tasks.py is a thin retry/
logging wrapper over the pure functions called here, so no
broker, no eager mode, and no Celery import is needed.

NO MOCKS OF ANY KIND, deliberately. Nothing in Game_logic/ or
Context_assembler/main.py makes an HTTP call -- the FPL API is only
ever touched by Data -- so there is no external seam to fake,
and the DB is real as everywhere else in this suite.

TIME. Almost every deadline comparison in this app happens in Postgres
(SQL NOW()), not Python -- see deadlines.DEADLINE_PASSED_QUERY. A
Python clock library (freezegun/time-machine) would move the wrong
clock and silently do nothing, so this file crosses a deadline the only
way that actually works: by UPDATE-ing ml.fixtures.kickoff_time into
the past. That is legal because ml.enforce_fixture_identity_fn guards
only home_team_id/away_team_id/fpl_id, leaving kickoff_time and
finished freely updatable. All relative offsets are computed DB-side
via now() +/- make_interval(...) rather than Python deltas, so there is
no clock-skew risk between the test process and the database.

RULE COVERAGE. See backend/Tests/README.md for the current split between
closed rule gaps and the few deliberate remaining differences.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from main import app as fastapi_app
from Shared.deadlines import DEADLINE_PASSED_QUERY

client = TestClient(fastapi_app)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 -- matches test_beat_scheduling.py's convention

# 2 GK / 5 DEF / 5 MID / 3 FWD, laid out so fpl ids are contiguous per
# position: 9000-9001 GK, 9002-9006 DEF, 9007-9011 MID, 9012-9014 FWD.
SQUAD_POSITIONS = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
SQUAD_ID_OFFSET = 9000
SQUAD_TEAM_ID_OFFSET = 8000
PLAYER_COST = 60  # 15 * 60 = 900 of the 1000 cap, leaving 100 in the bank

# 1-4-4-2. Bench is [DEF, MID, GK, FWD] -- the GK deliberately THIRD, so
# nothing here can pass by assuming "the bench GK is slot 12 or 15"
# (same guard test_starting_xi.py:34 uses).
XI_FPL_IDS = [9000, 9002, 9003, 9004, 9005, 9007, 9008, 9009, 9010, 9012, 9013]
BENCH_FPL_IDS = [9006, 9011, 9001, 9014]


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def make_user(make_user, engine):
    """Uuid-unique users with per-test teardown of everything this file
    writes. Users themselves are NOT deleted: transfers has an
    immutability trigger (enforce_transfers_immutability_fn) that blocks
    deleting through it, so a user who has transferred can never be
    removed -- the same permanent-by-design situation test_transfers.py
    and test_beat_scheduling.py document, hence the uuid identity.

    mini_leagues/league_members/leaderboard_snapshots are left alone,
    matching test_leagues.py -- leaderboard_snapshots' immutability
    trigger blocks deleting through them anyway. The `chips` table itself
    is gone (dropped in the tactical migration), so there is nothing left
    to clean there.
    """
    # Creation delegated to conftest's make_user (same name, received as an
    # argument -- pytest resolves it to the parent fixture). The prefix is
    # passed so the rows this file creates are still named as they were.
    # The teardown below stays here because it is specific to this file.
    factory = make_user

    def _make():
        return factory("lifecycle")

    yield _make

    with engine.begin() as conn:
        for uid in factory.created:
            conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": uid})
            conn.execute(
                text("DELETE FROM squad_players WHERE user_squad_id IN "
                     "(SELECT id FROM user_squads WHERE user_id = :uid)"),
                {"uid": uid},
            )
            conn.execute(text("DELETE FROM user_squads WHERE user_id = :uid"), {"uid": uid})


# ---------------------------------------------------------------- helpers


def _build_lifecycle_squad(make_team, make_player):
    """Creates the 15 ml.players rows (one club each, so the 3-per-club
    cap is never the thing under test) WITHOUT submitting them.

    Returns {fpl_id: internal ml.players.id} -- make_gw_stat needs the
    internal id, while every endpoint and squad_players row uses the raw
    fpl id, so both are needed throughout this file.
    """
    internal_ids = {}
    for i, position in enumerate(SQUAD_POSITIONS):
        fpl_id = SQUAD_ID_OFFSET + i
        team_id = make_team(
            fpl_id=SQUAD_TEAM_ID_OFFSET + i, name=f"LifecycleClub{i}", short_name=f"LC{i}"
        )
        internal_ids[fpl_id] = make_player(
            fpl_id=fpl_id, position=position, team_id=team_id, cost_start=PLAYER_COST
        )
    return internal_ids


def _add_candidate(make_team, make_player, fpl_id, position, cost=PLAYER_COST):
    """A transfer target: exists in ml.players, in nobody's squad, on its
    own club. Ids live at 9500+ / teams 8500+ so they can never collide
    with the squad's 9000-9014 / 8000-8014."""
    team_id = make_team(
        fpl_id=fpl_id - 1000, name=f"CandidateClub{fpl_id}", short_name=f"CC{fpl_id}"
    )
    return make_player(fpl_id=fpl_id, position=position, team_id=team_id, cost_start=cost)


def _select_squad(user_id):
    return client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": XI_FPL_IDS + BENCH_FPL_IDS}, headers=bearer_headers(user_id)
    )






def _gw_fixtures(make_fixture, gameweek, first_kickoff, second_kickoff, id_offset=7000):
    """Two fixtures per gameweek, so 'deadline = MIN(kickoff)' and
    'gameweek is over = EVERY fixture done' can be moved independently --
    which is exactly what separates the lock (deadline) from the Free Hit
    revert (gameweek over).

    home/away are left NULL: nothing in the lifecycle path joins a fixture
    back to a team (deadlines are MIN(kickoff_time) over the gameweek, and
    live_status counts fixtures), so binding real clubs here would only
    imply a coupling that doesn't exist."""
    make_fixture(
        fpl_id=id_offset + gameweek * 2,
        gameweek=gameweek,
        home_team_id=None,
        away_team_id=None,
        kickoff_time=first_kickoff,
    )
    make_fixture(
        fpl_id=id_offset + gameweek * 2 + 1,
        gameweek=gameweek,
        home_team_id=None,
        away_team_id=None,
        kickoff_time=second_kickoff,
    )


def _advance_deadline(engine, gameweek, hours_ago=2, only_first=False):
    """Moves this gameweek's kickoff(s) into the past so the deadline has
    passed. Computed DB-side; legal because ml.enforce_fixture_identity_fn
    guards only the team/fpl identity columns, not kickoff_time."""
    sql = (
        "UPDATE ml.fixtures SET kickoff_time = now() - make_interval(hours => :h) "
        "WHERE season = :s AND gameweek = :gw"
    )
    if only_first:
        sql += " AND fpl_id = (SELECT MIN(fpl_id) FROM ml.fixtures WHERE season = :s AND gameweek = :gw)"
    with engine.begin() as conn:
        conn.execute(text(sql), {"h": hours_ago, "s": TEST_SEASON, "gw": gameweek})














def _budget_remaining(engine, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT budget_remaining FROM user_squads WHERE user_id = :u AND season = :s"),
            {"u": user_id, "s": TEST_SEASON},
        ).scalar()






# ---------------------------------------------------------------- deadline boundary


def test_deadline_boundary_is_inclusive_at_the_exact_kickoff_instant(engine, make_fixture):
    """Pins '<=' rather than '<' in deadlines.DEADLINE_PASSED_QUERY.

    Exact, not delta-based: Postgres now() is transaction_timestamp(), so
    it is a single constant for the whole transaction. Setting kickoff to
    now() and then asking the production query inside that SAME
    transaction compares the instant against itself -- the only way to
    express "exactly at the deadline" without faking a clock.

    Flipping that query's '<=' to '<' would fail this test and nothing
    else in the suite; test_beat_scheduling.py:202-228 only covers the
    +/-60s cases either side.
    """
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ml.fixtures SET kickoff_time = now() WHERE season = :s AND gameweek = 1"),
            {"s": TEST_SEASON},
        )
        passed = conn.execute(
            DEADLINE_PASSED_QUERY, {"season": TEST_SEASON, "gameweek": 1}
        ).scalar()

    assert passed is True  # the kickoff instant itself is already too late


# ---------------------------------------------------------------- lock wiring






# ---------------------------------------------------------------- chips


# ---------------------------------------------------------------- transfer window across gameweeks


def test_next_gameweek_reopens_transfers_after_the_previous_one_locked(
    engine, make_user, make_team, make_player, make_fixture
):
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9500, position="DEF")
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))
    _gw_fixtures(make_fixture, gameweek=2, first_kickoff=NOW() + timedelta(days=9),
                 second_kickoff=NOW() + timedelta(days=10))

    assert _select_squad(user_id).status_code == 200
    _advance_deadline(engine, gameweek=1)

    pair = {"transfers": [{"player_out_id": 9002, "player_in_id": 9500}]}

    closed = client.post("/transfers", json={"season": TEST_SEASON, "gameweek": 1, **pair}, headers=bearer_headers(user_id))
    assert closed.status_code == 422

    reopened = client.post("/transfers", json={"season": TEST_SEASON, "gameweek": 2, **pair}, headers=bearer_headers(user_id))
    assert reopened.status_code == 200

    # And gameweek 1 is still shut, so "reopened" is proven against a
    # still-closed neighbour rather than against nothing.
    assert client.post(
        "/transfers", json={"season": TEST_SEASON, "gameweek": 1,
                            "transfers": [{"player_out_id": 9003, "player_in_id": 9002}]}, headers=bearer_headers(user_id)
    ).status_code == 422


def test_free_transfer_allowance_banks_when_a_gameweek_goes_unused(
    engine, make_user, make_team, make_player, make_fixture
):
    """A gameweek that passes with no transfers banks its free transfer,
    so the next one opens with two.

    Cross-module coverage only -- the banking rule itself, including the
    cap at 5 and the hit arithmetic either side of it, is exercised
    directly in test_transfers.py's banking section.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))

    assert _select_squad(user_id).status_code == 200

    gw1 = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert gw1["free_transfers_used"] == 0
    assert gw1["free_transfers_remaining"] == 1

    _advance_deadline(engine, gameweek=1)  # gameweek 1 passes with zero transfers made

    gw2 = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)).json()
    assert gw2["free_transfers_used"] == 0
    assert gw2["free_transfers_remaining"] == 2  # gameweek 1's went unused and rolled over






# ---------------------------------------------------------------- free hit, end to end


















# ---------------------------------------------------------------- gap characterization: pricing


def test_sell_price_uses_half_profit_rounded_down_after_a_price_rise(
    engine, make_user, make_team, make_player
):
    """A player bought at 6.0 and now worth 9.0 sells for 7.5; an incoming
    player is bought at current price."""
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")

    assert _select_squad(user_id).status_code == 200
    assert _budget_remaining(engine, user_id) == 100

    # Both a squad player and a transfer target rise 60 -> 90. Legal:
    # ml.enforce_player_identity_fn guards only fpl_id/season.
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ml.players SET now_cost = 90 WHERE season = :s AND fpl_id = ANY(:ids)"),
            {"s": TEST_SEASON, "ids": [9002, 9502]},
        )

    resp = client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    )
    assert resp.status_code == 200

    transfer = resp.json()["transfers"][0]
    assert transfer["price_out"] == 75
    assert transfer["price_in"] == 90
    assert resp.json()["budget_remaining"] == 85  # 100 + 75 - 90

    with engine.connect() as conn:
        sell_price = conn.execute(
            text(
                "SELECT sp.sell_price FROM squad_players sp JOIN user_squads us ON us.id = sp.user_squad_id "
                "WHERE us.user_id = :u AND us.season = :s AND sp.player_id = 9002"
            ),
            {"u": user_id, "s": TEST_SEASON},
        ).scalar()
    assert sell_price == 75


# ---------------------------------------------------------------- gap characterization: double and blank gameweeks






# ---------------------------------------------------------------- gap characterization: scoring inputs




# ---------------------------------------------------------------- the full week


