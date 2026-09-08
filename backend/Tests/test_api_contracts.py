"""
test_api_contracts.py — cross-cutting negative tests applied to every POST
endpoint in the app: malformed JSON body and missing required fields must
always come back as a clean 422 (FastAPI/Pydantic validation), never a 500.
Endpoint-specific business-rule validation (formation counts, budget caps,
lock checks, etc.) is already covered per-router in the other test files --
this file only checks the baseline contract every endpoint should share.

AUTH ORDERING, and why these requests now carry a token. FastAPI resolves
security dependencies BEFORE it validates the body or the query string, so
after the Stage 2 cutover an unauthenticated request to a classic endpoint
is a 401 whatever its body looks like -- the malformed JSON never reaches
Pydantic at all. The contract being asserted here is unchanged (bad input
is a clean 422, never a 500); it simply has to get past the door first, so
every classic call below presents a valid credential.

That ordering is itself worth pinning, and
test_unauthenticated_post_is_401_whatever_the_body_looks_like does: a
caller with no token must be refused on the credential, not told which
fields it got wrong. Leaking validation detail to an anonymous caller
would be a small information disclosure, and the 401-before-422 ordering
is what prevents it.

Dream11's endpoints used to sit in a separate list here because they were
unauthenticated. They no longer are -- dream11.py's router carries the
auth dependency itself -- so there is one list again, and every POST in
the app is held to both contracts: 422 on bad input WITH a credential,
401 on bad input WITHOUT one.
"""

import pytest
from fastapi.testclient import TestClient

from conftest import bearer_headers
from main import app

client = TestClient(app)

# Every POST in the app -- classic (authenticated at Stage 2) and dream11
# (authenticated at the router). One list on purpose: the split that used to
# be here is exactly the shape of gap that let dream11's endpoints go
# unauthenticated while the suite stayed green.
POST_ENDPOINTS = [
    "/chat",
    "/squad/select",
    "/gw_selection",
    "/transfers",
    "/leagues",
    "/leagues/join",
    "/dream11/contests",
    "/dream11/contests/join",
    "/dream11/contests/1/team",
]

# Authenticated GETs whose remaining query params are still required.
GET_ENDPOINTS_MISSING_PARAMS = ["/squad", "/gw_selection", "/transfers/used", "/team"]


@pytest.fixture
def auth(make_user):
    """A valid credential. Its owner has no squad, no league and no drafts --
    these tests never get far enough to care."""
    return bearer_headers(make_user())


# --------------------------------------------------------------- 422 contract


@pytest.mark.parametrize("path", POST_ENDPOINTS)
def test_malformed_json_body_returns_422_not_500(path, auth):
    resp = client.post(path, content="{not valid json",
                       headers={"Content-Type": "application/json", **auth})

    assert resp.status_code == 422, f"{path} returned {resp.status_code} for malformed JSON, expected 422"


@pytest.mark.parametrize("path", POST_ENDPOINTS)
def test_empty_body_returns_422_not_500(path, auth):
    resp = client.post(path, json={}, headers=auth)

    assert resp.status_code == 422, f"{path} returned {resp.status_code} for an empty body, expected 422"


@pytest.mark.parametrize("path", POST_ENDPOINTS)
def test_wrong_type_for_all_fields_returns_422_not_500(path, auth):
    """Every field a string when the model expects ints/lists/etc -- a
    common malformed-client scenario (e.g. a frontend bug serializing a
    form field wrong) -- must fail validation cleanly, not crash the
    handler with a TypeError before Pydantic even gets a chance."""
    resp = client.post(path, json={"garbage_field": "unexpected"}, headers=auth)

    assert resp.status_code == 422, f"{path} returned {resp.status_code} for an unrecognized body shape, expected 422"


# ------------------------------------------------------- auth beats validation


@pytest.mark.parametrize("path", POST_ENDPOINTS)
def test_unauthenticated_post_is_401_whatever_the_body_looks_like(path):
    """No token means no answer -- not even a description of what was wrong
    with the request. Pins the dependency-before-validation ordering."""
    for body in ({}, {"garbage_field": "unexpected"}):
        resp = client.post(path, json=body)
        assert resp.status_code == 401, (
            f"{path} returned {resp.status_code} for an unauthenticated call with body {body}, expected 401"
        )


# ------------------------------------------------------- missing query params


def test_get_players_missing_required_query_param_returns_422():
    """/players carries no identity and stayed unauthenticated, so this one
    needs no credential -- the original contract applies unchanged."""
    resp = client.get("/players")
    assert resp.status_code == 422


@pytest.mark.parametrize("path", GET_ENDPOINTS_MISSING_PARAMS)
def test_get_missing_required_query_params_returns_422(path, auth):
    resp = client.get(path, headers=auth)
    assert resp.status_code == 422, f"{path} returned {resp.status_code} with no query params, expected 422"


@pytest.mark.parametrize("path", GET_ENDPOINTS_MISSING_PARAMS)
def test_get_missing_required_query_params_is_401_before_422_without_a_token(path):
    resp = client.get(path)
    assert resp.status_code == 401, f"{path} returned {resp.status_code} unauthenticated, expected 401"
