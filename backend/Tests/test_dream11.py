"""
test_dream11.py — tests for Game_logic/dream11.py (create/join/submit-team
HTTP endpoints), Game_logic/dream11_scoring.py (calculate_dream11_points,
score_dream11_contest), and Data_ingestion/dream11_live_poll.py
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

import uuid
from types import SimpleNamespace

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app as fastapi_app
from dream11_scoring import calculate_dream11_points, score_dream11_contest
import dream11_live_poll

client = TestClient(fastapi_app)


@pytest.fixture
def make_user(engine):
    created = []

    def _make():
        unique = uuid.uuid4().hex[:12]
        with engine.begin() as conn:
            uid = conn.execute(
                text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
                {"e": f"pytest_d11_{unique}@example.com", "u": f"pytest_d11_{unique}", "p": "not_a_real_hash"},
            ).scalar()
        created.append(uid)
        return uid

    yield _make

    with engine.begin() as conn:
        for uid in created:
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})  # cascades everything dream11


DEFAULT_SIDE_POSITIONS = ["GK"] * 2 + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3  # 15/side, 30 total


def _seed_fixture_and_pool(
    engine, make_team, make_player, fpl_id_base, gameweek=1, kickoff_time=None,
    home_positions=None, away_positions=None, rolling_points=None,
):
    """Seeds a fixture + a full player pool for both teams.
    rolling_points: optional {fpl_id: value} to seed ml.player_gw_features.
    pts_rolling_5gw for specific pool players (gameweek - 1, i.e. strictly
    before the fixture); players not in this dict get no row at all (NaN).
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
        with engine.begin() as conn:
            for p in pool:
                if p["fpl_id"] in rolling_points:
                    conn.execute(
                        text(
                            "INSERT INTO ml.player_gw_features (player_id, season, gameweek, position_encoded, price_current, pts_rolling_5gw) "
                            "VALUES (:pid, :s, :gw, 0, 50, :pts)"
                        ),
                        {"pid": p["internal_id"], "s": TEST_SEASON, "gw": gameweek - 1, "pts": rolling_points[p["fpl_id"]]},
                    )

    return fixture_id, home_team_id, away_team_id, pool


def _pick_valid_team(pool):
    """1 GK + 4 DEF + 4 MID + 2 FWD = 11, within Dream11's DEF/MID 3-5, FWD 1-3."""
    gks = [p["fpl_id"] for p in pool if p["position"] == "GK"]
    defs = [p["fpl_id"] for p in pool if p["position"] == "DEF"]
    mids = [p["fpl_id"] for p in pool if p["position"] == "MID"]
    fwds = [p["fpl_id"] for p in pool if p["position"] == "FWD"]
    return gks[:1] + defs[:4] + mids[:4] + fwds[:2]


def _create_contest(fixture_id, user_id, name="Test Contest", max_members=50):
    resp = client.post(
        "/dream11/contests", json={"fixture_id": fixture_id, "name": name, "user_id": user_id, "max_members": max_members}
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _join_contest(user_id, code):
    return client.post("/dream11/contests/join", json={"user_id": user_id, "code": code})


def _submit_team(contest_id, user_id, player_ids, captain_id, vice_captain_id):
    return client.post(
        f"/dream11/contests/{contest_id}/team",
        json={"user_id": user_id, "player_ids": player_ids, "captain_id": captain_id, "vice_captain_id": vice_captain_id},
    )


def _contest_prices(engine, contest_id):
    with engine.connect() as conn:
        return {r.player_id: float(r.credit_price) for r in conn.execute(
            text("SELECT player_id, credit_price FROM dream11.player_prices WHERE contest_id = :cid"), {"cid": contest_id}
        )}


def _lock_contest(engine, contest_id):
    with engine.begin() as conn:
        conn.execute(text("UPDATE dream11.contests SET is_locked = TRUE WHERE id = :cid"), {"cid": contest_id})


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
    contest = _create_contest(fixture_id, creator, max_members=1)
    joiner = make_user()

    resp = _join_contest(joiner, contest["code"])
    assert resp.status_code == 422
    assert any("at capacity" in e for e in resp.json()["detail"])


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
    with engine.begin() as conn:
        for p in pool:
            if p["fpl_id"] in rolling_points:
                conn.execute(
                    text(
                        "INSERT INTO ml.player_gw_features (player_id, season, gameweek, position_encoded, price_current, pts_rolling_5gw) "
                        "VALUES (:pid, :s, 0, 0, 50, :pts) ON CONFLICT DO NOTHING"
                    ),
                    {"pid": p["internal_id"], "s": TEST_SEASON, "pts": rolling_points[p["fpl_id"]]},
                )

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


def test_assist_points():
    assert calculate_dream11_points(_stats(assists=1), "MID") == 3
    assert calculate_dream11_points(_stats(assists=2), "FWD") == 6


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

def _seed_gw_stat_internal(engine, internal_id, gameweek, **overrides):
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
            text(f"INSERT INTO ml.player_gw_stats (player_id, season, gameweek, {cols}) VALUES (:pid, :s, :gw, {placeholders})"),
            {"pid": internal_id, "s": TEST_SEASON, "gw": gameweek, **row},
        )


def test_score_dream11_contest_captain_vice_math(engine, make_user, make_team, make_player):
    creator = make_user()
    fixture_id, *_rest, pool = _seed_fixture_and_pool(engine, make_team, make_player, 5000, gameweek=1)
    contest = _create_contest(fixture_id, creator)
    team = _pick_valid_team(pool)
    resp = _submit_team(contest["contest_id"], creator, team, captain_id=team[0], vice_captain_id=team[1])
    assert resp.status_code == 200

    internal_by_fpl = {p["fpl_id"]: p["internal_id"] for p in pool}
    # captain: 2 assists = 6 pts. vice: 4 assists = 12 pts. other 9: 1 assist each = 3 pts.
    _seed_gw_stat_internal(engine, internal_by_fpl[team[0]], 1, minutes=90, assists=2, total_points=6)
    _seed_gw_stat_internal(engine, internal_by_fpl[team[1]], 1, minutes=90, assists=4, total_points=12)
    for fid in team[2:]:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, minutes=90, assists=1, total_points=3)

    summary = score_dream11_contest(engine, contest["contest_id"])
    assert creator in summary["scored"]
    assert summary["failed"] == []

    # raw = 6 + 12 + 9*3 = 45. captain_bonus = 6*1.0 = 6.0. vice_bonus = 12*0.5 = 6.0. final = 57.
    with engine.connect() as conn:
        member = conn.execute(
            text("SELECT total_points FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
            {"cid": contest["contest_id"], "uid": creator},
        ).first()
    assert member.total_points == 57


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
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, minutes=90, assists=1, total_points=3)

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
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, minutes=90, goals_scored=1, total_points=4)

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

    # Position-independent scores via assists only (ASSIST_POINTS=3
    # regardless of position): team[0]=4 assists=12pts, team[1]=2
    # assists=6pts, everyone else=0.
    _seed_gw_stat_internal(engine, internal_by_fpl[team[0]], 1, minutes=90, assists=4, total_points=12)
    _seed_gw_stat_internal(engine, internal_by_fpl[team[1]], 1, minutes=90, assists=2, total_points=6)
    for fid in team[2:]:
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, minutes=90, total_points=0)

    summary = score_dream11_contest(engine, contest["contest_id"])
    assert summary["failed"] == []

    def _member(uid):
        with engine.connect() as conn:
            return conn.execute(
                text("SELECT total_points, rank FROM dream11.contest_members WHERE contest_id = :cid AND user_id = :uid"),
                {"cid": contest["contest_id"], "uid": uid},
            ).first()

    # raw_points = 12 + 6 + 0*9 = 18 for everyone (same 11 players' stats).
    # creator/m2: captain=team[0](12) -> +12.0; vice=team[2](0) -> +0. final = 30.
    # m3: captain=team[1](6) -> +6.0; vice=team[2](0) -> +0. final = 24.
    creator_row, m2_row, m3_row = _member(creator), _member(m2), _member(m3)
    assert creator_row.total_points == 30
    assert m2_row.total_points == 30
    assert m3_row.total_points == 24

    assert creator_row.rank == 1
    assert m2_row.rank == 1
    assert m3_row.rank == 3  # ties for 1st skip rank 2


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
        team[1]: dict(minutes=90, goals_scored=0, assists=1, goals_conceded=0),   # DEF, vice: assist(3) + cs(4) = 7
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
        _seed_gw_stat_internal(engine, internal_by_fpl[fid], 1, total_points=0, **s)

    # Hand calculation, using the real position-aware rules.
    expected_per_player = {
        team[0]: 14, team[1]: 7, team[2]: 4, team[3]: -1, team[4]: 0,
        team[5]: 6, team[6]: 0, team[7]: -1, team[8]: 0, team[9]: 4, team[10]: -2,
    }
    raw_points = sum(expected_per_player.values())  # 14+7+4-1+0+6+0-1+0+4-2 = 31
    captain_bonus = expected_per_player[team[0]] * 1.0  # 14.0
    vice_bonus = expected_per_player[team[1]] * 0.5     # 3.5
    expected_final = round(raw_points + captain_bonus + vice_bonus)  # round(31+14.0+3.5) = round(48.5) = 48

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
        dream11_live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 45, "goals_scored": 0, "total_points": 2}}]},
    )
    summary1 = dream11_live_poll.poll_fixture_checkpoint(engine, fixture_id, "halftime")
    assert target["fpl_id"] in summary1["updated"]

    with engine.connect() as conn:
        row1 = conn.execute(
            text("SELECT is_live, minutes FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row1.is_live is True
    assert row1.minutes == 45

    monkeypatch.setattr(
        dream11_live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 45, "goals_scored": 1, "total_points": 8}}]},
    )
    summary2 = dream11_live_poll.poll_fixture_checkpoint(engine, fixture_id, "halftime")
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
        dream11_live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 90, "goals_scored": 1, "total_points": 6}}]},
    )
    summary = dream11_live_poll.poll_fixture_checkpoint(engine, fixture_id, "fulltime")
    assert target["fpl_id"] in summary["updated"]

    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT is_live, minutes, goals_scored FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row.is_live is False
    assert row.minutes == 90

    monkeypatch.setattr(
        dream11_live_poll, "fetch_json",
        lambda path: {"elements": [{"id": target["fpl_id"], "stats": {"minutes": 90, "goals_scored": 2, "total_points": 12}}]},
    )
    summary2 = dream11_live_poll.poll_fixture_checkpoint(engine, fixture_id, "fulltime")
    assert target["fpl_id"] in summary2["already_settled"]
    assert target["fpl_id"] not in summary2["updated"]

    with engine.connect() as conn:
        row2 = conn.execute(
            text("SELECT goals_scored FROM ml.player_gw_stats WHERE player_id = :pid AND season = :s AND gameweek = 1"),
            {"pid": target["internal_id"], "s": TEST_SEASON},
        ).first()
    assert row2.goals_scored == 1  # unchanged -- second poll's data was correctly rejected, not applied
