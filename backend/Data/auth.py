"""
auth.py — FastAPI endpoints for registration, login, and identity.

Follows the same shape as squad_selection.py / leagues.py: sync `def`
handlers, SQLAlchemy Core with text() and bound parameters, module-level
statement constants, and collect-all-errors validation that returns every
failure at once (422) rather than stopping at the first.

Password hashing uses the `bcrypt` package directly rather than
passlib[bcrypt]. passlib 1.7.4 (its last release, 2020) is incompatible
with bcrypt >= 4.1 -- it probes a removed `bcrypt.__about__` attribute and
its 72-byte wrap-bug detection now raises ValueError outright, which was
confirmed by actually installing the pair and calling CryptContext.hash()
before settling on the direct dependency.

team_name is written to public.users, NOT user_squads. That column already
exists (Migrations revision 1acbf07cfb53) and is already the one read by
team_dashboard.py's GET /team and leagues.py's leaderboard COALESCE, so a
name captured at registration shows up in both places with no further
wiring. Registration deliberately does NOT create a user_squads row --
squad creation stays entirely owned by squad_selection.py.

JWT_SECRET is read from the project .env using the same unbounded walk-up
that db_utils.py uses, so it resolves identically no matter which
directory uvicorn/pytest was launched from. There is no default value:
a missing secret raises at call time rather than silently signing tokens
with a guessable constant.
"""

import hashlib
import logging
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import bcrypt
import jwt
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Shared.db_utils import get_engine
from Shared.rate_limit import check_email_limit

# Imported as a module, not by name, so tests can monkeypatch is_configured
# and send on it -- the two things worth exercising here are "SMTP off" and
# "SMTP on but the provider failed", and neither should need a mail server.
from Shared import mailer

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)

router = APIRouter()

MIN_PASSWORD_LENGTH = 8
# bcrypt hashes at most 72 bytes and raises on anything longer (it stopped
# silently truncating in 4.x). Rejecting it up front in validation keeps
# that a 422 alongside the other field errors instead of a 500 from the
# hashing call.
MAX_PASSWORD_BYTES = 72
MAX_TEAM_NAME_LENGTH = 50  # matches users.team_name varchar(50)

TOKEN_TTL = timedelta(hours=12)
JWT_ALGORITHM = "HS256"

# How long a forgot-password link stays usable. Short on purpose -- unlike
# the login JWT this gets emailed/logged in plaintext, so a shorter window
# limits the damage if that link leaks somewhere it shouldn't.
RESET_TOKEN_TTL = timedelta(minutes=30)

# Deliberately identical for "no such account" and "wrong password" --
# a distinguishable message would let anyone enumerate registered emails.
INVALID_CREDENTIALS_MESSAGE = "invalid email or password"

# Good enough to reject the obviously-malformed (no @, no dot in the
# domain, whitespace) without pulling in email_validator just for this.
# Deliverability isn't knowable from syntax anyway.
_EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_SEARCH_DIRS = [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents)

# auto_error=False so a missing/!Bearer header reaches our own handler and
# returns the same 401 shape as a bad token, rather than HTTPBearer's
# built-in 403.
_bearer_scheme = HTTPBearer(auto_error=False)

EMAIL_EXISTS_QUERY = text("SELECT 1 FROM users WHERE lower(email) = lower(:email)")
USERNAME_EXISTS_QUERY = text("SELECT 1 FROM users WHERE lower(username) = lower(:username)")

INSERT_USER_STMT = text(
    """
    INSERT INTO users (email, username, password_hash, team_name)
    VALUES (:email, :username, :password_hash, :team_name)
    RETURNING id, email, username, team_name
    """
)

# team_name is selected here as well as in register: the client caches it for
# the header, and a returning user logs in rather than registering, so leaving
# it out meant their team name silently disappeared until they hit a page that
# reads GET /team.
#
# deleted_at IS NULL on both queries below: a soft-deleted account's email is
# scrubbed to an unguessable placeholder (see DELETE_ACCOUNT_STMT), so login
# would already fail on the email lookup alone -- but a JWT issued before
# deletion is still cryptographically valid for up to TOKEN_TTL (12h), and
# USER_BY_ID_QUERY is what get_current_user resolves that token against, not
# email. Without this filter, a just-deleted account's still-live token would
# keep authenticating for up to 12 hours after "deletion".
LOGIN_LOOKUP_QUERY = text(
    "SELECT id, email, username, password_hash, team_name "
    "FROM users WHERE lower(email) = lower(:email) AND deleted_at IS NULL"
)

USER_BY_ID_QUERY = text(
    "SELECT id, email, username, team_name FROM users WHERE id = :user_id AND deleted_at IS NULL"
)

# Same lower(email) + deleted_at filter as LOGIN_LOOKUP_QUERY -- a
# soft-deleted or nonexistent account must not be able to request (or,
# more importantly, receive evidence of) a reset link.
FORGOT_PASSWORD_LOOKUP_QUERY = text(
    "SELECT id FROM users WHERE lower(email) = lower(:email) AND deleted_at IS NULL"
)

INSERT_RESET_TOKEN_STMT = text(
    """
    INSERT INTO password_reset_tokens (user_id, token_hash, expires_at)
    VALUES (:user_id, :token_hash, :expires_at)
    """
)

# used_at IS NULL AND expires_at > now() in one WHERE -- a token that is
# either already consumed or past its window is equally "not found" to the
# caller; RESET_TOKEN_ROW_QUERY doesn't distinguish which, same way
# INVALID_CREDENTIALS_MESSAGE doesn't distinguish "no such account" from
# "wrong password".
RESET_TOKEN_ROW_QUERY = text(
    """
    SELECT id, user_id FROM password_reset_tokens
    WHERE token_hash = :token_hash AND used_at IS NULL AND expires_at > now()
    """
)

UPDATE_PASSWORD_STMT = text(
    "UPDATE users SET password_hash = :password_hash, updated_at = now() WHERE id = :user_id"
)

CONSUME_RESET_TOKEN_STMT = text(
    "UPDATE password_reset_tokens SET used_at = now() WHERE id = :token_id"
)

# Burns every other still-live token for this user once one of them is
# successfully redeemed -- an older reset link/email that leaked somewhere
# (a shared inbox, a browser history) can't be replayed after the account
# owner has already reset their password with a newer one.
INVALIDATE_OTHER_RESET_TOKENS_STMT = text(
    """
    UPDATE password_reset_tokens
    SET used_at = now()
    WHERE user_id = :user_id AND used_at IS NULL AND id != :token_id
    """
)

# Placeholders are derived from the user's own id -- deterministic and
# guaranteed unique (ids are unique), no extra randomness source needed.
# password_hash reuses the same "deliberately unusable" placeholder text
# Tests/conftest.py's fixture users already carry: verify_password returns
# False on a malformed hash rather than raising, so it can never be a live
# password for anyone. team_name is cleared to NULL rather than a
# placeholder string: leagues.py's leaderboard already falls back to
# username via COALESCE, so a deleted member reads as "deleted_user_<id>"
# there instead of carrying a stale chosen name forward.
DELETE_ACCOUNT_STMT = text(
    """
    UPDATE users
    SET deleted_at = now(),
        email = :placeholder_email,
        username = :placeholder_username,
        team_name = NULL,
        password_hash = :placeholder_hash,
        updated_at = now()
    WHERE id = :user_id AND deleted_at IS NULL
    """
)


@lru_cache(maxsize=1)
def _jwt_secret() -> str:
    """Find and load the nearest .env, then return JWT_SECRET."""
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    secret = os.getenv("JWT_SECRET")
    if not secret:
        looked = "\n  ".join(str(d / ".env") for d in _SEARCH_DIRS[:4])
        raise RuntimeError("JWT_SECRET not set. Looked for a .env in:\n  " + looked)
    return secret


@lru_cache(maxsize=1)
def _frontend_base_url() -> str:
    """The frontend origin to build a reset link against, e.g.
    "http://localhost:5173/reset-password?token=...".

    Reuses ALLOWED_ORIGINS (already set for CORS -- see
    Context_assembler/main.py's _allowed_origins()) rather than adding a
    second env var that names the same origin. Not imported from that
    module directly: main.py imports this router, so importing back would
    be circular -- same reason every module that reads a shared .env value
    (this file's own _jwt_secret, Shared/db_utils.py's DATABASE_URL) has its
    own small copy of the search-and-load logic rather than a shared one.
    Takes the first entry when several are configured, same as a browser
    only ever running from one of them at a time in dev.
    """
    for d in _SEARCH_DIRS:
        candidate = d / ".env"
        if candidate.is_file():
            load_dotenv(candidate)
            break
    else:
        load_dotenv()

    raw = os.getenv("ALLOWED_ORIGINS")
    if not raw:
        looked = "\n  ".join(str(d / ".env") for d in _SEARCH_DIRS[:4])
        raise RuntimeError("ALLOWED_ORIGINS not set. Looked for a .env in:\n  " + looked)
    origins = [origin.strip() for origin in raw.split(",") if origin.strip()]
    return origins[0]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """False rather than raising on a malformed/legacy stored hash -- rows
    predating this module hold placeholder text, not a real bcrypt digest,
    and those accounts should simply fail to log in."""
    try:
        return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    payload = {"user_id": user_id, "sub": str(user_id), "iat": now, "exp": now + TOKEN_TTL}
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


class RegisterRequest(BaseModel):
    email: str
    username: str
    password: str
    team_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user_id: int
    team_name: str | None = None


class CurrentUser(BaseModel):
    id: int
    email: str
    username: str


def _validate_password(password: str) -> list[str]:
    """The two length rules every new password must pass, whether it's
    arriving via registration or a password reset -- pulled out so the two
    call sites can't drift on what "a valid password" means."""
    errors: list[str] = []
    if len(password) < MIN_PASSWORD_LENGTH:
        errors.append(f"password must be at least {MIN_PASSWORD_LENGTH} characters")
    if len(password.encode("utf-8")) > MAX_PASSWORD_BYTES:
        errors.append(f"password must be at most {MAX_PASSWORD_BYTES} bytes")
    return errors


def _validate_registration(req: RegisterRequest, conn) -> list[str]:
    """Every failure at once, matching squad_selection._validate_squad."""
    errors: list[str] = []

    email = req.email.strip()
    if not _EMAIL_PATTERN.match(email):
        errors.append(f"invalid email format: {req.email!r}")
    elif conn.execute(EMAIL_EXISTS_QUERY, {"email": email}).first() is not None:
        errors.append("email already registered")

    username = req.username.strip()
    if not username:
        errors.append("username must not be empty")
    elif conn.execute(USERNAME_EXISTS_QUERY, {"username": username}).first() is not None:
        errors.append("username already taken")

    errors.extend(_validate_password(req.password))

    if req.team_name is not None and len(req.team_name.strip()) > MAX_TEAM_NAME_LENGTH:
        errors.append(f"team_name must be at most {MAX_TEAM_NAME_LENGTH} characters")

    return errors


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> CurrentUser:
    """Reusable dependency: validates the bearer token and loads the user.

    Built but deliberately not retrofitted onto the existing endpoints yet
    -- those still take an explicit user_id query/body field, and swapping
    them over is a separate, breaking change.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(status_code=401, detail="not authenticated")

    try:
        payload = jwt.decode(credentials.credentials, _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="token expired") from None
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="invalid token") from None

    user_id = payload.get("user_id")
    if user_id is None:
        raise HTTPException(status_code=401, detail="invalid token")

    engine = get_engine()
    with engine.connect() as conn:
        row = conn.execute(USER_BY_ID_QUERY, {"user_id": user_id}).first()

    # A token can outlive the account it names (deleted user) -- that's a
    # 401, not a 404, since the credential itself is no longer usable.
    if row is None:
        raise HTTPException(status_code=401, detail="invalid token")

    return CurrentUser(id=row.id, email=row.email, username=row.username)


@router.post("/auth/register", response_model=TokenResponse)
def register(req: RegisterRequest) -> TokenResponse:
    engine = get_engine()
    team_name = req.team_name.strip() if req.team_name and req.team_name.strip() else None

    try:
        with engine.begin() as conn:
            errors = _validate_registration(req, conn)
            if errors:
                raise HTTPException(status_code=422, detail=errors)

            row = conn.execute(
                INSERT_USER_STMT,
                {
                    "email": req.email.strip(),
                    "username": req.username.strip(),
                    "password_hash": hash_password(req.password),
                    "team_name": team_name,
                },
            ).first()
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        # The uniqueness pre-checks above and this INSERT share one
        # transaction, but a concurrent registration can still land
        # between them -- the DB's UNIQUE constraints are the real
        # guarantee, so a race surfaces here as a 422 rather than a 500.
        logger.error("Registration failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=422, detail=["email or username already registered"]) from e

    return TokenResponse(
        access_token=create_access_token(row.id), user_id=row.id, team_name=row.team_name
    )


@router.post("/auth/login", response_model=TokenResponse)
def login(req: LoginRequest) -> TokenResponse:
    # Per-account, before any bcrypt work: the per-IP limit in
    # Shared/rate_limit.py alone doesn't stop a guesser rotating IPs.
    check_email_limit("login", req.email)
    engine = get_engine()

    try:
        with engine.connect() as conn:
            row = conn.execute(LOGIN_LOOKUP_QUERY, {"email": req.email.strip()}).first()
    except SQLAlchemyError as e:
        logger.error("Login lookup failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    # Same 401 and same message whether the email is unknown or the
    # password is wrong -- see INVALID_CREDENTIALS_MESSAGE.
    if row is None or not verify_password(req.password, row.password_hash):
        raise HTTPException(status_code=401, detail=INVALID_CREDENTIALS_MESSAGE)

    return TokenResponse(
        access_token=create_access_token(row.id), user_id=row.id, team_name=row.team_name
    )


class ForgotPasswordRequest(BaseModel):
    email: str


class ForgotPasswordResponse(BaseModel):
    message: str = "If that email is registered, a reset link has been sent."


# This app has no email-sending capability yet (no SMTP/SendGrid/SES
# anywhere) -- logging the link is the dev-appropriate stand-in until one is
# wired in. Swapping it for a real send is a one-function change: replace
# this log call with a call to whatever provider gets added, the token
# generation/storage/expiry logic above it doesn't change at all.
RESET_EMAIL_SUBJECT = "Reset your PitchSide AI password"

RESET_EMAIL_BODY = """\
Someone asked to reset the password for your PitchSide AI account.

Open this link to choose a new one:

{link}

The link expires in {minutes} minutes and can only be used once.

If this wasn't you, you can ignore this email -- your password has not
been changed, and nobody can use this link without receiving it.
"""


def _deliver_reset_link(email: str, user_id: int, link: str) -> None:
    """Email the reset link, falling back to logging it when SMTP is off.

    NEVER RAISES. The token is already committed by the time this runs, and
    the caller must return the same generic response whatever happens here:

      * raising would turn a provider outage into a 500, telling the caller
        their address IS registered (the 500 only happens on the branch
        where a real account was found), which is exactly the enumeration
        leak the generic response exists to prevent;
      * it would also strand a perfectly valid token behind an error, when
        a retry a minute later would have worked.

    So a failed send is logged as an error and the request still succeeds.
    The user sees "if that email is registered, a link has been sent" and
    nothing arrives -- which is indistinguishable, from their side, from
    having typed an address that was never registered.
    """
    if not mailer.is_configured():
        # The original behaviour, and still what local development and the
        # test suite rely on: no mail server needed anywhere, link goes to
        # the server log. Logged at WARNING rather than INFO so that a
        # DEPLOYED server quietly running without SMTP is visible in the
        # logs rather than blending into normal traffic.
        logger.warning(
            "SMTP not configured -- password reset link for user_id=%s not emailed. Link: %s",
            user_id,
            link,
        )
        return

    try:
        mailer.send(
            to=email,
            subject=RESET_EMAIL_SUBJECT,
            body=RESET_EMAIL_BODY.format(
                link=link, minutes=int(RESET_TOKEN_TTL.total_seconds() // 60)
            ),
        )
    except mailer.MailError as e:
        # Deliberately does not log the link here. On a configured server the
        # link belongs in the user's inbox, not in a log file that more
        # people can read than can read their email.
        logger.error("Could not email password reset to user_id=%s: %s", user_id, e)
    else:
        logger.info("Password reset link emailed to user_id=%s", user_id)


@router.post("/auth/forgot-password", response_model=ForgotPasswordResponse)
def forgot_password(req: ForgotPasswordRequest) -> ForgotPasswordResponse:
    # Per-email: stops one inbox being flooded with reset links.
    check_email_limit("forgot-password", req.email)
    engine = get_engine()

    try:
        with engine.begin() as conn:
            row = conn.execute(
                FORGOT_PASSWORD_LOOKUP_QUERY, {"email": req.email.strip()}
            ).first()

            # Same response whether or not the email is registered -- see
            # INVALID_CREDENTIALS_MESSAGE's reasoning. Only a real match gets
            # a token generated and stored.
            if row is not None:
                token = secrets.token_urlsafe(32)
                token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
                expires_at = datetime.now(timezone.utc) + RESET_TOKEN_TTL
                conn.execute(
                    INSERT_RESET_TOKEN_STMT,
                    {"user_id": row.id, "token_hash": token_hash, "expires_at": expires_at},
                )
                _deliver_reset_link(
                    req.email.strip(),
                    row.id,
                    f"{_frontend_base_url()}/reset-password?token={token}",
                )
    except SQLAlchemyError as e:
        logger.error("Forgot-password lookup failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return ForgotPasswordResponse()


class ResetPasswordRequest(BaseModel):
    token: str
    new_password: str


class ResetPasswordResponse(BaseModel):
    reset: bool = True


@router.post("/auth/reset-password", response_model=ResetPasswordResponse)
def reset_password(req: ResetPasswordRequest) -> ResetPasswordResponse:
    errors = _validate_password(req.new_password)
    if errors:
        raise HTTPException(status_code=422, detail=errors)

    token_hash = hashlib.sha256(req.token.encode("utf-8")).hexdigest()
    engine = get_engine()

    try:
        with engine.begin() as conn:
            token_row = conn.execute(RESET_TOKEN_ROW_QUERY, {"token_hash": token_hash}).first()
            if token_row is None:
                raise HTTPException(status_code=422, detail=["invalid or expired reset link"])

            conn.execute(
                UPDATE_PASSWORD_STMT,
                {"password_hash": hash_password(req.new_password), "user_id": token_row.user_id},
            )
            conn.execute(CONSUME_RESET_TOKEN_STMT, {"token_id": token_row.id})
            conn.execute(
                INVALIDATE_OTHER_RESET_TOKENS_STMT,
                {"user_id": token_row.user_id, "token_id": token_row.id},
            )
    except HTTPException:
        raise
    except SQLAlchemyError as e:
        logger.error("Reset-password failed: %s: %s", type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return ResetPasswordResponse()


@router.get("/auth/me", response_model=CurrentUser)
def me(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    return current_user


class DeleteAccountResponse(BaseModel):
    deleted: bool = True


@router.delete("/auth/me", response_model=DeleteAccountResponse)
def delete_account(current_user: CurrentUser = Depends(get_current_user)) -> DeleteAccountResponse:
    """Soft-deletes the caller's own account. See migration a1f4e9c07b32
    for why this scrubs identity fields and stamps deleted_at rather than
    issuing a real DELETE FROM users: a hard delete fails outright for any
    account that has ever made a transfer (enforce_transfers_immutability_fn
    blocks it even via cascade), which is the normal case, not an edge one.

    Irreversible from the API's point of view -- there is no undelete
    endpoint. Every historical row this user is referenced from (transfers,
    gw_scores, squad_players, league standings, Dream11 teams) is left
    exactly as it was, so other users' leagues and leaderboards keep
    working; only the fields that could log this person back in or that
    display as their own identity get scrubbed.

    Idempotent by construction rather than by an explicit check: a second
    call is unreachable in practice, because the token that would carry it
    stops resolving the moment deleted_at is set (USER_BY_ID_QUERY filters
    on deleted_at IS NULL), so get_current_user already returns 401 before
    this handler would ever run again for the same account.
    """
    engine = get_engine()
    user_id = current_user.id

    try:
        with engine.begin() as conn:
            conn.execute(
                DELETE_ACCOUNT_STMT,
                {
                    "user_id": user_id,
                    "placeholder_email": f"deleted-user-{user_id}@deleted.local",
                    "placeholder_username": f"deleted_user_{user_id}",
                    "placeholder_hash": "not_a_real_hash",
                },
            )
    except SQLAlchemyError as e:
        logger.error("Account deletion failed for user_id=%s: %s: %s", user_id, type(e).__name__, e)
        raise HTTPException(status_code=500, detail="Internal server error") from e

    return DeleteAccountResponse(deleted=True)
