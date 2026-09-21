"""Numerical scoring contracts against production scoring helpers."""

import sys
from pathlib import Path
from types import SimpleNamespace

BACKEND = Path(__file__).resolve().parent.parent / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

# Phase 4c: general_points replaces the deleted _component_score. It has no
# total_points fallback -- the point table IS the rule -- so a row with
# minutes alone now scores its appearance points rather than deferring.
from Results.tactical_scoring import general_points


def row(position="MID", **overrides):
    data = {
        "position": position,
        "minutes": 90,
        "goals_scored": 0,
        "assists": 0,
        "clean_sheets": 0,
        "goals_conceded": 0,
        "saves": 0,
        "bonus": 0,
        "yellow_cards": 0,
        "red_cards": 0,
        "own_goals": 0,
        "penalties_saved": 0,
        "penalties_missed": 0,
        "defensive_contributions": 0,
        "total_points": 0,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_goal_assist_clean_sheet_appearance_bonus_components():
    scored = row("DEF", goals_scored=1, assists=1, clean_sheets=1, bonus=2)
    assert general_points(scored, scored.position) == 17


def test_cards_own_goal_penalty_miss_are_negative():
    scored = row("MID", yellow_cards=1, red_cards=1, own_goals=1, penalties_missed=1)
    assert general_points(scored, scored.position) == -6


def test_goalkeeper_saves_and_penalty_save():
    scored = row("GK", saves=6, penalties_saved=1, clean_sheets=1)
    assert general_points(scored, scored.position) == 13


def test_goals_conceded_penalty_for_defenders():
    scored = row("DEF", goals_conceded=4)
    assert general_points(scored, scored.position) == 0


def test_short_appearance_gets_one_point():
    scored = row("FWD", minutes=12, assists=1)
    assert general_points(scored, scored.position) == 4


def test_no_component_signal_uses_production_total_points_fallback():
    scored = row("MID", minutes=90, total_points=13)
    assert general_points(scored, scored.position) == 13
