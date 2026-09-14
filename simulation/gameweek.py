"""Complete gameweek simulation using production scoring and standings."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from time import perf_counter
import random

from .clock import SimulationClock
from .db import SIM_SEASON, force_deadline_passed, seed_basic_gameweek, seed_transfer_history
from .events import MatchEvent, MatchEventType
from .lifecycle import GameweekStatus, transition_gameweek
from .logging import correlation_id, log_event
from .match import simulate_match
from .production import compute_league_standings, lock_expired_gameweeks, score_gameweek


@dataclass
class GameweekSimulationResult:
    season: str
    gameweek: int
    status: GameweekStatus
    users: int
    score_summary: dict
    standings_summary: dict
    metrics: dict


def simulate_gameweek(
    engine,
    gameweek_id: int,
    users: int = 100,
    season: str = SIM_SEASON,
    seed: int = 12345,
    accelerated: bool = True,
) -> GameweekSimulationResult:
    correlation = correlation_id()
    status = GameweekStatus.DRAFT
    log_event("Gameweek simulation started", correlation, season=season, gameweek=gameweek_id, users=users)

    started = perf_counter()
    kickoff = SimulationClock.now() + timedelta(days=1)
    seeded = seed_basic_gameweek(engine, season, gameweek_id, users, seed, kickoff)
    status = transition_gameweek(status, GameweekStatus.OPEN)
    transfers_written = seed_transfer_history(engine, seeded["users"], season, gameweek_id, seed)

    if accelerated:
        SimulationClock.advance(days=1, minutes=1)
        force_deadline_passed(engine, season, gameweek_id)
    status = transition_gameweek(status, GameweekStatus.LOCKED)
    lock_summary = lock_expired_gameweeks(engine)
    log_event("Gameweek locked", correlation, lock_summary=lock_summary)

    status = transition_gameweek(status, GameweekStatus.LIVE)
    events = _deterministic_events(list(seeded["players"].keys()), seed)
    simulate_match(engine, seeded["fixture_id"], events, correlation)

    status = transition_gameweek(status, GameweekStatus.PROCESSING)
    score_started = perf_counter()
    score_summary = score_gameweek(engine, season, gameweek_id)
    score_seconds = perf_counter() - score_started
    standings_started = perf_counter()
    standings_summary = compute_league_standings(engine, season, gameweek_id)
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
    log_event("Gameweek finalized", correlation, score=score_summary, standings=standings_summary, metrics=metrics)
    return GameweekSimulationResult(season, gameweek_id, status, users, score_summary, standings_summary, metrics)


def _deterministic_events(player_ids: list[int], seed: int) -> list[MatchEvent]:
    rng = random.Random(seed)
    selected = rng.sample(player_ids, 22)
    events = [MatchEvent(MatchEventType.KICKOFF, minute=0)]
    for player_id in selected:
        events.append(MatchEvent(MatchEventType.APPEARANCE, player_id=player_id, minute=1, metadata={"minutes": 90}))
    events.extend(
        [
            MatchEvent(MatchEventType.GOAL, player_id=selected[5], minute=12, provider_event_id=f"SIM-GW-{seed}-GOAL-1"),
            MatchEvent(MatchEventType.ASSIST, player_id=selected[6], minute=12, provider_event_id=f"SIM-GW-{seed}-AST-1"),
            MatchEvent(MatchEventType.YELLOW_CARD, player_id=selected[7], minute=40, provider_event_id=f"SIM-GW-{seed}-YC-1"),
            MatchEvent(MatchEventType.MATCH_FINISHED, minute=90),
        ]
    )
    return events
