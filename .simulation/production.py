"""Import shims for production code used by the simulator."""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
for subdir in ("Data", "Game_logic", "Worker", "Feature_engineering", "Predict"):
    path = BACKEND / subdir
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))


def get_engine():
    from Shared.db_utils import get_engine as _get_engine

    return _get_engine()


def score_gameweek(engine, season: str, gameweek: int) -> dict:
    from Results.scoring import score_gameweek as _score_gameweek

    return _score_gameweek(engine, season, gameweek)


def compute_league_standings(engine, season: str, gameweek: int) -> dict:
    from Results.standings import compute_league_standings as _compute

    return _compute(engine, season, gameweek)


def lock_expired_gameweeks(engine) -> dict:
    from GameEngine.gameweek_lock import lock_expired_gameweeks as _lock

    return _lock(engine)
