"""Typed match events used as simulated inputs."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import uuid4


class MatchEventType(StrEnum):
    KICKOFF = "KICKOFF"
    GOAL = "GOAL"
    ASSIST = "ASSIST"
    OWN_GOAL = "OWN_GOAL"
    PENALTY = "PENALTY"
    PENALTY_SAVE = "PENALTY_SAVE"
    PENALTY_MISS = "PENALTY_MISS"
    YELLOW_CARD = "YELLOW_CARD"
    RED_CARD = "RED_CARD"
    CLEAN_SHEET = "CLEAN_SHEET"
    SUBSTITUTION = "SUBSTITUTION"
    APPEARANCE = "APPEARANCE"
    INJURY_SUBSTITUTION = "INJURY_SUBSTITUTION"
    MATCH_ABANDONED = "MATCH_ABANDONED"
    MATCH_POSTPONED = "MATCH_POSTPONED"
    MATCH_FINISHED = "MATCH_FINISHED"


@dataclass(frozen=True)
class MatchEvent:
    event_type: MatchEventType
    player_id: int | None = None
    minute: int = 0
    provider_event_id: str = field(default_factory=lambda: f"SIM-{uuid4().hex}")
    related_player_id: int | None = None
    metadata: dict = field(default_factory=dict)


def Goal(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.GOAL, player_id, minute, provider_event_id or f"SIM-GOAL-{player_id}-{minute}")


def Assist(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.ASSIST, player_id, minute, provider_event_id or f"SIM-AST-{player_id}-{minute}")


def YellowCard(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.YELLOW_CARD, player_id, minute, provider_event_id or f"SIM-YC-{player_id}-{minute}")


def OwnGoal(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.OWN_GOAL, player_id, minute, provider_event_id or f"SIM-OG-{player_id}-{minute}")


def Penalty(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.PENALTY, player_id, minute, provider_event_id or f"SIM-PEN-{player_id}-{minute}")


def PenaltyMiss(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.PENALTY_MISS, player_id, minute, provider_event_id or f"SIM-PENMISS-{player_id}-{minute}")


def PenaltySave(player_id: int, minute: int, provider_event_id: str | None = None) -> MatchEvent:
    return MatchEvent(MatchEventType.PENALTY_SAVE, player_id, minute, provider_event_id or f"SIM-PENSAVE-{player_id}-{minute}")
