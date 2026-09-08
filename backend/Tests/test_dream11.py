"""
test_dream11.py — tests for Game_logic/dream11.py (create/join/submit-team
HTTP endpoints), Game_logic/dream11_scoring.py (calculate_dream11_points,
score_dream11_contest), and Data/live_poll.py
(poll_fixture_checkpoint).

dream11.* tables have no immutability trigger blocking DELETE (only
BEFORE UPDATE on player_prices, BEFORE INSERT on teams) -- confirmed
before writing this file -- so unlike transfers.py/leaderboard_
snapshots' tests earlier this session, a normal DELETE-based test_user
teardown works fine here; no permanent-row workaround needed.

poll_fixture_checkpoint calls FPL's live API, which cannot be exercised
with synthetic test data (matches don't happen on demand) -- fetch_json
is monkeypatched for those tests specifically, same precedent as
test_context_assembler.py mocking call_groq (an external API), while
everything else in this file hits the real DB, no mocks.
"""

from types import SimpleNamespace

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app as fastapi_app
from Data.auth import create_access_token
from Game_logic.dream11 import MAX_MAX_MEMBERS, MIN_MAX_MEMBERS
import dream11_scoring
from dream11_scoring import (
    calculate_dream11_points,
    finalize_dream11_contest,
    find_contests_needing_finalization,
    score_dream11_contest,
)
import live_poll

client = TestClient(fastapi_app)


@pytest.fixture
def make_user(make_user, engine):
    # Creation delegated to conftest's make_user (same name, received as an
    # argument -- pytest resolves it to the parent fixture). The prefix is
    # passed so the rows this file creates are still named as they were.
    # The teardown below stays here because it is specific to this file.
    factory = make_user

    def _make():
        return factory("d11")

    yield _make

    with engine.begin() as conn:
        for uid in factory.created:
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})  # cascades everything dream11


DEFAULT_SIDE_POSITIONS = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3  # 15/side, 30 total


def _seed_prior_points(engine, internal_id, gameweek, total_points):
    """One completed-gameweek row, so the player's mean-of-last-5
    total_points -- what dream11.py prices on -- is exactly total_points."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ml.player_gw_stats (player_id, season, gameweek, minutes, goals_scored, "
                "assists, clean_sheets, saves, bonus, bps, ict_index, expected_goals, expected_assists, "
                "expected_goal_involvements, total_points, value, selected, was_home) "
                "VALUES (:pid, :s, :gw, 90, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, :pts, 50, 1000, TRUE) "
                "ON CONFLICT DO NOTHING"
            ),
            {"pid": internal_id, "s": TEST_SEASON, "gw": gameweek, "pts": total_points},
        )


def _seed_fixture_and_pool(
    engine, make_team, make_player, fpl_id_base, gameweek=1, kickoff_time=None,
    home_positions=None, away_positions=None, rolling_points=None,
):
    """Seeds a fixture + a full player pool for both teams.
    rolling_points: optional {fpl_id: value} giving specific pool players a
    single ml.player_gw_stats row at (gameweek - 1) worth that many
    total_points -- with one prior gameweek, the mean-of-last-5 that
    dream11.py prices on is exactly that value. Players not in this dict get
    no row at all, i.e. no rolling history (priced at the floor).
    Seeds player_gw_stats, NOT ml.player_gw_features: nothing in the
    codebase writes that table, and dream11.py no longer reads it.
    Returns (fixture_id, home_team_id, away_team_id, pool) where pool is
    [{"fpl_id", "internal_id", "position", "team"}, ...].
    """
    home_positions = home_positions if home_positions is not None else DEFAULT_SIDE_POSITIONS
    away_positions = away_positions if away_positions is not None else DEFAULT_SIDE_POSITIONS

    home_team_id = make_team(fpl_id=fpl_id_base + 8000, name=f"Home{fpl_id_base}", short_name=f"H{fpl_id_base}")
    away_team_id = make_team(fpl_id=fpl_id_base + 8001, name=f"Away{fpl_id_base}", short_name=f"A{fpl_id_base}")

    with engine.begin() as conn:
        fixture_id = conn.execute(
            text(
                "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, away_team_id, kickoff_time, finished) "
                "VALUES (:fpl_id, :season, :gw, :home, :away, :ko, FALSE) RETURNING id"
            ),
            {"fpl_id": fpl_id_base, "season": TEST_SEASON, "gw": gameweek, "home": home_team_id, "away": away_team_id, "ko": kickoff_time},
        ).scalar()

    pool = []
    fid = fpl_id_base + 1
    for pos in home_positions:
        internal_id = make_player(fpl_id=fid, position=pos, team_id=home_team_id, cost_start=0)
        pool.append({"fpl_id": fid, "internal_id": internal_id, "position": pos, "team": "home"})
        fid += 1
    for pos in away_positions:
        internal_id = make_player(fpl_id=fid, position=pos, team_id=away_team_id, cost_start=0)
        pool.append({"fpl_id": fid, "internal_id": internal_id, "position": pos, "team": "away"})
        fid += 1

    if rolling_points:
        for p in pool:
            if p["fpl_id"] in rolling_points:
                _seed_prior_points(engine, p["internal_id"], gameweek - 1, rolling_points[p["fpl_id"]])

    return fixture_id, home_team_id, away_team_id, pool


def _pick_valid_team(pool):
    """1 GK + 4 DEF + 4 MID + 2 FWD = 11, within Dream11's DEF/MID 3-5, FWD 1-3.

    Alternates home/away within each position so the result also satisfies
    MAX_PLAYERS_PER_CLUB. Taking the first N of each position instead drew
    every pick from the home side (the pool is seeded home-first), which was a
    legal team until the per-club cap landed and an 11-from-one-club 422 after.
    The split lands at 6 home / 5 away, comfortably inside the cap of 7.
    """
    def take(position, count):
        home = [p["fpl_id"] for p in pool if p["position"] == position and p["team"] == "home"]
        away = [p["fpl_id"] for p in pool if p["position"] == position and p["team"] == "away"]
        picked = []
        # Interleave, then fall back to whichever side still has players so
        # tests that seed lopsided squads still get their requested count.
        while len(picked) < count and (home or away):
            for side in (home, away):
                if side and len(picked) < count:
                    picked.append(side.pop(0))
        return picked

    return take("GK", 1) + take("DEF", 4) + take("MID", 4) + take("FWD", 2)


def _auth_headers(user_id):
    """EVERY dream11 endpoint is behind a bearer token (the dependency is on
    the router itself), so every helper below sends one. The make_user
    fixture inserts rows directly (their password_hash is placeholder text,
    so /auth/login can't work for them), so mint the token the same way
    /auth/login would.

    user_id here means "who is calling", not "whose data" -- since the auth
    fix, that is the only way any of these endpoints can be told which user
    is acting.
    """
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _create_contest(fixture_id, user_id, name="Test Contest", max_members=50):
    resp = client.post(
        "/dream11/contests",
        json={"fixture_id": fixture_id, "name": name, "max_members": max_members},
        headers=_auth_headers(user_id),
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _join_contest(user_id, code):
    return client.post(
        "/dream11/contests/join", json={"code": code}, headers=_auth_headers(user_id)
    )


def _submit_team(contest_id, user_id, player_ids, captain_id, vice_captain_id):
    return client.post(
        f"/dream11/contests/{contest_id}/team",
        json={"player_ids": player_ids, "captain_id": captain_id, "vice_captain_id": vice_captain_id},
        headers=_auth_headers(user_id),
    )


def _get_team(contest_id, target_user_id, as_user_id=None):
    """as_user_id defaults to the team's owner -- i.e. reading your own."""
    viewer = as_user_id if as_user_id is not None else target_user_id
    return client.get(
        f"/dream11/contests/{contest_id}/team",
        params={"user_id": target_user_id},
        headers=_auth_headers(viewer),
    )


def _contest_prices(engine, contest_id):
    with engine.connect() as conn:
        return {r.player_id: float(r.credit_price) for r in conn.execute(
            text("SELECT player_id, credit_price FROM dream11.player_prices WHERE contest_id = :cid"), {"cid": contest_id}
        )}


def _lock_contest(engine, contest_id):
    with engine.begin() as conn:
        conn.execute(text("UPDATE dream11.contests SET is_locked = TRUE WHERE id = :cid"), {"cid": contest_id})


def _finish_fixture(engine, fixture_id):
    """The real signal finalization gates on. enforce_fixture_identity only
    blocks the identity columns, so flipping `finished` is a plain UPDATE."""
    with engine.begin() as conn:
        conn.execute(text("UPDATE ml.fixtures SET finished = TRUE WHERE id = :fid"), {"fid": fixture_id})


def _contest_finalized_at(engine, contest_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT finalized_at FROM dream11.contests WHERE id = :cid"), {"cid": contest_id}
        ).scalar()


def _member_points(engine, contest_id, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :c AND user_id = :u"),
            {"c": contest_id, "u": user_id},
        ).scalar()


# ---------------------------------------------------------------- read endpoints

def test_get_contest_returns_fixture_and_membership_context(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6000)
    contest = _create_contest(fixture_id, creator, name="Readable")

    resp = client.get(
        f"/dream11/contests/{contest['contest_id']}", headers=_auth_headers(creator)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["name"] == "Readable"
    assert body["fixture_id"] == fixture_id
    assert body["member_count"] == 1
    assert body["is_locked"] is False
    assert body["user_is_member"] is True
    assert body["user_has_team"] is False
    assert body["budget_cap"] == 100.0
    assert body["team_size"] == 11
    assert body["home_team"] and body["away_team"]


def test_get_contest_user_fields_are_empty_for_a_non_member(engine, make_user, make_team, make_player):
    """The pre-join case: a caller who hasn't joined still gets the contest
    row back, with the user_* fields at their defaults rather than the row
    disappearing -- that is what the LEFT JOIN in _CONTEST_SUMMARY_SELECT is
    for, and it is what the "look at this contest before joining" screen
    needs.

    These fields describe the CALLER now. They used to be driven by an
    optional user_id query param, so this same assertion could be had by
    simply omitting it -- which made the test agree with the code without
    saying anything about who the flags belonged to."""
    creator = make_user()
    outsider = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 6050)
    contest = _create_contest(fixture_id, creator)

    resp = client.get(
        f"/dream11/contests/{contest['contest_id']}", headers=_auth_headers(outsider)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["member_count"] == 1  # still the real count
    assert body["user_is_member"] is False
    assert body["user_has_team"] is False
    assert body["user_total_points"] == 0


def test_get_contest_user_fields_follow_the_token_not_a_parameter(engine, make_user, make_team, make_player):
    """The same contest, read by a member and by an outsider, differs only in
    the user_* block -- and a user_id query param naming the member cannot
    move it. Pins that the flags are the caller's, which is the whole point
    of folding the parameter away."""
    creator = make_user()
    outsider = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 6060)
    cid = _create_contest(fixture_id, creator)["contest_id"]

    as_member = client.get(f"/dream11/contests/{cid}", headers=_auth_headers(creator)).json()
    as_outsider = client.get(
        f"/dream11/contests/{cid}",
        params={"user_id": creator},          # <- names the member on purpose
        headers=_auth_headers(outsider),      # <- but the token is the outsider
    ).json()

    assert as_member["user_is_member"] is True
    assert as_outsider["user_is_member"] is False, "the stale parameter must be inert"
    # Everything not describing the caller is identical between the two.
    assert as_member["contest_id"] == as_outsider["contest_id"]
    assert as_member["member_count"] == as_outsider["member_count"] == 1


def test_get_contest_unknown_id_404(engine, make_user):
    resp = client.get("/dream11/contests/99999999", headers=_auth_headers(make_user()))
    assert resp.status_code == 404


def test_get_user_contests_lists_only_contests_the_user_joined(engine, make_user, make_team, make_player):
    """Scoped by the TOKEN, not by a parameter -- there is no longer a user_id
    query param here to enumerate someone else's contests (and their join
    codes) with."""
    member = make_user()
    outsider = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 6100)
    contest = _create_contest(fixture_id, member)

    mine = client.get("/dream11/contests", headers=_auth_headers(member))
    theirs = client.get("/dream11/contests", headers=_auth_headers(outsider))

    assert mine.status_code == 200
    assert contest["contest_id"] in [c["contest_id"] for c in mine.json()]
    assert contest["contest_id"] not in [c["contest_id"] for c in theirs.json()]

    # OLD BEHAVIOUR: user_id was the identity, so this returned the member's
    # contests -- codes included -- to anyone who guessed an id. Now the
    # parameter is inert.
    spoofed = client.get(
        "/dream11/contests", params={"user_id": member}, headers=_auth_headers(outsider)
    )
    assert spoofed.status_code == 200
    assert contest["contest_id"] not in [c["contest_id"] for c in spoofed.json()]


def test_get_contest_pool_returns_every_priced_player(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6200)
    contest = _create_contest(fixture_id, creator)

    resp = client.get(
        f"/dream11/contests/{contest['contest_id']}/players", headers=_auth_headers(creator)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == len(pool) == 30
    assert {p["player_id"] for p in body} == {p["fpl_id"] for p in pool}
    # fpl_ids, not internal ml.players ids -- the whole-API convention
    assert all(p["credit_price"] >= 6.0 for p in body)
    assert {p["is_home_team"] for p in body} == {True, False}
    prices = [p["credit_price"] for p in body]
    assert prices == sorted(prices, reverse=True)


def test_get_contest_pool_unknown_contest_404(engine, make_user):
    resp = client.get("/dream11/contests/99999999/players", headers=_auth_headers(make_user()))
    assert resp.status_code == 404


def test_get_leaderboard_orders_scored_members_before_unscored(engine, make_user, make_team, make_player):
    creator = make_user()
    joiner = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6300)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    _join_contest(joiner, contest["code"])

    # joiner has a real rank, creator is still rank 0 (never scored)
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dream11.contest_members SET total_points = 42, rank = 1 WHERE contest_id = :c AND user_id = :u"),
            {"c": cid, "u": joiner},
        )

    resp = client.get(f"/dream11/contests/{cid}/leaderboard", headers=_auth_headers(creator))

    assert resp.status_code == 200
    body = resp.json()
    assert body["contest"]["contest_id"] == cid
    assert [r["user_id"] for r in body["rows"]] == [joiner, creator]
    assert body["rows"][0]["total_points"] == 42
    assert body["rows"][0]["has_submitted_team"] is False


def test_get_user_team_returns_players_with_live_points(engine, make_user, make_team, make_player, make_gw_stat):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6400, gameweek=2)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    team = _pick_valid_team(pool)
    # _pick_valid_team is ordered GK, 4x DEF, 4x MID, 2x FWD -- so team[5] is
    # a MID, deliberately: a MID takes no goals-conceded penalty and gets no
    # clean sheet with goals against, leaving the goal as the only scoring
    # event in play and the arithmetic below unambiguous.
    captain, vice = team[5], team[6]
    assert next(p["position"] for p in pool if p["fpl_id"] == captain) == "MID"
    resp = _submit_team(cid, creator, team, captain_id=captain, vice_captain_id=vice)
    assert resp.status_code == 200, resp.json()

    # Captain scores a goal in the contest's own gameweek; everyone else blank.
    captain_internal = next(p["internal_id"] for p in pool if p["fpl_id"] == captain)
    make_gw_stat(captain_internal, gameweek=2, fixture_id=fixture_id, goals_scored=1, goals_conceded=3)

    body = _get_team(cid, creator).json()

    assert body["team_id"] > 0
    assert len(body["players"]) == 11
    assert {p["player_id"] for p in body["players"]} == set(team)
    assert sum(p["is_captain"] for p in body["players"]) == 1
    assert sum(p["is_vice_captain"] for p in body["players"]) == 1

    MID_GOAL_POINTS = 5
    captain_row = next(p for p in body["players"] if p["player_id"] == captain)
    assert captain_row["points"] == MID_GOAL_POINTS  # per-player points are PRE-multiplier
    # ...and the captain's 2x shows up as an additive bonus on the total
    assert body["captain_bonus"] == MID_GOAL_POINTS * 1.0
    assert body["vice_captain_bonus"] == 0.0  # vice was blank, so their 1.5x adds nothing
    assert body["live_total_points"] == MID_GOAL_POINTS * 2


def test_get_user_team_before_kickoff_is_all_zeroes_not_an_error(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6500)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    team = _pick_valid_team(pool)
    _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1])

    body = _get_team(cid, creator).json()

    assert body["live_total_points"] == 0
    assert all(p["points"] == 0 and p["minutes"] == 0 for p in body["players"])
    assert body["contest_total_points"] == 0
    assert body["contest_rank"] == 0


def test_get_user_team_when_none_submitted_404(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 6600)
    contest = _create_contest(fixture_id, creator)

    resp = _get_team(contest["contest_id"], creator)

    assert resp.status_code == 404


def _seed_contest_with_two_teams(engine, make_user, make_team, make_player, fpl_id_base):
    """A locked-capable contest where both members have submitted."""
    owner = make_user()
    rival = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, fpl_id_base)
    contest = _create_contest(fixture_id, owner)
    cid = contest["contest_id"]
    _join_contest(rival, contest["code"])
    team = _pick_valid_team(pool)
    _submit_team(cid, owner, team, captain_id=team[0], vice_captain_id=team[1])
    _submit_team(cid, rival, team, captain_id=team[1], vice_captain_id=team[0])
    return cid, owner, rival


def test_get_user_team_requires_authentication(engine, make_user, make_team, make_player):
    cid, owner, _rival = _seed_contest_with_two_teams(engine, make_user, make_team, make_player, 6750)

    # No Authorization header at all -- the pre-fix behaviour returned 200.
    resp = client.get(f"/dream11/contests/{cid}/team", params={"user_id": owner})

    assert resp.status_code == 401


def test_get_user_team_rejects_opponents_team_before_lock(engine, make_user, make_team, make_player):
    """The whole point of the gate: pre-kickoff, a rival's XI and captain is
    exactly the information that decides the contest."""
    cid, owner, rival = _seed_contest_with_two_teams(engine, make_user, make_team, make_player, 6800)

    resp = _get_team(cid, target_user_id=owner, as_user_id=rival)

    assert resp.status_code == 403
    assert "hidden until the contest locks" in resp.json()["detail"]


def test_get_user_team_allows_own_team_before_lock(engine, make_user, make_team, make_player):
    cid, owner, _rival = _seed_contest_with_two_teams(engine, make_user, make_team, make_player, 6850)

    resp = _get_team(cid, target_user_id=owner, as_user_id=owner)

    assert resp.status_code == 200
    assert len(resp.json()["players"]) == 11


def test_get_user_team_allows_opponents_team_after_lock(engine, make_user, make_team, make_player):
    cid, owner, rival = _seed_contest_with_two_teams(engine, make_user, make_team, make_player, 6900)

    _lock_contest(engine, cid)

    resp = _get_team(cid, target_user_id=owner, as_user_id=rival)

    assert resp.status_code == 200
    assert len(resp.json()["players"]) == 11


def test_get_user_team_non_member_blocked_even_after_lock(engine, make_user, make_team, make_player):
    """Locking makes picks final, not public -- an outsider still has no
    business reading a team out of a contest they never joined."""
    cid, owner, _rival = _seed_contest_with_two_teams(engine, make_user, make_team, make_player, 6950)
    outsider = make_user()

    _lock_contest(engine, cid)

    resp = _get_team(cid, target_user_id=owner, as_user_id=outsider)

    assert resp.status_code == 403
    assert "only contest members" in resp.json()["detail"]


def test_get_user_team_cannot_be_spoofed_by_claiming_another_user_id(engine, make_user, make_team, make_player):
    """The reason this endpoint authenticates instead of trusting the
    user_id parameter: identity comes from the token, so passing the
    victim's id as your own gains a snooper nothing."""
    cid, owner, rival = _seed_contest_with_two_teams(engine, make_user, make_team, make_player, 6980)

    # rival's token, but asking as though they were the owner
    resp = client.get(
        f"/dream11/contests/{cid}/team",
        params={"user_id": owner},
        headers=_auth_headers(rival),
    )

    assert resp.status_code == 403


def test_get_contest_reflects_user_has_team_after_submitting(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6700)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    team = _pick_valid_team(pool)
    _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1])

    body = client.get(f"/dream11/contests/{cid}", headers=_auth_headers(creator)).json()

    assert body["user_has_team"] is True


# ---------------------------------------------------------------- contest creation

def test_create_contest_correct_pool_size_and_creator_auto_joined(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 1000)

    contest = _create_contest(fixture_id, creator)

    assert contest["pool_size"] == len(pool) == 30
    with engine.connect() as conn:
        member = conn.execute(
            text("SELECT total_points, rank FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
            {"cid": contest["contest_id"], "uid": creator},
        ).first()
    assert member is not None
    assert member.total_points == 0
    assert member.rank == 0


def test_create_contest_prices_locked_and_in_range(engine, make_user, make_team, make_player):
    creator = make_user()
    fpl_id_base = 1100
    # fpl_ids are assigned sequentially starting at fpl_id_base + 1, in pool
    # order (home then away) -- predicted here rather than referencing the
    # pool's own return value, since that doesn't exist yet while still
    # building this same call's rolling_points argument.
    rolling_points = {fpl_id_base + 1 + i: i * 10 for i in range(30)}  # 0, 10, 20, ..., 290 -- a real spread
    fixture_id, *_rest, pool = _seed_fixture_and_pool(
        engine, make_team, make_player, fpl_id_base, rolling_points=rolling_points
    )

    contest = _create_contest(fixture_id, creator)

    prices = _contest_prices(engine, contest["contest_id"])
    assert len(prices) == len(pool)  # every pool player priced and locked
    assert all(6.0 <= v <= 11.0 for v in prices.values())
    assert min(prices.values()) == 6.0  # lowest rolling value
    assert max(prices.values()) == 11.0  # highest rolling value


def test_create_contest_nan_history_player_floored_to_6(engine, make_user, make_team, make_player):
    creator = make_user()
    fpl_id_base = 1200
    # Everyone EXCEPT the very first pool slot (fpl_id_base + 1) has data.
    rolling_points = {fpl_id_base + 1 + i: i * 10 for i in range(30) if i != 0}
    fixture_id, *_rest, pool = _seed_fixture_and_pool(
        engine, make_team, make_player, fpl_id_base, rolling_points=rolling_points
    )
    no_history_player = pool[0]
    assert no_history_player["fpl_id"] == fpl_id_base + 1  # sanity: this really is the excluded slot

    contest = _create_contest(fixture_id, creator)

    prices = _contest_prices(engine, contest["contest_id"])
    assert prices[no_history_player["internal_id"]] == 6.0


def test_create_contest_all_identical_or_empty_floors_everyone(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 1300)  # no rolling_points at all

    contest = _create_contest(fixture_id, creator)

    prices = _contest_prices(engine, contest["contest_id"])
    assert all(v == 6.0 for v in prices.values())


# ---------------------------------------------------------------- join

def test_join_valid_code_succeeds(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 2000)
    contest = _create_contest(fixture_id, creator)
    joiner = make_user()

    resp = _join_contest(joiner, contest["code"])
    assert resp.status_code == 200
    assert resp.json()["contest_id"] == contest["contest_id"]


def test_join_invalid_code_rejected(make_user):
    joiner = make_user()
    resp = _join_contest(joiner, "NOTREAL")
    assert resp.status_code == 422
    assert "code does not match any existing contest" in resp.json()["detail"]


def test_join_already_member_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 2100)
    contest = _create_contest(fixture_id, creator)

    resp = _join_contest(creator, contest["code"])  # already a member (auto-joined at creation)
    assert resp.status_code == 422
    assert "user is already a member of this contest" in resp.json()["detail"]


def test_join_at_capacity_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 2200)
    # 2 is the floor now, so the creator (auto-joined) plus one joiner fills it.
    contest = _create_contest(fixture_id, creator, max_members=MIN_MAX_MEMBERS)
    assert _join_contest(make_user(), contest["code"]).status_code == 200

    resp = _join_contest(make_user(), contest["code"])
    assert resp.status_code == 422
    assert any("at capacity" in e for e in resp.json()["detail"])


def test_create_contest_max_members_out_of_range_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 2250)

    for bad in (0, 1, MAX_MAX_MEMBERS + 1):
        resp = client.post(
            "/dream11/contests",
            json={"fixture_id": fixture_id, "name": "Bad", "max_members": bad},
            headers=_auth_headers(creator),
        )
        assert resp.status_code == 422, (bad, resp.json())
        assert any("max_members must be between" in e for e in resp.json()["detail"]), bad

    # ...and both ends of the range are accepted.
    for good in (MIN_MAX_MEMBERS, MAX_MAX_MEMBERS):
        assert _create_contest(fixture_id, creator, max_members=good)["max_members"] == good


def test_contest_summary_carries_the_scoring_rules(engine, make_user, make_team, make_player):
    """The match-detail rule strip reads "11 players · 100 credits · Captain 2x,
    Vice 1.5x" -- all four come from the API so they can't drift from
    dream11_scoring.py."""
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 2280)
    contest = _create_contest(fixture_id, creator)

    body = client.get(
        f"/dream11/contests/{contest['contest_id']}", headers=_auth_headers(creator)
    ).json()

    assert body["team_size"] == 11
    assert body["budget_cap"] == 100.0
    assert body["captain_multiplier"] == 2.0
    assert body["vice_captain_multiplier"] == 1.5


def test_join_locked_contest_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 2300)
    contest = _create_contest(fixture_id, creator)
    _lock_contest(engine, contest["contest_id"])
    joiner = make_user()

    resp = _join_contest(joiner, contest["code"])
    assert resp.status_code == 422
    assert any("locked" in e for e in resp.json()["detail"])


# ---------------------------------------------------------------- team submission

def test_submit_team_valid_succeeds(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3000)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 200, resp.json()
    body = resp.json()
    assert {p["player_id"] for p in body["players"]} == set(team)
    assert sum(1 for p in body["players"] if p["is_captain"]) == 1
    assert sum(1 for p in body["players"] if p["is_vice_captain"]) == 1

    with engine.connect() as conn:
        rows = list(conn.execute(
            text(
                "SELECT tp.player_id, tp.is_captain, tp.is_vice_captain FROM dream11.team_players tp "
                "JOIN dream11.teams t ON t.id = tp.team_id WHERE t.contest_id = :cid AND t.user_id = :uid"
            ),
            {"cid": contest["contest_id"], "uid": creator},
        ))
    assert len(rows) == 11


def test_submit_team_wrong_count_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3100)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)[:10]  # 10, not 11

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    assert "team must contain exactly 11 players, got 10" in resp.json()["detail"]


def test_submit_team_duplicate_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3200)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)
    team_with_dup = team[:-1] + [team[0]]

    resp = _submit_team(contest["contest_id"], creator, team_with_dup, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    assert any("duplicate player_id" in e for e in resp.json()["detail"])


def test_submit_team_unknown_player_rejected(engine, make_user, make_team, make_player):
    """Distinct from 'not in pool': this fpl_id resolves to NO real player at all."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3300)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)
    team[-1] = 999999  # never seeded into ml.players at all

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("not found in ml.players" in e and "999999" in e for e in errors)
    assert not any("not part of this contest's player pool" in e for e in errors)


def test_submit_team_player_not_in_pool_rejected(engine, make_user, make_team, make_player):
    """Distinct from 'unknown player': this IS a real player, just not on
    either of this fixture's two teams."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3400)
    contest = _create_contest(fixture_id, creator)

    third_team_id = make_team(fpl_id=93400, name="ThirdTeam", short_name="TT")
    outsider_internal_id = make_player(fpl_id=93401, position="MID", team_id=third_team_id, cost_start=0)

    team = _pick_valid_team(pool)
    team[-1] = 93401  # a real player, just not in this fixture's pool

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("not part of this contest's player pool" in e and "93401" in e for e in errors)
    assert not any("not found in ml.players" in e for e in errors)


def test_submit_team_formation_boundaries(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3500)
    contest = _create_contest(fixture_id, creator)

    gks = [p["fpl_id"] for p in pool if p["position"] == "GK"]
    defs = [p["fpl_id"] for p in pool if p["position"] == "DEF"]
    mids = [p["fpl_id"] for p in pool if p["position"] == "MID"]
    fwds = [p["fpl_id"] for p in pool if p["position"] == "FWD"]

    cases = [
        (gks[:2] + defs[:3] + mids[:4] + fwds[:2], "expected exactly 1 GK, got 2"),
        (gks[:1] + defs[:2] + mids[:5] + fwds[:3], "DEF count must be between 3 and 5, got 2"),
        (gks[:1] + defs[:6] + mids[:3] + fwds[:1], "DEF count must be between 3 and 5, got 6"),
        (gks[:1] + defs[:5] + mids[:2] + fwds[:3], "MID count must be between 3 and 5, got 2"),
        (gks[:1] + defs[:3] + mids[:6] + fwds[:1], "MID count must be between 3 and 5, got 6"),
        (gks[:1] + defs[:5] + mids[:5] + fwds[:0], "FWD count must be between 1 and 3, got 0"),
        (gks[:1] + defs[:3] + mids[:3] + fwds[:4], "FWD count must be between 1 and 3, got 4"),
    ]

    for team, expected_error in cases:
        assert len(team) == 11, f"test setup bug: {team} is not 11 players"
        resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
        assert resp.status_code == 422, f"expected rejection for {expected_error}"
        assert expected_error in resp.json()["detail"], resp.json()["detail"]


def test_submit_team_over_budget_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    # Pick 11 specific players to be priced at the max (11.0), plus one
    # low-priced outlier elsewhere in the pool to establish a real range
    # (avoiding the "everyone identical -> floor to 6.0" rule).
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3600)
    team = _pick_valid_team(pool)
    rolling_points = {fid: 100 for fid in team}
    rolling_points[pool[-1]["fpl_id"]] = 0  # a real low anchor, not part of the chosen 11

    # Re-seed rolling points for this specific fixture (helper already ran
    # without any -- insert directly here instead of re-seeding the whole pool).
    for p in pool:
        if p["fpl_id"] in rolling_points:
            _seed_prior_points(engine, p["internal_id"], 0, rolling_points[p["fpl_id"]])

    contest = _create_contest(fixture_id, creator)
    prices = _contest_prices(engine, contest["contest_id"])
    team_internal_ids = [p["internal_id"] for p in pool if p["fpl_id"] in team]
    assert all(prices[iid] == 11.0 for iid in team_internal_ids)  # sanity: all 11 really did price at the ceiling

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    errors = resp.json()["detail"]
    assert any("exceeds budget cap" in e for e in errors)


def test_submit_team_captain_eq_vice_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3700)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[0])

    assert resp.status_code == 422
    assert "captain_id and vice_captain_id must be different players" in resp.json()["detail"]


def test_submit_team_captain_not_in_11_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3800)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=424242, vice_captain_id=team[1])

    assert resp.status_code == 422
    assert "captain_id 424242 is not in the submitted team" in resp.json()["detail"]


def test_submit_team_vice_not_in_11_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 3900)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=434343)

    assert resp.status_code == 422
    assert "vice_captain_id 434343 is not in the submitted team" in resp.json()["detail"]


def test_submit_team_without_joining_first_rejected(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 4000)
    contest = _create_contest(fixture_id, creator)
    non_member = make_user()
    team = _pick_valid_team(pool)

    resp = _submit_team(contest["contest_id"], non_member, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    assert "user must join this contest before submitting a team" in resp.json()["detail"]


def test_submit_team_locked_contest_rejected_clean_422(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 4100)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)

    _lock_contest(engine, contest["contest_id"])

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert resp.status_code == 422
    assert resp.json()["detail"] == "Contest is locked, team can no longer be submitted"


def test_submit_team_twice_rejected_clean_422_not_500(engine, make_user, make_team, make_player):
    """uq_d11_teams_contest_user is a user-facing rule (one team per contest,
    no edit path), so a second submission must read as a validation failure --
    it previously fell through to a generic 500."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 4150)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)

    first = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert first.status_code == 200, first.json()

    second = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])

    assert second.status_code == 422, second.json()
    assert "user has already submitted a team for this contest" in second.json()["detail"]


# ---------------------------------------------------------------- calculate_dream11_points

def _stats(**overrides):
    # goals_conceded defaults to 1, NOT 0 -- with the also-defaulted
    # minutes=90, a default of 0 would silently satisfy the clean-sheet
    # condition (goals_conceded==0 and minutes>=54) and add an unintended
    # clean-sheet bonus to every test that doesn't explicitly test clean
    # sheets. 1 conceded is neutral: breaks the clean-sheet condition
    # without tripping the (>=2-conceded) GK/DEF penalty either.
    base = dict(
        goals_scored=0, assists=0, goals_conceded=1, minutes=90, saves=0,
        yellow_cards=0, red_cards=0, own_goals=0, penalties_saved=0, penalties_missed=0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_goal_points_by_position():
    assert calculate_dream11_points(_stats(goals_scored=1), "GK") == 10
    assert calculate_dream11_points(_stats(goals_scored=1), "DEF") == 6
    assert calculate_dream11_points(_stats(goals_scored=1), "MID") == 5
    assert calculate_dream11_points(_stats(goals_scored=1), "FWD") == 4


def test_assist_points_are_flat_across_every_position():
    """20 per assist, position-independent -- unlike goals, which ARE
    position-weighted (see test_goal_points_by_position above). Checked on
    all four positions for the same reason that test does: the flatness is
    the rule, so it has to be asserted rather than sampled."""
    for position in ("GK", "DEF", "MID", "FWD"):
        assert calculate_dream11_points(_stats(assists=1), position) == 20, position


def test_assist_points_scale_linearly():
    assert calculate_dream11_points(_stats(assists=2), "FWD") == 40
    assert calculate_dream11_points(_stats(assists=3), "MID") == 60


def test_clean_sheet_points_by_position():
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=90), "GK") == 4
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=90), "DEF") == 4
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=90), "MID") == 1
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=90), "FWD") == 0


def test_clean_sheet_at_exactly_54_minutes_counts():
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=54), "DEF") == 4


def test_clean_sheet_at_53_minutes_does_not_count():
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=53), "DEF") == 0


def test_clean_sheet_requires_actually_playing():
    """goals_conceded=0 but minutes=0 (didn't play) must NOT get clean sheet points."""
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=0), "DEF") == 0
    assert calculate_dream11_points(_stats(goals_conceded=0, minutes=0), "GK") == 0


def test_goals_conceded_penalty_gk_def_only():
    assert calculate_dream11_points(_stats(goals_conceded=2, minutes=90), "GK") == -1
    assert calculate_dream11_points(_stats(goals_conceded=2, minutes=90), "DEF") == -1
    assert calculate_dream11_points(_stats(goals_conceded=2, minutes=90), "MID") == 0  # no penalty for MID/FWD
    assert calculate_dream11_points(_stats(goals_conceded=4, minutes=90), "GK") == -2  # 4 // 2 = 2 penalties


def test_saves_points_gk_only():
    # goals_conceded=1 (not 0/2) isolates saves from both the clean-sheet
    # and the conceded-penalty terms.
    assert calculate_dream11_points(_stats(saves=3, goals_conceded=1, minutes=90), "GK") == 1
    assert calculate_dream11_points(_stats(saves=3, goals_conceded=1, minutes=90), "DEF") == 0
    assert calculate_dream11_points(_stats(saves=6, goals_conceded=1, minutes=90), "GK") == 2  # 6 // 3 = 2


def test_card_points():
    assert calculate_dream11_points(_stats(yellow_cards=1), "MID") == -1
    assert calculate_dream11_points(_stats(red_cards=1), "MID") == -3
    assert calculate_dream11_points(_stats(yellow_cards=1, red_cards=1), "MID") == -4


def test_own_goal_and_penalty_points():
    assert calculate_dream11_points(_stats(own_goals=1), "DEF") == -2
    assert calculate_dream11_points(_stats(penalties_saved=1), "GK") == 5
    assert calculate_dream11_points(_stats(penalties_missed=1), "FWD") == -2


# ---------------------------------------------------------------- score_dream11_contest

def _seed_gw_stat_internal(engine, internal_id, gameweek, fixture_id, **overrides):
    # fixture_id is REQUIRED, not optional, and that is the point: Dream11's
    # scoring joins are scoped to the contest's own fixture, so a row seeded
    # without one matches nothing and every player silently scores 0. Making
    # it positional means a test cannot forget it. It also matches what
    # actually writes these rows in production -- live_poll.py polls one
    # fixture at a time and always stamps its id.
    # goals_conceded defaults to 1, not 0 -- same clean-sheet-contamination
    # reasoning as _stats() above; callers testing clean sheets explicitly
    # override it.
    row = dict(
        minutes=0, goals_scored=0, assists=0, clean_sheets=0, goals_conceded=1, saves=0,
        bonus=0, bps=0, yellow_cards=0, red_cards=0, own_goals=0,
        penalties_saved=0, penalties_missed=0, value=50, total_points=0,
    )
    row.update(overrides)
    cols = ", ".join(row.keys())
    placeholders = ", ".join(f":{k}" for k in row.keys())
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO ml.player_gw_stats (player_id, season, gameweek, fixture_id, {cols}) "
                f"VALUES (:pid, :s, :gw, :fid, {placeholders})"
            ),
            {"pid": internal_id, "s": TEST_SEASON, "gw": gameweek, "fid": fixture_id, **row},
        )


def test_score_dream11_contest_captain_vice_math(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5000, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)
    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert resp.status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    # Assists only, at 20 each. goals_conceded defaults to 1, so nobody
    # takes a clean sheet and nobody takes the GK/DEF concession penalty
    # either (1 // 2 == 0) -- assists are the whole score.
    # captain: 2 assists = 40 pts. vice: 4 assists = 80 pts. other 9: 1 assist each = 20 pts.
    _seed_gw_stat_internal(engine, internal_by_fpl[team[0]], 1, fixture_id, minutes=90, assists=2, total_points=6)
    _seed_gw_stat_internal(engine, internal_by_fpl[team[1]], 1, fixture_id, minutes=90, assists=4, total_points=12)
    for fid in team[2:]:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, minutes=90, assists=1, total_points=3)

    summary = score_dream11_contest(engine, contest["contest_id"])
    assert creator in summary["scored"]
    assert summary["failed"] == []

    # raw = 40 + 80 + 9*20 = 300. captain_bonus = 40*1.0 = 40.0.
    # vice_bonus = 80*0.5 = 40.0. final = 300 + 40 + 40 = 380.
    with engine.connect() as conn:
        member = conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
            {"cid": contest["contest_id"], "uid": creator},
        ).first()
    assert member.total_points == 380


def test_score_dream11_contest_non_playing_player_contributes_zero(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5100, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)
    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert resp.status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    # Only give stats to 10 of the 11 -- the 11th has no ml.player_gw_stats row at all.
    for fid in team[:-1]:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, minutes=90, assists=1, total_points=3)

    summary = score_dream11_contest(engine, contest["contest_id"])  # must not raise
    assert summary["failed"] == []
    assert creator in summary["scored"]


def test_score_dream11_contest_idempotent(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5200, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)
    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert resp.status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    for fid in team:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, minutes=90, goals_scored=1, total_points=4)

    score_dream11_contest(engine, contest["contest_id"])
    with engine.connect() as conn:
        first = conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
            {"cid": contest["contest_id"], "uid": creator},
        ).scalar()

    score_dream11_contest(engine, contest["contest_id"])
    with engine.connect() as conn:
        second = conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
            {"cid": contest["contest_id"], "uid": creator},
        ).scalar()

    assert first == second


def test_score_dream11_contest_ranking_with_a_real_tie_and_a_non_tie(engine, make_user, make_team, make_player):
    """All three users submit the SAME 11 players (so raw_points is
    identical for all of them) but DIFFERENT captain picks -- since the
    captain bonus is the only thing that then differs, this produces a
    genuine tie and a genuine non-tie through real score_dream11_contest
    execution, no manual DB manipulation of the result."""
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5400, gameweek=1)
    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    team = _pick_valid_team(pool)

    creator = make_user()
    contest = _create_contest(fixture_id, creator)
    m2 = make_user()
    _join_contest(m2, contest["code"])
    m3 = make_user()
    _join_contest(m3, contest["code"])

    # creator and m2 both captain team[0] (10 pts) -- identical picks, so identical final_points.
    for uid in (creator, m2):
        resp = _submit_team(contest["contest_id"], uid, team, captain_id=team[0], vice_captain_id=team[2])
        assert resp.status_code == 200
    # m3 captains team[1] (5 pts) instead -- same raw_points, smaller bonus, lower final_points.
    resp = _submit_team(contest["contest_id"], m3, team, captain_id=team[1], vice_captain_id=team[2])
    assert resp.status_code == 200

    # Position-independent scores via assists only (ASSIST_POINTS=20
    # regardless of position): team[0]=4 assists=80pts, team[1]=2
    # assists=40pts, everyone else=0.
    _seed_gw_stat_internal(engine, internal_by_fpl[team[0]], 1, fixture_id, minutes=90, assists=4, total_points=12)
    _seed_gw_stat_internal(engine, internal_by_fpl[team[1]], 1, fixture_id, minutes=90, assists=2, total_points=6)
    for fid in team[2:]:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, minutes=90, total_points=0)

    summary = score_dream11_contest(engine, contest["contest_id"])
    assert summary["failed"] == []

    def _member(uid):
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT total_points, rank FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
                {"cid": contest["contest_id"], "uid": uid},
            ).first()

    # raw_points = 80 + 40 + 0*9 = 120 for everyone (same 11 players' stats).
    # creator/m2: captain=team[0](80) -> +80.0; vice=team[2](0) -> +0. final = 200.
    # m3: captain=team[1](40) -> +40.0; vice=team[2](0) -> +0. final = 160.
    creator_row, m2_row, m3_row = _member(creator), _member(m2), _member(m3)
    assert creator_row.total_points == 200
    assert m2_row.total_points == 200
    assert m3_row.total_points == 160

    assert creator_row.rank == 1
    assert m2_row.rank == 1
    assert m3_row.rank == 3  # ties for 1st skip rank 2


# ---------------------------------------------------------------- finalization
#
# A contest's result is frozen once its match is over: scored one last
# time, stamped with dream11.contests.finalized_at, and never recomputed
# again. These tests exist because the alternative was demonstrably wrong
# -- changing ASSIST_POINTS from 3 to 20 left already-scored contests with
# a stored total and a live recompute that disagreed, and the frontend
# shows the stored one on the leaderboard and the recomputed one on the
# team panel.
#
# The scoring-constant changes below are monkeypatched rather than
# hypothetical for exactly that reason: it is the real thing that broke.


def _finalized_contest(engine, make_user, make_team, make_player, fpl_id_base):
    """A contest whose match is over and whose result is frozen.
    Returns (contest_id, fixture_id, user_id, team, pool)."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, fpl_id_base, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    team = _pick_valid_team(pool)
    assert _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1]).status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    # One assist each -> every player is worth ASSIST_POINTS, so a change to
    # that constant moves the total by a lot and cannot be mistaken for
    # rounding. goals_conceded defaults to 1, so no clean sheets are in play.
    for fid in team:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, minutes=90, assists=1)

    _finish_fixture(engine, fixture_id)
    result = finalize_dream11_contest(engine, cid)
    assert result["finalized"] is True, result

    return cid, fixture_id, creator, team, pool


def test_a_finalized_contests_total_survives_a_scoring_constant_change(
    engine, make_user, make_team, make_player, monkeypatch
):
    """THE REGRESSION. 11 players x 1 assist x 20 = 220 raw, plus a 20
    captain bonus and a 10 vice bonus = 250. Change ASSIST_POINTS to 3 and
    re-run everything that could possibly rescore: the frozen total must
    not move by a single point."""
    cid, _fixture_id, creator, _team, _pool = _finalized_contest(
        engine, make_user, make_team, make_player, 5700
    )

    frozen = _member_points(engine, cid, creator)
    assert frozen == 250, "11 assists at 20 + captain 20 + vice 10"

    monkeypatch.setattr(dream11_scoring, "ASSIST_POINTS", 3)

    # Everything that writes a contest score, aimed straight at it.
    assert score_dream11_contest(engine, cid)["skipped_finalized"] is True
    assert finalize_dream11_contest(engine, cid)["reason"] == "already finalized"
    assert find_contests_needing_finalization(engine) == [] or cid not in find_contests_needing_finalization(engine)

    assert _member_points(engine, cid, creator) == frozen, (
        "a finalized contest was rescored under new constants"
    )


def test_a_live_contest_does_reflect_a_scoring_constant_change(
    engine, make_user, make_team, make_player, monkeypatch
):
    """The counterpart, and what makes the test above meaningful rather
    than tautological: before finalization the same contest DOES move when
    the constant changes. If this ever fails, the test above is passing for
    the wrong reason."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5710, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    team = _pick_valid_team(pool)
    assert _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1]).status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    for fid in team:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, minutes=90, assists=1)

    # Fixture NOT finished -> contest stays open.
    score_dream11_contest(engine, cid)
    assert _member_points(engine, cid, creator) == 250
    assert _contest_finalized_at(engine, cid) is None

    monkeypatch.setattr(dream11_scoring, "ASSIST_POINTS", 3)
    score_dream11_contest(engine, cid)

    # 11 x 3 = 33 raw, + captain 3 + vice 1.5 = 37.5 -> round() = 38.
    assert _member_points(engine, cid, creator) == 38, "an unfinalized contest must still rescore"


def test_get_user_team_on_a_finalized_contest_never_reads_player_gw_stats(
    engine, make_user, make_team, make_player
):
    """Proved by removing the evidence: once the contest is finalized, the
    underlying ml.player_gw_stats rows are DELETED outright. A live
    recompute would then score every player 0. The response must be
    unchanged, which is only possible if it is reading the stored
    per-player breakdown instead.

    Nothing blocks the delete -- the dream11 tables' triggers are BEFORE
    UPDATE / BEFORE INSERT only, as this file's own docstring notes."""
    cid, _fixture_id, creator, team, pool = _finalized_contest(
        engine, make_user, make_team, make_player, 5720
    )

    before = _get_team(cid, creator).json()
    assert before["is_finalized"] is True
    assert before["live_total_points"] == 250
    assert before["contest_total_points"] == 250, "frozen live total must equal the stored total"
    assert sorted(p["points"] for p in before["players"]) == [20] * 11

    internal_ids = [p["internal_id"] for p in pool]
    with engine.begin() as conn:
        conn.execute(
            text("DELETE FROM ml.player_gw_stats WHERE player_id = ANY(:ids) AND season = :s"),
            {"ids": internal_ids, "s": TEST_SEASON},
        )

    after = _get_team(cid, creator).json()

    assert after["live_total_points"] == 250, "the stats are gone -- this recomputed and got 0s"
    assert after["contest_total_points"] == 250
    assert sorted(p["points"] for p in after["players"]) == [20] * 11
    assert [p["minutes"] for p in after["players"]] == [90] * 11, "minutes come from storage too"
    assert after["captain_bonus"] == 20.0 and after["vice_captain_bonus"] == 10.0


def test_get_user_team_on_a_live_contest_still_recomputes(engine, make_user, make_team, make_player):
    """The unfinalized path is untouched: points still come from current
    stats, so a team with no stats rows yet reads as a legitimate all-zero
    team rather than an error."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5730, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    cid = contest["contest_id"]
    team = _pick_valid_team(pool)
    _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1])

    body = _get_team(cid, creator).json()

    assert body["is_finalized"] is False
    assert body["live_total_points"] == 0
    assert all(p["points"] == 0 for p in body["players"])


# --- what finalization refuses to do ---------------------------------


def test_finalization_refuses_while_the_fixture_is_unfinished(engine, make_user, make_team, make_player):
    """The gate is the fixture's own finished flag, not the clock. A
    contest polled at kickoff+115min while the match is still officially in
    progress gets scored but stays open -- freezing a guess is the one
    thing that cannot be undone."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5740, gameweek=1)
    cid = _create_contest(fixture_id, creator)["contest_id"]
    team = _pick_valid_team(pool)
    _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1])

    result = finalize_dream11_contest(engine, cid)

    assert result["finalized"] is False
    assert result["reason"] == "fixture is not finished"
    assert result["score"] is not None, "it should still have scored the contest"
    assert _contest_finalized_at(engine, cid) is None


def test_finalization_refuses_when_a_team_failed_to_score(engine, make_user, make_team, make_player):
    """score_dream11_contest isolates each team, so one broken team lands
    in `failed` rather than aborting the batch. Freezing then would make
    that team's zero permanent, so the whole contest stays open for the
    sweep to retry.

    The break here is a deleted team_players row, which trips
    _score_one_team's 11-row guard."""
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5750, gameweek=1)
    cid = _create_contest(fixture_id, creator)["contest_id"]
    team = _pick_valid_team(pool)
    _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1])

    with engine.begin() as conn:
        conn.execute(
            text(
                "DELETE FROM dream11.team_players WHERE id = ("
                "  SELECT tp.id FROM dream11.team_players tp"
                "  JOIN dream11.teams t ON t.id = tp.team_id"
                "  WHERE t.contest_id = :cid LIMIT 1)"
            ),
            {"cid": cid},
        )
    _finish_fixture(engine, fixture_id)

    result = finalize_dream11_contest(engine, cid)

    assert result["finalized"] is False
    assert result["reason"] == "one or more teams failed to score"
    assert _contest_finalized_at(engine, cid) is None, "a contest with a failed team must stay open"


def test_the_database_blocks_rescoring_a_finalized_contest(engine, make_user, make_team, make_player):
    """Belt and braces. The application refuses to rescore a finalized
    contest; this asserts the database refuses too, so a future code path
    that bypasses score_dream11_contest still cannot move a settled
    leaderboard -- the same stance ml.enforce_gw_stats_immutability_fn
    takes for settled gameweek stats."""
    cid, _fixture_id, creator, _team, _pool = _finalized_contest(
        engine, make_user, make_team, make_player, 5760
    )

    with pytest.raises(Exception, match="finalized Dream11 contest"):
        with engine.begin() as conn:
            conn.execute(
                text("UPDATE dream11.contest_members SET total_points = 9999 WHERE contest_id = :c AND user_id = :u"),
                {"c": cid, "u": creator},
            )

    assert _member_points(engine, cid, creator) == 250


def test_an_unrelated_update_to_a_finalized_members_row_is_still_allowed(
    engine, make_user, make_team, make_player
):
    """The trigger is scoped to total_points/rank changes specifically, so
    it does not turn contest_members into a fully read-only table."""
    cid, _fixture_id, creator, _team, _pool = _finalized_contest(
        engine, make_user, make_team, make_player, 5770
    )

    with engine.begin() as conn:
        conn.execute(
            text("UPDATE dream11.contest_members SET joined_at = now() WHERE contest_id = :c AND user_id = :u"),
            {"c": cid, "u": creator},
        )  # must not raise


# --- the sweep -------------------------------------------------------


def test_find_contests_needing_finalization_selects_finished_and_unfrozen(
    engine, make_user, make_team, make_player
):
    """The sweep's query: fixture finished AND not yet finalized. This is
    what guarantees a contest gets finalized at all, since the kickoff+115
    checkpoint usually runs before FPL sets the finished flag -- and may
    never have been booked, if the broker was down at contest creation."""
    creator = make_user()

    # Two pools in ONE test, so the bases must be >30 apart -- each seeds
    # players at base+1 .. base+30.
    unfinished_fx, *_r1, pool1 = _seed_fixture_and_pool(engine, make_team, make_player, 5780, gameweek=1)
    unfinished_cid = _create_contest(unfinished_fx, creator)["contest_id"]

    finished_fx, *_r2, pool2 = _seed_fixture_and_pool(engine, make_team, make_player, 5900, gameweek=1)
    finished_cid = _create_contest(finished_fx, creator)["contest_id"]
    _finish_fixture(engine, finished_fx)

    due = find_contests_needing_finalization(engine)

    assert finished_cid in due, "a finished fixture's contest is due for finalization"
    assert unfinished_cid not in due, "an in-progress fixture's contest is not"

    # And once frozen it drops out, which is what makes the sweep idempotent.
    team = _pick_valid_team(pool2)
    _submit_team(finished_cid, creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert finalize_dream11_contest(engine, finished_cid)["finalized"] is True

    assert finished_cid not in find_contests_needing_finalization(engine)


def test_finalization_freezes_a_contest_with_no_submitted_teams(engine, make_user, make_team, make_player):
    """A contest nobody submitted a team to still finalizes -- there is
    nothing to score, so there is nothing that can fail, and leaving it
    permanently in the sweep's queue would mean re-querying it forever."""
    creator = make_user()
    fixture_id, *_rest, _pool = _seed_fixture_and_pool(engine, make_team, make_player, 5800, gameweek=1)
    cid = _create_contest(fixture_id, creator)["contest_id"]
    _finish_fixture(engine, fixture_id)

    result = finalize_dream11_contest(engine, cid)

    assert result["finalized"] is True
    assert result["score"]["scored"] == []
    assert _contest_finalized_at(engine, cid) is not None


def test_contest_summary_exposes_is_finalized(engine, make_user, make_team, make_player):
    """is_locked never goes back to FALSE, so on its own it cannot tell a
    match in progress from one that ended months ago. is_finalized is the
    settled state the UI needs to render a result as final."""
    cid, _fixture_id, creator, _team, _pool = _finalized_contest(
        engine, make_user, make_team, make_player, 5810
    )

    body = client.get(f"/dream11/contests/{cid}", headers=_auth_headers(creator)).json()

    assert body["is_finalized"] is True
    assert body["is_locked"] is False, "finalization is independent of the kickoff lock"


# ---------------------------------------------------------------- double gameweeks
#
# A gameweek can contain TWO matches for the same club. A Dream11 contest
# is played on exactly one of them, so its scoring joins are scoped to the
# contest's own fixture_id -- not just the gameweek.
#
# ml.player_gw_stats is unique on
# (player_id, season, gameweek, COALESCE(fixture_id, -1)), so both
# matches' rows coexist legitimately. Matching on gameweek alone pulled
# both, an 11-player team came back as 12 rows, _score_one_team's size
# guard raised, and the team failed. Since finalization refuses to freeze
# a contest with any failed team, that left the contest stuck unfinalized
# permanently, retried by the sweep on every pass forever.


def _seed_second_fixture_same_gameweek(engine, home_team_id, away_team_id, fpl_id, gameweek=1):
    """A SECOND fixture in the same gameweek for the same two clubs -- the
    thing that makes a double gameweek. Returns its fixture_id."""
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, away_team_id, kickoff_time, finished) "
                "VALUES (:fpl_id, :season, :gw, :home, :away, NULL, FALSE) RETURNING id"
            ),
            {"fpl_id": fpl_id, "season": TEST_SEASON, "gw": gameweek, "home": home_team_id, "away": away_team_id},
        ).scalar()


def test_scoring_ignores_stats_from_another_match_in_the_same_gameweek(
    engine, make_user, make_team, make_player
):
    """Every player has TWO stat rows for gameweek 1: a quiet game in the
    contest's own fixture and a hat-trick in the other one. Only the
    contest's fixture may count.

    Under the gameweek-only join this raised
    ValueError("expected 11 team_players rows ... got 22") and the team
    scored nothing at all.
    """
    creator = make_user()
    fixture_a, home_id, away_id, pool = _seed_fixture_and_pool(
        engine, make_team, make_player, 6900, gameweek=1
    )
    fixture_b = _seed_second_fixture_same_gameweek(engine, home_id, away_id, fpl_id=6899, gameweek=1)

    cid = _create_contest(fixture_a, creator)["contest_id"]
    team = _pick_valid_team(pool)
    assert _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1]).status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    for fid in team:
        # Fixture A (the contest's): one assist -> 20 points each.
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_a, minutes=90, assists=1)
        # Fixture B (same gameweek, different match): a hat-trick that must
        # be invisible here. Deliberately large so leakage is unmissable.
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_b, minutes=90, goals_scored=3)

    summary = score_dream11_contest(engine, cid)

    assert summary["failed"] == [], f"the double gameweek broke scoring again: {summary['failed']}"
    assert creator in summary["scored"]
    # 11 x 20 = 220 raw, + captain 20 + vice 10 = 250. Fixture B's goals
    # would have added at least 11 x 4 more had any of it leaked in.
    assert _member_points(engine, cid, creator) == 250

    # And the live team view agrees -- the two queries must match the same
    # rows or the leaderboard and the team panel would disagree.
    body = _get_team(cid, creator).json()
    assert body["live_total_points"] == 250
    assert sorted(p["points"] for p in body["players"]) == [20] * 11
    assert len(body["players"]) == 11, "the team panel returned duplicate players"


def test_a_double_gameweek_contest_still_finalizes_normally(engine, make_user, make_team, make_player):
    """THE END-TO-END CASE, and the reason this fix was urgent.

    Contest on fixture A; the same clubs also play fixture B that gameweek.
    Once fixture A finishes the contest must score from A's stats only and
    finalize -- regardless of what fixture B is doing, since B is not this
    contest's match and may not even have kicked off.

    Before the fix this contest could never finalize: every team failed the
    row-count guard, finalization correctly refused to freeze a contest
    with failures, and the sweep would have retried it forever.
    """
    creator = make_user()
    fixture_a, home_id, away_id, pool = _seed_fixture_and_pool(
        engine, make_team, make_player, 6950, gameweek=1
    )
    fixture_b = _seed_second_fixture_same_gameweek(engine, home_id, away_id, fpl_id=6949, gameweek=1)

    cid = _create_contest(fixture_a, creator)["contest_id"]
    team = _pick_valid_team(pool)
    assert _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1]).status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    for fid in team:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_a, minutes=90, assists=1)
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_b, minutes=90, goals_scored=3)

    # Fixture A is over; fixture B has NOT been played -- exactly the state
    # a Saturday-then-Wednesday double gameweek is in for several days.
    _finish_fixture(engine, fixture_a)

    assert cid in find_contests_needing_finalization(engine)

    result = finalize_dream11_contest(engine, cid)

    assert result["finalized"] is True, result
    assert result["score"]["failed"] == []
    assert _contest_finalized_at(engine, cid) is not None
    assert _member_points(engine, cid, creator) == 250
    assert cid not in find_contests_needing_finalization(engine), "must drop out of the sweep once frozen"

    # Frozen, and reading the stored breakdown -- fixture B's hat-tricks
    # never appear, before or after finalization.
    body = _get_team(cid, creator).json()
    assert body["is_finalized"] is True
    assert body["live_total_points"] == 250
    assert sorted(p["points"] for p in body["players"]) == [20] * 11


def test_fixture_b_finishing_later_cannot_move_a_frozen_contest(engine, make_user, make_team, make_player):
    """The other half of the timeline: fixture B finishes days after the
    contest froze. Nothing about B may touch a settled result -- it was
    never this contest's match, and the contest is finalized besides."""
    creator = make_user()
    fixture_a, home_id, away_id, pool = _seed_fixture_and_pool(
        engine, make_team, make_player, 7000, gameweek=1
    )
    fixture_b = _seed_second_fixture_same_gameweek(engine, home_id, away_id, fpl_id=6999, gameweek=1)

    cid = _create_contest(fixture_a, creator)["contest_id"]
    team = _pick_valid_team(pool)
    _submit_team(cid, creator, team, captain_id=team[0], vice_captain_id=team[1])

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    for fid in team:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_a, minutes=90, assists=1)

    _finish_fixture(engine, fixture_a)
    assert finalize_dream11_contest(engine, cid)["finalized"] is True
    frozen = _member_points(engine, cid, creator)
    assert frozen == 250

    # Days later: fixture B is played and its stats land.
    for fid in team:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_b, minutes=90, goals_scored=3)
    _finish_fixture(engine, fixture_b)

    assert cid not in find_contests_needing_finalization(engine)
    assert score_dream11_contest(engine, cid)["skipped_finalized"] is True
    assert _member_points(engine, cid, creator) == frozen


# ---------------------------------------------------------------- full end-to-end cross-check

def test_full_11_player_team_score_matches_hand_calculation(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5500, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)  # 1 GK, 4 DEF, 4 MID, 2 FWD
    positions_by_fpl = {p["fpl_id"]: p["position"] for p in pool}
    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}

    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert resp.status_code == 200

    # Deliberately varied, hand-calculable stats per player. Every entry
    # here that sets goals_conceded=0 with minutes>=54 ALSO earns a clean
    # sheet bonus for that position (GK/DEF=4, MID=1, FWD=0) -- accounted
    # for explicitly in expected_per_player below, not forgotten.
    stats_by_fpl = {
        team[0]: dict(minutes=90, goals_scored=1, assists=0, goals_conceded=0),   # GK, captain: goal(10) + cs(4) = 14
        team[1]: dict(minutes=90, goals_scored=0, assists=1, goals_conceded=0),   # DEF, vice: assist(20) + cs(4) = 24
        team[2]: dict(minutes=90, goals_scored=0, assists=0, goals_conceded=0),   # DEF: cs(4) = 4
        team[3]: dict(minutes=90, goals_scored=0, assists=0, goals_conceded=2),   # DEF: conceded penalty (-1) = -1
        team[4]: dict(minutes=53, goals_scored=0, assists=0, goals_conceded=0),   # DEF: below CS threshold = 0
        team[5]: dict(minutes=90, goals_scored=1, assists=0, goals_conceded=0),   # MID: goal(5) + cs(1) = 6
        team[6]: dict(minutes=90, goals_scored=0, assists=0, goals_conceded=0, yellow_cards=1),  # MID: card(-1) + cs(1) = 0
        team[7]: dict(minutes=90, goals_scored=0, assists=0, goals_conceded=0, own_goals=1),      # MID: own goal(-2) + cs(1) = -1
        team[8]: dict(minutes=0, goals_scored=0, assists=0, goals_conceded=0),    # MID: didn't play (minutes=0) = 0
        team[9]: dict(minutes=90, goals_scored=1, assists=0, goals_conceded=0),   # FWD: goal(4) + cs(0) = 4
        team[10]: dict(minutes=90, goals_scored=0, assists=0, goals_conceded=0, penalties_missed=1),  # FWD: pen missed(-2) + cs(0) = -2
    }
    for fid, s in stats_by_fpl.items():
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, fixture_id, total_points=0, **s)

    # Hand calculation, using the real position-aware rules.
    expected_per_player = {
        team[0]: 14, team[1]: 24, team[2]: 4, team[3]: -1, team[4]: 0,
        team[5]: 6, team[6]: 0, team[7]: -1, team[8]: 0, team[9]: 4, team[10]: -2,
    }
    raw_points = sum(expected_per_player.values())  # 14+24+4-1+0+6+0-1+0+4-2 = 48
    captain_bonus = expected_per_player[team[0]] * 1.0  # 14.0
    vice_bonus = expected_per_player[team[1]] * 0.5     # 12.0
    expected_final = round(raw_points + captain_bonus + vice_bonus)  # round(48+14.0+12.0) = 74

    summary = score_dream11_contest(engine, contest["contest_id"])
    assert summary["failed"] == []

    with engine.connect() as conn:
        actual = conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
            {"cid": contest["contest_id"], "uid": creator},
        ).scalar()
    assert actual == expected_final


# ---------------------------------------------------------------- poll_fixture_checkpoint

def test_poll_halftime_sets_is_live_true_and_stays_updatable(engine, make_team, make_player, monkeypatch):
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6000, gameweek=1)
    target = pool[0]

    monkeypatch.setattr(
        live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 45, "goals_scored": 0, "total_points": 2}}]},
    )
    summary1 = live_poll.poll_fixture_checkpoint(engine, fixture_id, "halftime")
    assert target["fpl_id"] in summary1["updated"]

    with engine.connect() as conn:
        row1 = conn.execute(
            text("SELECT is_live, minutes FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row1.is_live is True
    assert row1.minutes == 45

    monkeypatch.setattr(
        live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 45, "goals_scored": 1, "total_points": 8}}]},
    )
    summary2 = live_poll.poll_fixture_checkpoint(engine, fixture_id, "halftime")
    assert target["fpl_id"] in summary2["updated"]

    with engine.connect() as conn:
        row2 = conn.execute(
            text("SELECT goals_scored FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row2.goals_scored == 1  # successfully updated -- proves is_live=TRUE stays correctable


def test_poll_fulltime_settles_and_blocks_further_update(engine, make_team, make_player, monkeypatch):
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 6100, gameweek=1)
    target = pool[0]

    monkeypatch.setattr(
        live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 90, "goals_scored": 1, "total_points": 6}}]},
    )
    summary = live_poll.poll_fixture_checkpoint(engine, fixture_id, "fulltime")
    assert target["fpl_id"] in summary["updated"]

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT is_live, minutes, goals_scored FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row.is_live is False
    assert row.minutes == 90

    monkeypatch.setattr(
        live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 90, "goals_scored": 2, "total_points": 12}}]},
    )
    summary2 = live_poll.poll_fixture_checkpoint(engine, fixture_id, "fulltime")
    assert target["fpl_id"] in summary2["already_settled"]
    assert target["fpl_id"] not in summary2["updated"]

    with engine.connect() as conn:
        row2 = conn.execute(
            text("SELECT goals_scored FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row2.goals_scored == 1  # unchanged -- second poll's data was correctly rejected, not applied
