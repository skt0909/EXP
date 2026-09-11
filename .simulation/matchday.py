"""Matchday simulation orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field

from .events import MatchEvent
from .logging import correlation_id, log_event
from .match import MatchSimulationResult, simulate_match


@dataclass
class MatchdaySimulationResult:
    fixture_ids: list[int]
    results: list[MatchSimulationResult] = field(default_factory=list)


def simulate_matchday(engine, fixture_events: dict[int, list[MatchEvent]]) -> MatchdaySimulationResult:
    correlation = correlation_id()
    result = MatchdaySimulationResult(list(fixture_events))
    log_event("Matchday simulation started", correlation, fixtures=result.fixture_ids)
    for fixture_id, events in fixture_events.items():
        result.results.append(simulate_match(engine, fixture_id, events, correlation))
    log_event("Matchday simulation finished", correlation, fixtures=result.fixture_ids)
    return result
