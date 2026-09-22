"""GET /team reports `is_locked`, computed the same way as everywhere else.

The dashboard was the only one of the three places that care about the lock
which could not answer "can this manager still change anything?". It had
`has_lineup`, which is a different question -- a manager who never submitted
has `has_lineup=False` both the day before the deadline, when they can still
act, and a week after it, when they cannot.

THE LOCK HAS TWO INDEPENDENT SOURCES and the codebase already OR's them in
two places. Gameplay/transfers.py:447-448, quoted exactly:

    locked_by_flag = gw_selection_row is not None and gw_selection_row.is_locked
    if locked_by_flag or deadline_passed:

and Gameplay/starting_xi.py does the same job with the same two sources -- the
trigger `enforce_selection_lock_fn` covers the already-locked ROW, and
`deadline_has_passed` covers the never-submitted case the trigger structurally
cannot see, because there is no row for a BEFORE UPDATE trigger to fire on.

The dashboard reuses that formula rather than inventing a fourth answer. These
tests exist to pin the two halves separately: a test that only checked a
submitted, locked selection would pass against an implementation that read the
flag alone, which is exactly the bug transfers.py's comment describes.
"""
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON
from main import app

client = TestClient(app)

GAMEWEEK = 21


def _team(user_id, auth_headers, gameweek=GAMEWEEK):
    resp = client.get(f"/team?season={TEST_SEASON}&gameweek={gameweek}",
                      headers=auth_headers(user_id))
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture
def deadline(engine, make_team, make_fixture):
    """Put this gameweek's only kickoff a given number of days from now.

    The deadline is derived from ml.fixtures, never stored, so this is the
    only honest way to move it -- and it is the same technique the rest of the
    suite uses, for the reason deadlines.py's docstring gives: freezing
    Python's clock would move the wrong clock, because every comparison
    happens in SQL.
    """
    def _set(days, gameweek=GAMEWEEK):
        home = make_team(fpl_id=79001, name="LockH", short_name="LKH")
        away = make_team(fpl_id=79002, name="LockA", short_name="LKA")
        with engine.connect() as conn:
            kickoff = conn.execute(
                text("SELECT now() + make_interval(days => :d)"), {"d": days}).scalar()
        make_fixture(fpl_id=79010, gameweek=gameweek, home_team_id=home,
                     away_team_id=away, kickoff_time=kickoff)

    return _set


@pytest.fixture
def submitted(engine):
    """A gw_selections row for this manager, optionally already locked."""
    def _submit(user_id, is_locked, gameweek=GAMEWEEK):
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO gw_selections (user_id, season, gameweek, tactic, "
                "submitted_at, is_locked) VALUES (:u, :s, :g, 'balanced', now(), :l)"),
                {"u": user_id, "s": TEST_SEASON, "g": gameweek, "l": is_locked})

    return _submit


# ---- the never-submitted case, which the flag alone cannot answer ---------

def test_a_fresh_manager_before_the_deadline_is_not_locked(
    make_user, auth_headers, deadline
):
    """No gw_selections row and the deadline still ahead: they can act."""
    uid = make_user()
    deadline(days=3)

    assert _team(uid, auth_headers)["is_locked"] is False


def test_a_fresh_manager_after_the_deadline_is_locked(
    make_user, auth_headers, deadline
):
    """No gw_selections row, so no is_locked flag exists anywhere -- and yet
    the gameweek kicked off two days ago and nothing about it can be changed.
    Reading the flag alone would say False here, which is the bug
    Gameplay/transfers.py documents at line 137."""
    uid = make_user()
    deadline(days=-2)

    assert _team(uid, auth_headers)["is_locked"] is True


# ---- the submitted case ---------------------------------------------------

def test_a_submitted_and_locked_selection_is_locked(
    make_user, auth_headers, deadline, submitted
):
    """The flag alone is enough here. The deadline is deliberately still in
    the future so that ONLY the flag can be producing the True -- otherwise
    this test would pass against an implementation that ignored it."""
    uid = make_user()
    deadline(days=4)
    submitted(uid, is_locked=True)

    assert _team(uid, auth_headers)["is_locked"] is True


def test_a_submitted_selection_before_the_deadline_is_not_locked(
    make_user, auth_headers, deadline, submitted
):
    """Submitted early and still editable -- the ordinary mid-week state."""
    uid = make_user()
    deadline(days=4)
    submitted(uid, is_locked=False)

    body = _team(uid, auth_headers)
    assert body["is_locked"] is False
    assert body["has_lineup"] is True


# ---- is_locked is not has_lineup ------------------------------------------

def test_is_locked_and_has_lineup_answer_different_questions(
    make_user, auth_headers, deadline
):
    """Both are False for a fresh manager pre-deadline and they diverge the
    moment the deadline passes. Pinned because has_lineup was the field the
    dashboard was using as a stand-in for both."""
    uid = make_user()
    deadline(days=-2)

    body = _team(uid, auth_headers)
    assert body["has_lineup"] is False
    assert body["is_locked"] is True


def test_an_unknown_deadline_does_not_lock_the_gameweek(
    make_user, auth_headers
):
    """No fixtures ingested for this gameweek at all. deadline_has_passed
    treats an unknown deadline as 'not passed' on purpose, so missing
    ingestion can never silently freeze a gameweek nobody can play -- the
    dashboard must inherit that, not invent a stricter answer."""
    uid = make_user()

    assert _team(uid, auth_headers)["is_locked"] is False
