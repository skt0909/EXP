"""
test_leagues.py — tests for Game_logic/leagues.py (create/join HTTP
endpoints) and Game_logic/standings.py's compute_league_standings
(called directly, same approach test_scoring.py uses for
score_gameweek -- bypasses the Celery task wrapper entirely).

Covers: league creation (unique code, creator auto-joined), join
validation (bad code / already-member / at-capacity), classic standings
(ranking with ties, leaderboard_snapshots + rank_movement across two
gameweeks), H2H standings (schedule auto-generation with and without a
bye, match-result determination, season_points as match points vs.
last_gw_points as real FPL points -- the trickiest semantic to get
right), and idempotent re-runs (no duplicate snapshots, schedule not
regenerated).

users/mini_leagues/league_members/league_h2h_fixtures/leaderboard_
snapshots are NOT cleaned up in teardown: leaderboard_snapshots has an
immutability trigger blocking UPDATE/DELETE (same shape as transfers.py's),
and it cascades from both users and mini_leagues, so neither can be
deleted once a snapshot exists. Same fix as test_transfers.py/
test_scoring.py: every user gets a UUID-unique identity so tests never
collide, and nothing is deleted -- these rows are permanent by the
schema's own design.
"""

import uuid

from sqlalchemy import text
import pytest
from fastapi.testclient import TestClient

from conftest import TEST_SEASON
from main import app  # shared FastAPI app -- leagues' router is mounted on it
from standings import compute_league_standings

client = TestClient(app)


@pytest.fixture
def make_user(engine):
    def _make():
        unique = uuid.uuid4().hex[:12]
        with engine.begin() as conn:
            return conn.execute(
                text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
                {"e": f"pytest_leagues_{unique}@example.com", "u": f"pytest_leagues_{unique}", "p": "not_a_real_hash"},
            ).scalar()

    return _make


def _create_league(user_id, name, season, league_type, scoring_type, max_members=50):
    resp = client.post(
        "/leagues",
        json={
            "user_id": user_id,
            "name": name,
            "season": season,
            "league_type": league_type,
            "scoring_type": scoring_type,
            "max_members": max_members,
        },
    )
    assert resp.status_code == 200, resp.json()
    return resp.json()


def _join_league(user_id, code):
    return client.post("/leagues/join", json={"user_id": user_id, "code": code})


def _seed_gw_score(engine, user_id, season, gameweek, total_points, season_total):
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO gw_scores (user_id, season, gameweek, raw_points, final_points, transfer_hits, hit_deductions, total_points, season_total) "
                "VALUES (:u, :s, :gw, :tp, :tp, 0, 0, :tp, :st)"
            ),
            {"u": user_id, "s": season, "gw": gameweek, "tp": total_points, "st": season_total},
        )


def _member_row(engine, league_id, user_id):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT season_points, rank, last_gw_points FROM league_members WHERE league_id = :lid AND user_id = :uid"),
            {"lid": league_id, "uid": user_id},
        ).first()


def _snapshot_row(engine, league_id, user_id, season, gameweek):
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT points_this_gw, total_points, rank, rank_movement FROM leaderboard_snapshots "
                "WHERE league_id = :lid AND user_id = :uid AND season = :s AND gameweek = :gw"
            ),
            {"lid": league_id, "uid": user_id, "s": season, "gw": gameweek},
        ).first()


def _fixtures_for_gw(engine, league_id, season, gameweek):
    with engine.connect() as conn:
        return list(
            conn.execute(
                text(
                    "SELECT user_id_1, user_id_2, points_1, points_2, result FROM league_h2h_fixtures "
                    "WHERE league_id = :lid AND season = :s AND gameweek = :gw"
                ),
                {"lid": league_id, "s": season, "gw": gameweek},
            )
        )


def _all_fixtures_count(engine, league_id, season):
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT COUNT(*) FROM league_h2h_fixtures WHERE league_id = :lid AND season = :s"),
            {"lid": league_id, "s": season},
        ).scalar()


# ---------------------------------------------------------------- create/join

def test_create_league_generates_code_and_auto_joins_creator(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "My League", TEST_SEASON, "private", "classic")

    assert len(league["code"]) == 7
    assert league["code"].isalnum()
    assert league["code"] == league["code"].upper()

    member = _member_row(engine, league["league_id"], creator)
    assert member is not None
    assert member.season_points == 0
    assert member.rank == 0
    assert member.last_gw_points == 0


def test_join_with_valid_code_succeeds(make_user):
    creator = make_user()
    league = _create_league(creator, "Joinable", TEST_SEASON, "public", "classic")
    joiner = make_user()

    resp = _join_league(joiner, league["code"])
    assert resp.status_code == 200
    assert resp.json()["league_id"] == league["league_id"]


def test_join_with_invalid_code_rejected(make_user):
    joiner = make_user()
    resp = _join_league(joiner, "NOTREAL")

    assert resp.status_code == 422
    assert "code does not match any existing league" in resp.json()["detail"]


def test_join_already_member_rejected(make_user):
    creator = make_user()
    league = _create_league(creator, "AlreadyIn", TEST_SEASON, "public", "classic")

    resp = _join_league(creator, league["code"])

    assert resp.status_code == 422
    assert "user is already a member of this league" in resp.json()["detail"]


def test_join_at_capacity_rejected(make_user):
    creator = make_user()
    league = _create_league(creator, "Full", TEST_SEASON, "public", "classic", max_members=1)
    joiner = make_user()

    resp = _join_league(joiner, league["code"])

    assert resp.status_code == 422
    assert any("at capacity" in e for e in resp.json()["detail"])


# ---------------------------------------------------------------- classic

def test_classic_standings_ranks_correctly_with_ties(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "Classic Ties", TEST_SEASON, "private", "classic")
    league_id = league["league_id"]
    m2, m3 = make_user(), make_user()
    _join_league(m2, league["code"])
    _join_league(m3, league["code"])

    _seed_gw_score(engine, creator, TEST_SEASON, 1, total_points=20, season_total=50)
    _seed_gw_score(engine, m2, TEST_SEASON, 1, total_points=25, season_total=50)  # tied with creator
    _seed_gw_score(engine, m3, TEST_SEASON, 1, total_points=10, season_total=30)

    summary = compute_league_standings(engine, TEST_SEASON, 1)
    assert league_id in summary["processed"]
    assert summary["failed"] == []

    assert _member_row(engine, league_id, creator).rank == 1
    assert _member_row(engine, league_id, m2).rank == 1
    assert _member_row(engine, league_id, m3).rank == 3  # skips rank 2 -- two tied for 1st

    assert _member_row(engine, league_id, creator).season_points == 50
    assert _member_row(engine, league_id, creator).last_gw_points == 20


def test_classic_snapshot_and_rank_movement_across_two_gameweeks(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "Classic Movement", TEST_SEASON, "private", "classic")
    league_id = league["league_id"]
    m2, m3 = make_user(), make_user()
    _join_league(m2, league["code"])
    _join_league(m3, league["code"])

    # GW1: creator 1st, m2 2nd, m3 3rd
    _seed_gw_score(engine, creator, TEST_SEASON, 1, 30, 50)
    _seed_gw_score(engine, m2, TEST_SEASON, 1, 20, 30)
    _seed_gw_score(engine, m3, TEST_SEASON, 1, 10, 10)
    compute_league_standings(engine, TEST_SEASON, 1)

    snap1 = _snapshot_row(engine, league_id, creator, TEST_SEASON, 1)
    assert snap1.points_this_gw == 30
    assert snap1.total_points == 50
    assert snap1.rank == 1
    assert snap1.rank_movement == 0  # no prior snapshot

    # GW2: m2 overtakes creator, m3 stays last
    _seed_gw_score(engine, creator, TEST_SEASON, 2, 5, 55)
    _seed_gw_score(engine, m2, TEST_SEASON, 2, 40, 70)
    _seed_gw_score(engine, m3, TEST_SEASON, 2, 5, 15)
    compute_league_standings(engine, TEST_SEASON, 2)

    m2_snap2 = _snapshot_row(engine, league_id, m2, TEST_SEASON, 2)
    assert m2_snap2.rank == 1
    assert m2_snap2.rank_movement == 1  # was rank 2, now rank 1 -> +1 (moved up)

    creator_snap2 = _snapshot_row(engine, league_id, creator, TEST_SEASON, 2)
    assert creator_snap2.rank == 2
    assert creator_snap2.rank_movement == -1  # was rank 1, now rank 2 -> -1 (moved down)

    m3_snap2 = _snapshot_row(engine, league_id, m3, TEST_SEASON, 2)
    assert m3_snap2.rank == 3
    assert m3_snap2.rank_movement == 0  # unchanged


# ---------------------------------------------------------------- head-to-head

def test_h2h_schedule_generates_correct_fixture_count_even_members(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "H2H Even", TEST_SEASON, "private", "head_to_head")
    league_id = league["league_id"]
    others = [make_user() for _ in range(3)]  # 4 members total, even
    for u in others:
        _join_league(u, league["code"])
    for u in [creator] + others:
        _seed_gw_score(engine, u, TEST_SEASON, 1, 10, 10)

    compute_league_standings(engine, TEST_SEASON, 1)

    fixtures = _fixtures_for_gw(engine, league_id, TEST_SEASON, 1)
    assert len(fixtures) == 2  # 4 members -> 2 pairs, no bye
    assert all(f.user_id_2 is not None for f in fixtures)


def test_h2h_schedule_generates_bye_for_odd_members(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "H2H Odd", TEST_SEASON, "private", "head_to_head")
    league_id = league["league_id"]
    others = [make_user() for _ in range(2)]  # 3 members total, odd
    for u in others:
        _join_league(u, league["code"])
    for u in [creator] + others:
        _seed_gw_score(engine, u, TEST_SEASON, 1, 10, 10)

    compute_league_standings(engine, TEST_SEASON, 1)

    fixtures = _fixtures_for_gw(engine, league_id, TEST_SEASON, 1)
    assert len(fixtures) == 2  # padded to 4 -> 2 pairs/round, one is a bye
    byes = [f for f in fixtures if f.user_id_2 is None]
    assert len(byes) == 1
    assert byes[0].result == "bye"


def test_h2h_match_result_determined_from_fpl_points(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "H2H Result", TEST_SEASON, "private", "head_to_head")
    league_id = league["league_id"]
    opponent = make_user()
    _join_league(opponent, league["code"])

    _seed_gw_score(engine, creator, TEST_SEASON, 1, 40, 40)
    _seed_gw_score(engine, opponent, TEST_SEASON, 1, 38, 38)

    compute_league_standings(engine, TEST_SEASON, 1)

    fixtures = _fixtures_for_gw(engine, league_id, TEST_SEASON, 1)
    assert len(fixtures) == 1
    fx = fixtures[0]
    if fx.user_id_1 == creator:
        assert fx.points_1 == 40 and fx.points_2 == 38 and fx.result == "win_1"
    else:
        assert fx.user_id_2 == creator
        assert fx.points_2 == 40 and fx.points_1 == 38 and fx.result == "win_2"


def test_h2h_last_gw_points_is_real_fpl_points_not_match_points(engine, make_user):
    """The trickiest assumption: a 40-38 win should record last_gw_points=40
    (real FPL score), NOT 3 (match points for a win)."""
    creator = make_user()
    league = _create_league(creator, "H2H LastGW", TEST_SEASON, "private", "head_to_head")
    league_id = league["league_id"]
    opponent = make_user()
    _join_league(opponent, league["code"])

    _seed_gw_score(engine, creator, TEST_SEASON, 1, 40, 40)
    _seed_gw_score(engine, opponent, TEST_SEASON, 1, 38, 38)

    compute_league_standings(engine, TEST_SEASON, 1)

    winner = _member_row(engine, league_id, creator)
    assert winner.last_gw_points == 40  # real FPL points, NOT match points (3)
    assert winner.season_points == 3    # match points for a win, NOT their FPL total (40)

    loser = _member_row(engine, league_id, opponent)
    assert loser.last_gw_points == 38
    assert loser.season_points == 0


def test_h2h_season_points_accumulates_match_points_not_fpl_points(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "H2H Accum", TEST_SEASON, "private", "head_to_head")
    league_id = league["league_id"]
    opponent = make_user()
    _join_league(opponent, league["code"])

    # GW1: creator wins (40 vs 38) -> +3 match points
    _seed_gw_score(engine, creator, TEST_SEASON, 1, 40, 40)
    _seed_gw_score(engine, opponent, TEST_SEASON, 1, 38, 38)
    compute_league_standings(engine, TEST_SEASON, 1)

    # GW2: draw (20 vs 20) -> +1 match point each
    _seed_gw_score(engine, creator, TEST_SEASON, 2, 20, 60)
    _seed_gw_score(engine, opponent, TEST_SEASON, 2, 20, 58)
    compute_league_standings(engine, TEST_SEASON, 2)

    creator_member = _member_row(engine, league_id, creator)
    assert creator_member.season_points == 4  # 3 (win) + 1 (draw), NOT their FPL season_total (60)
    assert creator_member.last_gw_points == 20  # gw2's real FPL points

    opponent_member = _member_row(engine, league_id, opponent)
    assert opponent_member.season_points == 1  # 0 (loss) + 1 (draw)
    assert opponent_member.last_gw_points == 20


# ---------------------------------------------------------------- idempotency

def test_rerunning_classic_standings_is_idempotent(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "Idempotent Classic", TEST_SEASON, "private", "classic")
    league_id = league["league_id"]
    m2 = make_user()
    _join_league(m2, league["code"])

    _seed_gw_score(engine, creator, TEST_SEASON, 1, 20, 50)
    _seed_gw_score(engine, m2, TEST_SEASON, 1, 15, 40)

    compute_league_standings(engine, TEST_SEASON, 1)
    compute_league_standings(engine, TEST_SEASON, 1)

    with engine.connect() as conn:
        count = conn.execute(
            text("SELECT COUNT(*) FROM leaderboard_snapshots WHERE league_id = :lid AND season = :s AND gameweek = 1"),
            {"lid": league_id, "s": TEST_SEASON},
        ).scalar()
    assert count == 2  # one per member, not duplicated


def test_rerunning_h2h_standings_does_not_regenerate_schedule(engine, make_user):
    creator = make_user()
    league = _create_league(creator, "Idempotent H2H", TEST_SEASON, "private", "head_to_head")
    league_id = league["league_id"]
    opponent = make_user()
    _join_league(opponent, league["code"])

    _seed_gw_score(engine, creator, TEST_SEASON, 1, 40, 40)
    _seed_gw_score(engine, opponent, TEST_SEASON, 1, 38, 38)

    compute_league_standings(engine, TEST_SEASON, 1)
    count1 = _all_fixtures_count(engine, league_id, TEST_SEASON)

    compute_league_standings(engine, TEST_SEASON, 1)
    count2 = _all_fixtures_count(engine, league_id, TEST_SEASON)

    assert count1 == count2  # full-season schedule generated once, not regenerated
    assert count1 == 38  # 2 members -> 1-round cycle, wraps across all 38 gameweeks

    with engine.connect() as conn:
        snap_count = conn.execute(
            text("SELECT COUNT(*) FROM leaderboard_snapshots WHERE league_id = :lid AND season = :s AND gameweek = 1"),
            {"lid": league_id, "s": TEST_SEASON},
        ).scalar()
    assert snap_count == 2  # one per member, not duplicated
