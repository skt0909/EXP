"""CLI entry points for local simulation runs.

Examples:
    python -m simulation.cli simulate-gameweek --gameweek 5 --users 1000
    python -m simulation.cli simulate-season --gameweeks 38 --users 10000 --seed 123
    python -m simulation.cli advance-game-clock --minutes 30
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict

from .clock import SimulationClock
from .db import create_player_pool, create_teams, reset_simulation_data, simulate_users
from .events import Assist, Goal, MatchEvent, MatchEventType, YellowCard
from .gameweek import simulate_gameweek
from .load import run_load_levels
from .match import simulate_match
from .production import get_engine
from .safety import assert_simulation_environment
from .season import simulate_season


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(prog="python -m simulation.cli")
    parser.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    sub = parser.add_subparsers(dest="command", required=True)

    match = sub.add_parser("simulate-match")
    match.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    match.add_argument("--match-id", type=int, required=True)
    match.add_argument("--sample-events", action="store_true", help="Use a deterministic sample event list.")

    gw = sub.add_parser("simulate-gameweek")
    gw.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    gw.add_argument("--gameweek", type=int, required=True)
    gw.add_argument("--users", type=int, default=100)
    gw.add_argument("--season", default="SIM-2026")
    gw.add_argument("--seed", type=int, default=12345)

    season = sub.add_parser("simulate-season")
    season.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    season.add_argument("--gameweeks", type=int, default=38)
    season.add_argument("--users", type=int, default=100)
    season.add_argument("--season", default="SIM-2026")
    season.add_argument("--seed", type=int, default=12345)

    clock = sub.add_parser("advance-game-clock")
    clock.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    clock.add_argument("--minutes", type=int, default=0)
    clock.add_argument("--hours", type=int, default=0)
    clock.add_argument("--days", type=int, default=0)

    gen = sub.add_parser("generate-test-users")
    gen.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    gen.add_argument("--users", type=int, required=True)
    gen.add_argument("--season", default="SIM-2026")
    gen.add_argument("--seed", type=int, default=12345)

    load = sub.add_parser("load-test")
    load.add_argument("--force", action="store_true", help="Bypass ENVIRONMENT/database safety checks.")
    load.add_argument("--max-level", type=int, default=1, choices=[1, 2, 3, 4, 5])
    load.add_argument("--seed", type=int, default=12345)

    args = parser.parse_args()
    assert_simulation_environment(force=args.force)

    if args.command == "advance-game-clock":
        print(json.dumps({"now": SimulationClock.advance(minutes=args.minutes, hours=args.hours, days=args.days).isoformat()}))
        return

    engine = get_engine()
    if args.command == "simulate-match":
        events = _sample_events() if args.sample_events else [MatchEvent(MatchEventType.KICKOFF, minute=0), MatchEvent(MatchEventType.MATCH_FINISHED, minute=90)]
        print(json.dumps(asdict(simulate_match(engine, args.match_id, events)), default=str))
    elif args.command == "generate-test-users":
        reset_simulation_data(engine, args.season)
        teams = create_teams(engine, args.season)
        player_map = create_player_pool(engine, args.season, teams)
        users = simulate_users(engine, args.users, args.season, list(player_map), args.seed)
        print(json.dumps({"season": args.season, "users": len(users), "players": len(player_map)}))
    if args.command == "simulate-gameweek":
        result = simulate_gameweek(engine, args.gameweek, args.users, args.season, args.seed)
        print(json.dumps(asdict(result), default=str))
    elif args.command == "simulate-season":
        result = simulate_season(engine, args.season, args.gameweeks, args.users, args.seed)
        print(json.dumps(asdict(result), default=str))
    elif args.command == "load-test":
        result = run_load_levels(engine, args.max_level, seed=args.seed)
        print(json.dumps(asdict(result), default=str))


def _sample_events() -> list[MatchEvent]:
    return [
        MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="SIM-SAMPLE-KO"),
        Goal(player_id=60005, minute=12, provider_event_id="SIM-SAMPLE-GOAL"),
        Assist(player_id=60006, minute=12, provider_event_id="SIM-SAMPLE-ASSIST"),
        YellowCard(player_id=60007, minute=40, provider_event_id="SIM-SAMPLE-YELLOW"),
        MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="SIM-SAMPLE-FT"),
    ]


if __name__ == "__main__":
    main()
