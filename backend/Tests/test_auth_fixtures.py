"""
test_auth_fixtures.py — pins the identity fixtures conftest.py provides:
make_user, auth_headers and authed_client.

Stage 1 of the auth cutover added those three so that Stage 2 can move all
17 classic endpoints from a caller-supplied user_id to get_current_user.
Until Stage 2 lands, NOTHING ELSE in the suite exercises them -- the six
files that use make_user only ever need an id, and never authenticate. So
without this file a broken fixture would sit undetected until the moment
thirteen files start depending on it at once, which is the worst possible
time to discover it.

Kept rather than thrown away for that reason. Stage 2 will add the real
coverage (401 on every endpoint, and user A denied user B's data); these
tests stay useful underneath it as the contract for the fixtures
themselves, not for any endpoint.

WHY THE TOKEN IS MINTED, NOT LOGGED IN FOR. make_user writes
password_hash = 'not_a_real_hash', and auth.verify_password returns False
rather than raising on a malformed digest -- a deliberate choice so that
rows predating the auth module simply fail to log in. Every test user is
therefore un-loggable by design, and test_login_is_impossible_for_a_test_user
below pins that, because it is the reason auth_headers exists in the shape
it does. create_access_token needs only an id; get_current_user needs only
the users row to exist.

No user is deleted here (or by conftest's make_user): a user who has
transferred cannot be removed at all, since enforce_transfers_immutability_fn
blocks deleting through the transfers table. uuid identity is what makes the
leftover rows harmless -- the same stance test_leagues.py takes.
"""

from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON
from main import app as fastapi_app

client = TestClient(fastapi_app)


# ---------------------------------------------------------------- make_user


def test_make_user_creates_distinct_persisted_users(make_user, engine):
    a, b = make_user(), make_user()
    assert a != b

    with engine.connect() as conn:
        found = conn.execute(
            text("SELECT id FROM users WHERE id IN (:a, :b)"), {"a": a, "b": b}
        ).scalars().all()
    assert sorted(found) == sorted([a, b])


def test_make_user_names_rows_after_the_calling_module(make_user, engine):
    """The default prefix is derived from the test module, so a leftover row
    still says which file created it. The six files that override this
    fixture pass their historical prefix explicitly instead."""
    uid = make_user()
    with engine.connect() as conn:
        username = conn.execute(
            text("SELECT username FROM users WHERE id = :i"), {"i": uid}
        ).scalar()
    assert username.startswith("pytest_auth_fixtures_")


def test_make_user_tracks_what_it_created(make_user):
    """`_make.created` is what the overriding fixtures read in their own
    teardown, so it is part of the contract, not an implementation detail."""
    before = list(make_user.created)
    uid = make_user()
    assert make_user.created == before + [uid]


# ------------------------------------------------------------- auth_headers


def test_login_is_impossible_for_a_test_user(make_user, engine):
    """The constraint that rules out a login-based fixture."""
    uid = make_user()
    with engine.connect() as conn:
        email = conn.execute(
            text("SELECT email FROM users WHERE id = :i"), {"i": uid}
        ).scalar()

    resp = client.post("/auth/login", json={"email": email, "password": "not_a_real_hash"})
    assert resp.status_code == 401


def test_auth_headers_mints_a_token_the_server_accepts(make_user, auth_headers):
    uid = make_user()
    resp = client.get("/auth/me", headers=auth_headers(uid))
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == uid


def test_without_the_header_the_same_call_is_401():
    """Proves the header is doing the work, not something ambient."""
    assert client.get("/auth/me").status_code == 401


# ------------------------------------------------------------ authed_client


def test_authed_client_is_the_given_user(make_user, authed_client):
    uid = make_user()
    resp = authed_client(uid).get("/auth/me")
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == uid


def test_two_authed_clients_stay_distinct(make_user, authed_client):
    """Needed by the multi-user tests in test_leagues.py and test_dream11.py."""
    a, b = make_user(), make_user()
    assert authed_client(a).get("/auth/me").json()["id"] == a
    assert authed_client(b).get("/auth/me").json()["id"] == b


def test_authed_client_reaches_an_ordinary_endpoint(make_user, authed_client):
    """GET /squad still takes user_id as a query parameter -- Stage 2 is what
    changes that. This asserts only that carrying the header breaks nothing
    today; after Stage 2 the same call drops the parameter and the identity
    comes from the token instead.

    A user with no squad is a legitimate 200 with an empty list, not a 404,
    so this needs no seeding.
    """
    uid = make_user()
    resp = authed_client(uid).get(
        "/squad", params={"user_id": uid, "season": TEST_SEASON}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["players"] == []
