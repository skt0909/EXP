"""B1 anchor (option B): ruleset_epochs, the stored first new-rules Gameweek.

The anchor is STORED, never derived. The earlier proposal -- "the earliest
gameweek with a gw_selections row" -- was rejected because those rows are freely
deletable and cascade from users, so deleting one account could move the anchor
and silently restate every manager's allowance. This table is append-only and
has no foreign keys, so nothing cascades into it and nothing can move it.

`test_deleting_a_user_does_not_move_any_other_managers_allowance` is the test
that would have failed under the rejected definition. It is the point of the
whole change.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON
from main import app
from Shared.rules import FIRST_GAMEWEEK, RULES_VERSION

client = TestClient(app)


@pytest.fixture
def epoch(engine):
    """Set this season's epoch, and clean it up. The table is append-only, so
    the fixture has to disable the guard to tidy up after itself -- which is
    itself a demonstration that ordinary code cannot."""
    created = []

    def _set(first_gameweek, season=TEST_SEASON, rules_version=RULES_VERSION):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO ruleset_epochs (season, rules_version, first_gameweek) "
                "VALUES (:s, :rv, :fg) ON CONFLICT DO NOTHING"),
                {"s": season, "rv": rules_version, "fg": first_gameweek})
        created.append((season, rules_version))
        return first_gameweek

    yield _set

    for season, rv in created:
        with engine.begin() as conn:
            conn.execute(text("ALTER TABLE ruleset_epochs DISABLE TRIGGER enforce_ruleset_epochs_immutability"))
            conn.execute(text(
                "DELETE FROM ruleset_epochs WHERE season = :s AND rules_version = :rv"),
                {"s": season, "rv": rv})
            conn.execute(text("ALTER TABLE ruleset_epochs ENABLE TRIGGER enforce_ruleset_epochs_immutability"))


# ---- the table is append-only ---------------------------------------------

def test_updating_a_row_raises(engine, epoch):
    epoch(7)
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE ruleset_epochs SET first_gameweek = 9 WHERE season = :s"),
                {"s": TEST_SEASON})
    assert "append-only" in str(exc.value).lower() or "immutable" in str(exc.value).lower()


def test_deleting_a_row_raises(engine, epoch):
    epoch(7)
    with pytest.raises(Exception) as exc:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM ruleset_epochs WHERE season = :s"),
                         {"s": TEST_SEASON})
    assert "append-only" in str(exc.value).lower() or "immutable" in str(exc.value).lower()


def test_the_table_has_no_foreign_keys_so_nothing_cascades_into_it(engine):
    with engine.connect() as conn:
        n = conn.execute(text(
            "SELECT count(*) FROM pg_constraint "
            "WHERE conrelid = 'ruleset_epochs'::regclass AND contype = 'f'")).scalar()
    assert n == 0


def test_reinserting_the_same_epoch_is_a_no_op_not_a_duplicate(engine, epoch):
    epoch(7)
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO ruleset_epochs (season, rules_version, first_gameweek) "
            "VALUES (:s, :rv, 99) ON CONFLICT DO NOTHING"),
            {"s": TEST_SEASON, "rv": RULES_VERSION})
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT first_gameweek FROM ruleset_epochs WHERE season = :s AND rules_version = :rv"),
            {"s": TEST_SEASON, "rv": RULES_VERSION}).all()
    assert [r.first_gameweek for r in rows] == [7]


# ---- the helper ------------------------------------------------------------

def test_no_epoch_row_falls_back_to_first_gameweek(engine):
    from Gameplay.transfers import ruleset_first_gameweek
    with engine.connect() as conn:
        assert ruleset_first_gameweek(conn, "no-such-season") == FIRST_GAMEWEEK


def test_an_epoch_row_is_used_when_present(engine, epoch):
    from Gameplay.transfers import ruleset_first_gameweek
    epoch(7)
    with engine.connect() as conn:
        assert ruleset_first_gameweek(conn, TEST_SEASON) == 7


# ---- the allowance, anchored on a stored epoch ----------------------------

@pytest.fixture
def manager(engine, make_user, auth_headers):
    user_id = make_user()
    yield {"user_id": user_id, "headers": auth_headers(user_id)}


def _used(manager, gameweek):
    resp = client.get(f"/transfers/used?season={TEST_SEASON}&gameweek={gameweek}",
                      headers=manager["headers"])
    assert resp.status_code == 200, resp.text
    return resp.json()["free_transfers_remaining"]


def test_an_epoch_of_seven_gives_one_at_gameweek_seven(epoch, manager):
    epoch(7)
    assert _used(manager, 7) == 1


def test_an_epoch_of_seven_gives_two_at_gameweek_eight_with_nothing_used(epoch, manager):
    epoch(7)
    assert _used(manager, 8) == 2


def test_without_an_epoch_the_allowance_behaves_as_it_did_before(manager):
    # A missing row means the season started under these rules at gameweek 1.
    assert _used(manager, 1) == 1
    assert _used(manager, 2) == 2


# ---- transfers before the epoch -------------------------------------------

def test_a_transfer_before_the_epoch_is_ignored_and_warned(engine, epoch, manager, caplog):
    import logging
    from Gameplay.transfers import free_transfers_available

    epoch(7)
    # A transfer in gameweek 3, three gameweeks before the new rules began.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO transfers (user_id, season, gameweek, player_in_id, "
            "player_out_id, price_in, price_out, is_free, transferred_at) "
            "VALUES (:u, :s, 3, 1, 2, 50, 50, TRUE, now())"),
            {"u": manager["user_id"], "s": TEST_SEASON})

    with caplog.at_level(logging.WARNING):
        with engine.connect() as conn:
            available = free_transfers_available(conn, manager["user_id"], TEST_SEASON, 8)

    # Ignored: the allowance is the same 2 it would be with no history at all.
    assert available == 2
    assert any("before the ruleset epoch" in r.getMessage() for r in caplog.records)


# ---- the test the rejected definition would have failed -------------------

def test_deleting_a_user_does_not_move_any_other_managers_allowance(
    engine, epoch, make_user, auth_headers
):
    """Under the rejected anchor this was the silent failure: delete the user
    holding the earliest gw_selections row and MIN() returns a later gameweek,
    restating everyone's allowance. With a stored epoch it cannot happen."""
    epoch(7)
    victim = make_user()
    survivor = make_user()
    survivor_headers = auth_headers(survivor)

    # The victim holds the earliest selection in the season.
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO gw_selections (user_id, season, gameweek, tactic, submitted_at) "
            "VALUES (:u, :s, 7, 'balanced', now())"),
            {"u": victim, "s": TEST_SEASON})

    before = client.get(f"/transfers/used?season={TEST_SEASON}&gameweek=9",
                        headers=survivor_headers).json()["free_transfers_remaining"]

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": victim})

    after = client.get(f"/transfers/used?season={TEST_SEASON}&gameweek=9",
                       headers=survivor_headers).json()["free_transfers_remaining"]

    assert before == after == 2
