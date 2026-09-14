"""State machines used by match and gameweek simulations."""

from __future__ import annotations

from enum import StrEnum


class MatchStatus(StrEnum):
    SCHEDULED = "SCHEDULED"
    LIVE = "LIVE"
    HALFTIME = "HALFTIME"
    FINISHED = "FINISHED"
    PROCESSED = "PROCESSED"
    POSTPONED = "POSTPONED"
    ABANDONED = "ABANDONED"


class GameweekStatus(StrEnum):
    DRAFT = "DRAFT"
    OPEN = "OPEN"
    LOCKED = "LOCKED"
    LIVE = "LIVE"
    PROCESSING = "PROCESSING"
    FINISHED = "FINISHED"


MATCH_TRANSITIONS = {
    MatchStatus.SCHEDULED: {MatchStatus.LIVE, MatchStatus.POSTPONED, MatchStatus.ABANDONED},
    MatchStatus.LIVE: {MatchStatus.HALFTIME, MatchStatus.FINISHED, MatchStatus.ABANDONED},
    MatchStatus.HALFTIME: {MatchStatus.LIVE, MatchStatus.ABANDONED},
    MatchStatus.FINISHED: {MatchStatus.PROCESSED},
    MatchStatus.PROCESSED: set(),
    MatchStatus.POSTPONED: {MatchStatus.SCHEDULED},
    MatchStatus.ABANDONED: set(),
}

GAMEWEEK_TRANSITIONS = {
    GameweekStatus.DRAFT: {GameweekStatus.OPEN},
    GameweekStatus.OPEN: {GameweekStatus.LOCKED},
    GameweekStatus.LOCKED: {GameweekStatus.LIVE},
    GameweekStatus.LIVE: {GameweekStatus.PROCESSING},
    GameweekStatus.PROCESSING: {GameweekStatus.FINISHED},
    GameweekStatus.FINISHED: set(),
}


def transition(current, target, allowed_map):
    if target not in allowed_map[current]:
        raise ValueError(f"invalid transition {current.value} -> {target.value}")
    return target


def transition_match(current: MatchStatus, target: MatchStatus) -> MatchStatus:
    return transition(current, target, MATCH_TRANSITIONS)


def transition_gameweek(current: GameweekStatus, target: GameweekStatus) -> GameweekStatus:
    return transition(current, target, GAMEWEEK_TRANSITIONS)
