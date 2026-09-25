"""Orchestration for one deterministic, database-free Tactical gameweek."""

from collections import Counter
from decimal import Decimal

from Gameplay.selection_rules import (
    BONUS_POSITION,
    SelectionInput,
    last_fixture_end,
    validate_selection,
)
from Results.tactical_scoring import (
    ROLE_AUTO_SUB_COVER,
    ROLE_AUTO_SUB_REPLACED,
    general_points_breakdown,
    score_selection,
    tactical_points_breakdown,
)
from Shared.rules import FORMATION_MIN, STARTING_XI_SIZE
from Simulation.result_trace import (
    AutoSubTrace,
    BonusPlayerTrace,
    PlayerScoreTrace,
    SimulationResult,
    TacticalSwapTrace,
    ValidationTrace,
)
from Simulation.scenario import SyntheticFixture, SyntheticScenario
from Simulation.squad_rules import validate_squad


def simulate_gameweek(scenario: SyntheticScenario) -> SimulationResult:
    squad_errors = validate_squad(scenario.squad)
    players = {p.player_id: p for p in scenario.squad.players}
    positions = {p.player_id: p.position for p in scenario.squad.players}

    selection_input = scenario.selection.to_selection_input(
        season=scenario.season,
        gameweek=scenario.gameweek,
    )
    fixtures_by_player = _fixtures_by_player(
        players,
        scenario.submission_schedule,
        kickoff_only=True,
    )
    selection_errors = validate_selection(
        selection_input,
        set(players),
        positions,
        fixtures_by_player,
        check_timing=True,
    )

    validation_errors = [
        ValidationTrace(err.code, err.message) for err in squad_errors
    ] + [
        ValidationTrace("selection", message) for message in selection_errors
    ]

    formation_before = _formation_for_ids(scenario.selection.starter_ids, positions)
    if validation_errors:
        return SimulationResult(
            valid=False,
            validation_errors=validation_errors,
            tactic=scenario.selection.tactic,
            general_points=None,
            tactical_points=None,
            sub_bonus=None,
            final_points=None,
            formation_before=formation_before,
            formation_after=formation_before,
            player_lines=[],
            bonus_players=[],
            auto_subs=[],
            tactical_swaps=[],
        )

    stats_by_player = _stats_by_player(scenario)
    scoring_selection = scenario.selection.to_scoring_selection()
    score = score_selection(scoring_selection, stats_by_player, positions)

    player_lines = _player_traces(score, players, positions, stats_by_player, scenario.selection.tactic)
    formation_after = _formation_after_resolution(score.players, positions, formation_before)
    bonus_players = _bonus_traces(
        selection_input,
        player_lines,
        scenario.selection.tactic,
        positions,
    )
    auto_subs = _auto_sub_traces(score.players)
    tactical_swaps = _swap_traces(
        score.swaps,
        players,
        scenario.submission_schedule,
    )

    return SimulationResult(
        valid=True,
        validation_errors=[],
        tactic=scenario.selection.tactic,
        general_points=score.raw_points,
        tactical_points=score.tactical_points,
        sub_bonus=score.sub_bonus,
        final_points=score.total,
        formation_before=formation_before,
        formation_after=formation_after,
        player_lines=player_lines,
        bonus_players=bonus_players,
        auto_subs=auto_subs,
        tactical_swaps=tactical_swaps,
    )


def _fixtures_by_player(players, fixtures: list[SyntheticFixture], *, kickoff_only: bool):
    out = {}
    for player_id, player in players.items():
        player_fixtures = [
            fixture
            for fixture in fixtures
            if player.club_id in (fixture.club_a, fixture.club_b)
        ]
        out[player_id] = [fixture.kickoff for fixture in player_fixtures] if kickoff_only else player_fixtures
    return out


def _stats_by_player(scenario: SyntheticScenario):
    out = {}
    for row in scenario.stats:
        scoring_row = row.as_scoring_row()
        creativity = scoring_row.get("creativity")
        if not isinstance(creativity, Decimal):
            raise TypeError("SyntheticPlayerFixtureStats.creativity must be Decimal")
        out.setdefault(row.player_id, []).append(scoring_row)
    return out


def _formation_for_ids(player_ids, positions):
    counts = Counter(positions[player_id] for player_id in player_ids)
    return {position: counts.get(position, 0) for position in FORMATION_MIN}


def _formation_after_resolution(player_lines, positions, formation_before):
    counts = Counter(formation_before)
    for line in player_lines:
        if line.role == ROLE_AUTO_SUB_REPLACED:
            counts[positions[line.player_id]] -= 1
    for line in player_lines:
        if line.role == ROLE_AUTO_SUB_COVER:
            counts[positions[line.player_id]] += 1
    return {position: counts.get(position, 0) for position in FORMATION_MIN}


def _player_traces(score, players, positions, stats_by_player, tactic):
    traces = []
    by_player_line = {line.player_id: line for line in score.players}
    for player_id, line in by_player_line.items():
        rows = stats_by_player.get(player_id) or []
        appeared = sum(row.get("minutes", 0) for row in rows) > 0
        general_parts = []
        tactical_parts = []
        for row in rows:
            general_parts.extend(general_points_breakdown(row, positions[player_id]))
            if line.is_bonus and line.tactical_points:
                tactical_parts.extend(tactical_points_breakdown(row, positions[player_id], tactic))

        player = players[player_id]
        traces.append(
            PlayerScoreTrace(
                player_id=player_id,
                name=player.name,
                position=line.position,
                slot=line.position_slot,
                role=line.role,
                appeared=appeared,
                counted=line.counted,
                is_bonus=line.is_bonus,
                general_points=line.general_points,
                tactical_points=line.tactical_points,
                general_breakdown=_merge_breakdown(general_parts),
                tactical_breakdown=_merge_breakdown(tactical_parts),
            )
        )
    return sorted(traces, key=lambda p: p.slot)


def _bonus_traces(selection: SelectionInput, player_lines, tactic, positions):
    by_player = {p.player_id: p for p in player_lines}
    wanted_position = BONUS_POSITION.get(tactic)
    traces = []
    for player_id in selection.bonus_player_ids:
        line = by_player[player_id]
        traces.append(
            BonusPlayerTrace(
                player_id=player_id,
                eligible=(
                    player_id in selection.player_ids
                    and wanted_position is not None
                    and positions[player_id] == wanted_position
                ),
                appeared=line.appeared,
                general_points=line.general_points,
                tactical_points=line.tactical_points,
                breakdown=line.tactical_breakdown,
            )
        )
    return traces


def _auto_sub_traces(player_lines):
    # TODO: rejected-reason traces should come from a later extraction of the
    # production Auto Sub resolver. Milestone 1 reports only outcomes exposed
    # safely by score_selection().
    return [
        AutoSubTrace(
            outgoing_player_id=line.covers_player_id,
            incoming_player_id=line.player_id,
            executed=True,
            reason="production scorer selected this cover",
        )
        for line in player_lines
        if line.role == ROLE_AUTO_SUB_COVER
    ]


def _swap_traces(swap_lines, players, submission_schedule):
    fixtures_by_player = _fixtures_by_player(players, submission_schedule, kickoff_only=True)
    traces = []
    for swap in swap_lines:
        out_fixtures = fixtures_by_player.get(swap.player_out_id) or []
        in_fixtures = fixtures_by_player.get(swap.player_in_id) or []
        traces.append(
            TacticalSwapTrace(
                outgoing_player_id=swap.player_out_id,
                incoming_player_id=swap.player_in_id,
                executed=True,
                outgoing_general_points=swap.general_out,
                incoming_general_points=swap.general_in,
                sub_bonus=swap.sub_bonus,
                outgoing_last_end=last_fixture_end(out_fixtures) if out_fixtures else None,
                incoming_first_kickoff=min(in_fixtures) if in_fixtures else None,
            )
        )
    return traces


def _merge_breakdown(parts):
    merged, order = {}, []
    for part in parts:
        rule = part["rule"]
        if rule not in merged:
            merged[rule] = 0
            order.append(rule)
        merged[rule] += part["points"]
    return [{"rule": rule, "points": merged[rule]} for rule in order if merged[rule]]

