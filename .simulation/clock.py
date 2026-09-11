"""Clock abstraction for deterministic simulation time."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class GameClock:
    """Production clock facade."""

    @classmethod
    def now(cls) -> datetime:
        return datetime.now(timezone.utc)


class SimulationClock:
    """Mutable test clock. Never changes the operating system clock."""

    current_time: datetime = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls) -> datetime:
        return cls.current_time

    @classmethod
    def set(cls, value: datetime) -> None:
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        cls.current_time = value

    @classmethod
    def advance(cls, **kwargs) -> datetime:
        cls.current_time += timedelta(**kwargs)
        return cls.current_time
