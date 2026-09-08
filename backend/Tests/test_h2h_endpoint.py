"""
test_h2h_endpoint.py — GET /leagues/{league_id}/h2h.

The H2H *computation* (schedule generation, match results, match-point
accumulation) is covered by test_leagues.py and is not retested here. This
file covers the read/serve layer that was missing: turning
league_h2h_fixtures rows into a response a client can render.

What that layer actually has to get right, and what each test pins:

  * translating the stored win_1/win_2 into the CALLER's point of view,
    which is the one thing a client would otherwise have to decode against
    whichever side it happens to be on -- easy to get backwards, and wrong
    in a way that looks plausible
  * a bye, where side_2 is NULL rather than a player
  * the difference between a settled result and a provisional one, since
    standings.py recomputes during a live gameweek and treats a member with
    no gw_scores row as 0 -- so a mid-gameweek 40-0 can mean "not scored
    yet", not "blanked"
  * refusing a classic league loudly instead of returning an empty list
  * 401 for an unauthenticated caller, per the auth cutover
"""

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from main import app
from Results.standings import compute_league_standings
from Shared.rules import BYE_POINTS, WIN_POINTS

client = TestClient(app)

GAMEWEEK = 1


@pytest.fixture
def make_user(make_user, engine):
    # Mirrors test_leagues.py: users, mini_leagues and league_members are left
    # in place (leaderboard_snapshots' immutability trigger blocks deleting
    # through them anyway), so uuid identity is what keeps runs independent.
    factory = make_user

    def _make():
        return factory("h2h")

    yield _make

    with engine.begin() as conn:
        for uid in factory.created:
            conn.execute(text("DELETE FROM gw_scores WHERE user_id = :u"), {"u": uid})


def _create_league(user_id, name, scoring_type):
    resp = client.post(
        "/leagues",
        json={
            "name": name,
            "season": TEST_SEASON,
            "league_type": "private",
            "scoring_type": scoring_type,
            "max_members": 50,
        },
        headers=bearer_headers(user_id),
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


def _join(user_id, code):
    resp = client.post("/leagues/join", json={"code": code}, headers=bearer_headers(user_id))
    assert resp.status_code == 200, resp.text


def _seed_score(engine, user_id, gameweek, points):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gw_scores (user_id, season, gameweek, raw_points, final_points, "
                "transfer_hits, hit_deductions, total_points, season_total, rules_version) "
                "VALUES (:u, :s, :gw, :p, :p, 0, 0, :p, :p, 2)"
            ),
            {"u": user_id, "s": TEST_SEASON, "gw": gameweek, "p": points},
        )


def _seed_fixtures(engine, gameweek, count=1, finished=True, base=6100):
    """Fixture rows drive `status`. All finished -> 'final'; unfinished but
    kicked off -> 'live'. Without any rows the gameweek reads 'upcoming'."""
    with engine.begin() as conn:
        for i in range(count):
            home = conn.execute(
                text("INSERT INTO ml.teams (fpl_id, season, name, short_name) "
                     "VALUES (:f, :s, :n, :sn) RETURNING id"),
                {"f": base + i * 2, "s": TEST_SEASON, "n": f"H{base+i}", "sn": f"H{i}"},
            ).scalar()
            away = conn.execute(
                text("INSERT INTO ml.teams (fpl_id, season, name, short_name) "
                     "VALUES (:f, :s, :n, :sn) RETURNING id"),
                {"f": base + i * 2 + 1, "s": TEST_SEASON, "n": f"A{base+i}", "sn": f"A{i}"},
            ).scalar()
            conn.execute(
                text("INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, "
                     "away_team_id, kickoff_time, finished) "
                     "VALUES (:f, :s, :gw, :h, :a, now() - interval '3 hours', :fin)"),
                {"f": base + i, "s": TEST_SEASON, "gw": gameweek, "h": home, "a": away,
                 "fin": finished},
            )


def _get(league_id, gameweek, user_id):
    return client.get(
        f"/leagues/{league_id}/h2h",
        params={"gameweek": gameweek},
        headers=bearer_headers(user_id),
    )


# ------------------------------------------------------------ a real matchup


def test_a_real_matchup_returns_both_sides_and_the_callers_outcome(engine, make_user):
    winner, loser = make_user(), make_user()
    league = _create_league(winner, "H2H Endpoint", "head_to_head")
    _join(loser, league["code"])

    _seed_score(engine, winner, GAMEWEEK, 40)
    _seed_score(engine, loser, GAMEWEEK, 38)
    _seed_fixtures(engine, GAMEWEEK, finished=True)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    resp = _get(league["league_id"], GAMEWEEK, winner)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["gameweek"] == GAMEWEEK
    assert body["season"] == TEST_SEASON
    assert body["status"] == "final"
    assert body["is_provisional"] is False
    assert len(body["fixtures"]) == 1

    fx = body["your_fixture"]
    assert fx is not None
    assert fx["is_bye"] is False
    assert fx["result"] in ("win_1", "win_2")

    sides = [fx["side_1"], fx["side_2"]]
    mine = next(s for s in sides if s["is_current_user"])
    theirs = next(s for s in sides if not s["is_current_user"])
    assert mine["user_id"] == winner and mine["points"] == 40
    assert theirs["user_id"] == loser and theirs["points"] == 38

    # The whole point of this field: the caller sees "win" without having to
    # decode win_1/win_2 against which side they landed on.
    assert fx["outcome_for_current_user"] == "win"


def test_the_same_fixture_reads_as_a_loss_for_the_opponent(engine, make_user):
    """Same row, opposite viewpoint -- the field that would be wrong if the
    win_1/win_2 translation were inverted."""
    winner, loser = make_user(), make_user()
    league = _create_league(winner, "H2H Viewpoint", "head_to_head")
    _join(loser, league["code"])

    _seed_score(engine, winner, GAMEWEEK, 40)
    _seed_score(engine, loser, GAMEWEEK, 38)
    _seed_fixtures(engine, GAMEWEEK, finished=True, base=6200)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    assert _get(league["league_id"], GAMEWEEK, winner).json()["your_fixture"][
        "outcome_for_current_user"] == "win"
    assert _get(league["league_id"], GAMEWEEK, loser).json()["your_fixture"][
        "outcome_for_current_user"] == "loss"


def test_a_draw_reads_as_a_draw_for_both(engine, make_user):
    a, b = make_user(), make_user()
    league = _create_league(a, "H2H Draw", "head_to_head")
    _join(b, league["code"])

    _seed_score(engine, a, GAMEWEEK, 44)
    _seed_score(engine, b, GAMEWEEK, 44)
    _seed_fixtures(engine, GAMEWEEK, finished=True, base=6300)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    for uid in (a, b):
        fx = _get(league["league_id"], GAMEWEEK, uid).json()["your_fixture"]
        assert fx["result"] == "draw"
        assert fx["outcome_for_current_user"] == "draw"


# -------------------------------------------------------------------- a bye


def test_a_bye_returns_a_null_second_side(engine, make_user):
    """Odd membership means someone sits out. side_2 is genuinely absent
    rather than a placeholder opponent with 0 points."""
    a, b, c = make_user(), make_user(), make_user()
    league = _create_league(a, "H2H Bye", "head_to_head")
    _join(b, league["code"])
    _join(c, league["code"])

    for uid in (a, b, c):
        _seed_score(engine, uid, GAMEWEEK, 30)
    _seed_fixtures(engine, GAMEWEEK, finished=True, base=6400)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    body = _get(league["league_id"], GAMEWEEK, a).json()
    byes = [f for f in body["fixtures"] if f["is_bye"]]
    assert len(byes) == 1, f"three members should produce exactly one bye, got {body['fixtures']}"

    bye = byes[0]
    assert bye["side_2"] is None
    assert bye["result"] == "bye"
    assert bye["side_1"]["points"] is None or isinstance(bye["side_1"]["points"], int)

    # And the member who got it sees it as their own fixture, marked "bye".
    benched = bye["side_1"]["user_id"]
    theirs = _get(league["league_id"], GAMEWEEK, benched).json()["your_fixture"]
    assert theirs["is_bye"] is True
    assert theirs["outcome_for_current_user"] == "bye"


# ------------------------------------------------------- provisional / live


def test_an_unfinished_gameweek_is_flagged_provisional(engine, make_user):
    """standings recomputes during a live gameweek and scores a member with no
    gw_scores row as 0 -- so this 40-0 is exactly the misleading case the flag
    exists for. The result is served, but marked not-settled."""
    ahead, behind = make_user(), make_user()
    league = _create_league(ahead, "H2H Live", "head_to_head")
    _join(behind, league["code"])

    _seed_score(engine, ahead, GAMEWEEK, 40)
    # `behind` deliberately has NO gw_scores row yet.
    _seed_fixtures(engine, GAMEWEEK, finished=False, base=6500)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    body = _get(league["league_id"], GAMEWEEK, ahead).json()

    assert body["status"] == "live"
    assert body["is_provisional"] is True, (
        "a result computed mid-gameweek must not present as settled"
    )
    fx = body["your_fixture"]
    unscored = next(s for s in (fx["side_1"], fx["side_2"]) if s["user_id"] == behind)
    assert unscored["points"] == 0  # standings' documented assumption, surfaced as-is


def test_a_finished_gameweek_is_not_provisional(engine, make_user):
    a, b = make_user(), make_user()
    league = _create_league(a, "H2H Settled", "head_to_head")
    _join(b, league["code"])

    _seed_score(engine, a, GAMEWEEK, 50)
    _seed_score(engine, b, GAMEWEEK, 20)
    _seed_fixtures(engine, GAMEWEEK, finished=True, base=6600)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    body = _get(league["league_id"], GAMEWEEK, a).json()
    assert body["status"] == "final"
    assert body["is_provisional"] is False


def test_an_unplayed_future_gameweek_is_not_provisional(engine, make_user):
    """The schedule is generated for the whole season upfront, so future
    gameweeks exist as rows with result NULL. Nothing has been computed, so
    there is nothing provisional about them."""
    a, b = make_user(), make_user()
    league = _create_league(a, "H2H Future", "head_to_head")
    _join(b, league["code"])

    _seed_score(engine, a, GAMEWEEK, 10)
    _seed_score(engine, b, GAMEWEEK, 10)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    body = _get(league["league_id"], 30, a).json()  # far-future gameweek
    assert body["status"] == "upcoming"
    assert body["is_provisional"] is False
    for fx in body["fixtures"]:
        assert fx["result"] is None
        assert fx["outcome_for_current_user"] is None
        assert fx["side_1"]["points"] is None


# ------------------------------------------------------------ season records


def test_records_accumulate_wins_and_byes_across_gameweeks(engine, make_user):
    a, b, c = make_user(), make_user(), make_user()
    league = _create_league(a, "H2H Records", "head_to_head")
    _join(b, league["code"])
    _join(c, league["code"])

    for gw in (1, 2):
        for uid, pts in ((a, 50), (b, 40), (c, 30)):
            _seed_score(engine, uid, gw, pts)
        _seed_fixtures(engine, gw, finished=True, base=6700 + gw * 10)
        compute_league_standings(engine, TEST_SEASON, gw)

    records = {r["user_id"]: r for r in _get(league["league_id"], 2, a).json()["records"]}
    assert set(records) == {a, b, c}

    for r in records.values():
        played = r["wins"] + r["draws"] + r["losses"] + r["byes"]
        assert played == 2, f"two gameweeks should give two results, got {r}"
        assert r["match_points"] == (
            r["wins"] * WIN_POINTS + r["byes"] * BYE_POINTS + r["draws"] * 1
        )


def test_records_include_a_member_with_no_completed_fixture_yet(engine, make_user):
    """The LEFT JOIN out from league_members: a member must still appear with
    zeroes rather than dropping out of the table."""
    a, b = make_user(), make_user()
    league = _create_league(a, "H2H Zero", "head_to_head")
    _join(b, league["code"])

    _seed_score(engine, a, GAMEWEEK, 10)
    _seed_score(engine, b, GAMEWEEK, 10)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    # Ask about a future gameweek; nothing is completed, so every record is 0
    # but both members are still listed.
    records = {r["user_id"]: r for r in _get(league["league_id"], 30, a).json()["records"]}
    assert set(records) == {a, b}


# ------------------------------------------------------------------- guards


def test_a_classic_league_is_rejected_not_returned_empty(engine, make_user):
    """An empty fixtures array would read as 'no matches this week' rather
    than 'this league has no head-to-head at all'."""
    owner = make_user()
    league = _create_league(owner, "Classic League", "classic")

    resp = _get(league["league_id"], GAMEWEEK, owner)

    assert resp.status_code == 422, resp.text
    detail = " ".join(resp.json()["detail"])
    assert "head_to_head" in detail
    assert "classic" in detail


def test_an_unknown_league_is_404(make_user):
    resp = _get(99999999, GAMEWEEK, make_user())
    assert resp.status_code == 404
    assert resp.json()["detail"] == "league not found"


def test_a_non_member_sees_the_league_but_has_no_fixture(engine, make_user):
    """Matches GET /leagues/{id}/table, which is also readable by any
    authenticated user -- your_fixture is simply None for an outsider."""
    a, b, outsider = make_user(), make_user(), make_user()
    league = _create_league(a, "H2H Outsider", "head_to_head")
    _join(b, league["code"])

    _seed_score(engine, a, GAMEWEEK, 25)
    _seed_score(engine, b, GAMEWEEK, 20)
    _seed_fixtures(engine, GAMEWEEK, finished=True, base=6800)
    compute_league_standings(engine, TEST_SEASON, GAMEWEEK)

    body = _get(league["league_id"], GAMEWEEK, outsider).json()
    assert len(body["fixtures"]) == 1
    assert body["your_fixture"] is None
    assert all(
        not s["is_current_user"]
        for f in body["fixtures"]
        for s in (f["side_1"], f["side_2"])
        if s is not None
    )


def test_unauthenticated_call_is_401():
    resp = client.get("/leagues/1/h2h", params={"gameweek": GAMEWEEK})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "not authenticated"


def test_missing_gameweek_param_is_422(make_user):
    resp = client.get("/leagues/1/h2h", headers=bearer_headers(make_user()))
    assert resp.status_code == 422
