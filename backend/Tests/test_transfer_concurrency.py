"""
test_transfer_concurrency.py — pins the per-user advisory lock that closes
the TOCTOU gap in submit_transfers.

THE RACE. Before Stage 3, submit_transfers read the budget, the
free-transfer consumption and the allowance in an engine.connect() block
that CLOSED, then opened a separate engine.begin() to write a plan already
decided from those reads. Two batches for the same manager could each
validate against the same pre-state and both commit: both stamped
is_free = TRUE against a single free slot, and both subtracted their cost
from the same starting budget.

WHY A DETERMINISTIC WINDOW, NOT A HOPEFUL ONE. Firing two requests and
hoping they interleave makes a test that passes on a fast machine and
fails in CI, or worse, passes everywhere while proving nothing. Instead
each test below widens the read phase by patching
transfers.free_transfers_available -- the last read before validation --
to sleep. That makes the interleaving certain: without a lock both
requests are guaranteed to be inside the read phase together.

The lock is what makes that widened window harmless. The second request
blocks on pg_advisory_xact_lock BEFORE its first read, so it never sees
the stale state at all -- it waits out the first request's sleep, then
reads the committed result. A test that only passes because the machine
was fast would not survive the patch; these do.

PROVEN AGAINST THE OLD CODE, twice. Each assertion here was re-run with
(a) the lock removed entirely and (b) the lock moved to the write
transaction only -- the "just add one line" version -- and fails in both.
(b) is the one worth remembering: serialising the writes changes nothing,
because by then both requests have already decided is_free and
budget_remaining_after.
"""

import threading
import time

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

import Gameplay.transfers as transfers_mod
from conftest import TEST_SEASON, bearer_headers
from main import app

FULL_SQUAD_POSITIONS = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
GAMEWEEK = 1
# A gameweek by which the bank has reached FREE_TRANSFER_BANK_CAP (2), so
# two batches can both be within the allowance and race for something else.
BANKED_GAMEWEEK = 3
READ_PHASE_DELAY = 0.6


@pytest.fixture
def test_user(make_user):
    return make_user("concurrency")


@pytest.fixture
def slow_read_phase(monkeypatch):
    """Widen submit_transfers' read phase so an unlocked implementation is
    guaranteed to interleave. free_transfers_available is the last read
    before validation, so sleeping here holds both requests inside the
    window they used to share."""
    real = transfers_mod.free_transfers_available

    def _slow(conn, user_id, season, gameweek):
        value = real(conn, user_id, season, gameweek)
        time.sleep(READ_PHASE_DELAY)
        return value

    monkeypatch.setattr(transfers_mod, "free_transfers_available", _slow)
    return _slow


def _seed_squad(engine, make_team, make_player, user_id, id_offset=9000, cost=60):
    team_ids = {}
    fpl_ids = []
    for i, pos in enumerate(FULL_SQUAD_POSITIONS):
        if i not in team_ids:
            team_ids[i] = make_team(fpl_id=id_offset + 1000 + i, name=f"Club{i}", short_name=f"C{i}")
        fid = id_offset + i
        make_player(fpl_id=fid, position=pos, team_id=team_ids[i], cost_start=cost)
        fpl_ids.append(fid)

    budget = 1000 - cost * len(FULL_SQUAD_POSITIONS)
    with engine.begin() as conn:
        squad_id = conn.execute(
            text("INSERT INTO user_squads (user_id, season, budget_remaining) "
                 "VALUES (:u, :s, :b) RETURNING id"),
            {"u": user_id, "s": TEST_SEASON, "b": budget},
        ).scalar()
        for fid in fpl_ids:
            conn.execute(
                text("INSERT INTO squad_players (user_squad_id, player_id, purchase_price, is_active) "
                     "VALUES (:q, :p, :c, TRUE)"),
                {"q": squad_id, "p": fid, "c": cost},
            )
    return {"squad_id": squad_id, "budget": budget,
            "GK": fpl_ids[0:2], "DEF": fpl_ids[2:7], "MID": fpl_ids[7:12], "FWD": fpl_ids[12:15]}


def _make_spare(make_team, make_player, fpl_id, position, cost=60, team_base=9800):
    team = make_team(fpl_id=team_base + fpl_id % 100, name=f"Spare{fpl_id}", short_name=f"S{fpl_id}")
    make_player(fpl_id=fpl_id, position=position, team_id=team, cost_start=cost)
    return fpl_id


def _fire_together(payloads, user_id):
    """Two POST /transfers at once, each on its own client so they are
    genuinely concurrent rather than queued behind one portal."""
    results = {}
    barrier = threading.Barrier(len(payloads))

    def _go(idx, body):
        c = TestClient(app)
        barrier.wait()
        results[idx] = c.post("/transfers", json=body, headers=bearer_headers(user_id))

    threads = [threading.Thread(target=_go, args=(i, b)) for i, b in enumerate(payloads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    return [results[i] for i in sorted(results)]


def _transfer_rows(engine, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT player_in_id, is_free FROM transfers "
                 "WHERE user_id = :u AND season = :s AND gameweek = :g ORDER BY id"),
            {"u": user_id, "s": TEST_SEASON, "g": GAMEWEEK},
        ).all()


def _budget(engine, squad_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT budget_remaining FROM user_squads WHERE id = :i"), {"i": squad_id}
        ).scalar()


# ---------------------------------------------------------------- the race


def test_two_concurrent_batches_cannot_both_spend_the_last_free_transfer(
    engine, make_team, make_player, test_user, slow_read_phase
):
    """Gameweek 1 carries exactly ONE free transfer.

    Each batch is a single swap, so each is perfectly valid on its own and
    would be free if it ran first. Gameweek 1 grants exactly one, and under
    the tactical rules there is NO paid fallback -- so run together, one must
    win and the other must be REJECTED. Unlocked, both read
    free_used_count = 0, both decide they are within the allowance, and both
    commit.

    Re-pinned for the tactical rules. The guarantee is unchanged and the test
    is now STRICTER than it was: previously the loser was merely charged 4
    points (200, is_free = FALSE), so a lock failure cost points; now it
    would hand out a transfer that does not exist.
    """
    squad = _seed_squad(engine, make_team, make_player, test_user)
    in_a = _make_spare(make_team, make_player, 9501, "MID")
    in_b = _make_spare(make_team, make_player, 9502, "MID")

    def body(out_id, in_id):
        return {"season": TEST_SEASON, "gameweek": GAMEWEEK,
                "transfers": [{"player_out_id": out_id, "player_in_id": in_id}]}

    responses = _fire_together(
        [body(squad["MID"][0], in_a), body(squad["MID"][1], in_b)], test_user
    )

    codes = sorted(r.status_code for r in responses)
    assert codes == [200, 422], (
        f"expected exactly one winner and one rejection, got {codes}: "
        f"{[r.text for r in responses]}"
    )

    rows = _transfer_rows(engine, test_user)
    assert len(rows) == 1, (
        f"gameweek 1 grants exactly one free transfer and there are no paid "
        f"ones, so exactly one row may exist -- got {len(rows)}: {rows}"
    )
    assert rows[0].is_free is True

    rejected = next(r for r in responses if r.status_code == 422)
    assert any("free transfer" in e for e in rejected.json()["detail"])


def test_two_concurrent_batches_cannot_both_spend_the_same_budget(
    engine, make_team, make_player, test_user, slow_read_phase
):
    """Budget is read-modify-write: budget_remaining_after is computed from
    the budget read at the start. Unlocked, both batches subtract from the
    same starting figure and the second silently overwrites the first, so
    one swap's cost vanishes.

    Squad is seeded at cost 60 (budget 100) and both incoming players cost
    90, so each swap removes 30. Sequentially that is 100 -> 70 -> 40.

    Re-pinned for the tactical rules: this runs in gameweek 3, not gameweek 1.
    The bank reaches FREE_TRANSFER_BANK_CAP (2) by then, so BOTH batches are
    within the allowance and the budget is what they race for. In gameweek 1
    the allowance is 1, so the second batch would be rejected for the
    allowance and never reach the budget arithmetic this test exists to pin.
    """
    squad = _seed_squad(engine, make_team, make_player, test_user, cost=60)
    in_a = _make_spare(make_team, make_player, 9601, "DEF", cost=90)
    in_b = _make_spare(make_team, make_player, 9602, "DEF", cost=90)

    def body(out_id, in_id):
        return {"season": TEST_SEASON, "gameweek": BANKED_GAMEWEEK,
                "transfers": [{"player_out_id": out_id, "player_in_id": in_id}]}

    responses = _fire_together(
        [body(squad["DEF"][0], in_a), body(squad["DEF"][1], in_b)], test_user
    )
    assert [r.status_code for r in responses] == [200, 200], [r.text for r in responses]

    assert _budget(engine, squad["squad_id"]) == 40, (
        "budget must reflect BOTH swaps; a higher figure means the second batch "
        "computed from the pre-first-batch budget"
    )


def test_serialised_batches_are_unaffected_by_the_lock(
    engine, make_team, make_player, test_user, slow_read_phase
):
    """The control: the lock must not change single-threaded behaviour.

    Re-pinned for the tactical rules. Sequentially in gameweek 1 the first
    batch takes the single free transfer and the second is rejected, because
    paid transfers no longer exist. That is exactly the outcome the
    concurrent test above demands -- which is the point of the control: the
    lock makes the racing case behave like this serial one, rather than
    inventing behaviour of its own."""
    squad = _seed_squad(engine, make_team, make_player, test_user)
    in_a = _make_spare(make_team, make_player, 9701, "FWD")
    in_b = _make_spare(make_team, make_player, 9702, "FWD")
    client = TestClient(app)

    expected = [200, 422]
    for (out_id, in_id), want in zip(
        ((squad["FWD"][0], in_a), (squad["FWD"][1], in_b)), expected
    ):
        resp = client.post(
            "/transfers",
            json={"season": TEST_SEASON, "gameweek": GAMEWEEK,
                  "transfers": [{"player_out_id": out_id, "player_in_id": in_id}]},
            headers=bearer_headers(test_user),
        )
        assert resp.status_code == want, resp.text

    rows = _transfer_rows(engine, test_user)
    assert [r.is_free for r in rows] == [True]


def test_a_rejected_batch_releases_the_lock(engine, make_team, make_player, test_user):
    """pg_advisory_xact_lock is transaction-scoped, so the 422 path -- which
    raises INSIDE the transaction to abort it -- must release the lock on
    rollback. If it did not, this file's own second call would hang, and so
    would every later request from this manager."""
    squad = _seed_squad(engine, make_team, make_player, test_user)
    client = TestClient(app)

    bad = client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": GAMEWEEK,
              "transfers": [{"player_out_id": squad["GK"][0], "player_in_id": squad["GK"][0]}]},
        headers=bearer_headers(test_user),
    )
    assert bad.status_code == 422

    with engine.connect() as conn:
        held = conn.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' "
                 "AND classid = :ns AND objid = :uid"),
            {"ns": transfers_mod.USER_TRANSFER_LOCK_NAMESPACE, "uid": test_user},
        ).scalar()
    assert held == 0, "the rejected batch left its advisory lock held"

    # And the manager can still transfer afterwards.
    good = _make_spare(make_team, make_player, 9801, "GK")
    ok = client.post(
        "/transfers",
        json={"season": TEST_SEASON, "gameweek": GAMEWEEK,
              "transfers": [{"player_out_id": squad["GK"][0], "player_in_id": good}]},
        headers=bearer_headers(test_user),
    )
    assert ok.status_code == 200, ok.text


def test_two_different_managers_do_not_block_each_other(
    engine, make_team, make_player, make_user, slow_read_phase
):
    """The lock is per-user, so unrelated managers must still run in
    parallel. With the read phase slowed to 0.6s, serialising everyone
    would take at least 1.2s; concurrent execution stays near 0.6s."""
    a, b = make_user("concurrency"), make_user("concurrency")
    sq_a = _seed_squad(engine, make_team, make_player, a, id_offset=9000)
    sq_b = _seed_squad(engine, make_team, make_player, b, id_offset=9200)
    in_a = _make_spare(make_team, make_player, 9901, "MID")
    in_b = _make_spare(make_team, make_player, 9902, "MID")

    results = {}
    barrier = threading.Barrier(2)

    def _go(idx, uid, out_id, in_id):
        c = TestClient(app)
        barrier.wait()
        results[idx] = c.post(
            "/transfers",
            json={"season": TEST_SEASON, "gameweek": GAMEWEEK,
                  "transfers": [{"player_out_id": out_id, "player_in_id": in_id}]},
            headers=bearer_headers(uid),
        )

    t0 = time.time()
    threads = [
        threading.Thread(target=_go, args=(0, a, sq_a["MID"][0], in_a)),
        threading.Thread(target=_go, args=(1, b, sq_b["MID"][0], in_b)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    elapsed = time.time() - t0

    assert [results[0].status_code, results[1].status_code] == [200, 200]
    assert elapsed < READ_PHASE_DELAY * 1.8, (
        f"took {elapsed:.2f}s for two DIFFERENT managers -- they are serialising "
        "against each other, so the lock key is not per-user"
    )
    # Each got their own free transfer; they share no allowance.
    assert [r.is_free for r in _transfer_rows(engine, a)] == [True]
    assert [r.is_free for r in _transfer_rows(engine, b)] == [True]
