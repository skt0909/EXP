"""Pure squad validation for synthetic Tactical scenarios."""

from collections import Counter
from dataclasses import dataclass

from Shared.rules import BUDGET_CAP, MAX_PER_CLUB, POSITION_REQUIREMENTS, SQUAD_SIZE
from Simulation.scenario import SyntheticSquad


@dataclass(frozen=True)
class SquadValidationError:
    code: str
    message: str


def validate_squad(squad: SyntheticSquad) -> list[SquadValidationError]:
    errors: list[SquadValidationError] = []
    players = list(squad.players)

    if len(players) != SQUAD_SIZE:
        errors.append(SquadValidationError("squad_size", f"squad must contain exactly {SQUAD_SIZE} players, got {len(players)}"))

    ids = [p.player_id for p in players]
    duplicates = sorted(pid for pid, count in Counter(ids).items() if count > 1)
    if duplicates:
        errors.append(SquadValidationError("duplicate_players", f"duplicate player_id(s): {duplicates}"))

    position_counts = Counter(p.position for p in players)
    for position, required in POSITION_REQUIREMENTS.items():
        actual = position_counts.get(position, 0)
        if actual != required:
            errors.append(SquadValidationError("position_count", f"expected {required} {position}, got {actual}"))

    total_cost = sum(p.price_tenths for p in players)
    if total_cost > BUDGET_CAP:
        errors.append(SquadValidationError("budget", f"squad cost {total_cost} exceeds budget cap {BUDGET_CAP}"))

    club_counts = Counter(p.club_id for p in players)
    over_cap = {club_id: count for club_id, count in club_counts.items() if count > MAX_PER_CLUB}
    if over_cap:
        errors.append(SquadValidationError("club_cap", f"max {MAX_PER_CLUB} players per club exceeded: {over_cap}"))

    return errors

