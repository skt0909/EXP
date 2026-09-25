"""Synthetic inputs for the database-free Tactical simulator."""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from Gameplay.selection_rules import SelectionInput, SwapInput
from Results.tactical_scoring import Selection, Slot, Swap


@dataclass(frozen=True)
class SyntheticPlayer:
    player_id: int
    name: str
    position: str
    club_id: int
    price_tenths: int


@dataclass(frozen=True)
class SyntheticSquad:
    players: list[SyntheticPlayer]


@dataclass(frozen=True)
class SyntheticFixture:
    fixture_id: int
    club_a: int
    club_b: int
    kickoff: datetime
    end: datetime


@dataclass(frozen=True)
class SyntheticPlayerFixtureStats:
    player_id: int
    fixture_id: int
    minutes: int = 90
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    goals_conceded: int = 0
    saves: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    own_goals: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    defensive_contributions: int = 0
    creativity: Decimal = Decimal("0")

    def as_scoring_row(self) -> dict:
        return {
            "minutes": self.minutes,
            "goals_scored": self.goals_scored,
            "assists": self.assists,
            "clean_sheets": self.clean_sheets,
            "goals_conceded": self.goals_conceded,
            "saves": self.saves,
            "penalties_saved": self.penalties_saved,
            "penalties_missed": self.penalties_missed,
            "own_goals": self.own_goals,
            "yellow_cards": self.yellow_cards,
            "red_cards": self.red_cards,
            "defensive_contributions": self.defensive_contributions,
            "creativity": self.creativity,
        }


@dataclass(frozen=True)
class SyntheticSelection:
    tactic: str
    starter_ids: list[int]
    bench_order: list[int]
    bonus_player_ids: list[int]
    swaps: list[SwapInput] = field(default_factory=list)

    def to_selection_input(self, season: str, gameweek: int) -> SelectionInput:
        return SelectionInput(
            season=season,
            gameweek=gameweek,
            tactic=self.tactic,
            player_ids=list(self.starter_ids),
            bench_order=list(self.bench_order),
            bonus_player_ids=list(self.bonus_player_ids),
            swaps=list(self.swaps),
        )

    def to_scoring_selection(self) -> Selection:
        bonus = set(self.bonus_player_ids)
        slots = [
            Slot(position_slot=slot, player_id=player_id, is_bonus=player_id in bonus)
            for slot, player_id in enumerate(self.starter_ids + self.bench_order, start=1)
        ]
        swaps = [
            Swap(player_out_id=s.player_out_id, player_in_id=s.player_in_id)
            for s in self.swaps
        ]
        return Selection(tactic=self.tactic, slots=slots, swaps=swaps)


@dataclass(frozen=True)
class SyntheticScenario:
    gameweek: int
    squad: SyntheticSquad
    selection: SyntheticSelection
    submission_schedule: list[SyntheticFixture]
    actual_schedule: list[SyntheticFixture]
    stats: list[SyntheticPlayerFixtureStats]
    season: str = "SIM-TAC"

