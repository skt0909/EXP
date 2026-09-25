"""
rate_limit.py — request rate limits for the API.

What these protect on a 1 GB / 2 shared-vCPU e2-micro is mostly CPU and
money, not memory: every login or register attempt costs a bcrypt hash
(~0.25 s of CPU), /chat spends Groq quota, and registration and contest
spam grow the database. Memory is guarded separately (nginx
client_max_body_size, uvicorn --limit-concurrency; see deploy/).

Two layers:
  * RateLimitMiddleware applies RULES by method + path, keyed per user
    when the request carries a valid bearer token and per client IP
    otherwise. The first matching rule wins.
  * check_email_limit() is called inside /auth/login and
    /auth/forgot-password, keyed by the email in the body, so rotating IPs
    doesn't get around the limit on guessing one account's password.

Counters live in RATE_LIMIT_STORAGE_URL (production: Redis db 2, separate
from Celery's db 0) so they survive an API restart and cost the API
process no memory. Unset, they are in-process ("memory://"), which is
fine for a single uvicorn process. If the store is unreachable the
request is let through and the error logged: a Redis blip must not take
logins down with it.

Behind nginx, request.client.host is only the real client IP when uvicorn
runs with --proxy-headers --forwarded-allow-ips=127.0.0.1 (deploy/systemd/
fpl-api.service); without it every client shares nginx's one bucket.

RATE_LIMIT_ENABLED=0 turns it all off -- .env.test sets that, since the
suite logs in far more often than 5 times a minute; the limiter's own
tests switch it on explicitly.
"""

import logging
import math
import os
import re
import time

import jwt
from fastapi import HTTPException
from limits import parse
from limits.storage import storage_from_string
from limits.strategies import MovingWindowRateLimiter
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

logger = logging.getLogger(__name__)

# (method, path regex, limits, key). Most specific first; the first match wins.
RULES = [
    ("POST", r"/auth/login", ["5/minute"], "ip"),
    ("POST", r"/auth/register", ["3/hour"], "ip"),
    ("POST", r"/auth/forgot-password", ["3/hour"], "ip"),
    ("POST", r"/auth/reset-password", ["5 per 15 minutes"], "ip"),
    ("POST", r"/chat", ["10/minute", "100/day"], "user"),
    ("POST", r"/dream11/contests(/join)?", ["10/minute"], "user"),
    # Every other write: squad, transfers, lineup, leagues, team submissions.
    ("POST|PUT|PATCH|DELETE", r"/.*", ["30/minute"], "user"),
]

EMAIL_LIMITS = {
    "login": ["10 per 15 minutes"],
    "forgot-password": ["3/hour"],
}

_COMPILED = [
    (re.compile(f"^(?:{m})$"), re.compile(f"^{p}$"), [parse(l) for l in lims], key)
    for m, p, lims, key in RULES
]
_EMAIL_COMPILED = {k: [parse(l) for l in v] for k, v in EMAIL_LIMITS.items()}


def _enabled() -> bool:
    return os.getenv("RATE_LIMIT_ENABLED", "1") != "0"


_limiter: MovingWindowRateLimiter | None = None


def _get_limiter() -> MovingWindowRateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = MovingWindowRateLimiter(storage_from_string(os.getenv("RATE_LIMIT_STORAGE_URL", "memory://")))
    return _limiter


def reset_limiter() -> None:
    """Drop the limiter (and, for memory://, every counter). For tests."""
    global _limiter
    _limiter = None


def _hit(limits_, *identifiers) -> int | None:
    """Count one request against every limit. Returns seconds until the
    first exhausted one resets, or None if all allow it."""
    limiter = _get_limiter()
    for limit in limits_:
        if not limiter.hit(limit, *identifiers):
            reset_at, _remaining = limiter.get_window_stats(limit, *identifiers)
            return max(1, math.ceil(reset_at - time.time()))
    return None


def _user_key(request) -> str | None:
    """'user:<id>' from a valid bearer token, else None. The signature is
    verified, so a forged token can't pick someone else's bucket."""
    auth = request.headers.get("authorization", "")
    if not auth.lower().startswith("bearer "):
        return None
    try:
        from Data.auth import JWT_ALGORITHM, _jwt_secret
        payload = jwt.decode(auth[7:], _jwt_secret(), algorithms=[JWT_ALGORITHM])
    except jwt.InvalidTokenError:
        return None
    user_id = payload.get("user_id")
    return f"user:{user_id}" if user_id is not None else None


def _too_many(retry_after: int) -> JSONResponse:
    return JSONResponse(
        {"detail": f"Too many requests. Try again in {retry_after} seconds."},
        status_code=429,
        headers={"Retry-After": str(retry_after)},
    )


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if not _enabled() or request.method == "OPTIONS":
            return await call_next(request)

        path = request.url.path
        for method_re, path_re, limits_, key_kind in _COMPILED:
            if not (method_re.match(request.method) and path_re.match(path)):
                continue
            client_ip = request.client.host if request.client else "unknown"
            key = (_user_key(request) if key_kind == "user" else None) or f"ip:{client_ip}"
            try:
                retry_after = _hit(limits_, path_re.pattern, key)
            except Exception:
                logger.exception("rate limit store failed -- letting %s %s through", request.method, path)
                break
            if retry_after is not None:
                logger.warning("rate limited %s %s for %s", request.method, path, key)
                return _too_many(retry_after)
            break

        return await call_next(request)


def check_email_limit(action: str, email: str) -> None:
    """Raise 429 if this email has hit the limit for `action` ('login' or
    'forgot-password'). Called before any bcrypt work in those handlers."""
    if not _enabled():
        return
    try:
        retry_after = _hit(_EMAIL_COMPILED[action], f"email-{action}", email.strip().lower())
    except Exception:
        logger.exception("rate limit store failed -- letting %s for an email through", action)
        return
    if retry_after is not None:
        raise HTTPException(
            status_code=429,
            detail=f"Too many attempts for this account. Try again in {retry_after} seconds.",
            headers={"Retry-After": str(retry_after)},
        )
