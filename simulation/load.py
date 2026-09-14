"""Progressive load-test helpers for simulation runs."""

from __future__ import annotations

from dataclasses import dataclass, field

from .gameweek import simulate_gameweek


LOAD_LEVELS = {
    1: 100,
    2: 1_000,
    3: 10_000,
    4: 100_000,
    5: 1_000_000,
}


@dataclass
class LoadTestResult:
    levels: dict[int, int]
    metrics: list[dict] = field(default_factory=list)


def run_load_levels(engine, max_level: int = 1, season_prefix: str = "SIM-LOAD", seed: int = 12345) -> LoadTestResult:
    selected = {level: users for level, users in LOAD_LEVELS.items() if level <= max_level}
    result = LoadTestResult(selected)
    for level, users in selected.items():
        run = simulate_gameweek(
            engine,
            gameweek_id=level,
            users=users,
            season=f"{season_prefix}{level}"[:9],
            seed=seed + level,
            accelerated=True,
        )
        result.metrics.append({"level": level, **run.metrics})
    return result
