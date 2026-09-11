"""Season-level simulation orchestration."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from time import perf_counter

from .clock import SimulationClock
from .db import SIM_SEASON, force_deadline_passed, seed_gameweek_from_world, seed_simulation_world, seed_transfer_history
from .gameweek import GameweekSimulationResult
from .gameweek import _deterministic_events
from .lifecycle import GameweekStatus, transition_gameweek
from .logging import correlation_id, log_event
from .match import simulate_match
from .production import compute_league_standings, lock_expired_gameweeks, score_gameweek


@dataclass
class SeasonSimulationResult:
    season: str
    gameweeks: int
    users: int
    results: list[GameweekSimulationResult] = field(default_factory=list)


def simulate_season(
    engine,
    season_id: str = SIM_SEASON,
    gameweeks: int = 38,
    users: int = 100,
    seed: int = 12345,
) -> SeasonSimulationResult:
    result = SeasonSimulationResult(season_id, gameweeks, users)
    world = seed_simulation_world(engine, season_id, users, seed, SimulationClock.now() + timedelta(days=1))
    for gameweek in range(1, gameweeks + 1):
        result.results.append(_simulate_world_gameweek(engine, world, season_id, gameweek, users, seed + gameweek))
    return result


def _simulate_world_gameweek(engine, world: dict, season: str, gameweek: int, users: int, seed: int) -> GameweekSimulationResult:
    correlation = correlation_id()
    started = perf_counter()
    status = transition_gameweek(GameweekStatus.DRAFT, GameweekStatus.OPEN)
    kickoff = SimulationClock.now() + timedelta(days=gameweek)
    fixture_id = seed_gameweek_from_world(engine, world, season, gameweek, kickoff)
    transfers_written = seed_transfer_history(engine, world["users"], season, gameweek, seed)
    SimulationClock.advance(days=1, minutes=1)
    status = transition_gameweek(status, GameweekStatus.LOCKED)
    force_deadline_passed(engine, season, gameweek)
    lock_expired_gameweeks(engine)
    status = transition_gameweek(status, GameweekStatus.LIVE)
    events = _deterministic_events(list(world["players"].keys()), seed)
    simulate_match(engine, fixture_id, events, correlation)
    status = transition_gameweek(status, GameweekStatus.PROCESSING)
    score_started = perf_counter()
    score_summary = score_gameweek(engine, season, gameweek)
    score_seconds = perf_counter() - score_started
    standings_started = perf_counter()
    standings_summary = compute_league_standings(engine, season, gameweek)
    standings_seconds = perf_counter() - standings_started
    status = transition_gameweek(status, GameweekStatus.FINISHED)
    metrics = {
        "duration_seconds": perf_counter() - started,
        "score_seconds": score_seconds,
        "leaderboard_seconds": standings_seconds,
        "users": users,
        "transfers_written": transfers_written,
        "events_processed": len(events),
    }
    log_event("Season gameweek finalized", correlation, season=season, gameweek=gameweek, metrics=metrics)
    return GameweekSimulationResult(season, gameweek, status, users, score_summary, standings_summary, metrics)
