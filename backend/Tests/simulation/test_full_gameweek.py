from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from Gameplay.selection_rules import SwapInput
from Simulation.factories import make_fixture, make_player, make_stats
from Simulation.scenario import SyntheticScenario, SyntheticSelection, SyntheticSquad
from Simulation.tactical_simulator import simulate_gameweek


@pytest.fixture(autouse=True)
def _clean_ml_test_data():
    yield


@pytest.fixture
def engine():
    raise AssertionError("simulation tests must stay database-free")


def _line(result, player_id):
    return next(line for line in result.player_lines if line.player_id == player_id)


def test_single_synthetic_tactical_gameweek_returns_score_and_trace():
    t0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    # Clubs 1/2 play early; clubs 3/4 play late enough for MID 9 -> MID 14.
    early = make_fixture(1, 1, 2, kickoff=t0)
    late = make_fixture(2, 3, 4, kickoff=t0 + timedelta(minutes=130))
    late_extra = make_fixture(3, 5, 6, kickoff=t0 + timedelta(minutes=130))

    players = [
        make_player(1, "GK", 1, name="GK Starter"),
        make_player(12, "GK", 5, name="GK Bench"),
        make_player(2, "DEF", 1),
        make_player(3, "DEF", 2),
        make_player(4, "DEF", 3),
        make_player(5, "DEF", 2),
        make_player(13, "DEF", 5, name="Auto Sub DEF"),
        make_player(6, "MID", 2, name="Bonus MID Haul"),
        make_player(7, "MID", 4, name="Bonus MID Quiet"),
        make_player(8, "MID", 3),
        make_player(9, "MID", 1, name="Swapped Out MID"),
        make_player(14, "MID", 4, name="Swapped In MID"),
        make_player(10, "FWD", 3),
        make_player(11, "FWD", 4),
        make_player(15, "FWD", 6),
    ]
    squad = SyntheticSquad(players)
    selection = SyntheticSelection(
        tactic="balanced",
        starter_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        bench_order=[12, 13, 14, 15],
        bonus_player_ids=[6, 7],
        swaps=[SwapInput(9, 14)],
    )

    fixture_for = {
        1: early, 2: early, 3: early, 5: early, 6: early, 9: early,
        4: late, 7: late, 8: late, 10: late, 11: late, 14: late,
        12: late_extra,
        13: late_extra, 15: late_extra,
    }
    stats = [
        make_stats(pid, fixture_for[pid].fixture_id)
        for pid in fixture_for
    ]
    overrides = {
        # Normal starter no-show. Slot-13 DEF covers him legally.
        5: make_stats(5, early.fixture_id, minutes=0),
        # Auto Sub earns General only: appearance 2 + DEF goal 6 = 8.
        13: make_stats(13, late.fixture_id, goals_scored=1),
        # Bonus MID earns General 7 and Tactical 4: goal + creativity tier.
        6: make_stats(6, early.fixture_id, goals_scored=1, creativity=Decimal("40")),
        # Outgoing swap player scores 2; incoming scores 7, so +1 Sub Bonus.
        14: make_stats(14, late.fixture_id, goals_scored=1),
    }
    stats = [overrides.get(row.player_id, row) for row in stats]

    scenario = SyntheticScenario(
        gameweek=1,
        squad=squad,
        selection=selection,
        submission_schedule=[early, late, late_extra],
        actual_schedule=[early, late, late_extra],
        stats=stats,
    )

    result = simulate_gameweek(scenario)

    assert result.valid is True
    assert result.validation_errors == []

    # Manual expected points:
    # Baseline starters = 11 * 2 = 22.
    # Starter 5 no-shows and is replaced: -2 baseline, +0 for starter 5, +8 cover.
    # Bonus MID 6 scores a goal: -2 baseline, +7 actual general.
    # Tactical swap adds incoming MID 14's 7 while outgoing MID 9's 2 remains banked.
    # General = 22 - 2 + 8 - 2 + 7 + 7 = 40.
    # Tactical = MID 6 goal/assist bonus 1 + creativity tier 3 = 4.
    # Sub Bonus = incoming 14 general 7 > outgoing 9 general 2 = 1.
    assert result.general_points == 40
    assert result.tactical_points == 4
    assert result.sub_bonus == 1
    assert result.final_points == 45
    assert result.final_points == result.general_points + result.tactical_points + result.sub_bonus

    assert result.formation_before == {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}
    assert result.formation_after == {"GK": 1, "DEF": 4, "MID": 4, "FWD": 2}

    auto_sub = result.auto_subs[0]
    assert auto_sub.executed is True
    assert auto_sub.outgoing_player_id == 5
    assert auto_sub.incoming_player_id == 13
    assert _line(result, 13).role == "auto_sub_cover"
    assert _line(result, 13).general_points == 8
    assert _line(result, 13).tactical_points == 0

    swap = result.tactical_swaps[0]
    assert swap.executed is True
    assert swap.outgoing_player_id == 9
    assert swap.incoming_player_id == 14
    assert swap.outgoing_general_points == 2
    assert swap.incoming_general_points == 7
    assert swap.sub_bonus == 1

    bonus = {trace.player_id: trace for trace in result.bonus_players}
    assert bonus[6].eligible is True
    assert bonus[6].appeared is True
    assert bonus[6].general_points == 7
    assert bonus[6].tactical_points == 4
    assert bonus[6].breakdown == [
        {"rule": "balanced_goal_or_assist", "points": 1},
        {"rule": "balanced_creativity_tier", "points": 3},
    ]
    assert bonus[7].tactical_points == 0

    assert _line(result, 13).is_bonus is False
    assert _line(result, 13).tactical_breakdown == []
