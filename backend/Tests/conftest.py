"""
conftest.py — shared pytest fixtures for the FPL project's test suite.

Uses the real local Postgres instance (DATABASE_URL from the project
.env), not mocks -- this matches the "real data, real output" principle
used throughout this project's manual verification. ml-schema fixture
data lives under a dedicated fake season (TEST_SEASON) so it can never
collide with real ingested data; it's wiped before AND after every test
(defensive on both ends) via cascading foreign keys.
"""

import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
# Explicitly on sys.path (not left to incidental cwd insertion from
# `python -m pytest`) because Game_logic.db_utils/squad_selection/
# starting_xi are now imported by absolute dotted path (e.g.
# `from Game_logic.db_utils import get_engine`), which needs the backend
# directory itself findable, not just each module's own directory.
sys.path.insert(0, str(_ROOT))
for _sub in ("Feature_engineering", "Predict", "Context_assembler", "Worker", "Game_logic", "Data_ingestion"):
    sys.path.insert(0, str(_ROOT / _sub))

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

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

TEST_SEASON = "9999-00"


@pytest.fixture(scope="session")
def engine():
    return create_engine(os.getenv("DATABASE_URL"), pool_pre_ping=True)


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
    ) -> int:
        web_name = web_name or f"TestPlayer{fpl_id}"
        with engine.begin() as conn:
            return conn.execute(
                text(
                    "INSERT INTO ml.players (fpl_id, season, fpl_name, web_name, position, "
                    "position_encoded, team_id, status, cost_start) VALUES "
                    "(:fpl_id, :season, :name, :name, :position, :pos_enc, :team_id, :status, :cost_start) "
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
    def _make(player_id: int, gameweek: int, **overrides) -> None:
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
        )
        row.update(overrides)
        cols = ", ".join(row.keys())
        placeholders = ", ".join(f":{k}" for k in row.keys())
        with engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO ml.player_gw_stats (player_id, season, gameweek, {cols}) "
                    f"VALUES (:player_id, :season, :gw, {placeholders})"
                ),
                {"player_id": player_id, "season": TEST_SEASON, "gw": gameweek, **row},
            )

    return _make
