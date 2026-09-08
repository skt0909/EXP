"""
test_fixtures.py — tests for Game_logic/fixtures.py (GET /fixtures) and
dream11.py's GET /dream11/fixtures/{fixture_id}/contests.

Both back the Contests-mode match screens: the fixture list is how a client
discovers a fixture_id at all (POST /dream11/contests needs one), and the
per-fixture contest list is the match-detail view of it.

Hits the real DB with TEST_SEASON data, no mocks -- same stance as
test_dream11.py.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from conftest import TEST_SEASON, bearer_headers
from main import app as fastapi_app
from Game_logic.fixtures import LIVE_WINDOW_MINUTES

client = TestClient(fastapi_app)

NOW = lambda: datetime.now(timezone.utc)  # noqa: E731 -- matches test_beat_scheduling.py


@pytest.fixture
def make_user(make_user, engine):
    # Creation delegated to conftest's make_user (same name, received as an
    # argument -- pytest resolves it to the parent fixture). The prefix is
    # passed so the rows this file creates are still named as they were.
    # The teardown below stays here because it is specific to this file.
    factory = make_user

    def _make():
        return factory("fx")

    yield _make

    with engine.begin() as conn:
        for uid in factory.created:
            conn.execute(text("DELETE FROM users WHERE id = :uid"), {"uid": uid})


def _seed_match(engine, make_team, make_fixture, base, gameweek=1, kickoff_time=None, finished=False):
    home = make_team(fpl_id=base + 1, name=f"Home{base}", short_name=f"H{base}")
    away = make_team(fpl_id=base + 2, name=f"Away{base}", short_name=f"A{base}")
    fixture_id = make_fixture(
        fpl_id=base, gameweek=gameweek, home_team_id=home, away_team_id=away,
        kickoff_time=kickoff_time, finished=finished,
    )
    return fixture_id, home, away


def _seed_contest(engine, user_id, fixture_id, name="FixtureTest"):
    with engine.begin() as conn:
        contest_id = conn.execute(
            text(
                "INSERT INTO dream11.contests (fixture_id, name, code, created_by, max_members) "
                "VALUES (:f, :n, :c, :u, 50) RETURNING id"
            ),
            {"f": fixture_id, "n": name, "c": uuid.uuid4().hex[:9].upper(), "u": user_id},
        ).scalar()
        conn.execute(
            text("INSERT INTO dream11.contest_members (contest_id, user_id, total_points, rank) VALUES (:c, :u, 0, 0)"),
            {"c": contest_id, "u": user_id},
        )
    return contest_id


# GET /fixtures authenticates as of the Stage 2 auth cutover, so there is no
# longer an anonymous caller. Most tests here are about fixture status and
# scores, not identity, and just need to be *somebody* -- this supplies one
# rather than making fifteen tests each create a user they do not care about.
_VIEWER: list[int] = []


@pytest.fixture(autouse=True)
def _default_viewer(make_user):
    _VIEWER.append(make_user())
    yield
    _VIEWER.clear()


def _get_fixtures(user_id=None, **params):
    """GET /fixtures as a given user, defaulting to the ambient viewer.

    Identity travels in the Authorization header now, not as a user_id query
    parameter -- the endpoint reads it from get_current_user.
    """
    viewer = user_id if user_id is not None else _VIEWER[0]
    return client.get("/fixtures", params=params, headers=bearer_headers(viewer))


# ---------------------------------------------------------------- GET /fixtures

def test_get_fixtures_returns_both_teams_and_kickoff(engine, make_team, make_fixture):
    kickoff = NOW() + timedelta(days=2)
    fixture_id, home, away = _seed_match(engine, make_team, make_fixture, 7000, kickoff_time=kickoff)

    resp = _get_fixtures(season=TEST_SEASON)

    assert resp.status_code == 200
    row = next(f for f in resp.json() if f["fixture_id"] == fixture_id)
    assert row["home_team_id"] == home and row["away_team_id"] == away
    assert row["home_team"] == "H7000" and row["away_team"] == "A7000"
    assert row["home_team_name"] == "Home7000"
    assert row["gameweek"] == 1
    assert row["kickoff_time"] is not None


def test_get_fixtures_has_started_is_separate_from_finished(engine, make_team, make_fixture):
    """A match can be started-but-unfinished -- exactly the window where a
    Dream11 contest is locked and still being scored."""
    live_id, *_ = _seed_match(engine, make_team, make_fixture, 7100, kickoff_time=NOW() - timedelta(minutes=30))
    future_id, *_ = _seed_match(engine, make_team, make_fixture, 7200, gameweek=2, kickoff_time=NOW() + timedelta(days=1))

    body = {f["fixture_id"]: f for f in _get_fixtures(season=TEST_SEASON).json()}

    assert body[live_id]["has_started"] is True
    assert body[live_id]["finished"] is False
    assert body[future_id]["has_started"] is False


def test_get_fixtures_no_kickoff_time_is_not_started(engine, make_team, make_fixture):
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 7250, kickoff_time=None)

    row = next(f for f in _get_fixtures(season=TEST_SEASON).json() if f["fixture_id"] == fixture_id)

    assert row["kickoff_time"] is None
    assert row["has_started"] is False


def test_get_fixtures_gameweek_filter(engine, make_team, make_fixture):
    gw1_id, *_ = _seed_match(engine, make_team, make_fixture, 7300, gameweek=1)
    gw2_id, *_ = _seed_match(engine, make_team, make_fixture, 7400, gameweek=2)

    ids = [f["fixture_id"] for f in _get_fixtures(season=TEST_SEASON, gameweek=2).json()]

    assert gw2_id in ids
    assert gw1_id not in ids


def test_get_fixtures_upcoming_only_excludes_past_kickoffs(engine, make_team, make_fixture):
    past_id, *_ = _seed_match(engine, make_team, make_fixture, 7500, kickoff_time=NOW() - timedelta(days=1))
    future_id, *_ = _seed_match(engine, make_team, make_fixture, 7600, gameweek=2, kickoff_time=NOW() + timedelta(days=1))

    all_ids = [f["fixture_id"] for f in _get_fixtures(season=TEST_SEASON).json()]
    upcoming_ids = [f["fixture_id"] for f in _get_fixtures(season=TEST_SEASON, upcoming_only=True).json()]

    assert past_id in all_ids and future_id in all_ids  # default returns both
    assert future_id in upcoming_ids
    assert past_id not in upcoming_ids


def test_get_fixtures_default_includes_past_so_a_finished_season_is_not_empty(engine, make_team, make_fixture):
    """Guards the documented choice to default upcoming_only=False: every
    ingested fixture in the dev database is in the past, so defaulting the
    other way would make the first call look broken."""
    past_id, *_ = _seed_match(engine, make_team, make_fixture, 7650, kickoff_time=NOW() - timedelta(days=5), finished=True)

    ids = [f["fixture_id"] for f in _get_fixtures(season=TEST_SEASON).json()]

    assert past_id in ids


def test_get_fixtures_contest_counts(engine, make_user, make_team, make_fixture):
    owner = make_user()
    outsider = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 7700)
    _seed_contest(engine, owner, fixture_id, name="One")
    _seed_contest(engine, owner, fixture_id, name="Two")

    mine = next(f for f in _get_fixtures(season=TEST_SEASON, user_id=owner).json() if f["fixture_id"] == fixture_id)
    theirs = next(f for f in _get_fixtures(season=TEST_SEASON, user_id=outsider).json() if f["fixture_id"] == fixture_id)
    other = next(f for f in _get_fixtures(season=TEST_SEASON).json() if f["fixture_id"] == fixture_id)

    assert mine["contest_count"] == 2 and mine["user_contest_count"] == 2
    # contest_count is a plain total -- visible to anyone, and leaks no codes.
    assert theirs["contest_count"] == 2 and theirs["user_contest_count"] == 0
    # The ambient viewer joined nothing, so they see the same public total
    # as `outsider` -- post-cutover there is no anonymous caller to test.
    assert other["contest_count"] == 2 and other["user_contest_count"] == 0


def test_get_fixtures_unknown_season_is_empty_not_an_error(engine):
    resp = _get_fixtures(season="1900-01")

    assert resp.status_code == 200
    assert resp.json() == []


def test_get_fixtures_ordered_by_kickoff(engine, make_team, make_fixture):
    late_id, *_ = _seed_match(engine, make_team, make_fixture, 7800, kickoff_time=NOW() + timedelta(days=3))
    early_id, *_ = _seed_match(engine, make_team, make_fixture, 7900, gameweek=2, kickoff_time=NOW() + timedelta(days=1))

    ids = [f["fixture_id"] for f in _get_fixtures(season=TEST_SEASON, upcoming_only=True).json()]

    assert ids.index(early_id) < ids.index(late_id)


def test_get_fixtures_status_groups_the_match_list(engine, make_team, make_fixture):
    upcoming_id, *_ = _seed_match(engine, make_team, make_fixture, 7960, kickoff_time=NOW() + timedelta(days=1))
    live_id, *_ = _seed_match(engine, make_team, make_fixture, 7970, gameweek=2, kickoff_time=NOW() - timedelta(minutes=30))
    done_id, *_ = _seed_match(engine, make_team, make_fixture, 7980, gameweek=3, kickoff_time=NOW() - timedelta(days=1), finished=True)

    body = {f["fixture_id"]: f for f in _get_fixtures(season=TEST_SEASON).json()}

    assert body[upcoming_id]["status"] == "upcoming"
    assert body[live_id]["status"] == "live"
    assert body[done_id]["status"] == "completed"


def test_get_fixtures_status_completed_past_the_live_window_despite_unfinished_flag(engine, make_team, make_fixture):
    """FPL reports finished_provisional=True / finished=False for a match that
    has ended but whose bonus points aren't confirmed, and this project doesn't
    ingest that column -- so a long-past fixture must not still read as Live."""
    stale_id, *_ = _seed_match(
        engine, make_team, make_fixture, 7990,
        kickoff_time=NOW() - timedelta(minutes=LIVE_WINDOW_MINUTES + 20), finished=False,
    )

    row = next(f for f in _get_fixtures(season=TEST_SEASON).json() if f["fixture_id"] == stale_id)

    assert row["finished"] is False   # the flag really is still unset...
    assert row["status"] == "completed"  # ...but it stopped being Live anyway
    assert row["has_started"] is True


def test_get_fixtures_status_still_live_just_inside_the_window(engine, make_team, make_fixture):
    fresh_id, *_ = _seed_match(
        engine, make_team, make_fixture, 7995,
        kickoff_time=NOW() - timedelta(minutes=LIVE_WINDOW_MINUTES - 20), finished=False,
    )

    row = next(f for f in _get_fixtures(season=TEST_SEASON).json() if f["fixture_id"] == fresh_id)

    assert row["status"] == "live"


def test_get_fixtures_status_upcoming_when_kickoff_unknown(engine, make_team, make_fixture):
    tbc_id, *_ = _seed_match(engine, make_team, make_fixture, 7998, kickoff_time=None)

    row = next(f for f in _get_fixtures(season=TEST_SEASON).json() if f["fixture_id"] == tbc_id)

    assert row["status"] == "upcoming"


def test_get_fixtures_returns_scores_when_played(engine, make_team, make_fixture):
    played_id, *_ = _seed_match(engine, make_team, make_fixture, 7950, kickoff_time=NOW() - timedelta(days=1), finished=True)
    unplayed_id, *_ = _seed_match(engine, make_team, make_fixture, 7960, gameweek=2, kickoff_time=NOW() + timedelta(days=1))
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE ml.fixtures SET home_score = 1, away_score = 1 WHERE id = :f"), {"f": played_id}
        )

    body = {f["fixture_id"]: f for f in _get_fixtures(season=TEST_SEASON).json()}

    assert body[played_id]["home_score"] == 1 and body[played_id]["away_score"] == 1
    # None, not 0 -- an unplayed match has no score, which isn't the same as 0-0.
    assert body[unplayed_id]["home_score"] is None
    assert body[unplayed_id]["away_score"] is None


def test_get_fixtures_user_performance(engine, make_user, make_team, make_fixture):
    """Backs the "Your Performance -- Rank 3rd of 10 -- 42 pts" line."""
    owner = make_user()
    rival = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 8400)
    contest_id = _seed_contest(engine, owner, fixture_id)
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO dream11.contest_members (contest_id, user_id, total_points, rank) VALUES (:c, :u, 0, 0)"),
            {"c": contest_id, "u": rival},
        )
        conn.execute(
            text("UPDATE dream11.contest_members SET total_points = 42, rank = 3 WHERE contest_id = :c AND user_id = :u"),
            {"c": contest_id, "u": owner},
        )

    row = next(f for f in _get_fixtures(season=TEST_SEASON, user_id=owner).json() if f["fixture_id"] == fixture_id)

    assert row["user_points"] == 42
    assert row["user_rank"] == 3
    assert row["user_contest_size"] == 2  # the "of 10" half of the line
    assert row["user_contest_id"] == contest_id


def test_get_fixtures_user_performance_picks_best_ranked_contest(engine, make_user, make_team, make_fixture):
    """One line per match, but a user can hold teams in several contests on it."""
    owner = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 8500)
    worse = _seed_contest(engine, owner, fixture_id, name="Worse")
    better = _seed_contest(engine, owner, fixture_id, name="Better")
    with engine.begin() as conn:
        conn.execute(text("UPDATE dream11.contest_members SET total_points = 10, rank = 7 WHERE contest_id = :c"), {"c": worse})
        conn.execute(text("UPDATE dream11.contest_members SET total_points = 55, rank = 1 WHERE contest_id = :c"), {"c": better})

    row = next(f for f in _get_fixtures(season=TEST_SEASON, user_id=owner).json() if f["fixture_id"] == fixture_id)

    assert row["user_contest_id"] == better
    assert row["user_rank"] == 1 and row["user_points"] == 55
    assert row["user_contest_count"] == 2


def test_get_fixtures_user_performance_prefers_a_scored_contest_over_an_unscored_one(engine, make_user, make_team, make_fixture):
    """rank = 0 is the not-scored-yet default, so it must sort behind any real
    rank rather than winning as the numerically smallest."""
    owner = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 8550)
    unscored = _seed_contest(engine, owner, fixture_id, name="Unscored")  # stays rank 0
    scored = _seed_contest(engine, owner, fixture_id, name="Scored")
    with engine.begin() as conn:
        conn.execute(text("UPDATE dream11.contest_members SET total_points = 12, rank = 4 WHERE contest_id = :c"), {"c": scored})

    row = next(f for f in _get_fixtures(season=TEST_SEASON, user_id=owner).json() if f["fixture_id"] == fixture_id)

    assert row["user_contest_id"] == scored
    assert row["user_rank"] == 4
    assert unscored != scored


def test_get_fixtures_user_performance_empty_for_a_viewer_with_no_contest(engine, make_user, make_team, make_fixture):
    owner = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 8600)
    _seed_contest(engine, owner, fixture_id)

    row = next(f for f in _get_fixtures(season=TEST_SEASON).json() if f["fixture_id"] == fixture_id)

    assert row["user_contest_id"] is None
    assert row["user_points"] == 0 and row["user_rank"] == 0 and row["user_contest_size"] == 0


# ------------------------------------- GET /dream11/fixtures/{id}/contests

def test_fixture_contests_lists_only_this_fixtures_contests(engine, make_user, make_team, make_fixture):
    owner = make_user()
    fixture_a, *_ = _seed_match(engine, make_team, make_fixture, 8000)
    fixture_b, *_ = _seed_match(engine, make_team, make_fixture, 8100, gameweek=2)
    contest_a = _seed_contest(engine, owner, fixture_a, name="On A")
    contest_b = _seed_contest(engine, owner, fixture_b, name="On B")

    resp = client.get(f"/dream11/fixtures/{fixture_a}/contests", headers=bearer_headers(owner))

    assert resp.status_code == 200
    ids = [c["contest_id"] for c in resp.json()]
    assert contest_a in ids
    assert contest_b not in ids


def test_fixture_contests_excludes_contests_the_user_has_not_joined(engine, make_user, make_team, make_fixture):
    owner = make_user()
    outsider = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 8200)
    _seed_contest(engine, owner, fixture_id)

    resp = client.get(f"/dream11/fixtures/{fixture_id}/contests", headers=bearer_headers(outsider))

    assert resp.status_code == 200
    assert resp.json() == []

    # And a stale user_id naming the owner does not widen it -- the token
    # scopes this endpoint, the parameter is gone.
    spoofed = client.get(
        f"/dream11/fixtures/{fixture_id}/contests",
        params={"user_id": owner},
        headers=bearer_headers(outsider),
    )
    assert spoofed.status_code == 200
    assert spoofed.json() == []


def test_fixture_contests_carry_the_full_summary(engine, make_user, make_team, make_fixture):
    owner = make_user()
    fixture_id, *_ = _seed_match(engine, make_team, make_fixture, 8300, kickoff_time=NOW() + timedelta(days=1))
    contest_id = _seed_contest(engine, owner, fixture_id, name="Match Night")

    row = client.get(f"/dream11/fixtures/{fixture_id}/contests", headers=bearer_headers(owner)).json()[0]

    assert row["contest_id"] == contest_id
    assert row["name"] == "Match Night"
    assert row["fixture_id"] == fixture_id
    assert row["member_count"] == 1
    assert row["user_is_member"] is True
    assert row["home_team"] and row["away_team"]
    assert row["kickoff_time"] is not None


def test_fixture_contests_unknown_fixture_is_empty_not_an_error(engine, make_user):
    resp = client.get("/dream11/fixtures/99999999/contests", headers=bearer_headers(make_user()))

    assert resp.status_code == 200
    assert resp.json() == []
