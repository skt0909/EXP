"""Compatibility command shim for simulation management commands.

This project is FastAPI-based rather than Django-based, but the simulation
spec asks for `python manage.py ...` command names. The shim maps those names
to the real simulation CLI without introducing a Django dependency.
"""

from __future__ import annotations

import sys

from simulation.cli import main


ALIASES = {
    "simulate_match": "simulate-match",
    "simulate_gameweek": "simulate-gameweek",
    "simulate_season": "simulate-season",
    "advance_game_clock": "advance-game-clock",
    "generate_test_users": "generate-test-users",
    "load_test": "load-test",
}


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] in ALIASES:
        sys.argv[1] = ALIASES[sys.argv[1]]
    main()
