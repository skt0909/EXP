"""
mailer.py — sending transactional email over SMTP.

Stdlib smtplib, deliberately. Every provider worth using (Resend, SendGrid,
SES, Postmark, Mailgun, plain Gmail) speaks SMTP, so one implementation
reaches all of them and the project takes on no new dependency and no
vendor SDK to keep current. Swapping provider is an .env change.

OPTIONAL BY DESIGN. is_configured() is false when SMTP_HOST is unset, and
callers are expected to check it and fall back rather than fail. That is
what keeps local development and the test suite working with no mail
server anywhere: forgot-password logs its link exactly as it did before,
and nothing needs a fake SMTP daemon to run. Configuration is what turns
sending on, not a code change.

  SMTP_HOST      smtp.resend.com          required -- absent means "off"
  SMTP_PORT      587                      optional, defaults to 587
  SMTP_USER      resend                   optional (some relays are open)
  SMTP_PASSWORD  re_xxxxxxxx              optional
  SMTP_FROM      PitchSide <no-reply@...> optional, defaults to SMTP_USER

Read through the same unbounded .env walk-up every other module in this
project uses (Data/auth.py's _jwt_secret, Shared/db_utils.py's
DATABASE_URL), so it resolves the same regardless of which directory
uvicorn or pytest was started from.

STARTTLS on port 587, implicit TLS on 465, matching what those ports mean
to every provider. Port 25 is left unencrypted because that is what it
means; nothing here should be using it.

send() raises MailError on any failure and never swallows one. Deciding
that a failed send should not fail the surrounding request is the
caller's call to make, not this module's -- forgot_password makes exactly
that decision, and says why.
"""

import logging
import os
import smtplib
import ssl
from email.message import EmailMessage
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)

# A hung relay must not hang the HTTP request that triggered the send.
SMTP_TIMEOUT_SECONDS = 10

DEFAULT_SMTP_PORT = 587
IMPLICIT_TLS_PORT = 465

_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)


class MailError(RuntimeError):
    """Raised when a message could not be handed to the SMTP server."""


@lru_cache(maxsize=1)
def _settings() -> dict:
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    host = os.getenv("SMTP_HOST")
    user = os.getenv("SMTP_USER")
    raw_port = os.getenv("SMTP_PORT")
    try:
        port = int(raw_port) if raw_port else DEFAULT_SMTP_PORT
    except ValueError:
        # A typo here would otherwise surface as a confusing connection error
        # much later, at the first send.
        raise MailError(f"SMTP_PORT must be a number, got {raw_port!r}") from None

    return {
        "host": host,
        "port": port,
        "user": user,
        "password": os.getenv("SMTP_PASSWORD"),
        # Falling back to SMTP_USER covers the common case where the
        # authenticating account IS the sending address.
        "from": os.getenv("SMTP_FROM") or user,
    }


def is_configured() -> bool:
    """True when there is somewhere to send mail. Callers fall back when false.

    Keyed on SMTP_HOST alone: user/password are genuinely optional (an
    internal relay may need neither), and a host with no sender address is a
    misconfiguration worth surfacing at send time rather than silently
    reading as "email is switched off".
    """
    return bool(_settings()["host"])


def send(to: str, subject: str, body: str) -> None:
    """Send a plain-text message. Raises MailError if it could not be sent.

    Plain text only, no HTML alternative. A reset link is a URL; wrapping it
    in markup adds a rendering surface and a second copy of the link to keep
    in sync, and plain text lands in the inbox rather than the promotions
    tab more often than not.
    """
    cfg = _settings()
    if not cfg["host"]:
        raise MailError("SMTP_HOST is not set -- check is_configured() before calling send()")
    if not cfg["from"]:
        raise MailError("SMTP_FROM (or SMTP_USER) must be set to address the message from")

    message = EmailMessage()
    message["From"] = cfg["from"]
    message["To"] = to
    message["Subject"] = subject
    message.set_content(body)

    try:
        if cfg["port"] == IMPLICIT_TLS_PORT:
            with smtplib.SMTP_SSL(
                cfg["host"], cfg["port"],
                timeout=SMTP_TIMEOUT_SECONDS, context=ssl.create_default_context(),
            ) as server:
                _authenticate_and_send(server, cfg, message)
        else:
            with smtplib.SMTP(cfg["host"], cfg["port"], timeout=SMTP_TIMEOUT_SECONDS) as server:
                server.starttls(context=ssl.create_default_context())
                _authenticate_and_send(server, cfg, message)
    except MailError:
        raise
    except (smtplib.SMTPException, OSError) as e:
        # OSError covers DNS failure, refused connection and timeout, none of
        # which are SMTPException but all of which mean the same thing here.
        raise MailError(f"could not send mail via {cfg['host']}:{cfg['port']}: {type(e).__name__}: {e}") from e


def _authenticate_and_send(server, cfg: dict, message: EmailMessage) -> None:
    if cfg["user"] and cfg["password"]:
        server.login(cfg["user"], cfg["password"])
    server.send_message(message)
