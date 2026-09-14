"""Database-backed simulation integration tests."""

from datetime import datetime, timezone
import os
import sys
from pathlib import Path

import pytest
from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy import text

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

for candidate in (ROOT / ".env.test", ROOT / ".env"):
    if candidate.is_file():
        load_dotenv(candidate)

from simulation.db import create_fixture, create_player_pool, create_teams, reset_simulation_data
from simulation.events import Goal, MatchEvent, MatchEventType
from simulation.gameweek import simulate_gameweek
from simulation.match import simulate_match


SIM_TEST_SEASON = "SIM-TST"


@pytest.fixture(scope="session")
def engine():
    url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL/DATABASE_URL is not configured")
    return create_engine(url, pool_pre_ping=True)


@pytest.fixture
def sim_world(engine):
    reset_simulation_data(engine, SIM_TEST_SEASON)
    teams = create_teams(engine, SIM_TEST_SEASON, count=2)
    players = create_player_pool(engine, SIM_TEST_SEASON, teams, pool_size=30)
    yield {"teams": teams, "players": players}
    reset_simulation_data(engine, SIM_TEST_SEASON)


def test_duplicate_provider_event_is_processed_once(engine, sim_world):
    fixture_id = create_fixture(
        engine,
        SIM_TEST_SEASON,
        1,
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        sim_world["teams"][0],
        sim_world["teams"][1],
    )
    player_id = next(iter(sim_world["players"]))
    duplicate = Goal(player_id=player_id, minute=12, provider_event_id="SIM-DUP-GOAL")

    first = simulate_match(
        engine,
        fixture_id,
        [
            MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="SIM-DUP-KO"),
            duplicate,
            duplicate,
            MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="SIM-DUP-FT"),
        ],
    )
    second = simulate_match(engine, fixture_id, [duplicate])

    assert first.skipped_duplicate_event_ids == ["SIM-DUP-GOAL"]
    assert second.skipped_duplicate_event_ids == ["SIM-DUP-GOAL"]
    with engine.connect() as conn:
        event_count = conn.execute(
            text("SELECT COUNT(*) FROM simulation.football_events WHERE provider_event_id = 'SIM-DUP-GOAL'")
        ).scalar()
        goals = conn.execute(
            text(
                "SELECT goals_scored FROM ml.player_gw_stats s "
                "JOIN ml.players p ON p.id = s.player_id "
                "WHERE p.season = :season AND p.fpl_id = :player_id"
            ),
            {"season": SIM_TEST_SEASON, "player_id": player_id},
        ).scalar()
    assert event_count == 1
    assert goals == 1


def test_complete_gameweek_simulation_scores_users_and_leaderboard(engine):
    result = simulate_gameweek(engine, 1, users=10, season="SIMGWE2E", seed=42, accelerated=True)

    assert result.status.value == "FINISHED"
    assert len(result.score_summary["scored"]) == 10
    assert result.score_summary["failed"] == []
    assert result.standings_summary["failed"] == []
    assert result.metrics["users"] == 10
