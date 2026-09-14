"""Match simulator that writes production-shaped player gameweek stats."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from sqlalchemy import text

from .events import MatchEvent, MatchEventType
from .event_store import insert_events, load_fixture_events, mark_fixture_events_processed
from .lifecycle import MatchStatus, transition_match
from .logging import correlation_id, log_event


@dataclass
class PlayerStats:
    minutes: int = 0
    goals_scored: int = 0
    assists: int = 0
    clean_sheets: int = 0
    goals_conceded: int = 0
    saves: int = 0
    bonus: int = 0
    yellow_cards: int = 0
    red_cards: int = 0
    own_goals: int = 0
    penalties_saved: int = 0
    penalties_missed: int = 0
    defensive_contributions: int = 0
    total_points: int = 0


@dataclass
class MatchSimulationResult:
    match_id: int
    status: MatchStatus
    processed_event_ids: list[str] = field(default_factory=list)
    skipped_duplicate_event_ids: list[str] = field(default_factory=list)


def simulate_match(engine, match_id: int, events: list[MatchEvent], correlation: str | None = None) -> MatchSimulationResult:
    correlation = correlation or correlation_id()
    season, gameweek = _fixture_context(engine, match_id)
    status = MatchStatus.SCHEDULED
    stats: dict[int, PlayerStats] = defaultdict(PlayerStats)
    inserted, skipped = insert_events(engine, match_id, season, gameweek, sorted(events, key=lambda item: item.minute))

    log_event("Match simulation started", correlation, match_id=match_id, season=season, gameweek=gameweek)
    for provider_event_id in skipped:
        log_event("Duplicate event skipped", correlation, provider_event_id=provider_event_id)

    stored_events = load_fixture_events(engine, match_id)
    for event in stored_events:
        status = _apply_event(stats, status, event)
        log_event(
            "Event processed",
            correlation,
            provider_event_id=event.provider_event_id,
            type=event.event_type.value,
            player_id=event.player_id,
            minute=event.minute,
            status=status.value,
        )

    _write_stats(engine, match_id, season, gameweek, stats)
    mark_fixture_events_processed(engine, match_id)
    if status == MatchStatus.FINISHED:
        status = transition_match(status, MatchStatus.PROCESSED)
    log_event("Match simulation finished", correlation, match_id=match_id, status=status.value)
    return MatchSimulationResult(match_id, status, inserted, skipped)


def _fixture_context(engine, fixture_id: int) -> tuple[str, int]:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT season, gameweek FROM ml.fixtures WHERE id = :fixture_id"),
            {"fixture_id": fixture_id},
        ).first()
    if row is None:
        raise ValueError(f"fixture {fixture_id} does not exist")
    return row.season, row.gameweek


def _apply_event(stats: dict[int, PlayerStats], status: MatchStatus, event: MatchEvent) -> MatchStatus:
    if event.event_type == MatchEventType.KICKOFF:
        return transition_match(status, MatchStatus.LIVE)
    if event.event_type == MatchEventType.MATCH_POSTPONED:
        return transition_match(status, MatchStatus.POSTPONED)
    if event.event_type == MatchEventType.MATCH_ABANDONED:
        return transition_match(status, MatchStatus.ABANDONED)
    if event.event_type == MatchEventType.MATCH_FINISHED:
        return transition_match(status, MatchStatus.FINISHED)

    if status == MatchStatus.SCHEDULED:
        status = transition_match(status, MatchStatus.LIVE)

    if event.player_id is None:
        return status

    row = stats[event.player_id]
    row.minutes = max(row.minutes, int(event.metadata.get("minutes", 90 if event.minute > 0 else 1)))
    if event.event_type == MatchEventType.GOAL:
        row.goals_scored += 1
    elif event.event_type == MatchEventType.ASSIST:
        row.assists += 1
    elif event.event_type == MatchEventType.OWN_GOAL:
        row.own_goals += 1
    elif event.event_type == MatchEventType.PENALTY_MISS:
        row.penalties_missed += 1
    elif event.event_type == MatchEventType.PENALTY_SAVE:
        row.penalties_saved += 1
    elif event.event_type == MatchEventType.YELLOW_CARD:
        row.yellow_cards += 1
    elif event.event_type == MatchEventType.RED_CARD:
        row.red_cards += 1
    elif event.event_type == MatchEventType.CLEAN_SHEET:
        row.clean_sheets = 1
    elif event.event_type in {MatchEventType.SUBSTITUTION, MatchEventType.INJURY_SUBSTITUTION}:
        row.minutes = int(event.metadata.get("minutes", event.minute))
    elif event.event_type == MatchEventType.APPEARANCE:
        row.minutes = int(event.metadata.get("minutes", 90))
    return status


def _write_stats(engine, fixture_id: int, season: str, gameweek: int, stats: dict[int, PlayerStats]) -> None:
    if not stats:
        return
    with engine.begin() as conn:
        for fpl_id, row in stats.items():
            internal_id = conn.execute(
                text("SELECT id FROM ml.players WHERE season = :season AND fpl_id = :fpl_id"),
                {"season": season, "fpl_id": fpl_id},
            ).scalar()
            if internal_id is None:
                raise ValueError(f"player fpl_id={fpl_id} does not exist for season={season}")
            conn.execute(
                text(
                    "INSERT INTO ml.player_gw_stats (player_id, season, gameweek, fixture_id, minutes, goals_scored, "
                    "assists, clean_sheets, goals_conceded, saves, bonus, bps, ict_index, expected_goals, "
                    "expected_assists, expected_goal_involvements, total_points, value, selected, was_home, "
                    "is_live, yellow_cards, red_cards, own_goals, penalties_saved, penalties_missed, defensive_contributions) "
                    "VALUES (:player_id, :season, :gameweek, :fixture_id, :minutes, :goals_scored, :assists, "
                    ":clean_sheets, :goals_conceded, :saves, :bonus, 0, 0, 0, 0, 0, :total_points, 50, 1000, TRUE, TRUE, "
                    ":yellow_cards, :red_cards, :own_goals, :penalties_saved, :penalties_missed, :defensive_contributions)"
                    " ON CONFLICT (player_id, season, gameweek, COALESCE(fixture_id, -1)) DO UPDATE SET "
                    "minutes = EXCLUDED.minutes, goals_scored = EXCLUDED.goals_scored, assists = EXCLUDED.assists, "
                    "clean_sheets = EXCLUDED.clean_sheets, goals_conceded = EXCLUDED.goals_conceded, saves = EXCLUDED.saves, "
                    "bonus = EXCLUDED.bonus, total_points = EXCLUDED.total_points, yellow_cards = EXCLUDED.yellow_cards, "
                    "red_cards = EXCLUDED.red_cards, own_goals = EXCLUDED.own_goals, penalties_saved = EXCLUDED.penalties_saved, "
                    "penalties_missed = EXCLUDED.penalties_missed, defensive_contributions = EXCLUDED.defensive_contributions"
                ),
                {"player_id": internal_id, "season": season, "gameweek": gameweek, "fixture_id": fixture_id, **row.__dict__},
            )
        conn.execute(text("UPDATE ml.fixtures SET finished = TRUE WHERE id = :fixture_id"), {"fixture_id": fixture_id})
