"""
test_password_reset_email.py — delivery of the forgot-password link.

The token machinery (minting, hashing at rest, expiry, single use) is
tested elsewhere and is not retested here. This file covers only the step
that was missing until now: getting the link to the user.

The property that matters most is the one that is easiest to break by
accident -- POST /auth/forgot-password must answer IDENTICALLY in every
case. Registered or not, SMTP configured or not, provider up or down.
The moment any of those changes the status code or the body, the endpoint
becomes an oracle for "is this email registered", which is the exact leak
the generic message exists to prevent. A provider outage is the sharp
case: it only fails on the branch where a real account was found, so a
500 there would say "yes, that address is registered" out loud.

No mail server is involved. Shared.mailer is monkeypatched on the auth
module, which is why auth.py imports it as a module rather than pulling
is_configured/send in by name.
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import Data.auth as auth_mod
from conftest import bearer_headers  # noqa: F401  -- keeps conftest's path setup in play
from main import app
from Shared import mailer

client = TestClient(app)

REGISTERED = "reset_target@example.com"
PASSWORD = "correct horse battery"


@pytest.fixture
def registered_user(engine):
    """A real account to aim the reset at, registered through the real
    endpoint so its password_hash is a genuine bcrypt digest."""
    resp = client.post(
        "/auth/register",
        json={"email": REGISTERED, "username": "reset_target", "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    user_id = resp.json()["user_id"]

    yield user_id

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM password_reset_tokens WHERE user_id = :u"), {"u": user_id})
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": user_id})


@pytest.fixture
def sent(monkeypatch):
    """SMTP 'configured', with every send captured instead of dispatched."""
    captured = []

    def _send(to, subject, body):
        captured.append({"to": to, "subject": subject, "body": body})

    monkeypatch.setattr(auth_mod.mailer, "is_configured", lambda: True)
    monkeypatch.setattr(auth_mod.mailer, "send", _send)
    return captured


def _forgot(email):
    return client.post("/auth/forgot-password", json={"email": email})


def _stored_token_count(engine, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT count(*) FROM password_reset_tokens WHERE user_id = :u"), {"u": user_id}
        ).scalar()


# ------------------------------------------------------------- the happy path


def test_a_registered_email_receives_a_reset_link(registered_user, sent):
    resp = _forgot(REGISTERED)

    assert resp.status_code == 200
    assert len(sent) == 1, "exactly one email per request"

    mail = sent[0]
    assert mail["to"] == REGISTERED
    assert "/reset-password?token=" in mail["body"], mail["body"]
    assert "30 minutes" in mail["body"], "the expiry window belongs in the mail"


def test_the_emailed_token_is_the_one_that_actually_works(registered_user, engine, sent):
    """End to end: pull the link out of the email body and redeem it. Guards
    against emailing a token that was never stored, or storing a different
    one from the one sent -- both of which look fine until someone clicks."""
    _forgot(REGISTERED)
    token = sent[0]["body"].split("/reset-password?token=")[1].split()[0]

    new_password = "a different password entirely"
    resp = client.post(
        "/auth/reset-password", json={"token": token, "new_password": new_password}
    )
    assert resp.status_code == 200, resp.text

    # The new password works and the old one does not.
    assert client.post(
        "/auth/login", json={"email": REGISTERED, "password": new_password}
    ).status_code == 200
    assert client.post(
        "/auth/login", json={"email": REGISTERED, "password": PASSWORD}
    ).status_code == 401


# --------------------------------------------------- the response never varies


def test_an_unregistered_email_is_answered_identically_and_sends_nothing(sent):
    known = _forgot("definitely_not_registered_87d21f@example.com")

    assert known.status_code == 200
    assert sent == [], "no account, no email -- and no hint that this is why"


def test_registered_and_unregistered_responses_are_byte_identical(registered_user, sent):
    """The enumeration guard, asserted directly rather than inferred."""
    real = _forgot(REGISTERED)
    fake = _forgot("definitely_not_registered_87d21f@example.com")

    assert real.status_code == fake.status_code == 200
    assert real.json() == fake.json()


def test_a_provider_failure_still_returns_the_same_success_response(
    registered_user, engine, monkeypatch
):
    """The sharp case. A send failure only happens on the branch where a REAL
    account was found, so surfacing it would announce that the address is
    registered. It must look exactly like every other outcome."""
    def _boom(to, subject, body):
        raise mailer.MailError("connection refused")

    monkeypatch.setattr(auth_mod.mailer, "is_configured", lambda: True)
    monkeypatch.setattr(auth_mod.mailer, "send", _boom)

    resp = _forgot(REGISTERED)

    assert resp.status_code == 200, "a provider outage must not become a 500"
    assert resp.json()["message"].startswith("If that email is registered")
    # The token was already committed and is deliberately left usable -- the
    # user can retry, or an admin can recover the link from the logs.
    assert _stored_token_count(engine, registered_user) == 1


# ------------------------------------------------------- unconfigured fallback


def test_with_smtp_off_the_link_is_logged_and_nothing_is_sent(
    registered_user, monkeypatch, caplog
):
    """Local development and this suite run with no mail server at all. The
    original log-the-link behaviour has to survive, or every developer
    without SMTP loses the ability to reset a password."""
    monkeypatch.setattr(auth_mod.mailer, "is_configured", lambda: False)

    def _must_not_be_called(**kwargs):
        raise AssertionError("send() called even though SMTP is not configured")

    monkeypatch.setattr(auth_mod.mailer, "send", _must_not_be_called)

    with caplog.at_level("WARNING", logger=auth_mod.logger.name):
        resp = _forgot(REGISTERED)

    assert resp.status_code == 200
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "/reset-password?token=" in logged, logged
    # WARNING, not INFO: a deployed server running without SMTP should stand
    # out in the logs rather than blend into ordinary traffic.
    assert any(r.levelname == "WARNING" for r in caplog.records)


def test_a_configured_send_does_not_log_the_link(registered_user, sent, caplog):
    """Once mail works the link belongs in the inbox, not in a log file more
    people can read than can read the user's email."""
    with caplog.at_level("INFO", logger=auth_mod.logger.name):
        _forgot(REGISTERED)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "token=" not in logged, f"reset token leaked into the logs: {logged}"


# ------------------------------------------------------------- mailer itself


def test_is_configured_is_false_without_a_host(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    mailer._settings.cache_clear()
    try:
        assert mailer.is_configured() is False
    finally:
        mailer._settings.cache_clear()


def test_is_configured_is_true_with_a_host(monkeypatch):
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    mailer._settings.cache_clear()
    try:
        assert mailer.is_configured() is True
    finally:
        mailer._settings.cache_clear()


def test_send_without_a_host_raises_rather_than_silently_doing_nothing(monkeypatch):
    """A caller that forgets to check is_configured() should find out."""
    monkeypatch.delenv("SMTP_HOST", raising=False)
    mailer._settings.cache_clear()
    try:
        with pytest.raises(mailer.MailError, match="SMTP_HOST is not set"):
            mailer.send(to="x@example.com", subject="s", body="b")
    finally:
        mailer._settings.cache_clear()


def test_a_non_numeric_port_is_caught_at_settings_time(monkeypatch):
    """Rather than surfacing much later as a confusing connection error."""
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "not-a-number")
    mailer._settings.cache_clear()
    try:
        with pytest.raises(mailer.MailError, match="SMTP_PORT must be a number"):
            mailer.is_configured()
    finally:
        monkeypatch.delenv("SMTP_PORT", raising=False)
        mailer._settings.cache_clear()
