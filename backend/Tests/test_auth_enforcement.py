"""
test_auth_enforcement.py — the tests that hold the WHOLE API surface to the
auth contract.

Three things:

  1. Every authenticated endpoint in the app refuses an unauthenticated
     caller with a 401 -- enumerated in ENDPOINTS below.
  2. A token that is malformed, wrongly signed, expired, or names a user
     who no longer exists is refused the same way.
  3. CROSS-USER: a caller holding user A's token cannot act as, or read,
     user B -- even by sending B's id explicitly.

(3) is the one that matters, and it is deliberately written as a stale
client rather than an attacker: every request below carries A's token AND
a user_id naming B, exactly what the pre-cutover frontend still sends.
Unknown fields are ignored (that is what let the cutover ship before the
frontend changed), so the assertion is that the SERVER decides identity
and the parameter is inert.

That shape is what makes these tests real rather than tautological. Under
the old code the very same requests would have been honoured -- user_id was
the identity, so passing B's id WAS being B. Verified, not assumed: each
cross-user test was re-run against a handler patched to prefer the
parameter again, and each one fails there. The comment above each says what
the old behaviour would have been.

WHY THIS TABLE COVERS EVERYTHING, AND IS CHECKED TO.

ENDPOINTS used to hold "the 17 classic endpoints" and said, in a closing
note, that Dream11 was out of scope. That scoping note was the bug. Dream11
shipped nine endpoints of which exactly one authenticated; the other eight
took a caller-supplied user_id and would create a contest, join a contest,
or SUBMIT A TEAM as any user named, with no credential at all. The suite
stayed green throughout, because a table that describes part of the API
cannot fail for the part it does not describe.

So the table is now the whole authenticated surface, and
test_the_endpoint_table_covers_every_route_in_the_app walks the live
FastAPI app and fails if any route is in neither ENDPOINTS nor
PUBLIC_ROUTES. Adding an endpoint without deciding which list it belongs
in is now a test failure rather than a silent hole -- that is the only
mechanism here that actually prevents a recurrence; the table itself is
just data, and data goes stale.

PUBLIC_ROUTES is deliberately short and each entry carries its reason.
Putting a route there is a security decision, and the diff shows it as one.
"""

import re
from datetime import datetime, timedelta, timezone

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from Data.auth import JWT_ALGORITHM, _jwt_secret
from main import app

# The dream11 pool/contest seeding for the cross-user write test at the
# bottom. Imported rather than duplicated: building a submittable team needs
# a fixture, 30 priced players and a valid formation, and test_dream11.py
# already owns all three.
from test_dream11 import _create_contest, _join_contest, _pick_valid_team, _seed_fixture_and_pool

client = TestClient(app)

GAMEWEEK = 1

# (method, route template, query params, json body) for every authenticated
# endpoint in the app. The template is the FastAPI path -- placeholders and
# all -- so it can be compared directly against app.routes; the request path
# is derived from it by _request_path below.
#
# Bodies need not be valid: a 401 is decided before anything is parsed.
ENDPOINTS = [
    # --- identity
    ("GET", "/auth/me", None, None),
    ("DELETE", "/auth/me", None, None),
    # --- classic FPL
    ("GET", "/chips/used", {"season": TEST_SEASON, "gameweek": GAMEWEEK}, None),
    ("GET", "/fixtures", {"season": TEST_SEASON}, None),
    ("GET", "/leagues", {"season": TEST_SEASON}, None),
    ("POST", "/leagues", None, {"name": "X", "league_type": "classic", "scoring_type": "total"}),
    ("POST", "/leagues/join", None, {"code": "ABC123"}),
    ("GET", "/leagues/{league_id}/table", None, None),
    ("GET", "/leagues/{league_id}/h2h", {"gameweek": GAMEWEEK}, None),
    ("GET", "/gw_selection", {"season": TEST_SEASON, "gameweek": GAMEWEEK}, None),
    ("POST", "/gw_selection", None, {"season": TEST_SEASON, "gameweek": GAMEWEEK}),
    ("GET", "/squad", {"season": TEST_SEASON}, None),
    ("POST", "/squad/select", None, {"season": TEST_SEASON, "player_ids": []}),
    ("GET", "/team", {"season": TEST_SEASON, "gameweek": GAMEWEEK}, None),
    ("GET", "/gameweeks/current", None, None),
    ("GET", "/transfer-drafts", {"season": TEST_SEASON, "gameweek": GAMEWEEK}, None),
    ("PUT", "/transfer-drafts", None,
     {"season": TEST_SEASON, "gameweek": GAMEWEEK, "player_out_id": 1, "player_in_id": 2}),
    ("DELETE", "/transfer-drafts/{draft_id}", {"season": TEST_SEASON, "gameweek": GAMEWEEK}, None),
    ("GET", "/transfers/used", {"season": TEST_SEASON, "gameweek": GAMEWEEK}, None),
    ("POST", "/transfers", None, {"season": TEST_SEASON, "gameweek": GAMEWEEK, "transfers": []}),
    ("POST", "/chat", None, {"season": TEST_SEASON, "gameweek": GAMEWEEK, "message": "hi"}),
    # --- dream11: all nine, reads and writes alike. The three writes are the
    # ones that were exploitable -- an unauthenticated caller could create,
    # join and submit as anybody -- and the two scoped reads leaked any
    # user's contest list, join codes included.
    ("GET", "/dream11/contests", None, None),
    ("POST", "/dream11/contests", None, {"fixture_id": 1, "name": "X", "max_members": 10}),
    ("POST", "/dream11/contests/join", None, {"code": "ABC1234"}),
    ("GET", "/dream11/contests/{contest_id}", None, None),
    ("GET", "/dream11/contests/{contest_id}/players", None, None),
    ("GET", "/dream11/contests/{contest_id}/leaderboard", None, None),
    ("GET", "/dream11/contests/{contest_id}/team", {"user_id": 1}, None),
    ("POST", "/dream11/contests/{contest_id}/team", None,
     {"player_ids": [], "captain_id": 1, "vice_captain_id": 2}),
    ("GET", "/dream11/fixtures/{fixture_id}/contests", None, None),
]

# Routes that answer without a credential, and why. Everything not here must
# be in ENDPOINTS -- see test_the_endpoint_table_covers_every_route_in_the_app.
PUBLIC_ROUTES = {
    # You cannot present a token before you have one.
    ("POST", "/auth/register"),
    ("POST", "/auth/login"),
    # Same reasoning, for the same reason a locked-out user is contacting
    # these at all: no session exists yet to authenticate with. Neither
    # reveals whether an email is registered (see auth.py's
    # forgot_password) or anything about the account it might act on
    # without a valid, unexpired, single-use reset token.
    ("POST", "/auth/forgot-password"),
    ("POST", "/auth/reset-password"),
    # Season reference data: the same player catalogue for every caller, with
    # nothing user-scoped in it. Public by decision, not by omission.
    ("GET", "/players"),
    # Same category: the published point values for both games, identical for
    # every caller. The rules of the game are not a secret from someone who
    # hasn't signed in, and the "How Points Work" screens read it.
    ("GET", "/scoring-rules"),
    # FastAPI's own docs/schema routes, not application endpoints.
    ("GET", "/docs"),
    ("GET", "/docs/oauth2-redirect"),
    ("GET", "/redoc"),
    ("GET", "/openapi.json"),
    # Health checks. A monitoring probe (load balancer, uptime check) has
    # no user and no token to present -- requiring auth here would make
    # "is the service up" depend on a second thing (auth) that can itself
    # be the reason it's down. Neither leaks anything beyond DB reachability
    # and Beat-task run timestamps.
    ("GET", "/health"),
    ("GET", "/health/scheduled-tasks"),
}

IDS = [f"{m} {p}" for m, p, _, _ in ENDPOINTS]


def _request_path(template: str) -> str:
    """'/dream11/contests/{contest_id}/team' -> '/dream11/contests/1/team'.

    The table stores route templates so it can be diffed against the app's
    own routes; the id substituted here is irrelevant, since a 401 is
    decided before the path parameter is ever used."""
    return re.sub(r"\{[^}]+\}", "1", template)


def _app_routes() -> set[tuple[str, str]]:
    """Every (method, path template) the running app serves.

    Walks nested routers rather than reading app.routes flat: FastAPI 0.141
    stores an included router as a single _IncludedRouter entry holding the
    real routes on .original_router. Both shapes are handled so this keeps
    working across versions -- if this ever silently returned nothing, the
    coverage test below would pass vacuously, which is the one failure mode
    that would defeat its purpose, so it also asserts a plausible count.
    """
    found: set[tuple[str, str]] = set()

    def walk(routes):
        for route in routes:
            nested = getattr(route, "original_router", None)
            if nested is not None:
                walk(nested.routes)
                continue
            if hasattr(route, "routes") and not hasattr(route, "methods"):
                walk(route.routes)
                continue
            for method in getattr(route, "methods", None) or ():
                if method in ("HEAD", "OPTIONS"):
                    continue
                found.add((method, route.path))

    walk(app.routes)
    return found


def _call(method, path, params, body, headers=None):
    return client.request(method, path, params=params, json=body, headers=headers)


def _seed_squad_row(engine, user_id, budget=1000):
    with engine.begin() as conn:
        return conn.execute(
            text("INSERT INTO user_squads (user_id, season, budget_remaining) "
                 "VALUES (:u, :s, :b) RETURNING id"),
            {"u": user_id, "s": TEST_SEASON, "b": budget},
        ).scalar()


# ------------------------------------------------- 1. no credential at all


@pytest.mark.parametrize("method,template,params,body", ENDPOINTS, ids=IDS)
def test_every_endpoint_refuses_an_unauthenticated_call(method, template, params, body):
    path = _request_path(template)
    resp = _call(method, path, params, body)

    assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}, expected 401"
    assert resp.json()["detail"] == "not authenticated"


@pytest.mark.parametrize("method,template,params,body", ENDPOINTS, ids=IDS)
def test_every_endpoint_refuses_a_non_bearer_header(method, template, params, body):
    """HTTPBearer is configured auto_error=False so a wrong scheme lands on
    auth.py's own handler and returns the same 401 shape as a bad token,
    rather than HTTPBearer's built-in 403 (see auth.py's _bearer_scheme)."""
    path = _request_path(template)
    resp = _call(method, path, params, body, headers={"Authorization": "Basic Zm9vOmJhcg=="})

    assert resp.status_code == 401, f"{method} {path} returned {resp.status_code}, expected 401"


def test_the_endpoint_table_covers_every_route_in_the_app():
    """The guard that makes the table above mean something.

    ENDPOINTS is hand-written, so on its own it can only ever prove things
    about the routes someone remembered to add -- which is exactly how nine
    unauthenticated Dream11 endpoints coexisted with a green suite. This
    test closes that by comparing the table against the app itself: every
    route must be classified, either as authenticated (ENDPOINTS) or as
    deliberately public (PUBLIC_ROUTES, each entry with its reason).

    A new endpoint therefore cannot ship unclassified. If you land here
    after adding one: put it in ENDPOINTS if it touches user data, and in
    PUBLIC_ROUTES only if you mean it.
    """
    routes = _app_routes()

    # Guards against the walk silently finding nothing (see _app_routes) and
    # turning every assertion below into a tautology.
    assert len(routes) > 20, f"route discovery is broken -- only found {sorted(routes)}"

    tabled = {(method, template) for method, template, _, _ in ENDPOINTS}
    unclassified = routes - tabled - PUBLIC_ROUTES
    assert not unclassified, (
        "these routes are in neither ENDPOINTS nor PUBLIC_ROUTES, so nothing "
        f"asserts whether they require a credential: {sorted(unclassified)}"
    )

    # And the reverse: a table entry for a route that no longer exists is a
    # test that silently stopped covering anything.
    stale = tabled - routes
    assert not stale, f"ENDPOINTS names routes the app does not serve: {sorted(stale)}"


def test_every_dream11_route_is_in_the_authenticated_table():
    """Named separately from the coverage test above so a regression reads as
    what it is. This is the specific hole that shipped: dream11's router had
    no dependency, main.py mounted it without one, and get_current_user
    appeared on exactly one of its nine endpoints.

    Asserting the COUNT as well as the membership is the point -- a
    dream11 route added later and left out of ENDPOINTS fails here even if
    someone also adds it to PUBLIC_ROUTES.
    """
    dream11_routes = {(m, p) for m, p in _app_routes() if p.startswith("/dream11")}
    tabled = {(m, t) for m, t, _, _ in ENDPOINTS if t.startswith("/dream11")}

    assert dream11_routes == tabled
    assert len(tabled) == 9, f"expected all 9 dream11 routes, table has {len(tabled)}"


# ------------------------------------------------------- 2. bad credentials


def _token(payload):
    return jwt.encode(payload, _jwt_secret(), algorithm=JWT_ALGORITHM)


def test_a_garbage_token_is_401():
    resp = client.get("/squad", params={"season": TEST_SEASON},
                      headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


def test_a_token_signed_with_the_wrong_secret_is_401(make_user):
    uid = make_user()
    now = datetime.now(timezone.utc)
    forged = jwt.encode({"user_id": uid, "sub": str(uid), "iat": now,
                         "exp": now + timedelta(hours=1)},
                        "not-the-real-secret", algorithm=JWT_ALGORITHM)

    resp = client.get("/squad", params={"season": TEST_SEASON},
                      headers={"Authorization": f"Bearer {forged}"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


def test_an_expired_token_is_401(make_user):
    """Expiry is decided server-side on every request, so a token that was
    valid when issued stops working the moment it lapses -- the client is
    never trusted to check its own exp."""
    uid = make_user()
    past = datetime.now(timezone.utc) - timedelta(hours=24)
    expired = _token({"user_id": uid, "sub": str(uid), "iat": past,
                      "exp": past + timedelta(hours=1)})

    resp = client.get("/squad", params={"season": TEST_SEASON},
                      headers={"Authorization": f"Bearer {expired}"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "token expired"


def test_a_well_formed_token_with_no_user_id_claim_is_401():
    now = datetime.now(timezone.utc)
    resp = client.get("/squad", params={"season": TEST_SEASON},
                      headers={"Authorization": f"Bearer {_token({'sub': 'x', 'iat': now, 'exp': now + timedelta(hours=1)})}"})

    assert resp.status_code == 401
    assert resp.json()["detail"] == "invalid token"


def test_a_token_for_a_deleted_user_is_401(make_user, engine):
    """A credential can outlive the account it names. get_current_user
    re-reads the users row every request rather than trusting the claim."""
    uid = make_user()
    headers = bearer_headers(uid)
    assert client.get("/squad", params={"season": TEST_SEASON}, headers=headers).status_code == 200

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": uid})

    assert client.get("/squad", params={"season": TEST_SEASON}, headers=headers).status_code == 401


# ------------------------------------------------------------ 3. cross-user


def test_a_stale_user_id_query_param_cannot_read_another_users_squad(
    engine, make_user, make_team, make_player
):
    """OLD BEHAVIOUR: user_id was the identity, so this returned B's squad.
    Now the parameter is inert and the token decides."""
    a, b = make_user(), make_user()
    _seed_squad_row(engine, a, budget=1000)
    squad_b = _seed_squad_row(engine, b, budget=170)

    team = make_team(fpl_id=9400, name="Cross", short_name="CRS")
    player = make_player(fpl_id=9401, position="MID", team_id=team, cost_start=80)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                 "VALUES (:sq, :p, 80, TRUE)"),
            {"sq": squad_b, "p": 9401},
        )

    resp = client.get(
        "/squad",
        params={"season": TEST_SEASON, "user_id": b},   # <- names B on purpose
        headers=bearer_headers(a),                      # <- but the token is A
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == a, "the response must describe the token's owner, not the parameter"
    assert body["players"] == [], "A owns no players; B's squad must not leak"
    assert body["budget_remaining"] == 100.0, "A's budget, not B's 17.0"


def test_a_stale_user_id_body_field_cannot_write_a_draft_for_another_user(
    engine, make_user, make_team, make_player
):
    """OLD BEHAVIOUR: the draft would have been filed under B."""
    a, b = make_user(), make_user()
    team = make_team(fpl_id=9410, name="CrossD", short_name="CRD")
    out_id = make_player(fpl_id=9411, position="MID", team_id=team, cost_start=50)
    in_id = make_player(fpl_id=9412, position="MID", team_id=team, cost_start=50)

    resp = client.put(
        "/transfer-drafts",
        json={
            "user_id": b,                               # <- names B on purpose
            "season": TEST_SEASON,
            "gameweek": GAMEWEEK,
            "player_out_id": 9411,
            "player_in_id": 9412,
        },
        headers=bearer_headers(a),                      # <- but the token is A
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == a

    with engine.connect() as conn:
        owners = conn.execute(
            text("SELECT user_id FROM transfer_drafts WHERE season = :s AND gameweek = :g "
                 "AND user_id IN (:a, :b)"),
            {"s": TEST_SEASON, "g": GAMEWEEK, "a": a, "b": b},
        ).scalars().all()

    assert owners == [a], f"draft filed under {owners}, expected only A ({a})"

    # And B's own cart is untouched.
    b_cart = client.get("/transfer-drafts",
                        params={"season": TEST_SEASON, "gameweek": GAMEWEEK},
                        headers=bearer_headers(b))
    assert b_cart.json()["drafts"] == []


def test_a_stale_user_id_cannot_borrow_another_users_squad_to_transfer(engine, make_user):
    """The sharpest of the three: B has a squad and A does not.

    OLD BEHAVIOUR: passing user_id=B found B's squad and validation
    proceeded against it. Now the squad lookup uses the token's owner, so
    the failure names A -- proof the parameter was not consulted.
    """
    a, b = make_user(), make_user()
    _seed_squad_row(engine, b)   # only B has a squad

    resp = client.post(
        "/transfers",
        json={
            "user_id": b,                               # <- names B on purpose
            "season": TEST_SEASON,
            "gameweek": GAMEWEEK,
            "transfers": [{"player_out_id": 1, "player_in_id": 2}],
        },
        headers=bearer_headers(a),                      # <- but the token is A
    )

    assert resp.status_code == 422
    detail = " ".join(resp.json()["detail"])
    assert f"no squad found for user_id {a}" in detail, detail
    assert str(b) not in detail, "B's id must not appear -- the parameter was consulted"


def test_two_users_reading_the_same_endpoint_get_their_own_data(engine, make_user):
    """The positive counterpart: identity actually varies with the token."""
    a, b = make_user(), make_user()
    _seed_squad_row(engine, a, budget=1000)
    _seed_squad_row(engine, b, budget=250)

    a_body = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(a)).json()
    b_body = client.get("/squad", params={"season": TEST_SEASON}, headers=bearer_headers(b)).json()

    assert (a_body["user_id"], a_body["budget_remaining"]) == (a, 100.0)
    assert (b_body["user_id"], b_body["budget_remaining"]) == (b, 25.0)


# --------------------------------------------------- 4. cross-user, dream11
#
# The write path the router-level dependency was added for. A 401 check
# alone would NOT have closed this: an attacker who simply registers gets a
# valid token, and if user_id were still read from the body they would be
# back to submitting as anyone. So the assertion here is not "unauthenticated
# calls are refused" -- that is section 1 -- but that an AUTHENTICATED call
# carrying someone else's id in the body is filed under the token's owner.


def test_a_foreign_user_id_in_the_body_cannot_submit_a_team_as_someone_else(
    engine, make_user, make_team, make_player
):
    """A submits a team with B's id planted in the request body.

    OLD BEHAVIOUR: user_id was the submitter, so this filed the team under B
    -- overwriting whatever B intended to play, or locking B out of the
    contest entirely (dream11.teams has UNIQUE (contest_id, user_id) and
    there is no edit path, so the first team submitted for B is the only one
    B can ever have). Both users are deliberately members here, so nothing
    but identity decides the owner and the assertion cannot pass by accident.
    """
    a, b = make_user(), make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 7700)
    contest = _create_contest(fixture_id, a)          # A creates it and auto-joins
    cid = contest["contest_id"]
    assert _join_contest(b, contest["code"]).status_code == 200
    team = _pick_valid_team(pool)

    resp = client.post(
        f"/dream11/contests/{cid}/team",
        json={
            "user_id": b,                             # <- names B on purpose
            "player_ids": team,
            "captain_id": team[0],
            "vice_captain_id": team[1],
        },
        headers=bearer_headers(a),                    # <- but the token is A
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["user_id"] == a, "the response must name the token's owner, not the body field"

    with engine.connect() as conn:
        owners = conn.execute(
            text("SELECT user_id FROM dream11.teams WHERE contest_id = :c"), {"c": cid}
        ).scalars().all()

    assert owners == [a], f"team filed under {owners}, expected only A ({a})"

    # And B, whose id was planted, still has no team -- so B's own submission
    # is still available to them.
    b_team = client.get(
        f"/dream11/contests/{cid}/team", params={"user_id": b}, headers=bearer_headers(b)
    )
    assert b_team.status_code == 404, b_team.text
