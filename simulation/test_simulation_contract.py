"""Fast contract tests for the simulation-only layer.

These tests do not require a database; database-backed end-to-end runs should
use `python -m simulation.cli ...` against a dedicated simulation DB.
"""

from datetime import datetime, timezone

import pytest

from simulation.clock import SimulationClock
from simulation.events import Goal, MatchEvent, MatchEventType
from simulation.lifecycle import (
    GAMEWEEK_TRANSITIONS,
    MATCH_TRANSITIONS,
    GameweekStatus,
    MatchStatus,
    transition_gameweek,
    transition_match,
)
from simulation.safety import assert_simulation_environment


def test_simulation_clock_advances_without_system_clock():
    SimulationClock.set(datetime(2026, 1, 1, tzinfo=timezone.utc))
    assert SimulationClock.advance(minutes=30).minute == 30


def test_invalid_match_transition_is_rejected():
    with pytest.raises(ValueError):
        transition_match(MatchStatus.FINISHED, MatchStatus.LIVE)


def test_invalid_gameweek_transition_is_rejected():
    with pytest.raises(ValueError):
        transition_gameweek(GameweekStatus.FINISHED, GameweekStatus.LIVE)


def test_event_ids_can_be_deterministic_for_duplicate_tests():
    first = Goal(player_id=10, minute=12, provider_event_id="ABC123")
    second = Goal(player_id=10, minute=12, provider_event_id="ABC123")
    assert first.provider_event_id == second.provider_event_id


def test_all_required_match_event_types_are_supported():
    required = {
        "KICKOFF",
        "GOAL",
        "ASSIST",
        "OWN_GOAL",
        "PENALTY",
        "PENALTY_MISS",
        "PENALTY_SAVE",
        "YELLOW_CARD",
        "RED_CARD",
        "CLEAN_SHEET",
        "SUBSTITUTION",
        "APPEARANCE",
        "INJURY_SUBSTITUTION",
        "MATCH_ABANDONED",
        "MATCH_POSTPONED",
        "MATCH_FINISHED",
    }
    assert required <= {event.value for event in MatchEventType}


def test_every_declared_valid_match_transition_is_accepted():
    for current, targets in MATCH_TRANSITIONS.items():
        for target in targets:
            assert transition_match(current, target) == target


def test_every_declared_valid_gameweek_transition_is_accepted():
    for current, targets in GAMEWEEK_TRANSITIONS.items():
        for target in targets:
            assert transition_gameweek(current, target) == target


def test_safety_requires_simulation_environment(monkeypatch):
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/fantasy_simulation")
    with pytest.raises(RuntimeError, match="ENVIRONMENT=simulation"):
        assert_simulation_environment()


def test_safety_rejects_production_database_url(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "simulation")
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://localhost/production")
    with pytest.raises(RuntimeError, match="production-looking"):
        assert_simulation_environment()
