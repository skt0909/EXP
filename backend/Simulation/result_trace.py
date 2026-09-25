"""Structured traces returned by the Tactical simulator."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class ValidationTrace:
    code: str
    message: str


@dataclass(frozen=True)
class PlayerScoreTrace:
    player_id: int
    name: str
    position: str
    slot: int
    role: str
    appeared: bool
    counted: bool
    is_bonus: bool
    general_points: int
    tactical_points: int
    general_breakdown: list = field(default_factory=list)
    tactical_breakdown: list = field(default_factory=list)


@dataclass(frozen=True)
class BonusPlayerTrace:
    player_id: int
    eligible: bool
    appeared: bool
    general_points: int
    tactical_points: int
    breakdown: list = field(default_factory=list)


@dataclass(frozen=True)
class AutoSubTrace:
    outgoing_player_id: int | None
    incoming_player_id: int | None
    executed: bool
    reason: str


@dataclass(frozen=True)
class TacticalSwapTrace:
    outgoing_player_id: int
    incoming_player_id: int
    executed: bool
    outgoing_general_points: int
    incoming_general_points: int
    sub_bonus: int
    outgoing_last_end: datetime | None = None
    incoming_first_kickoff: datetime | None = None


@dataclass(frozen=True)
class SimulationResult:
    valid: bool
    validation_errors: list[ValidationTrace]
    tactic: str | None
    general_points: int | None
    tactical_points: int | None
    sub_bonus: int | None
    final_points: int | None
    formation_before: dict[str, int]
    formation_after: dict[str, int]
    player_lines: list[PlayerScoreTrace]
    bonus_players: list[BonusPlayerTrace]
    auto_subs: list[AutoSubTrace]
    tactical_swaps: list[TacticalSwapTrace]

