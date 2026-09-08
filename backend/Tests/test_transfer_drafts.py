"""
test_transfer_drafts.py — FastAPI TestClient tests for
Gameplay/transfer_drafts.py: the staged-but-not-confirmed transfer
cart (PUT/GET/DELETE /transfer-drafts).

Two things this file is really checking, beyond the CRUD:

  1. The upsert semantics of uq_transfer_drafts_user_season_gw_out --
     re-drafting the same OUTGOING player replaces the pair in place
     rather than adding a row, while the same INCOMING player against
     two different outgoing players is explicitly allowed (a normal
     mid-edit state, see the module docstring).

  2. That drafts are INERT. Nothing outside the three endpoints reads
     transfer_drafts, so staging a pair must not move a squad, a budget,
     a free-transfer count or a score. test_drafts_do_not_affect_transfers_used
     and test_drafts_do_not_touch_the_squad_or_budget pin that directly,
     because it is the entire safety argument for shipping this ahead of
     the rest of the draft-transfers work.

Touches no ml.* fixture data at all -- a draft is just two integers and
a gameweek, and this module never joins to ml.players (deliberately: it
does not validate that the players exist, since POST /transfers does
that authoritatively at Confirm). So there is no make_team/make_player
seeding here, unlike every other Game_logic test file.

Unlike test_transfers.py, this fixture CAN delete its users: transfer_drafts
cascades from users and carries no immutability trigger, so nothing is
left behind. That is the point of the ON DELETE CASCADE, and
test_deleting_a_user_cascades_their_drafts_away asserts it.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from main import app  # shared FastAPI app -- transfer_drafts' router is mounted on it

client = TestClient(app)

GAMEWEEK = 1


@pytest.fixture
def make_user(make_user, engine):
    """Fully cleans up, cascade included -- see the module docstring."""
    # Creation delegated to conftest's make_user (same name, received as an
    # argument -- pytest resolves it to the parent fixture). The prefix is
    # passed so the rows this file creates are still named as they were.
    # The teardown below stays here because it is specific to this file.
    factory = make_user

    def _make():
        return factory("drafts")

    yield _make

    with engine.begin() as conn:
        for uid in factory.created:
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})  # cascades to transfer_drafts


def _put(user_id, player_out_id, player_in_id, gameweek=GAMEWEEK, season=TEST_SEASON):
    return client.put(
        "/transfer-drafts",
        json={
            "season": season,
            "gameweek": gameweek,
            "player_out_id": player_out_id,
            "player_in_id": player_in_id,
        }, headers=bearer_headers(user_id)
    )


def _get(user_id, gameweek=GAMEWEEK, season=TEST_SEASON):
    return client.get(
        "/transfer-drafts",
        params={"season": season, "gameweek": gameweek}, headers=bearer_headers(user_id)
    )


def _delete(user_id, draft_id, gameweek=GAMEWEEK, season=TEST_SEASON):
    return client.request(
        "DELETE",
        f"/transfer-drafts/{draft_id}",
        params={"season": season, "gameweek": gameweek}, headers=bearer_headers(user_id)
    )


def _pairs(body):
    return [(d["player_out_id"], d["player_in_id"]) for d in body["drafts"]]


def _row_count(engine, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM transfer_drafts WHERE user_id = :u"), {"u": user_id}
        ).scalar()


# ---------------------------------------------------------------- add


def test_put_creates_a_draft_pair(engine, make_user):
    user_id = make_user()

    resp = _put(user_id, player_out_id=101, player_in_id=201)

    assert resp.status_code == 200
    body = resp.json()
    assert body["user_id"] == user_id
    assert body["season"] == TEST_SEASON
    assert body["gameweek"] == GAMEWEEK
    assert body["player_out_id"] == 101
    assert body["player_in_id"] == 201
    assert isinstance(body["id"], int)
    assert _row_count(engine, user_id) == 1


def test_put_accepts_players_that_do_not_exist_in_ml_players(make_user):
    """A draft is not validated against the game rules -- ownership,
    position match, budget and club cap are all POST /transfers' job at
    Confirm. These ids resolve to nothing at all and are still accepted,
    which is the deliberate line this module draws (see its docstring)."""
    user_id = make_user()

    assert _put(user_id, player_out_id=999_001, player_in_id=999_002).status_code == 200


def test_put_rejects_the_same_player_on_both_sides(engine, make_user):
    user_id = make_user()

    resp = _put(user_id, player_out_id=101, player_in_id=101)

    assert resp.status_code == 422
    assert any("must differ" in e for e in resp.json()["detail"])
    assert _row_count(engine, user_id) == 0


# ---------------------------------------------------------------- update (upsert)


def test_redrafting_the_same_outgoing_player_replaces_the_pair_in_place(engine, make_user):
    """uq_transfer_drafts_user_season_gw_out makes this an upsert: one
    row, same id, new incoming player -- not a second row the client
    would then have to reconcile."""
    user_id = make_user()

    first = _put(user_id, player_out_id=101, player_in_id=201).json()
    second = _put(user_id, player_out_id=101, player_in_id=202).json()

    assert second["id"] == first["id"]
    assert second["player_in_id"] == 202
    assert _row_count(engine, user_id) == 1
    assert _pairs(_get(user_id).json()) == [(101, 202)]


def test_redrafting_bumps_updated_at_but_keeps_created_at(engine, make_user):
    user_id = make_user()
    draft_id = _put(user_id, player_out_id=101, player_in_id=201).json()["id"]

    with engine.connect() as conn:
        before = conn.execute(
            text("SELECT created_at, updated_at FROM transfer_drafts WHERE id = :i"), {"i": draft_id}
        ).first()

    _put(user_id, player_out_id=101, player_in_id=202)

    with engine.connect() as conn:
        after = conn.execute(
            text("SELECT created_at, updated_at FROM transfer_drafts WHERE id = :i"), {"i": draft_id}
        ).first()

    assert after.created_at == before.created_at
    assert after.updated_at > before.updated_at


def test_the_same_incoming_player_may_be_queued_against_two_outgoing_players(engine, make_user):
    """Deliberately NOT constrained -- a manager mid-edit can have one
    target lined up against two different players before resolving which
    one they actually want. Constraining player_in_id would reject a
    normal intermediate state."""
    user_id = make_user()

    assert _put(user_id, player_out_id=101, player_in_id=201).status_code == 200
    assert _put(user_id, player_out_id=102, player_in_id=201).status_code == 200

    assert _row_count(engine, user_id) == 2
    assert _pairs(_get(user_id).json()) == [(101, 201), (102, 201)]


def test_drafts_are_scoped_per_gameweek(engine, make_user):
    user_id = make_user()

    _put(user_id, player_out_id=101, player_in_id=201, gameweek=1)
    _put(user_id, player_out_id=101, player_in_id=202, gameweek=2)

    assert _row_count(engine, user_id) == 2  # same outgoing player, different gameweeks
    assert _pairs(_get(user_id, gameweek=1).json()) == [(101, 201)]
    assert _pairs(_get(user_id, gameweek=2).json()) == [(101, 202)]


# ---------------------------------------------------------------- list


def test_get_returns_an_empty_cart_rather_than_404(make_user):
    user_id = make_user()

    resp = _get(user_id)

    assert resp.status_code == 200
    assert resp.json()["drafts"] == []


def test_get_returns_only_this_users_drafts(make_user):
    mine = make_user()
    theirs = make_user()

    _put(mine, player_out_id=101, player_in_id=201)
    _put(theirs, player_out_id=103, player_in_id=203)

    assert _pairs(_get(mine).json()) == [(101, 201)]
    assert _pairs(_get(theirs).json()) == [(103, 203)]


def test_get_orders_drafts_by_when_they_were_staged(make_user):
    user_id = make_user()

    for out_id, in_id in [(103, 203), (101, 201), (102, 202)]:
        _put(user_id, player_out_id=out_id, player_in_id=in_id)

    assert _pairs(_get(user_id).json()) == [(103, 203), (101, 201), (102, 202)]


# ---------------------------------------------------------------- delete


def test_delete_removes_one_pair_and_returns_the_remaining_cart(engine, make_user):
    user_id = make_user()
    first = _put(user_id, player_out_id=101, player_in_id=201).json()["id"]
    _put(user_id, player_out_id=102, player_in_id=202)

    resp = _delete(user_id, first)

    assert resp.status_code == 200
    assert _pairs(resp.json()) == [(102, 202)]  # the remaining cart, no follow-up GET needed
    assert _row_count(engine, user_id) == 1


def test_delete_of_an_unknown_draft_is_404(make_user):
    user_id = make_user()

    assert _delete(user_id, 999_999).status_code == 404


def test_a_user_cannot_delete_someone_elses_draft(engine, make_user):
    """Reported as 404, identically to a draft that does not exist --
    this app passes user_id explicitly rather than deriving it from the
    token, so owner-scoping the delete is what stops a guessed id."""
    owner = make_user()
    attacker = make_user()
    draft_id = _put(owner, player_out_id=101, player_in_id=201).json()["id"]

    resp = _delete(attacker, draft_id)

    assert resp.status_code == 404
    assert _row_count(engine, owner) == 1  # untouched


def test_redrafting_after_a_delete_starts_a_fresh_row(engine, make_user):
    """The unique constraint has no soft-delete predicate to trip over --
    the row is really gone, so the same outgoing player can be staged
    again."""
    user_id = make_user()
    draft_id = _put(user_id, player_out_id=101, player_in_id=201).json()["id"]
    _delete(user_id, draft_id)

    again = _put(user_id, player_out_id=101, player_in_id=203)

    assert again.status_code == 200
    assert again.json()["id"] != draft_id
    assert _row_count(engine, user_id) == 1


def test_deleting_a_user_cascades_their_drafts_away(engine):
    """transfer_drafts is the first transfer-shaped table a test can
    actually tear down -- `transfers` blocks even a cascade from its
    parent users row (see test_transfers.py's fixture docstring)."""
    unique = uuid.uuid4().hex[:12]
    with engine.begin() as conn:
        uid = conn.execute(
            text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, 'x') RETURNING id"),
            {"e": f"pytest_drafts_cascade_{unique}@example.com", "u": f"pytest_drafts_cascade_{unique}"},
        ).scalar()

    _put(uid, player_out_id=101, player_in_id=201)
    assert _row_count(engine, uid) == 1

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})

    assert _row_count(engine, uid) == 0


# ---------------------------------------------------------------- isolation


def test_drafts_do_not_affect_transfers_used(make_user):
    """The safety property this whole PR rests on: staging a pair must
    not move the free-transfer arithmetic, which reads `transfers` alone
    (transfers.FREE_TRANSFERS_USED_QUERY). If this ever fails, drafts
    have leaked into the confirm path."""
    user_id = make_user()

    before = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(user_id)
    ).json()

    for out_id, in_id in [(101, 201), (102, 202), (103, 203)]:
        _put(user_id, player_out_id=out_id, player_in_id=in_id)

    after = client.get(
        "/transfers/used", params={"season": TEST_SEASON, "gameweek": GAMEWEEK}, headers=bearer_headers(user_id)
    ).json()

    assert after == before
    assert after["free_transfers_used"] == 0
    assert after["free_transfers_remaining"] == 1
    assert after["total_transfers_this_gameweek"] == 0


def test_drafts_do_not_touch_the_squad_or_budget(engine, make_user):
    """A draft writes exactly one row to exactly one table -- no
    squad_players flip, no user_squads budget move, no transfers row."""
    user_id = make_user()
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO user_squads (user_id, season, budget_remaining) VALUES (:u, :s, 1000)"),
            {"u": user_id, "s": TEST_SEASON},
        )

    _put(user_id, player_out_id=101, player_in_id=201)

    with engine.connect() as conn:
        budget = conn.execute(
            text("SELECT budget_remaining FROM user_squads WHERE user_id = :u AND season = :s"),
            {"u": user_id, "s": TEST_SEASON},
        ).first()
        transfers = conn.execute(
            text("SELECT COUNT(*) FROM transfers WHERE user_id = :u"), {"u": user_id}
        ).scalar()
        squad_players = conn.execute(
            text("SELECT COUNT(*) FROM squad_players sp JOIN user_squads us ON us.id = sp.user_squad_id "
                 "WHERE us.user_id = :u"),
            {"u": user_id},
        ).scalar()

    assert budget.budget_remaining == 1000
    assert transfers == 0
    assert squad_players == 0

    with engine.begin() as conn:
        conn.execute(text("DELETE FROM user_squads WHERE user_id = :u"), {"u": user_id})


def test_post_to_the_draft_path_is_405_not_500(make_user):
    """The write is a PUT because staging is idempotent, which is why
    /transfer-drafts is absent from test_api_contracts.py's POST_ENDPOINTS
    list. Pinned here so that absence reads as deliberate."""
    user_id = make_user()

    resp = client.post(
        "/transfer-drafts",
        json={"season": TEST_SEASON, "gameweek": GAMEWEEK,
              "player_out_id": 101, "player_in_id": 201}, headers=bearer_headers(user_id)
    )

    assert resp.status_code == 405


def test_malformed_body_returns_422_not_500(make_user):
    """Same baseline contract test_api_contracts.py applies to every POST
    endpoint, applied here by hand since this route is a PUT."""
    resp = client.request(
        "PUT", "/transfer-drafts", content="{not valid json",
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422

    assert client.put("/transfer-drafts", json={}, headers=bearer_headers(make_user())).status_code == 422
