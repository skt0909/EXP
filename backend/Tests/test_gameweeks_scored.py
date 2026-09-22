"""The `gameweeks` table and `scored_at`.

`scored_at` is the first thing in this schema that records "this gameweek is
done". Everything before it was inferred: all fixtures `finished` means the
matches are over, not that they were scored; a `gw_scores` row means the job
ran, not that it will not run again; and the 5-day active window means
"probably final" by elapsed time rather than by state.

TWO CONDITIONS, both required (see scoring_job._mark_gameweek_scored):
  1. the batch run finished, and
  2. every fixture in that gameweek has finished = TRUE.

Scores written while fixtures are still in play are deliberately NOT marked,
because the 15-minute job will revisit and change them.
"""
import pytest
from sqlalchemy import text

from conftest import TEST_SEASON
from Results.scoring_job import score_gameweek_tactical
from Tests.test_scoring_job_tactical import _seed_stats, _squad_for

GAMEWEEK = 17
BASE = 9800


@pytest.fixture(autouse=True)
def _clean_gameweeks(engine):
    def _wipe():
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM gameweeks WHERE season = :s"), {"s": TEST_SEASON})
    _wipe()
    yield
    _wipe()


def _scored_at(engine, gameweek=GAMEWEEK):
    with engine.connect() as conn:
        return conn.execute(text(
            "SELECT scored_at FROM gameweeks WHERE season = :s AND gameweek = :g"),
            {"s": TEST_SEASON, "g": gameweek}).scalar()


def _manager_with_fixture(engine, make_user, make_team, make_player, make_fixture,
                          make_gw_stat, offset, finished, gameweek=GAMEWEEK):
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid, offset,
                       "balanced", (7, 8), gameweek=gameweek)
    _seed_stats(make_gw_stat, pairs, gameweek=gameweek)
    home = make_team(fpl_id=offset + 400, name=f"H{offset}", short_name=f"{offset % 1000:03d}")
    away = make_team(fpl_id=offset + 401, name=f"A{offset}", short_name=f"{(offset + 1) % 1000:03d}")
    make_fixture(fpl_id=offset + 410, gameweek=gameweek, home_team_id=home,
                 away_team_id=away, kickoff_time="2030-01-01T12:00:00+00:00",
                 finished=finished)
    return uid


# ---- the table -------------------------------------------------------------

def test_the_gameweeks_table_exists_with_the_agreed_shape(engine):
    with engine.connect() as conn:
        cols = {r.column_name: r.data_type for r in conn.execute(text(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='gameweeks'"))}
    assert cols == {
        "season": "character varying",
        "gameweek": "smallint",
        "scored_at": "timestamp with time zone",
    }
    with engine.connect() as conn:
        pk = conn.execute(text(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid='gameweeks'::regclass AND contype='p'")).scalar()
    assert pk == "PRIMARY KEY (season, gameweek)"


def test_scored_at_is_nullable(engine):
    """A row can exist before the gameweek is scored -- NULL means 'known
    about, not finished'."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gameweeks (season, gameweek) VALUES (:s, 99)"), {"s": TEST_SEASON})
    assert _scored_at(engine, 99) is None


# ---- condition 2: every fixture finished ----------------------------------

def test_a_gameweek_is_not_marked_while_a_fixture_is_unfinished(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """Scores ARE written -- they just are not final, because the 15-minute
    job will revisit them."""
    uid = _manager_with_fixture(engine, make_user, make_team, make_player,
                                make_fixture, make_gw_stat, BASE, finished=False)
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert uid in summary["scored"]
    assert _scored_at(engine) is None, "marked scored while a fixture was still in play"


def test_a_gameweek_is_marked_once_every_fixture_has_finished(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    uid = _manager_with_fixture(engine, make_user, make_team, make_player,
                                make_fixture, make_gw_stat, BASE + 100, finished=True)
    summary = score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert uid in summary["scored"]
    assert _scored_at(engine) is not None


def test_one_unfinished_fixture_among_several_is_enough_to_hold_it_back(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    _manager_with_fixture(engine, make_user, make_team, make_player,
                          make_fixture, make_gw_stat, BASE + 200, finished=True)
    # A second fixture in the same gameweek, still in play.
    home = make_team(fpl_id=98001, name="LateH", short_name="LTH")
    away = make_team(fpl_id=98002, name="LateA", short_name="LTA")
    make_fixture(fpl_id=98010, gameweek=GAMEWEEK, home_team_id=home, away_team_id=away,
                 kickoff_time="2030-01-01T12:00:00+00:00", finished=False)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert _scored_at(engine) is None


def test_a_gameweek_with_no_fixtures_at_all_is_not_marked(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """No fixtures means nothing to be finished. Marking it would claim a
    gameweek was completed when it was never played."""
    uid = make_user()
    pairs = _squad_for(engine, make_team, make_player, make_fixture, uid,
                       BASE + 300, "balanced", (7, 8), gameweek=GAMEWEEK)
    _seed_stats(make_gw_stat, pairs, gameweek=GAMEWEEK)
    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert _scored_at(engine) is None


# ---- idempotence -----------------------------------------------------------

def test_running_twice_does_not_move_an_already_set_scored_at(
    engine, make_user, make_team, make_player, make_fixture, make_gw_stat
):
    """The 15-minute job re-runs a gameweek for five days. scored_at records
    when it was FIRST complete; letting each run overwrite it would make it a
    'last touched' timestamp instead."""
    _manager_with_fixture(engine, make_user, make_team, make_player,
                          make_fixture, make_gw_stat, BASE + 400, finished=True)

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    first = _scored_at(engine)
    assert first is not None

    score_gameweek_tactical(engine, TEST_SEASON, GAMEWEEK)
    assert _scored_at(engine) == first

    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM gameweeks WHERE season = :s AND gameweek = :g"),
            {"s": TEST_SEASON, "g": GAMEWEEK}).scalar()
    assert n == 1


def test_a_skipped_run_never_marks_anything(engine):
    """A season the job refuses, or a gameweek before the epoch, must not
    leave a scored_at behind."""
    summary = score_gameweek_tactical(engine, "SIM38OK", GAMEWEEK)
    assert summary["skipped_reason"] is not None
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM gameweeks WHERE season = 'SIM38OK'")).scalar()
    assert n == 0
