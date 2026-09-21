"""Phase 4a fix E4: failure isolation in the batched scoring job.

The 4a job computed each manager in its own try/except but wrote the whole
batch in ONE transaction. So a write that failed took every manager in the
batch with it, where the old per-manager job lost only one. These tests pin the
two halves of the fix: a poisoned manager must not stop the others, and a
failed batch write must fall back to writing managers one at a time.

They also pin the reporting requirement: a run with failures must not be
reported as fully successful.
"""
import logging

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from conftest import TEST_SEASON
from Results import scoring_job
from Results.scoring_job import score_gameweek_tactical
from Tests.test_scoring_job_tactical import _score_row, _seed_stats, _squad_for

GAMEWEEK = 9
BASE = 8200


def _five_managers(engine, make_user, make_team, make_player, make_fixture, make_gw_stat):
    users, squads = [], []
    for i in range(5):
        uid = make_user()
        pairs = _squad_for(engine, make_team, make_player, make_fixture, uid,
                           BASE + i * 60, "balanced", (7, 8), gameweek=GAMEWEEK)
        _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)
        users.append(uid)
        squads.append(pairs)
    return users, squads


def _poison(engine, user_id):
    """Point one starting_xi row at a player that does not exist in ml.players
    for this season. The engine looks positions up by player id, so this is a
    KeyError at scoring time -- the same shape as the real data problem that
    surfaced during 4a development (ambiguity E6)."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE starting_xi SET player_id = 999999 "
            "WHERE gw_selection_id IN (SELECT id FROM gw_selections WHERE user_id = :u) "
            "AND position_slot = 3"), {"u": user_id})


# ---- one poisoned manager must not stop the other four -------------------

def test_a_poisoned_manager_is_skipped_and_the_others_are_written(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, caplog
):
    users, _ = _five_managers(engine, make_user, make_team, make_player,
                              make_fixture, make_gw_stat)
    victim = users[2]
    _poison(engine, victim)

    with caplog.at_level(logging.ERROR):
        summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    assert victim not in summary["scored"]
    assert victim in [uid for uid, _ in summary["failed"]]
    for other in users:
        if other is victim:
            continue
        assert other in summary["scored"]
        assert _score_row(engine, other, GAMEWEEK) is not None
    assert _score_row(engine, victim, GAMEWEEK) is None

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert str(victim) in messages
    assert TEST_SEASON in messages
    assert str(GAMEWEEK) in messages


def test_the_failure_is_reported_so_a_run_with_failures_is_not_fully_successful(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    users, _ = _five_managers(engine, make_user, make_team, make_player,
                              make_fixture, make_gw_stat)
    _poison(engine, users[0])
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert summary["failed"], "failures must be reported through the job result"
    assert len(summary["failed"]) >= 1


# ---- a failed batch write falls back to one manager at a time ------------

def test_a_batch_write_failure_falls_back_to_per_manager_writes(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat,
    monkeypatch, caplog
):
    """The E4 fix. The batch write is made to fail once; every manager must
    still end up written, one at a time, rather than the whole batch being
    lost."""
    users, _ = _five_managers(engine, make_user, make_team, make_player,
                              make_fixture, make_gw_stat)

    real = scoring_job._execute_writes
    calls = {"n": 0}

    def flaky(conn, writes, finance_writes):
        calls["n"] += 1
        if len(writes) > 1:
            raise SQLAlchemyError("simulated batch write failure")
        return real(conn, writes, finance_writes)

    monkeypatch.setattr(scoring_job, "_execute_writes", flaky)

    with caplog.at_level(logging.ERROR):
        summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    for uid in users:
        assert uid in summary["scored"], f"{uid} lost to a batch write failure"
        assert _score_row(engine, uid, GAMEWEEK) is not None
    assert calls["n"] > 1, "the fallback never ran"
    assert any("falling back" in r.getMessage().lower() for r in caplog.records)


def test_the_fallback_still_isolates_a_single_bad_write(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, monkeypatch
):
    """Batch write fails, and then ONE manager's individual write fails too.
    The other four must still be written, and the offender reported."""
    users, _ = _five_managers(engine, make_user, make_team, make_player,
                              make_fixture, make_gw_stat)
    doomed = users[3]

    real = scoring_job._execute_writes

    def flaky(conn, writes, finance_writes):
        if len(writes) > 1:
            raise SQLAlchemyError("simulated batch write failure")
        if writes and writes[0]["user_id"] == doomed:
            raise SQLAlchemyError("simulated single write failure")
        return real(conn, writes, finance_writes)

    monkeypatch.setattr(scoring_job, "_execute_writes", flaky)
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    assert doomed in [uid for uid, _ in summary["failed"]]
    assert doomed not in summary["scored"]
    for other in users:
        if other is doomed:
            continue
        assert _score_row(engine, other, GAMEWEEK) is not None


# ---- self-healing ----------------------------------------------------------

def test_a_second_run_after_fixing_the_data_scores_the_missing_manager(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    users, squads = _five_managers(engine, make_user, make_team, make_player,
                                   make_fixture, make_gw_stat)
    victim = users[1]
    # XI slot 3 holds pairs index 3: xi = [0, 2, 3, 4, 5, 7, 8, 9, 10, 12, 13].
    original = squads[1][3][0]
    _poison(engine, victim)

    first = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert victim in [uid for uid, _ in first["failed"]]
    assert _score_row(engine, victim, GAMEWEEK) is None

    # Fix the data.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE starting_xi SET player_id = :p "
            "WHERE gw_selection_id IN (SELECT id FROM gw_selections WHERE user_id = :u) "
            "AND position_slot = 3"), {"p": original, "u": victim})

    second = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert victim in second["scored"]
    assert second["failed"] == [] or victim not in [uid for uid, _ in second["failed"]]
    assert _score_row(engine, victim, GAMEWEEK) is not None


# ---- refresh_active_gameweeks must not call a failing gameweek a success ---

def test_refresh_active_gameweeks_surfaces_per_manager_failures(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, monkeypatch
):
    """refresh_active_gameweeks used to DISCARD score_gameweek's summary, so a
    gameweek with failed managers was reported as refreshed -- fully
    successful. It must report them."""
    from GameEngine import gameweek_finalize

    monkeypatch.setattr(gameweek_finalize, "find_active_gameweeks",
                        lambda engine, window_days=5: [(TEST_SEASON, GAMEWEEK)])
    monkeypatch.setattr(gameweek_finalize, "compute_league_standings",
                        lambda engine, season, gameweek: {"leagues": []})

    users, _ = _five_managers(engine, make_user, make_team, make_player,
                              make_fixture, make_gw_stat)
    _poison(engine, users[4])

    result = gameweek_finalize.refresh_active_gameweeks(engine)

    assert (TEST_SEASON, GAMEWEEK) not in result["refreshed"], \
        "a gameweek with failed managers must not be reported as refreshed"
    assert any(season == TEST_SEASON and gw == GAMEWEEK
               for season, gw, _ in result["failed"])
