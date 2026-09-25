"""Deterministic factories for Tactical simulator tests."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from Shared.rules import FIXTURE_DURATION_MIN
from Simulation.scenario import (
    SyntheticFixture,
    SyntheticPlayer,
    SyntheticPlayerFixtureStats,
)


def make_player(
    player_id: int,
    position: str,
    club_id: int,
    *,
    name: str | None = None,
    price_tenths: int = 50,
) -> SyntheticPlayer:
    return SyntheticPlayer(
        player_id=player_id,
        name=name or f"Player {player_id}",
        position=position,
        club_id=club_id,
        price_tenths=price_tenths,
    )


def make_fixture(
    fixture_id: int,
    club_a: int,
    club_b: int,
    *,
    kickoff: datetime | None = None,
    duration_minutes: int = FIXTURE_DURATION_MIN,
) -> SyntheticFixture:
    kickoff = kickoff or datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    return SyntheticFixture(
        fixture_id=fixture_id,
        club_a=club_a,
        club_b=club_b,
        kickoff=kickoff,
        end=kickoff + timedelta(minutes=duration_minutes),
    )


def make_stats(
    player_id: int,
    fixture_id: int,
    *,
    minutes: int = 90,
    goals_scored: int = 0,
    assists: int = 0,
    clean_sheets: int = 0,
    goals_conceded: int = 0,
    saves: int = 0,
    penalties_saved: int = 0,
    penalties_missed: int = 0,
    own_goals: int = 0,
    yellow_cards: int = 0,
    red_cards: int = 0,
    defensive_contributions: int = 0,
    creativity: Decimal | str | int = Decimal("0"),
) -> SyntheticPlayerFixtureStats:
    return SyntheticPlayerFixtureStats(
        player_id=player_id,
        fixture_id=fixture_id,
        minutes=minutes,
        goals_scored=goals_scored,
        assists=assists,
        clean_sheets=clean_sheets,
        goals_conceded=goals_conceded,
        saves=saves,
        penalties_saved=penalties_saved,
        penalties_missed=penalties_missed,
        own_goals=own_goals,
        yellow_cards=yellow_cards,
        red_cards=red_cards,
        defensive_contributions=defensive_contributions,
        creativity=creativity if isinstance(creativity, Decimal) else Decimal(str(creativity)),
    )

