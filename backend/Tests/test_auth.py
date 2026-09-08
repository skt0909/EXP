"""
test_auth.py — FastAPI TestClient tests for Data/auth.py's
POST /auth/register, POST /auth/login, and GET /auth/me.

Hits the real database like every other suite here (no mocks). Registered
users are created through the endpoint itself rather than seeded via SQL --
the whole point is to exercise hashing and the UNIQUE constraints -- so
each test registers under a uuid-suffixed email/username and the
`registered` fixture deletes whatever it created afterwards.

The "no account-existence leak" property is asserted structurally: the
wrong-password and unknown-email responses are compared to each other for
byte-identical status AND body, so the two paths can't drift apart later
without failing here.
"""

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from Data.auth import JWT_ALGORITHM, _jwt_secret
from main import app

client = TestClient(app)

PASSWORD = "correcthorse123"


def _unique(prefix: str = "pytest_auth") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def cleanup_users(engine):
    """Deletes every user this test created, by id. Registration is done
    through the endpoint, so ids aren't known until then -- tests append
    to the returned list."""
    created: list[int] = []
    yield created
    if created:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": created})


def _register(cleanup_users, **overrides):
    handle = _unique()
    body = {
        "email": f"{handle}@example.com",
        "username": handle,
        "password": PASSWORD,
        "team_name": "Test FC",
    }
    body.update(overrides)
    resp = client.post("/auth/register", json=body)
    if resp.status_code == 200:
        cleanup_users.append(resp.json()["user_id"])
    return resp, body


# --------------------------------------------------------------- register


def test_register_success_returns_token_and_team_name(cleanup_users):
    resp, body = _register(cleanup_users)
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["token_type"] == "bearer"
    assert isinstance(data["user_id"], int)
    assert data["team_name"] == "Test FC"

    decoded = jwt.decode(data["access_token"], _jwt_secret(), algorithms=[JWT_ALGORITHM])
    assert decoded["user_id"] == data["user_id"]


def test_register_writes_team_name_to_users_table(engine, cleanup_users):
    """team_name lands on public.users -- the column team_dashboard.py and
    leagues.py already read -- not on user_squads."""
    resp, _ = _register(cleanup_users, team_name="Hash United")
    assert resp.status_code == 200

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT team_name FROM users WHERE id = :uid"), {"uid": resp.json()["user_id"]}
        ).scalar()
    assert stored == "Hash United"


def test_register_does_not_create_user_squads_row(engine, cleanup_users):
    resp, _ = _register(cleanup_users)
    assert resp.status_code == 200

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM user_squads WHERE user_id = :uid"),
            {"uid": resp.json()["user_id"]},
        ).scalar()
    assert count == 0


def test_register_duplicate_email_rejected(cleanup_users):
    first, body = _register(cleanup_users)
    assert first.status_code == 200

    resp = client.post(
        "/auth/register",
        json={
            "email": body["email"],
            "username": _unique(),
            "password": PASSWORD,
            "team_name": None,
        },
    )
    assert resp.status_code == 422
    assert any("email already registered" in e for e in resp.json()["detail"])


def test_register_duplicate_username_rejected(cleanup_users):
    first, body = _register(cleanup_users)
    assert first.status_code == 200

    resp = client.post(
        "/auth/register",
        json={
            "email": f"{_unique()}@example.com",
            "username": body["username"],
            "password": PASSWORD,
            "team_name": None,
        },
    )
    assert resp.status_code == 422
    assert any("username already taken" in e for e in resp.json()["detail"])


def test_register_weak_password_rejected(cleanup_users):
    resp, _ = _register(cleanup_users, password="short")
    assert resp.status_code == 422
    assert any("at least 8 characters" in e for e in resp.json()["detail"])


@pytest.mark.parametrize("bad_email", ["not-an-email", "no-at-sign.com", "missing@domain", "a b@c.com"])
def test_register_malformed_email_rejected(cleanup_users, bad_email):
    resp, _ = _register(cleanup_users, email=bad_email)
    assert resp.status_code == 422
    assert any("invalid email format" in e for e in resp.json()["detail"])


def test_register_collects_all_errors_at_once(cleanup_users):
    """Matches squad_selection.py's convention: every failure in one 422,
    not just the first one hit."""
    resp, _ = _register(cleanup_users, email="bad", password="x")
    assert resp.status_code == 422

    detail = resp.json()["detail"]
    assert any("invalid email format" in e for e in detail)
    assert any("at least 8 characters" in e for e in detail)


def test_password_is_hashed_not_stored_plaintext(engine, cleanup_users):
    resp, _ = _register(cleanup_users)
    assert resp.status_code == 200

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT password_hash FROM users WHERE id = :uid"), {"uid": resp.json()["user_id"]}
        ).scalar()

    assert stored != PASSWORD
    assert PASSWORD not in stored
    assert stored.startswith("$2b$")  # bcrypt digest, not plaintext or a stub


# ------------------------------------------------------------------ login


def test_login_correct_credentials_returns_valid_token(cleanup_users):
    reg, body = _register(cleanup_users)
    assert reg.status_code == 200

    resp = client.post("/auth/login", json={"email": body["email"], "password": PASSWORD})
    assert resp.status_code == 200, resp.text

    decoded = jwt.decode(resp.json()["access_token"], _jwt_secret(), algorithms=[JWT_ALGORITHM])
    assert decoded["user_id"] == reg.json()["user_id"]


def test_login_returns_team_name(cleanup_users):
    """A returning manager logs in rather than registering, so login has to
    carry team_name too -- otherwise the client has no copy of it and the
    header falls back to the app name."""
    _, body = _register(cleanup_users, team_name="Returning FC")

    resp = client.post("/auth/login", json={"email": body["email"], "password": PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["team_name"] == "Returning FC"


def test_login_team_name_is_null_when_never_set(cleanup_users):
    _, body = _register(cleanup_users, team_name=None)

    resp = client.post("/auth/login", json={"email": body["email"], "password": PASSWORD})
    assert resp.status_code == 200
    assert resp.json()["team_name"] is None


def test_login_email_is_case_insensitive(cleanup_users):
    reg, body = _register(cleanup_users)
    assert reg.status_code == 200

    resp = client.post("/auth/login", json={"email": body["email"].upper(), "password": PASSWORD})
    assert resp.status_code == 200


def test_login_wrong_password_rejected(cleanup_users):
    _, body = _register(cleanup_users)
    resp = client.post("/auth/login", json={"email": body["email"], "password": "wrongpassword1"})
    assert resp.status_code == 401


def test_login_nonexistent_email_rejected(cleanup_users):
    resp = client.post(
        "/auth/login", json={"email": f"{_unique()}@example.com", "password": PASSWORD}
    )
    assert resp.status_code == 401


def test_login_failures_are_indistinguishable(cleanup_users):
    """The whole point of the generic message: a caller must not be able to
    tell 'no such account' from 'wrong password', or they can enumerate
    which emails are registered."""
    _, body = _register(cleanup_users)

    wrong_password = client.post(
        "/auth/login", json={"email": body["email"], "password": "wrongpassword1"}
    )
    unknown_email = client.post(
        "/auth/login", json={"email": f"{_unique()}@example.com", "password": PASSWORD}
    )

    assert wrong_password.status_code == unknown_email.status_code == 401
    assert wrong_password.json() == unknown_email.json()
    assert wrong_password.json()["detail"] == "invalid email or password"


# ---------------------------------------------------------------- /auth/me


def test_me_with_valid_token_returns_current_user(cleanup_users):
    reg, body = _register(cleanup_users)
    token = reg.json()["access_token"]

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text

    data = resp.json()
    assert data["id"] == reg.json()["user_id"]
    assert data["email"] == body["email"]
    assert data["username"] == body["username"]


def test_me_without_token_rejected():
    resp = client.get("/auth/me")
    assert resp.status_code == 401


def test_me_with_malformed_token_rejected():
    resp = client.get("/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_me_with_token_signed_by_wrong_secret_rejected(cleanup_users):
    reg, _ = _register(cleanup_users)
    forged = jwt.encode(
        {
            "user_id": reg.json()["user_id"],
            "exp": datetime.now(timezone.utc) + timedelta(hours=1),
        },
        "not-the-real-secret",
        algorithm=JWT_ALGORITHM,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert resp.status_code == 401


def test_me_with_expired_token_rejected(cleanup_users):
    reg, _ = _register(cleanup_users)
    expired = jwt.encode(
        {
            "user_id": reg.json()["user_id"],
            "iat": datetime.now(timezone.utc) - timedelta(hours=2),
            "exp": datetime.now(timezone.utc) - timedelta(hours=1),
        },
        _jwt_secret(),
        algorithm=JWT_ALGORITHM,
    )
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "token expired"


# --------------------------------------------------------------- delete account
#
# See migration a1f4e9c07b32 and Data/auth.py's DELETE_ACCOUNT_STMT for why
# this is a soft delete (stamp deleted_at, scrub identity fields) rather than
# a real DELETE FROM users: a hard delete fails for any account that has ever
# made a transfer (enforce_transfers_immutability_fn blocks it even via
# cascade), which is the normal case for a real player, not an edge one.


def test_delete_account_returns_deleted_true(cleanup_users):
    reg, _ = _register(cleanup_users)
    token = reg.json()["access_token"]

    resp = client.delete("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"deleted": True}


def test_delete_account_scrubs_identity_fields_and_stamps_deleted_at(engine, cleanup_users):
    reg, body = _register(cleanup_users)
    user_id = reg.json()["user_id"]
    token = reg.json()["access_token"]

    client.delete("/auth/me", headers={"Authorization": f"Bearer {token}"})

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT email, username, team_name, password_hash, deleted_at FROM users WHERE id = :u"),
            {"u": user_id},
        ).first()

    assert row.deleted_at is not None
    assert row.email == f"deleted-user-{user_id}@deleted.local"
    assert row.username == f"deleted_user_{user_id}"
    assert row.team_name is None
    assert row.password_hash == "not_a_real_hash"
    # Confirms the placeholders actually replaced the originals, not just
    # that SOME value is present.
    assert row.email != body["email"]
    assert row.username != body["username"]


def test_delete_account_without_token_rejected():
    resp = client.delete("/auth/me")
    assert resp.status_code == 401


def test_deleted_accounts_token_stops_working_immediately(cleanup_users):
    """The JWT is still cryptographically valid for up to TOKEN_TTL (12h)
    after deletion -- this pins that get_current_user rejects it anyway via
    USER_BY_ID_QUERY's deleted_at IS NULL filter, not by relying on the
    token's own expiry to eventually catch up."""
    reg, _ = _register(cleanup_users)
    token = reg.json()["access_token"]

    delete_resp = client.delete("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert delete_resp.status_code == 200

    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 401


def test_deleted_account_cannot_log_back_in(cleanup_users):
    reg, body = _register(cleanup_users)
    token = reg.json()["access_token"]

    client.delete("/auth/me", headers={"Authorization": f"Bearer {token}"})

    resp = client.post("/auth/login", json={"email": body["email"], "password": PASSWORD})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid email or password"


def test_deleted_email_and_username_can_be_reused_for_a_new_registration(cleanup_users):
    """The point of scrubbing rather than merely flagging the row: the
    original email/username are genuinely free again, not just hidden from
    view while still reserved."""
    reg, body = _register(cleanup_users)
    token = reg.json()["access_token"]

    client.delete("/auth/me", headers={"Authorization": f"Bearer {token}"})

    resp = client.post(
        "/auth/register",
        json={
            "email": body["email"],
            "username": body["username"],
            "password": PASSWORD,
            "team_name": "New FC",
        },
    )
    assert resp.status_code == 200, resp.text
    cleanup_users.append(resp.json()["user_id"])


# --------------------------------------------------------------- team_name


def test_register_without_team_name_stores_null(engine, cleanup_users):
    resp, _ = _register(cleanup_users, team_name=None)
    assert resp.status_code == 200
    assert resp.json()["team_name"] is None

    with engine.connect() as conn:
        stored = conn.execute(
            text("SELECT team_name FROM users WHERE id = :uid"), {"uid": resp.json()["user_id"]}
        ).scalar()
    assert stored is None


def test_registered_team_name_is_visible_to_team_dashboard(engine, cleanup_users):
    """The reason team_name lives on users: GET /team reads it straight
    back with no extra wiring."""
    resp, _ = _register(cleanup_users, team_name="Dashboard FC")
    # Uses the token registration just handed back, rather than minting one --
    # this is the real end-to-end path a newly registered client takes.
    token = resp.json()["access_token"]

    dash = client.get(
        "/team",
        params={"season": "9999-00", "gameweek": 1},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert dash.status_code == 200, dash.text
    assert dash.json()["team_name"] == "Dashboard FC"
