"""
test_rate_limit.py — Shared/rate_limit.py.

.env.test sets RATE_LIMIT_ENABLED=0 for the rest of the suite (it logs in
far more often than the limits allow), so every test here switches it on
and starts from empty in-memory counters.
"""

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import bearer_headers
from main import app as fastapi_app
from Shared import rate_limit


@pytest.fixture(autouse=True)
def _limits_on(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "1")
    monkeypatch.setenv("RATE_LIMIT_STORAGE_URL", "memory://")
    rate_limit.reset_limiter()
    yield
    rate_limit.reset_limiter()


client = TestClient(fastapi_app)


def _login(email="nobody@example.invalid"):
    return client.post("/auth/login", json={"email": email, "password": "wrong-password"})


def test_login_is_limited_per_ip():
    """An unknown email is a 401 without any bcrypt work, which keeps this
    fast; the limiter counts the request either way."""
    statuses = [_login(f"user{i}@example.invalid").status_code for i in range(6)]

    assert statuses[:5] == [401] * 5
    assert statuses[5] == 429
    assert int(_login().headers["Retry-After"]) > 0


def test_login_is_limited_per_email_across_ips(monkeypatch):
    """Rotating IPs must not reset the budget for guessing ONE account."""
    for _ in range(10):
        rate_limit.check_email_limit("login", "Target@Example.invalid")

    with pytest.raises(Exception) as exc:
        rate_limit.check_email_limit("login", "  target@example.invalid ")  # case/space-insensitive
    assert exc.value.status_code == 429


def test_the_user_key_needs_a_valid_signature():
    class _Req:
        def __init__(self, token):
            self.headers = {"authorization": f"Bearer {token}"}

    valid = bearer_headers(4242)["Authorization"].split(" ", 1)[1]
    forged = jwt.encode({"user_id": 4242}, "not-the-secret", algorithm="HS256")

    assert rate_limit._user_key(_Req(valid)) == "user:4242"
    assert rate_limit._user_key(_Req(forged)) is None, "a forged token falls back to the IP bucket"


def test_a_broken_counter_store_lets_requests_through(monkeypatch):
    """A Redis blip must not take logins down."""
    def _broken():
        raise ConnectionError("redis down")

    monkeypatch.setattr(rate_limit, "_get_limiter", _broken)

    assert [_login().status_code for _ in range(8)] == [401] * 8


def test_disabled_means_no_limits(monkeypatch):
    monkeypatch.setenv("RATE_LIMIT_ENABLED", "0")

    assert [_login().status_code for _ in range(8)] == [401] * 8


def test_chat_message_length_is_capped(engine, make_user):
    user_id = make_user()
    try:
        resp = client.post(
            "/chat",
            json={"season": "2099-00", "gameweek": 1, "message": "x" * 1001},
            headers=bearer_headers(user_id),
        )
        assert resp.status_code == 422
    finally:
        with engine.begin() as conn:  # make_user tears nothing down itself
            conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})
