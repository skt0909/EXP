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
    """Store a tactic the engine cannot score, behind the CHECK constraint.

    This used to point a starting_xi row at a player absent from ml.players,
    which raised KeyError and failed the manager. Closing ambiguity E6 made
    that survivable by design -- such a player now scores 0 and the rest of the
    manager is scored -- so it is no longer a poison. A tactic value outside
    ck_gw_selections_tactic still raises (ValueError from the engine), and is
    the same shape of problem: data the endpoint would never have written.
    """
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE gw_selections DROP CONSTRAINT ck_gw_selections_tactic"))
        conn.execute(text(
            "UPDATE gw_selections SET tactic = 'bogus' "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": user_id, "s": TEST_SEASON, "g": GAMEWEEK})
        conn.execute(text(
            "ALTER TABLE gw_selections ADD CONSTRAINT ck_gw_selections_tactic "
            "CHECK (tactic IN ('attack', 'defence', 'balanced')) NOT VALID"))


def _poison_missing_player(engine, user_id, slot=3, fake_id=999999):
    """Point one starting_xi row at a player absent from ml.players. Since E6
    this is survivable: he scores 0 and the manager is scored."""
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE starting_xi SET player_id = :f "
            "WHERE gw_selection_id IN (SELECT id FROM gw_selections WHERE user_id = :u) "
            "AND position_slot = :s"), {"f": fake_id, "u": user_id, "s": slot})


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
    users, _ = _five_managers(engine, make_user, make_team, make_player,
                              make_fixture, make_gw_stat)
    victim = users[1]
    _poison(engine, victim)

    first = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert victim in [uid for uid, _ in first["failed"]]
    assert _score_row(engine, victim, GAMEWEEK) is None

    # Fix the data.
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE gw_selections SET tactic = 'balanced' "
            "WHERE user_id = :u AND season = :s AND gameweek = :g"),
            {"u": victim, "s": TEST_SEASON, "g": GAMEWEEK})

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


# ---- the season filter still refuses non-real seasons, unconditionally ----

def test_a_sim_season_is_still_refused(engine):
    summary = score_gameweek_tactical(engine, "SIM38OK", 1)
    assert summary["scored"] == []
    assert "season" in (summary["skipped_reason"] or "")


# ---- E6: a selected player missing from ml.players ------------------------
#
# FORMATION LEGALITY for such a player: he is given the position "UNKNOWN",
# which matches none of FORMATION_MIN's keys, so he counts towards NO minimum.
# That is the honest reading -- we do not know whether he was a defender, so he
# cannot be credited with satisfying the defensive minimum. The practical
# effect is that an Auto Sub cover is judged on the other ten starters, which
# makes covering STRICTER rather than looser: a line that depended on him to
# reach its floor will refuse the cover instead of allowing one on a guess.

def test_a_player_missing_from_ml_players_does_not_fail_the_manager(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat, caplog
):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid,
                       BASE + 900, "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)
    _poison_missing_player(engine, uid)

    with caplog.at_level(logging.WARNING):
        summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)

    assert uid in summary["scored"], "the manager must still be scored"
    assert uid not in [u for u, _ in summary["failed"]]

    row = _score_row(engine, uid, GAMEWEEK)
    assert row is not None
    # 10 of the 11 starters score 2 each = 20. The missing player contributes
    # 0 -- and, having no minutes, he counts as a no-show, so the outfield Auto
    # Sub (a defender, like the slot he covers) comes on for 2 more. Being
    # UNCOVERABLE is not the rule; being unusable AS a cover is.
    assert row.raw_points == 22

    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "data-integrity" in messages.lower()
    assert "999999" in messages
    assert str(uid) in messages and TEST_SEASON in messages and str(GAMEWEEK) in messages


def test_a_missing_player_cannot_be_used_as_an_auto_sub_cover(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """The bench Auto Sub is the missing one, and a starter did not play. With
    no position on record he cannot be judged legal, so no cover happens and
    the starter simply scores 0."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid,
                       BASE + 1000, "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, {4: dict(minutes=0)}, gameweek=GAMEWEEK)
    with engine.begin() as conn:
        conn.execute(text(
            "UPDATE starting_xi SET player_id = 999998 "
            "WHERE gw_selection_id IN (SELECT id FROM gw_selections WHERE user_id = :u) "
            "AND position_slot = 13"), {"u": uid})

    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert uid in summary["scored"]
    row = _score_row(engine, uid, GAMEWEEK)
    # 10 starters at 2; the no-show contributes 0 and is NOT covered.
    assert row.raw_points == 20


def test_the_rest_of_the_manager_scores_normally_around_a_missing_player(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid,
                       BASE + 1100, "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs,
                {7: dict(minutes=90, goals_scored=1, creativity=40)},
                gameweek=GAMEWEEK)
    _poison_missing_player(engine, uid)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    row = _score_row(engine, uid, GAMEWEEK)
    # 9 ordinary starters x 2 = 18, plus the Bonus MID's 7 = 25, plus the Auto
    # Sub who covers the missing player = 27. Tactical points are unaffected by
    # any of it.
    assert row.raw_points == 27
    assert row.tactical_points == 4
