"""Production safety checks for simulation commands."""

from __future__ import annotations

import os


PRODUCTION_MARKERS = {"production", "prod", "live"}


def assert_simulation_environment(force: bool = False) -> None:
    env = os.getenv("ENVIRONMENT", "").strip().lower()
    database_url = os.getenv("DATABASE_URL", "").strip().lower()
    test_database_url = os.getenv("TEST_DATABASE_URL", "").strip().lower()

    if force:
        return
    if env != "simulation":
        raise RuntimeError("Refusing to run simulation unless ENVIRONMENT=simulation is set.")
    url = test_database_url or database_url
    if any(marker in url for marker in PRODUCTION_MARKERS):
        raise RuntimeError("Refusing to run simulation against a production-looking database URL.")
    if "fantasy_simulation" not in url and "test" not in url:
        raise RuntimeError("Simulation database URL must contain 'fantasy_simulation' or 'test'.")
