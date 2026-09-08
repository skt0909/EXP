"""
conftest.py — shared pytest fixtures for the FPL project's test suite.

Uses a real Postgres instance, not mocks -- this matches the "real data,
real output" principle used throughout this project's manual verification.
TEST_DATABASE_URL (a dedicated `fpl_game_test` database, schema created via
`alembic upgrade head`) is used when set, so test runs can never touch dev
data; DATABASE_URL is only a fallback for environments that haven't set up
a separate test database yet. ml-schema fixture data additionally lives
under a dedicated fake season (TEST_SEASON) so it can never collide with
real ingested data even when running against a shared database; it's wiped
before AND after every test (defensive on both ends) via cascading foreign
keys.
"""

import os
import sys
import uuid
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# Explicitly on sys.path (not left to incidental cwd insertion from
# `python -m pytest`) because Game_logic.db_utils/squad_selection/
# starting_xi are now imported by absolute dotted path (e.g.
# `from Game_logic.db_utils import get_engine`), which needs the backend
# directory itself findable, not just each module's own directory.
sys.path.insert(0, str(_ROOT))
for _sub in ("Feature_engineering", "Predict", "Context_assembler", "Worker", "Game_logic", "Data"):
    sys.path.insert(0, str(_ROOT / _sub))

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

# Safe at module level, and checked rather than assumed: create_access_token
# only signs a payload, and auth._jwt_secret() is called INSIDE it, never at
# import time. So importing this cannot make a JWT_SECRET-less environment
# fail a run that never authenticates -- which matters, because this file is
# imported by every test in the repo.
from Data.auth import create_access_token

# .env lives at the true project root, above backend/ -- same unbounded
# walk-up pattern every db_utils.py in this project uses, so this stays
# correct regardless of how deep this file is nested.
for _d in [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents):
    _candidate = _d / ".env"
    if _candidate.is_file():
        load_dotenv(_candidate)
        break
else:
    load_dotenv()

# .env.test lives next to .env at the project root and carries only
# TEST_DATABASE_URL. Loaded here (module level, before any test imports
# `main` or a Game_logic module) so it lands in this process's real
# os.environ -- every db_utils.py's get_engine() picks it up too, since
# they all check TEST_DATABASE_URL first. This only affects the pytest
# process; a separately-running dev `uvicorn` process never sees it.
for _d in [Path(__file__).resolve().parent] + list(Path(__file__).resolve().parents):
    _candidate = _d / ".env.test"
    if _candidate.is_file():
        load_dotenv(_candidate)
        break

TEST_SEASON = "9999-00"


@pytest.fixture(scope="session")
def engine():
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    return create_engine(url, pool_pre_ping=True)


@pytest.fixture(autouse=True)
def _clean_ml_test_data(engine):
    def _wipe():
        with engine.begin() as conn:
            # players cascades to player_gw_stats + ml_predictions
            conn.execute(text("DELETE FROM ml.players WHERE season = :s"), {"s": TEST_SEASON})
            conn.execute(text("DELETE FROM ml.fixtures WHERE season = :s"), {"s": TEST_SEASON})
            conn.execute(text("DELETE FROM ml.teams WHERE season = :s"), {"s": TEST_SEASON})

    _wipe()
    yield
    _wipe()


# --- identity ---------------------------------------------------------
#
# Added for the auth cutover: from Stage 2 every endpoint takes its user
# from a bearer token instead of a parameter, so all 13 endpoint-testing
# files need an identity and a way to present it. Six of them already had
# a private make_user; those now delegate here (see below) rather than
# growing a seventh copy.


@pytest.fixture
def make_user(engine, request):
    """Creates uuid-unique users and remembers them on `_make.created`.

    CREATION ONLY -- this fixture deliberately tears nothing down, and the
    six files that already had their own copy still own their cleanup.
    That is not laziness, it is that no single teardown is correct for all
    of them: test_dream11/test_fixtures/test_transfer_drafts delete the
    users row outright, test_beat_scheduling and test_gameweek_lifecycle
    must NOT (enforce_transfers_immutability_fn blocks deleting a user who
    has transferred, which is why those two delete the game rows and leave
    the user behind), and test_leagues deliberately cleans up nothing at
    all. Each of those files overrides this fixture BY THE SAME NAME,
    receives it as an argument, and adds its own teardown -- so the INSERT
    lives in one place while every cleanup rule stays exactly where it was
    written and documented.

    Users are never deleted here for the same reason: uuid identity is what
    makes an undeletable row harmless.

    The default prefix is derived from the calling module, so a stray row
    still names the file that made it; callers that want a specific one
    (the overriding fixtures below, preserving their historical prefixes)
    pass it explicitly.
    """
    created: list[int] = []

    def _make(prefix: str | None = None) -> int:
        # [:24] keeps the longest module name inside users.username's
        # varchar(50) alongside the 7-char tag and 12-char uuid.
        label = prefix or request.module.__name__.replace("test_", "", 1)[:24]
        unique = uuid.uuid4().hex[:12]
        with engine.begin() as conn:
            uid = conn.execute(
                text("INSERT INTO users (email, username, password_hash) VALUES (:e, :u, :p) RETURNING id"),
                {
                    "e": f"pytest_{label}_{unique}@example.com",
                    "u": f"pytest_{label}_{unique}",
                    "p": "not_a_real_hash",
                },
            ).scalar()
        created.append(uid)
        return uid

    _make.created = created
    return _make


def bearer_headers(user_id: int) -> dict[str, str]:
    """Bearer header for a user id, as a plain importable function.

    Exists alongside the auth_headers fixture below because this suite is
    full of MODULE-LEVEL request helpers -- test_transfer_drafts._delete,
    test_starting_xi._base_payload's callers, and so on -- and a plain
    function is the only thing they can reach: a fixture is only injectable
    into a test, so threading one down to those helpers would mean adding a
    parameter to every helper and then to every one of their callers.

    Same value either way; the fixture is the nicer form inside a test body,
    this is the form a helper can import.
    """
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


@pytest.fixture
def auth_headers():
    """Bearer header for a user id, for call sites that add one argument.

    Mints the token directly rather than going through POST /auth/login,
    which CANNOT work here: make_user writes password_hash literally as
    'not_a_real_hash', and auth.verify_password returns False rather than
    raising on a malformed digest, so every test user is deliberately
    un-loggable. create_access_token needs only the id, and
    get_current_user needs only the users row to exist.
    """
    return bearer_headers


@pytest.fixture
def authed_client(auth_headers):
    """A TestClient that IS the given user for every call it makes.

    Suits the long single-identity flows (test_gameweek_lifecycle.py,
    test_transfers.py); auth_headers suits one-off calls. Call it twice for
    the multi-user tests in test_leagues.py and test_dream11.py.

    `main` is imported inside the fixture, not at module scope: it builds
    the whole FastAPI app and pulls in groq_client, and this conftest is
    imported by pure-unit test files that should not have to pay for that.
    """
    from fastapi.testclient import TestClient
    from main import app

    return lambda user_id: TestClient(app, headers=auth_headers(user_id))


@pytest.fixture
def make_team(engine):
    def _make(fpl_id: int, name: str, short_name: str) -> int:
        with engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO ml.teams (fpl_id, season, name, short_name) "
                    "VALUES (:fpl_id, :season, :name, :short_name) RETURNING id"
                ),
                {"fpl_id": fpl_id, "season": TEST_SEASON, "name": name, "short_name": short_name},
            ).scalar()

    return _make


@pytest.fixture
def make_player(engine):
    def _make(
        fpl_id: int,
        position: str = "MID",
        position_encoded: int = 2,
        team_id: int | None = None,
        status: str = "a",
        web_name: str | None = None,
        cost_start: int = 0,
        now_cost: int | None = None,
    ) -> int:
        web_name = web_name or f"TestPlayer{fpl_id}"
        now_cost = cost_start if now_cost is None else now_cost
        with engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO ml.players (fpl_id, season, fpl_name, web_name, position, "
                    "position_encoded, team_id, status, cost_start, now_cost) VALUES "
                    "(:fpl_id, :season, :name, :name, :position, :pos_enc, :team_id, :status, :cost_start, :now_cost) "
                    "RETURNING id"
                ),
                {
                    "fpl_id": fpl_id,
                    "season": TEST_SEASON,
                    "name": web_name,
                    "position": position,
                    "pos_enc": position_encoded,
                    "team_id": team_id,
                    "status": status,
                    "cost_start": cost_start,
                    "now_cost": now_cost,
                },
            ).scalar()

    return _make


@pytest.fixture
def make_fixture(engine):
    def _make(
        fpl_id: int,
        gameweek: int,
        home_team_id: int,
        away_team_id: int,
        kickoff_time=None,
        finished: bool = False,
    ) -> int:
        with engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, away_team_id, kickoff_time, finished) "
                    "VALUES (:fpl_id, :season, :gw, :home, :away, :kickoff_time, :finished) RETURNING id"
                ),
                {
                    "fpl_id": fpl_id,
                    "season": TEST_SEASON,
                    "gw": gameweek,
                    "home": home_team_id,
                    "away": away_team_id,
                    "kickoff_time": kickoff_time,
                    "finished": finished,
                },
            ).scalar()

    return _make


@pytest.fixture
def make_gw_stat(engine):
    """fixture_id defaults to None, which is correct for the classic-FPL
    tests that dominate this fixture's callers: their scoring joins match on
    (player_id, season, gameweek) and a gameweek score sums across every
    match played. Dream11's joins are scoped to one fixture, so its tests
    must pass fixture_id explicitly or the row matches nothing."""
    def _make(player_id: int, gameweek: int, fixture_id: int | None = None, **overrides) -> None:
        row = dict(
            minutes=90,
            goals_scored=0,
            assists=0,
            clean_sheets=0,
            saves=0,
            bonus=0,
            bps=0,
            ict_index=0,
            expected_goals=0,
            expected_assists=0,
            expected_goal_involvements=0,
            total_points=0,
            value=50,
            selected=1000,
            was_home=True,
            defensive_contributions=0,
        )
        row.update(overrides)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO ml.player_gw_stats (player_id, season, gameweek, fixture_id, {cols}) "
                    f"VALUES (:player_id, :season, :gw, :fixture_id, {placeholders})"
                ),
                {
                    "player_id": player_id, "season": TEST_SEASON, "gw": gameweek,
                    "fixture_id": fixture_id, **row,
                },
            )

    return _make
