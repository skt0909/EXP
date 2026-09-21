"""
test_gameweek_lifecycle.py — cross-module gameweek sequencing: the
checkpoint chain a real gameweek walks through, from squad selection
through the deadline, the Beat lock, scoring, the Free Hit revert, and
on into the next gameweek.

Every other Game_logic test file tests ONE module against hand-seeded
state. This one is the only place the modules are run against each
other's output in order -- squad_selection -> transfers -> starting_xi
-> free_hit_revert -> scoring -> standings -- so it deliberately does NOT
re-test rules those files already cover (formation boundaries, chip
season caps, autosub mechanics in isolation, budget/club validation).
It tests the wiring between them, plus the places where this codebase
previously diverged from real FPL.

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
closed rule gaps and the few deliberate remaining differences. This file
keeps the end-to-end tests that prove the corrected rules still work
together across squad selection, transfers, deadline locking, scoring,
league standings, and Free Hit revert.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from main import app as fastapi_app
from GameEngine.free_hit_revert import revert_expired_free_hits
from GameEngine.gameweek_finalize import refresh_active_gameweeks
from GameEngine.gameweek_lock import lock_expired_gameweeks
from Shared.deadlines import DEADLINE_PASSED_QUERY, deadline_has_passed
# Phase 4c: Results/scoring.py is deleted. The tactical job is the scorer.
from Results.scoring_job import score_gameweek_tactical as score_gameweek
from Gameplay.starting_xi import LOCKED_DETAIL

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

CAPTAIN_FPL_ID = 9007  # first MID in the XI (slot 6)
VICE_FPL_ID = 9008  # second MID in the XI (slot 7)


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def make_user(make_user, engine):
    """Uuid-unique users with per-test teardown of everything this file
    writes. Users themselves are NOT deleted: transfers has an
    immutability trigger (enforce_transfers_immutability_fn) that blocks
    deleting through it, so a user who has transferred can never be
    removed -- the same permanent-by-design situation test_transfers.py
    and test_beat_scheduling.py document, hence the uuid identity.

    chips is cleaned here and NOT in test_beat_scheduling.py's otherwise
    identical fixture, because this file is the only one that activates
    chips for undeletable users. mini_leagues/league_members/
    leaderboard_snapshots are left alone, matching test_leagues.py --
    leaderboard_snapshots' immutability trigger blocks deleting through
    them anyway.
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
            conn.execute(text("DELETE FROM free_hit_squads WHERE user_id = :uid"), {"uid": uid})
            conn.execute(text("DELETE FROM chips WHERE user_id = :uid"), {"uid": uid})
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


def _xi_payload(user_id, gameweek, xi=None, bench=None, chip_used=None, captain=None, vice=None):
    return {
        "season": TEST_SEASON,
        "gameweek": gameweek,
        "player_ids": list(xi if xi is not None else XI_FPL_IDS),
        "bench_order": list(bench if bench is not None else BENCH_FPL_IDS),
        "captain_id": captain if captain is not None else CAPTAIN_FPL_ID,
        "vice_captain_id": vice if vice is not None else VICE_FPL_ID,
        "chip_used": chip_used,
    }


def _replace(ids, out_id, in_id):
    """Swaps one id in place, so a post-transfer XI/bench keeps the exact
    slot structure (and therefore the same autosub behavior) as before."""
    return [in_id if pid == out_id else pid for pid in ids]


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


def _finish_gameweek(engine, gameweek):
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ml.fixtures SET finished = TRUE WHERE season = :s AND gameweek = :gw"),
            {"s": TEST_SEASON, "gw": gameweek},
        )


def _is_locked(engine, user_id, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT is_locked FROM gw_selections WHERE user_id = :u AND season = :s AND gameweek = :gw"),
            {"u": user_id, "s": TEST_SEASON, "gw": gameweek},
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


def _active_player_ids(engine, user_id):
    with engine.connect() as conn:
        return sorted(
            r.player_id
            for r in conn.execute(
                text(
                    "SELECT sp.player_id FROM squad_players sp "
                    "JOIN user_squads us ON us.id = sp.user_squad_id "
                    "WHERE us.user_id = :u AND us.season = :s AND sp.is_active = TRUE"
                ),
                {"u": user_id, "s": TEST_SEASON},
            )
        )


def _free_hit_rows(engine, user_id, gameweek, unreverted_only=True):
    sql = (
        "SELECT player_id, purchase_price, budget_remaining, reverted_at FROM free_hit_squads "
        "WHERE user_id = :u AND season = :s AND gameweek = :gw"
    )
    if unreverted_only:
        sql += " AND reverted_at IS NULL"
    with engine.connect() as conn:
        return conn.execute(text(sql), {"u": user_id, "s": TEST_SEASON, "gw": gameweek}).all()


def _chip_count_for_gw(engine, user_id, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM chips WHERE user_id = :u AND season = :s AND gameweek_used = :gw"),
            {"u": user_id, "s": TEST_SEASON, "gw": gameweek},
        ).scalar()


def _budget_remaining(engine, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT budget_remaining FROM user_squads WHERE user_id = :u AND season = :s"),
            {"u": user_id, "s": TEST_SEASON},
        ).scalar()


def _seed_all_stats(make_gw_stat, internal_ids, gameweek, fpl_ids, minutes=90, total_points=0):
    for fpl_id in fpl_ids:
        make_gw_stat(
            player_id=internal_ids[fpl_id], gameweek=gameweek, minutes=minutes, total_points=total_points
        )


def _lines_by_player_id(body):
    lines = {}
    for group in body["lineup"].values():
        for line in group:
            lines[line["player_id"]] = line
    for line in body["bench"]:
        lines[line["player_id"]] = line
    return lines


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


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_beat_lock_makes_an_already_submitted_selection_unchangeable(
    engine, make_user, make_team, make_player, make_fixture
):
    """The wiring nothing else covers. test_starting_xi.py hand-sets
    is_locked; test_beat_scheduling.py locks but never calls an endpoint
    afterwards. This runs the real Beat function and then tries to act.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1), headers=bearer_headers(user_id)).status_code == 200

    _advance_deadline(engine, gameweek=1)
    summary = lock_expired_gameweeks(engine)

    # Membership, never equality: other test files legitimately leave
    # unlocked gw_selections rows for TEST_SEASON that co-lock here.
    assert (TEST_SEASON, 1) in summary["locked"]
    assert _is_locked(engine, user_id, 1) is True

    # The 422 actually arrives from starting_xi's deadline_has_passed
    # pre-check, which short-circuits before the enforce_selection_lock
    # trigger can fire. Both paths emit the identical detail by design
    # (starting_xi.py:100-105), so this assertion holds either way.
    resubmit = client.post("/gw_selection", json=_xi_payload(user_id, 1), headers=bearer_headers(user_id))
    assert resubmit.status_code == 422
    assert resubmit.json()["detail"] == LOCKED_DETAIL

    transfer = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [{"player_out_id": 9002, "player_in_id": 9500}],
        }, headers=bearer_headers(user_id)
    )
    assert transfer.status_code == 422
    assert any("gameweek 1 is locked" in e for e in transfer.json()["detail"])


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_beat_lock_does_not_block_the_next_gameweeks_selection(
    engine, make_user, make_team, make_player, make_fixture
):
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))
    _gw_fixtures(make_fixture, gameweek=2, first_kickoff=NOW() + timedelta(days=9),
                 second_kickoff=NOW() + timedelta(days=10))

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1), headers=bearer_headers(user_id)).status_code == 200

    _advance_deadline(engine, gameweek=1)
    lock_expired_gameweeks(engine)

    assert client.post("/gw_selection", json=_xi_payload(user_id, 1), headers=bearer_headers(user_id)).status_code == 422
    assert client.post("/gw_selection", json=_xi_payload(user_id, 2), headers=bearer_headers(user_id)).status_code == 200


# ---------------------------------------------------------------- chips


def test_second_chip_in_the_same_gameweek_replaces_the_first(
    engine, make_user, make_team, make_player
):
    """'One chip per gameweek' is not a validated rule here -- it is
    structural. gw_selections.chip_used is a single column, and
    DELETE_CHIP_FOR_GW_STMT clears the gameweek's chips row before
    conditionally reinserting, so a resubmit REPLACES rather than
    rejects, and the abandoned chip is refunded.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    assert _select_squad(user_id).status_code == 200

    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="bench_boost"), headers=bearer_headers(user_id)).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="triple_captain"), headers=bearer_headers(user_id)).status_code == 200

    assert _chip_count_for_gw(engine, user_id, 1) == 1

    body = client.get("/gw_selection", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert body["chip_used"] == "triple_captain"

    chips = client.get("/chips/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)).json()
    assert chips["bench_boost_available"] is True  # never actually played, so refunded
    assert chips["triple_captain_available"] is False


def test_a_chip_used_in_a_locked_gameweek_stays_unavailable_later(
    engine, make_user, make_team, make_player, make_fixture
):
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="triple_captain"), headers=bearer_headers(user_id)).status_code == 200

    _advance_deadline(engine, gameweek=1)
    lock_expired_gameweeks(engine)

    chips = client.get("/chips/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)).json()
    assert chips["triple_captain_available"] is False


def test_a_spent_restricted_chip_is_replenished_in_the_second_half(
    make_user, make_team, make_player
):
    """A chip spent in GW1 uses the first-half set; the GW20 second-half
    set is still available."""
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    assert _select_squad(user_id).status_code == 200

    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="bench_boost"), headers=bearer_headers(user_id)).status_code == 200

    chips = client.get("/chips/used", params={"season": TEST_SEASON, "gameweek": 20}, headers=bearer_headers(user_id)).json()
    assert chips["bench_boost_available"] is True

    resp = client.post("/gw_selection", json=_xi_payload(user_id, 20, chip_used="bench_boost"), headers=bearer_headers(user_id))
    assert resp.status_code == 200


def test_wildcards_are_one_per_half_of_the_season(
    make_user, make_team, make_player
):
    """The first-half wildcard cannot be used twice; the second-half
    wildcard is a separate GW20+ chip."""
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    assert _select_squad(user_id).status_code == 200

    assert client.post("/gw_selection", json=_xi_payload(user_id, 2, chip_used="wildcard"), headers=bearer_headers(user_id)).status_code == 200
    same_half = client.post("/gw_selection", json=_xi_payload(user_id, 5, chip_used="wildcard"), headers=bearer_headers(user_id))
    assert same_half.status_code == 422
    assert any("first half" in e for e in same_half.json()["detail"])

    chips = client.get("/chips/used", params={"season": TEST_SEASON, "gameweek": 25}, headers=bearer_headers(user_id)).json()
    assert chips["wildcard_used"] == 0
    assert chips["wildcard_remaining"] == 1

    resp = client.post("/gw_selection", json=_xi_payload(user_id, 25, chip_used="wildcard"), headers=bearer_headers(user_id))
    assert resp.status_code == 200


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


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_a_banked_allowance_reaches_scoring_as_a_smaller_deduction(
    engine, make_user, make_team, make_player, make_gw_stat
):
    """The banking rule has to survive all the way into gw_scores, not
    just into GET /transfers/used.

    scoring.py counts is_free = FALSE rows and knows nothing about
    banking (see the comment on its TRANSFER_HITS_QUERY), so this is
    really checking that transfers.py stamped the right number of rows
    free. With 2 banked, 3 transfers is one hit -- the same batch in
    gameweek 1 would have been two.
    """
    user_id = make_user()
    internal_ids = _build_lifecycle_squad(make_team, make_player)
    internal_ids[9502] = _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    internal_ids[9503] = _add_candidate(make_team, make_player, fpl_id=9503, position="MID")
    internal_ids[9504] = _add_candidate(make_team, make_player, fpl_id=9504, position="FWD")

    assert _select_squad(user_id).status_code == 200

    # Gameweek 1 goes by untouched, so gameweek 2 opens with 2.
    assert client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)
    ).json()["free_transfers_remaining"] == 2

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 2,
            "transfers": [
                {"player_out_id": 9002, "player_in_id": 9502},
                {"player_out_id": 9007, "player_in_id": 9503},
                {"player_out_id": 9012, "player_in_id": 9504},
            ],
        }, headers=bearer_headers(user_id)
    )
    assert resp.status_code == 200
    assert [t["is_free"] for t in resp.json()["transfers"]] == [True, True, False]

    xi = _replace(_replace(_replace(XI_FPL_IDS, 9002, 9502), 9007, 9503), 9012, 9504)
    assert client.post(
        "/gw_selection", json=_xi_payload(user_id, 2, xi=xi, captain=9008, vice=9009), headers=bearer_headers(user_id)
    ).status_code == 200

    _seed_all_stats(make_gw_stat, internal_ids, 2, xi + BENCH_FPL_IDS, minutes=90, total_points=0)
    score_gameweek(engine, TEST_SEASON, 2)

    row = _gw_score_row(engine, user_id, 2)
    assert row.transfer_hits == 1  # not 2 -- the banked transfer covered one
    assert row.hit_deductions == 4
    assert row.total_points == -4


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_paid_transfers_take_hits_under_the_normal_gameweek_cap(
    engine, make_user, make_team, make_player, make_gw_stat
):
    """End-to-end proof that paid transfers reach gw_scores through the
    real endpoint.

    test_scoring.py:285 covers hit arithmetic from hand-inserted
    transfers rows; nothing until now ran POST /transfers -> score_gameweek.

    The free slot goes to the FIRST transfer in submission order
    (transfers.py:395-398) -- not the cheapest, not the earliest-listed
    player.
    """
    user_id = make_user()
    internal_ids = _build_lifecycle_squad(make_team, make_player)
    internal_ids[9502] = _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    internal_ids[9503] = _add_candidate(make_team, make_player, fpl_id=9503, position="MID")
    internal_ids[9504] = _add_candidate(make_team, make_player, fpl_id=9504, position="FWD")

    assert _select_squad(user_id).status_code == 200

    resp = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [
                {"player_out_id": 9002, "player_in_id": 9502},
                {"player_out_id": 9007, "player_in_id": 9503},
                {"player_out_id": 9012, "player_in_id": 9504},
            ],
        }, headers=bearer_headers(user_id)
    )
    assert resp.status_code == 200
    assert [t["is_free"] for t in resp.json()["transfers"]] == [True, False, False]

    xi = _replace(_replace(XI_FPL_IDS, 9002, 9502), 9007, 9503)
    xi = _replace(xi, 9012, 9504)
    assert client.post(
        "/gw_selection",
        json=_xi_payload(user_id, 1, xi=xi, captain=9008, vice=9009), headers=bearer_headers(user_id)
    ).status_code == 200

    _seed_all_stats(make_gw_stat, internal_ids, 1, xi + BENCH_FPL_IDS, minutes=90, total_points=0)
    score_gameweek(engine, TEST_SEASON, 1)

    row = _gw_score_row(engine, user_id, 1)
    assert row.transfer_hits == 2
    assert row.hit_deductions == 8
    assert row.total_points == -8


# ---------------------------------------------------------------- free hit, end to end


def test_free_hit_snapshot_reverts_only_once_the_gameweek_is_over(
    engine, make_user, make_team, make_player, make_fixture
):
    """The full Free Hit chain, which no single test has run before:
    activate -> snapshot -> free transfers -> deadline -> lock ->
    (still not reverted) -> gameweek over -> reverted.

    RUNBOOK ADAPTATION: the runbook says Free Hit "reverts at the next
    deadline". This codebase reverts when the free-hit gameweek is OVER
    -- every fixture finished, or kicked off more than
    FREE_HIT_REVERT_BUFFER_HOURS ago -- see
    free_hit_revert.REVERTABLE_FREE_HITS_QUERY. That is
    deliberate: the free-hit squad has to stay visible through
    GET /squad while its own gameweek is being played.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    _add_candidate(make_team, make_player, fpl_id=9503, position="MID")
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200

    snapshot = _free_hit_rows(engine, user_id, 1)
    assert len(snapshot) == 15
    assert sorted(r.player_id for r in snapshot) == sorted(XI_FPL_IDS + BENCH_FPL_IDS)
    assert snapshot[0].budget_remaining == 100

    transfers = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [
                {"player_out_id": 9002, "player_in_id": 9502},
                {"player_out_id": 9007, "player_in_id": 9503},
            ],
        }, headers=bearer_headers(user_id)
    )
    assert transfers.status_code == 200
    assert all(t["is_free"] for t in transfers.json()["transfers"])  # FREE_CHIPS, uncapped

    _advance_deadline(engine, gameweek=1)
    lock_expired_gameweeks(engine)

    # Deadline gone and locked -- but the gameweek is still being played,
    # so the free-hit squad must still be the active one.
    assert (user_id, TEST_SEASON, 1) not in revert_expired_free_hits(engine)["reverted"]
    assert 9502 in _active_player_ids(engine, user_id)

    _finish_gameweek(engine, gameweek=1)
    result = revert_expired_free_hits(engine)

    assert (user_id, TEST_SEASON, 1) in result["reverted"]
    assert _active_player_ids(engine, user_id) == sorted(XI_FPL_IDS + BENCH_FPL_IDS)
    assert _budget_remaining(engine, user_id) == 100
    assert _free_hit_rows(engine, user_id, 1, unreverted_only=True) == []

    squad = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(user_id)).json()
    assert sorted(p["player_id"] for p in squad["players"]) == sorted(XI_FPL_IDS + BENCH_FPL_IDS)
    assert squad["budget_remaining"] == 10.0


def test_free_hit_does_not_revert_merely_because_the_next_deadline_passed(
    engine, make_user, make_team, make_player, make_fixture
):
    """The revert is keyed on the free-hit gameweek being over, not on
    any later gameweek's deadline. Gameweek 2 kicking off must not pull
    gameweek 1's Free Hit back while gameweek 1 still has a match left.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=5))
    _gw_fixtures(make_fixture, gameweek=2, first_kickoff=NOW() + timedelta(days=9),
                 second_kickoff=NOW() + timedelta(days=10))

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    ).status_code == 200

    # Gameweek 1's FIRST match is done; its second is still days away.
    _advance_deadline(engine, gameweek=1, only_first=True)
    # And gameweek 2's deadline has now gone too.
    _advance_deadline(engine, gameweek=2)

    assert (user_id, TEST_SEASON, 1) not in revert_expired_free_hits(engine)["reverted"]
    assert 9502 in _active_player_ids(engine, user_id)


def test_resubmitting_an_xi_under_an_active_free_hit_keeps_the_original_snapshot(
    engine, make_user, make_team, make_player
):
    """The snapshot is taken once, at activation, and is never retaken
    while it is still pending.

    This is the ordinary flow, not an edge case: after making free-hit
    transfers a manager MUST resubmit their XI, because the previous one
    names players they no longer own. When the snapshot was retaken on
    every submission, that resubmit silently overwrote the pre-chip squad
    with the post-chip one and the revert restored the free-hit team to
    itself.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert sorted(r.player_id for r in _free_hit_rows(engine, user_id, 1)) == sorted(XI_FPL_IDS + BENCH_FPL_IDS)

    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    ).status_code == 200

    new_xi = _replace(XI_FPL_IDS, 9002, 9502)
    assert client.post(
        "/gw_selection", json=_xi_payload(user_id, 1, xi=new_xi, chip_used="free_hit"), headers=bearer_headers(user_id)
    ).status_code == 200

    snapshot_ids = sorted(r.player_id for r in _free_hit_rows(engine, user_id, 1))
    assert snapshot_ids == sorted(XI_FPL_IDS + BENCH_FPL_IDS)  # untouched by the resubmit
    assert 9502 not in snapshot_ids  # the free-hit signing never enters the "pre-chip" squad

    # Still exactly 15 rows -- retaken snapshots would also have doubled
    # up, since the insert is a plain INSERT ... SELECT.
    assert len(_free_hit_rows(engine, user_id, 1)) == 15


def test_cancelling_a_free_hit_before_the_deadline_restores_the_pre_chip_squad(
    engine, make_user, make_team, make_player
):
    """Switching away from a live Free Hit cancels it: the pre-chip squad
    and budget come back, the snapshot is dropped, and the chip is
    refunded.

    Without the restore this was an unlimited-permanent-transfer hole,
    not merely a lost snapshot: activate Free Hit, make any number of
    free transfers, then resubmit with chip_used=null and the new squad
    was kept for good, no hits ever charged, and the chip handed back --
    a strictly better Wildcard, available every gameweek.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    _add_candidate(make_team, make_player, fpl_id=9503, position="MID")

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [
                  {"player_out_id": 9002, "player_in_id": 9502},
                  {"player_out_id": 9007, "player_in_id": 9503},
              ]}, headers=bearer_headers(user_id)
    ).status_code == 200
    assert 9502 in _active_player_ids(engine, user_id)

    # Cancel: same gameweek, chip switched off. The XI must be the
    # pre-chip 15, since that is the squad being restored.
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used=None), headers=bearer_headers(user_id)).status_code == 200

    assert _active_player_ids(engine, user_id) == sorted(XI_FPL_IDS + BENCH_FPL_IDS)
    assert _budget_remaining(engine, user_id) == 100
    assert _free_hit_rows(engine, user_id, 1, unreverted_only=False) == []  # no trace, not just unreverted
    assert _chip_count_for_gw(engine, user_id, 1) == 0

    chips = client.get("/chips/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)).json()
    assert chips["free_hit_available"] is True  # refunded, never played

    # Re-activating for the same gameweek must not collide with the
    # dropped snapshot -- uq_free_hit_squads_user_season_gw_player has no
    # reverted_at predicate, so cancelling has to DELETE, not mark.
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert len(_free_hit_rows(engine, user_id, 1)) == 15


def test_cancelling_a_free_hit_is_validated_against_the_restored_squad(
    engine, make_user, make_team, make_player
):
    """A cancelling submission is checked against the snapshot's 15, not
    the currently-active 15 -- after it commits the manager owns the
    pre-chip squad, so that is the squad the XI has to come from.

    Submitting the free-hit XI while cancelling is therefore a 422, with
    a message that names which squad is being checked rather than the
    generic "your active squad" (which would be actively misleading here,
    since the free-hit squad IS the active one at request time).
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    ).status_code == 200

    free_hit_xi = _replace(XI_FPL_IDS, 9002, 9502)
    resp = client.post("/gw_selection", json=_xi_payload(user_id, 1, xi=free_hit_xi, chip_used=None), headers=bearer_headers(user_id))

    assert resp.status_code == 422
    assert any("cancelling this gameweek's Free Hit" in e for e in resp.json()["detail"])

    # Nothing was restored or dropped by the rejected attempt.
    assert 9502 in _active_player_ids(engine, user_id)
    assert len(_free_hit_rows(engine, user_id, 1)) == 15


def test_cancelling_a_free_hit_refunds_the_gameweeks_free_transfer(
    make_user, make_team, make_player
):
    """"If cancelled, the transfers made as part of it are reversed and
    your previous transfer situation is restored" -- the free transfer is
    part of that situation, not just the squad.

    transfers is append-only, so the is_free row survives the cancel;
    cancelled_transfers names it and every counting query skips it, which
    is what puts the allowance back.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")

    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    ).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1, chip_used=None), headers=bearer_headers(user_id)).status_code == 200

    used = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert used["chip_active"] is False
    assert used["free_transfers_used"] == 0  # reversed, so it never counted
    assert used["free_transfers_remaining"] == 1  # exactly where they started
    assert used["total_transfers_this_gameweek"] == 0


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_cancelling_a_free_hit_refunds_every_transfer_it_used(
    engine, make_user, make_team, make_player
):
    """Not "some" allowance back -- the exact pre-chip count. Three
    free-hit transfers against a bank of three must leave all three
    available again.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    _add_candidate(make_team, make_player, fpl_id=9503, position="MID")
    _add_candidate(make_team, make_player, fpl_id=9504, position="FWD")

    assert _select_squad(user_id).status_code == 200

    # Gameweeks 1 and 2 pass untouched, so gameweek 3 opens with 3 banked.
    before = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 3}, headers=bearer_headers(user_id)
    ).json()
    assert before["free_transfers_remaining"] == 3

    assert client.post("/gw_selection", json=_xi_payload(user_id, 3, chip_used="free_hit"), headers=bearer_headers(user_id)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 3,
              "transfers": [
                  {"player_out_id": 9002, "player_in_id": 9502},
                  {"player_out_id": 9007, "player_in_id": 9503},
                  {"player_out_id": 9012, "player_in_id": 9504},
              ]}, headers=bearer_headers(user_id)
    ).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 3, chip_used=None), headers=bearer_headers(user_id)).status_code == 200

    after = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 3}, headers=bearer_headers(user_id)
    ).json()
    assert after["free_transfers_used"] == 0
    assert after["free_transfers_remaining"] == 3  # all three back, not one

    with engine.connect() as conn:
        cancelled = conn.execute(
            text(
                "SELECT COUNT(*) FROM cancelled_transfers c "
                "JOIN transfers t ON t.id = c.transfer_id WHERE t.user_id = :u"
            ),
            {"u": user_id},
        ).scalar()
    assert cancelled == 3  # the rows survive; they just stop counting


def test_cancelling_a_free_hit_with_no_bank_left_does_not_go_negative(
    engine, make_user, make_team, make_player
):
    """The zero boundary: a manager who spent their only free transfer
    BEFORE activating should come back to 0 remaining, not to -1 and not
    to a windfall 1.

    The pre-chip transfer is not part of the chip -- it stands in the
    restored squad and must keep counting against the allowance.
    """
    user_id = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    _add_candidate(make_team, make_player, fpl_id=9503, position="MID")

    assert _select_squad(user_id).status_code == 200

    # Gameweek 1 has exactly one free transfer; spend it before the chip.
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    ).status_code == 200
    spent = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)
    ).json()
    assert spent["free_transfers_remaining"] == 0

    assert client.post("/gw_selection", json=_xi_payload(
        user_id, 1, xi=_replace(XI_FPL_IDS, 9002, 9502), chip_used="free_hit"
    ), headers=bearer_headers(user_id)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9007, "player_in_id": 9503}]}, headers=bearer_headers(user_id)
    ).status_code == 200

    assert client.post("/gw_selection", json=_xi_payload(
        user_id, 1, xi=_replace(XI_FPL_IDS, 9002, 9502), chip_used=None
    ), headers=bearer_headers(user_id)).status_code == 200

    after = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)
    ).json()
    assert after["free_transfers_used"] == 1  # the pre-chip one still counts
    assert after["free_transfers_remaining"] == 0  # back to spent, not negative
    assert after["total_transfers_this_gameweek"] == 1

    # The pre-chip signing stays; only the chip's move was reversed.
    active = _active_player_ids(engine, user_id)
    assert 9502 in active
    assert 9503 not in active
    assert 9007 in active


def test_a_cancelled_free_hit_leaves_next_gameweek_banking_untouched(
    engine, make_user, make_team, make_player
):
    """The end-to-end claim: after a cancel, the following gameweek's
    allowance is exactly what it would have been had the chip never been
    activated at all.

    Asserted against a second manager who does nothing, so the expected
    number is derived from the rule rather than restated by hand.
    """
    canceller = make_user()
    bystander = make_user()
    _build_lifecycle_squad(make_team, make_player)
    _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    _add_candidate(make_team, make_player, fpl_id=9503, position="MID")

    assert _select_squad(canceller).status_code == 200

    assert client.post("/gw_selection", json=_xi_payload(canceller, 3, chip_used="free_hit"), headers=bearer_headers(canceller)).status_code == 200
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 3,
              "transfers": [
                  {"player_out_id": 9002, "player_in_id": 9502},
                  {"player_out_id": 9007, "player_in_id": 9503},
              ]}, headers=bearer_headers(canceller)
    ).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(canceller, 3, chip_used=None), headers=bearer_headers(canceller)).status_code == 200

    def remaining(uid, gameweek):
        return client.get(
            "/transfers/used", params={"season": TEST_SEASON, "gameweek": gameweek}, headers=bearer_headers(uid)
        ).json()["free_transfers_remaining"]

    assert remaining(canceller, 4) == remaining(bystander, 4) == 4  # 3 banked + 1 earned
    assert remaining(canceller, 5) == remaining(bystander, 5) == 5  # capped

    with engine.connect() as conn:
        still_recorded = conn.execute(
            text("SELECT COUNT(*) FROM transfers WHERE user_id = :u AND season = :s AND gameweek = 3"),
            {"u": canceller, "s": TEST_SEASON},
        ).scalar()
    assert still_recorded == 2  # history intact, arithmetic unaffected


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


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_a_double_gameweek_stores_and_scores_two_fixture_rows(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """A player can have two fixture stat rows in one FPL gameweek, and
    scoring adds both rows into that player's gameweek score."""
    user_id = make_user()
    team_id = make_team(fpl_id=8900, name="DgwClub", short_name="DGW")
    internal_id = make_player(fpl_id=9600, position="MID", team_id=team_id, cost_start=PLAYER_COST)
    fx1 = make_fixture(
        fpl_id=8910, gameweek=1, home_team_id=team_id, away_team_id=None,
        kickoff_time=NOW() + timedelta(days=2),
    )
    fx2 = make_fixture(
        fpl_id=8911, gameweek=1, home_team_id=team_id, away_team_id=None,
        kickoff_time=NOW() + timedelta(days=5),
    )

    internal_ids = _build_lifecycle_squad(make_team, make_player)
    internal_ids[9600] = internal_id
    squad_ids = _replace(XI_FPL_IDS + BENCH_FPL_IDS, 9007, 9600)
    assert client.post(
        "/squad/select",
        json={"season": TEST_SEASON, "player_ids": squad_ids}, headers=bearer_headers(user_id)
    ).status_code == 200
    xi = _replace(XI_FPL_IDS, 9007, 9600)
    assert client.post(
        "/gw_selection",
        json=_xi_payload(user_id, 1, xi=xi, bench=BENCH_FPL_IDS, captain=9600, vice=9008), headers=bearer_headers(user_id)
    ).status_code == 200

    make_gw_stat(player_id=internal_id, gameweek=1, fixture_id=fx1, minutes=90, total_points=6)
    make_gw_stat(player_id=internal_id, gameweek=1, fixture_id=fx2, minutes=75, total_points=4)
    _seed_all_stats(make_gw_stat, internal_ids, 1, XI_FPL_IDS[1:] + BENCH_FPL_IDS, minutes=90, total_points=0)

    score_gameweek(engine, TEST_SEASON, 1)
    row = _gw_score_row(engine, user_id, 1)

    assert row.raw_points == 10
    assert row.final_points == 20
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT total_points FROM ml.player_gw_stats WHERE player_id = :p AND season = :s AND gameweek = 1"),
            {"p": internal_id, "s": TEST_SEASON},
        ).all()
    assert [r.total_points for r in rows] == [6, 4]


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_a_blank_gameweek_starter_is_scored_as_a_zero_minute_no_show(
    engine, make_user, make_team, make_player, make_gw_stat
):
    """GAP -- a blank gameweek is indistinguishable from a no-show.

    A player whose club has no fixture simply has no player_gw_stats row,
    and scoring.py COALESCEs that to 0 minutes / 0 points
    (scoring.py:68-82). Autosub therefore fires for them exactly as it
    would for a player who was dropped by their manager.

    The OUTCOME here matches real FPL. The gap is that nothing in the
    schema records "this player had no fixture", so no rule could ever
    treat the two cases differently even if it wanted to.
    """
    user_id = make_user()
    internal_ids = _build_lifecycle_squad(make_team, make_player)
    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1), headers=bearer_headers(user_id)).status_code == 200

    # Everyone plays for 2 points -- except 9010, a starting MID, who has
    # NO stats row at all (his club blanked).
    played = [pid for pid in XI_FPL_IDS + BENCH_FPL_IDS if pid != 9010]
    _seed_all_stats(make_gw_stat, internal_ids, 1, played, minutes=90, total_points=2)

    score_gameweek(engine, TEST_SEASON, 1)
    row = _gw_score_row(engine, user_id, 1)

    # 10 surviving starters + the bench DEF autosubbed in for the blanker.
    assert row.raw_points == 22
    assert row.final_points == 24  # captain 9007 played, +2


# ---------------------------------------------------------------- gap characterization: scoring inputs


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_classic_scoring_computes_from_components_with_total_points_fallback(
    engine, make_user, make_team, make_player, make_gw_stat
):
    """When component columns are populated, classic scoring computes FPL
    points. Existing rows with only total_points still score via fallback."""
    user_id = make_user()
    internal_ids = _build_lifecycle_squad(make_team, make_player)
    assert _select_squad(user_id).status_code == 200
    assert client.post("/gw_selection", json=_xi_payload(user_id, 1), headers=bearer_headers(user_id)).status_code == 200

    for fpl_id in XI_FPL_IDS + BENCH_FPL_IDS:
        if fpl_id == 9002:
            # Component scoring: 2 appearance + 12 goals + 3 assist + 4 clean
            # sheet + 3 bonus = 24. saves are GK-only, so ignored here.
            make_gw_stat(
                player_id=internal_ids[fpl_id], gameweek=1, minutes=90,
                goals_scored=2, assists=1, clean_sheets=1, saves=6, bonus=3, bps=55,
                total_points=0,
            )
        elif fpl_id == 9003:
            # Nothing in any component column -- but total_points 17.
            make_gw_stat(player_id=internal_ids[fpl_id], gameweek=1, minutes=90, total_points=17)
        else:
            make_gw_stat(player_id=internal_ids[fpl_id], gameweek=1, minutes=90, total_points=0)

    score_gameweek(engine, TEST_SEASON, 1)
    row = _gw_score_row(engine, user_id, 1)

    assert row.raw_points == 41
    assert row.final_points == 41  # captain 9007 played but scored 0, so +0


# ---------------------------------------------------------------- the full week


@pytest.mark.skip(reason="Phase 4: these drive the CLASSIC scorer (hits, chips, captaincy) or the old selection payload. scoring.py is out of scope for Phase 3")
def test_full_gameweek_lifecycle_gw1_through_gw2(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """One test, gameweek 1 through gameweek 2, through the real
    endpoints and the real Beat functions in order.

    No broker: Worker/tasks.py's Beat tasks are thin wrappers over
    lock_expired_gameweeks / refresh_active_gameweeks /
    revert_expired_free_hits, which are called directly here.
    """
    user_id = make_user()
    internal_ids = _build_lifecycle_squad(make_team, make_player)
    internal_ids[9500] = _add_candidate(make_team, make_player, fpl_id=9500, position="DEF")
    internal_ids[9501] = _add_candidate(make_team, make_player, fpl_id=9501, position="MID")
    internal_ids[9502] = _add_candidate(make_team, make_player, fpl_id=9502, position="DEF")
    internal_ids[9503] = _add_candidate(make_team, make_player, fpl_id=9503, position="MID")
    internal_ids[9504] = _add_candidate(make_team, make_player, fpl_id=9504, position="FWD")

    _gw_fixtures(make_fixture, gameweek=1, first_kickoff=NOW() + timedelta(days=2),
                 second_kickoff=NOW() + timedelta(days=3))
    _gw_fixtures(make_fixture, gameweek=2, first_kickoff=NOW() + timedelta(days=9),
                 second_kickoff=NOW() + timedelta(days=10))

    league_id = client.post(
        "/leagues",
        json={
            "name": f"Lifecycle {uuid.uuid4().hex[:8]}", "season": TEST_SEASON,
            "league_type": "private", "scoring_type": "classic", "max_members": 50,
        }, headers=bearer_headers(user_id)
    ).json()["league_id"]

    # --- Phase 1: gameweek 1, window open --------------------------------
    squad = _select_squad(user_id)
    assert squad.status_code == 200
    assert squad.json()["budget_remaining"] == 100  # tenths: 1000 cap - 15 * 60

    used = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert used["free_transfers_remaining"] == 1
    assert used["chip_active"] is False

    transfers = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 1,
            "transfers": [
                {"player_out_id": 9005, "player_in_id": 9500},  # DEF -> DEF, takes the free slot
                {"player_out_id": 9011, "player_in_id": 9501},  # MID -> MID, paid
            ],
        }, headers=bearer_headers(user_id)
    )
    assert transfers.status_code == 200
    assert [t["is_free"] for t in transfers.json()["transfers"]] == [True, False]
    assert transfers.json()["budget_remaining"] == 100  # 60 out, 60 in, twice over

    used = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert used["free_transfers_used"] == 1
    assert used["free_transfers_remaining"] == 0
    assert used["total_transfers_this_gameweek"] == 2

    gw1_xi = _replace(XI_FPL_IDS, 9005, 9500)
    gw1_bench = _replace(BENCH_FPL_IDS, 9011, 9501)
    assert client.post(
        "/gw_selection", json=_xi_payload(user_id, 1, xi=gw1_xi, bench=gw1_bench), headers=bearer_headers(user_id)
    ).status_code == 200

    current = client.get("/gw_selection", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert current["has_selection"] is True
    assert current["player_ids"] == gw1_xi  # echoed back in position_slot order
    assert current["bench_order"] == gw1_bench

    team = client.get("/team", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert team["has_lineup"] is True
    assert team["has_score"] is False
    assert team["live_status"] == "upcoming"
    assert team["deadline"] is not None
    assert team["bank"] == 10.0
    assert team["team_value"] == 90.0

    # --- Phase 2: cross the deadline -------------------------------------
    _advance_deadline(engine, gameweek=1, hours_ago=2)
    assert deadline_has_passed(engine, TEST_SEASON, 1) is True

    blocked_transfer = client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 1,
              "transfers": [{"player_out_id": 9002, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    )
    assert blocked_transfer.status_code == 422
    assert any("gameweek 1 is locked" in e for e in blocked_transfer.json()["detail"])

    blocked_xi = client.post("/gw_selection", json=_xi_payload(user_id, 1, xi=gw1_xi, bench=gw1_bench), headers=bearer_headers(user_id))
    assert blocked_xi.status_code == 422
    assert blocked_xi.json()["detail"] == LOCKED_DETAIL

    assert (TEST_SEASON, 1) in lock_expired_gameweeks(engine)["locked"]
    assert _is_locked(engine, user_id, 1) is True

    # Now guarded twice over -- the deadline pre-check still answers first,
    # so the trigger never gets the chance to fire.
    assert client.post(
        "/gw_selection", json=_xi_payload(user_id, 1, xi=gw1_xi, bench=gw1_bench), headers=bearer_headers(user_id)
    ).status_code == 422

    # --- Phase 3: gameweek 1 played and scored ---------------------------
    # Everyone plays 90' for 5, except starting MID 9010 (slot 10) who gets
    # nothing. Bench: DEF 9006 played for 4, MID 9501 played for 3, and both
    # GK 9001 and FWD 9014 sat out.
    for fpl_id in gw1_xi:
        if fpl_id == 9010:
            make_gw_stat(player_id=internal_ids[fpl_id], gameweek=1, minutes=0, total_points=0)
        else:
            make_gw_stat(player_id=internal_ids[fpl_id], gameweek=1, minutes=90, total_points=5)
    make_gw_stat(player_id=internal_ids[9006], gameweek=1, minutes=90, total_points=4)
    make_gw_stat(player_id=internal_ids[9501], gameweek=1, minutes=90, total_points=3)
    make_gw_stat(player_id=internal_ids[9001], gameweek=1, minutes=0, total_points=0)
    make_gw_stat(player_id=internal_ids[9014], gameweek=1, minutes=0, total_points=0)

    _finish_gameweek(engine, gameweek=1)

    refreshed = refresh_active_gameweeks(engine)["refreshed"]
    assert (TEST_SEASON, 1) in refreshed
    assert (TEST_SEASON, 2) not in refreshed  # gameweek 2 kicks off in 9 days

    # Autosub: bench slot 12 (DEF 9006) comes on for the blank MID 9010, and
    # the resulting 1-5-3-2 is formation-legal, so it commits on the first
    # candidate tried.
    #   raw   = 5 (GK) + 20 (4 DEF) + 15 (3 MID) + 10 (2 FWD) + 4 (bench DEF) = 54
    #   final = 54 + 5 (captain 9007 played, doubled)                          = 59
    #   total = 59 - 4 (one paid transfer)                                     = 55
    row = _gw_score_row(engine, user_id, 1)
    assert (row.raw_points, row.final_points, row.transfer_hits, row.hit_deductions,
            row.total_points, row.season_total) == (54, 59, 1, 4, 55, 55)

    team = client.get("/team", params={"season": TEST_SEASON, "gameweek": 1}, headers=bearer_headers(user_id)).json()
    assert team["has_score"] is True
    assert team["live_status"] == "final"
    assert team["gw_points"] == 55
    assert team["raw_points"] == 54
    assert team["captain_bonus"] == 5
    assert team["hit_deductions"] == 4
    assert team["final_total"] == 55
    # gw_average / overall_rank are season-wide over gw_scores and can see
    # leftover users from other test files -- deliberately not asserted.

    lines = _lines_by_player_id(team)
    assert lines[9010]["is_autosubbed_out"] is True
    assert lines[9006]["is_autosubbed_in"] is True

    table = client.get("/leagues/{}/table".format(league_id), params={}, headers=bearer_headers(user_id)).json()
    my_row = next(r for r in table["rows"] if r["user_id"] == user_id)
    assert my_row["season_points"] == 55
    assert my_row["last_gw_points"] == 55

    # --- Phase 4: gameweek 2, free hit, revert ---------------------------
    used = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)).json()
    assert used["free_transfers_used"] == 0
    # Not 2: gameweek 1's allowance was spent (one of that batch's two was
    # is_free), so there was nothing to bank into this gameweek.
    assert used["free_transfers_remaining"] == 1

    assert client.post(
        "/gw_selection", json=_xi_payload(user_id, 2, xi=gw1_xi, bench=gw1_bench, chip_used="free_hit"), headers=bearer_headers(user_id)
    ).status_code == 200

    snapshot = _free_hit_rows(engine, user_id, 2)
    assert len(snapshot) == 15
    assert snapshot[0].budget_remaining == 100
    pre_chip_squad = sorted(r.player_id for r in snapshot)

    used = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 2}, headers=bearer_headers(user_id)).json()
    assert used["chip_active"] is True
    # 0 here means "uncapped", not "none left" (transfers.py:339) -- a real
    # UI-contract wart, pinned rather than fixed.
    assert used["free_transfers_remaining"] == 0

    fh_transfers = client.post(
        "/transfers",
        json={
            "season": TEST_SEASON, "gameweek": 2,
            "transfers": [
                {"player_out_id": 9002, "player_in_id": 9502},
                {"player_out_id": 9007, "player_in_id": 9503},
                {"player_out_id": 9012, "player_in_id": 9504},
            ],
        }, headers=bearer_headers(user_id)
    )
    assert fh_transfers.status_code == 200
    assert all(t["is_free"] for t in fh_transfers.json()["transfers"])

    squad_now = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(user_id)).json()
    held = {p["player_id"] for p in squad_now["players"]}
    assert {9502, 9503, 9504} <= held
    assert held.isdisjoint({9002, 9007, 9012})

    # The manager now HAS to re-pick, since the submitted XI names three
    # players they no longer own. This is the ordinary flow, and it must
    # leave the snapshot alone -- retaking it here would record the
    # free-hit squad as the "pre-chip" one and make the revert a no-op.
    fh_xi = _replace(_replace(_replace(gw1_xi, 9002, 9502), 9007, 9503), 9012, 9504)
    assert client.post(
        "/gw_selection",
        json=_xi_payload(user_id, 2, xi=fh_xi, bench=gw1_bench, chip_used="free_hit",
                         captain=9008, vice=9009), headers=bearer_headers(user_id)
    ).status_code == 200
    assert sorted(r.player_id for r in _free_hit_rows(engine, user_id, 2)) == pre_chip_squad

    # Only gameweek 2's FIRST fixture kicks off; the second is still days out.
    _advance_deadline(engine, gameweek=2, hours_ago=2, only_first=True)
    assert deadline_has_passed(engine, TEST_SEASON, 2) is True
    assert (TEST_SEASON, 2) in lock_expired_gameweeks(engine)["locked"]

    # The gameweek is under way but not over, so the Free Hit stands.
    assert (user_id, TEST_SEASON, 2) not in revert_expired_free_hits(engine)["reverted"]
    assert 9502 in _active_player_ids(engine, user_id)

    # Scoring reads starting_xi, which now names the free-hit 15 (the
    # re-pick above) -- NOT the snapshot, which only drives the revert.
    _seed_all_stats(make_gw_stat, internal_ids, 2, fh_xi + gw1_bench, minutes=90, total_points=2)

    _finish_gameweek(engine, gameweek=2)
    refreshed = refresh_active_gameweeks(engine)["refreshed"]
    assert (TEST_SEASON, 1) in refreshed  # re-scored, idempotently
    assert (TEST_SEASON, 2) in refreshed

    #   raw   = 11 * 2 = 22, everyone played so no autosub
    #   final = 22 + 2 (captain 9008), no hits under free hit
    gw2 = _gw_score_row(engine, user_id, 2)
    assert (gw2.raw_points, gw2.final_points, gw2.transfer_hits, gw2.hit_deductions,
            gw2.total_points, gw2.season_total) == (22, 24, 0, 0, 24, 79)

    gw1_again = _gw_score_row(engine, user_id, 1)
    assert (gw1_again.raw_points, gw1_again.final_points, gw1_again.transfer_hits,
            gw1_again.hit_deductions, gw1_again.total_points, gw1_again.season_total) == (54, 59, 1, 4, 55, 55)

    result = revert_expired_free_hits(engine)
    assert (user_id, TEST_SEASON, 2) in result["reverted"]
    assert not [f for f in result["failed"] if f[0] == user_id]

    assert _active_player_ids(engine, user_id) == sorted(gw1_xi + gw1_bench)
    assert _budget_remaining(engine, user_id) == 100
    assert _free_hit_rows(engine, user_id, 2, unreverted_only=True) == []

    # Gameweek 3 has no fixtures ingested, so its deadline is unknown and
    # the window is open -- the deliberate "an unknown deadline never
    # freezes a gameweek nobody can play" stance (deadlines.deadline_has_passed).
    used = client.get("/transfers/used", params={"season": TEST_SEASON, "gameweek": 3}, headers=bearer_headers(user_id)).json()
    assert used["free_transfers_remaining"] == 2
    assert client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": 3,
              "transfers": [{"player_out_id": 9500, "player_in_id": 9502}]}, headers=bearer_headers(user_id)
    ).status_code == 200
