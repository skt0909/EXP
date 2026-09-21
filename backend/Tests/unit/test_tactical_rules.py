"""Phase 2: the tactical constants and the two lowest-level pure functions.

No database, no network -- see this directory's conftest.py.

Tier thresholds are tested with Decimal, never float. `ml.player_gw_stats.
creativity` is numeric(6,1) and psycopg2 hands it back as Decimal, so a tier
boundary must be an exact comparison: 39.9 earns +1 and 40.0 earns +3, with no
possibility of a repr like 39.900000000000006 deciding it. The float-vs-Decimal
guard test below is the one that would catch a regression to float().
"""
from decimal import Decimal

import pytest

from Shared import rules
from Results.tactical_scoring import general_points, tactical_points, tier_points


# ---- constants exist and carry the Q1 values -------------------------------

def test_rules_version_is_the_integer_three_decided_in_phase_1():
    # IMPLEMENTATION_PLAN.md section 2: "rules_version is an INTEGER, not a
    # string ... The next generation is therefore 3."
    assert rules.RULES_VERSION == 3
    assert isinstance(rules.RULES_VERSION, int)
    assert not isinstance(rules.RULES_VERSION, bool)


def test_existing_constants_are_untouched_by_phase_2():
    # Phase 2 is additions only; removals are Phase 4. CURRENT_RULES_VERSION is
    # what the LIVE scorer still stamps, and must not move until Phase 4 wires
    # the new engine in.
    assert rules.CURRENT_RULES_VERSION == 2
    assert rules.MAX_BANKED_FREE_TRANSFERS == 5
    assert rules.HIT_COST == 4


def test_transfer_and_fixture_constants():
    assert rules.FREE_TRANSFER_BANK_CAP == 2
    assert rules.FIXTURE_DURATION_MIN == 115


def test_tactics_are_exactly_the_three_the_check_constraint_allows():
    # ck_gw_selections_tactic: CHECK (tactic IN ('attack','defence','balanced'))
    assert set(rules.TACTICS) == {"attack", "defence", "balanced"}


def test_formation_minimums_require_three_midfielders():
    # D1: 1 GK, >=3 DEF, >=3 MID, >=1 FWD. The MID floor moved from 2 to 3,
    # which removes exactly one formation: 5-2-3.
    assert rules.FORMATION_MIN == {"GK": 1, "DEF": 3, "MID": 3, "FWD": 1}


def test_there_is_no_separate_maximum_rule():
    # D1: maxima are implied by the 2/5/5/3 squad, not stated separately. A
    # cover can never exceed a position's squad count, so nothing needs to
    # check one -- see the proof in tactical_scoring._formation_is_legal.
    assert not hasattr(rules, "FORMATION_MAX")


def test_bench_slot_map():
    assert rules.BENCH_SLOT_ROLES == {
        12: "auto_gk",
        13: "auto_outfield",
        14: "tactical",
        15: "tactical",
    }


def test_q1_tactical_point_tables():
    assert rules.ATTACK_POINTS == {"goal": 3, "assist": 2}
    assert rules.DEFENCE_CLEAN_SHEET_POINTS == 2
    assert rules.DC_TIERS == ((8, 2), (10, 3))
    assert rules.BALANCED_GOAL_OR_ASSIST_POINTS == 1
    assert rules.CREATIVITY_TIERS == ((Decimal("20"), 1), (Decimal("40"), 3))


def test_tier_tables_are_ascending_so_highest_reached_is_well_defined():
    for tiers in (rules.DC_TIERS, rules.CREATIVITY_TIERS):
        minimums = [m for m, _ in tiers]
        assert minimums == sorted(minimums)


def test_q1_has_at_most_six_thresholds_which_is_acceptance_test_t5():
    # clean sheet + 2 DC + 2 creativity = 5, against a limit of 6.
    thresholds = 1 + len(rules.DC_TIERS) + len(rules.CREATIVITY_TIERS)
    assert thresholds == 5


# ---- tier_points -----------------------------------------------------------

@pytest.mark.parametrize("actions,expected", [
    (0, 0), (7, 0),          # below the first tier
    (8, 2), (9, 2),          # first tier
    (10, 3), (11, 3), (99, 3),  # second tier, and never more than the highest
])
def test_defensive_contribution_tier_boundaries(actions, expected):
    assert tier_points(actions, rules.DC_TIERS) == expected


@pytest.mark.parametrize("creativity,expected", [
    ("0", 0), ("19.9", 0),
    ("20", 1), ("20.0", 1), ("39.9", 1),
    ("40", 3), ("40.0", 3), ("120.5", 3),
])
def test_creativity_tier_boundaries_are_exact_with_decimal(creativity, expected):
    assert tier_points(Decimal(creativity), rules.CREATIVITY_TIERS) == expected


def test_tiers_are_not_stacked_only_the_highest_reached_counts():
    # 10 actions reaches BOTH tiers. Stacking would give 2 + 3 = 5.
    assert tier_points(10, rules.DC_TIERS) == 3
    assert tier_points(Decimal("40"), rules.CREATIVITY_TIERS) == 3


def test_tier_points_rejects_float_so_a_boundary_can_never_be_decided_by_repr():
    # The guard that keeps 39.9 from ever landing on the wrong side. Floats are
    # refused outright rather than coerced, because a silent Decimal(str(x))
    # would hide the caller's mistake instead of surfacing it.
    with pytest.raises(TypeError):
        tier_points(39.9, rules.CREATIVITY_TIERS)


# ---- general_points: one rule at a time ------------------------------------

def _row(**kw):
    base = dict(minutes=90, goals_scored=0, assists=0, clean_sheets=0,
                goals_conceded=0, saves=0, penalties_saved=0,
                penalties_missed=0, own_goals=0, yellow_cards=0, red_cards=0,
                defensive_contributions=0, creativity=Decimal("0"))
    base.update(kw)
    return base


@pytest.mark.parametrize("minutes,expected", [(0, 0), (1, 1), (59, 1), (60, 2), (90, 2)])
def test_appearance_points(minutes, expected):
    assert general_points(_row(minutes=minutes), "MID") == expected


@pytest.mark.parametrize("position,goal_value", [("GK", 10), ("DEF", 6), ("MID", 5), ("FWD", 4)])
def test_goal_value_by_position(position, goal_value):
    assert general_points(_row(goals_scored=1), position) == 2 + goal_value


@pytest.mark.parametrize("position,cs_value", [("GK", 4), ("DEF", 4), ("MID", 1), ("FWD", 0)])
def test_clean_sheet_value_by_position(position, cs_value):
    assert general_points(_row(clean_sheets=1), position) == 2 + cs_value


def test_clean_sheet_needs_sixty_minutes():
    assert general_points(_row(minutes=59, clean_sheets=1), "DEF") == 1


def test_defensive_contribution_is_the_general_points_rule_not_the_tactical_one():
    # General Points: flat +2 at 10 actions for DEF, 12 for MID/FWD. This is a
    # DIFFERENT rule from the Defence tactic's tiers, and they must not be
    # confused -- the tactical tiers start at 8 and pay 2 or 3.
    assert general_points(_row(defensive_contributions=9), "DEF") == 2
    assert general_points(_row(defensive_contributions=10), "DEF") == 4
    assert general_points(_row(defensive_contributions=11), "MID") == 2
    assert general_points(_row(defensive_contributions=12), "MID") == 4
    assert general_points(_row(defensive_contributions=12), "FWD") == 4


def test_goalkeepers_earn_no_defensive_contribution_points():
    assert general_points(_row(defensive_contributions=99), "GK") == 2


def test_goals_conceded_is_minus_one_per_two_for_gk_and_def_only():
    assert general_points(_row(goals_conceded=1), "DEF") == 2
    assert general_points(_row(goals_conceded=2), "DEF") == 1
    assert general_points(_row(goals_conceded=3), "DEF") == 1
    assert general_points(_row(goals_conceded=4), "GK") == 0
    assert general_points(_row(goals_conceded=4), "MID") == 2


def test_saves_and_penalty_saves_are_goalkeeper_only():
    assert general_points(_row(saves=2), "GK") == 2
    assert general_points(_row(saves=3), "GK") == 3
    assert general_points(_row(penalties_saved=1), "GK") == 7
    assert general_points(_row(saves=9, penalties_saved=1), "DEF") == 2


def test_cards_own_goals_and_penalty_misses():
    assert general_points(_row(yellow_cards=1), "MID") == 1
    assert general_points(_row(red_cards=1), "MID") == -1
    assert general_points(_row(own_goals=1), "MID") == 0
    assert general_points(_row(penalties_missed=1), "MID") == 0


def test_no_bonus_points_are_ever_added():
    # The tactical game removes FPL bonus entirely. A row carrying bonus must
    # score exactly as one without it.
    assert general_points(_row(bonus=3), "MID") == general_points(_row(), "MID")


def test_a_player_who_did_not_appear_still_takes_his_deductions():
    # A red card in stoppage time of a previous fixture, 0 minutes here: the
    # appearance points are not earned but the card still counts. Flagged as an
    # ambiguity in PHASE2_REPORT.md -- the spec does not say.
    assert general_points(_row(minutes=0, red_cards=1), "MID") == -3


# ---- tactical_points per fixture row ---------------------------------------

def test_attack_pays_for_goals_and_assists_only():
    row = _row(goals_scored=2, assists=1, clean_sheets=1, defensive_contributions=20,
               creativity=Decimal("99"))
    assert tactical_points(row, "FWD", "attack") == 2 * 3 + 1 * 2


def test_defence_pays_clean_sheet_plus_the_highest_dc_tier():
    assert tactical_points(_row(clean_sheets=1), "DEF", "defence") == 2
    assert tactical_points(_row(defensive_contributions=8), "DEF", "defence") == 2
    assert tactical_points(_row(clean_sheets=1, defensive_contributions=10), "DEF", "defence") == 2 + 3


def test_defence_clean_sheet_needs_sixty_minutes_like_general_points():
    assert tactical_points(_row(minutes=59, clean_sheets=1), "DEF", "defence") == 0


def test_balanced_pays_one_per_goal_or_assist_plus_the_highest_creativity_tier():
    assert tactical_points(_row(goals_scored=1, assists=1), "MID", "balanced") == 2
    assert tactical_points(_row(creativity=Decimal("39.9")), "MID", "balanced") == 1
    assert tactical_points(_row(creativity=Decimal("40")), "MID", "balanced") == 3
    assert tactical_points(_row(goals_scored=2, creativity=Decimal("40")), "MID", "balanced") == 2 + 3


def test_an_unknown_tactic_is_rejected_rather_than_scoring_zero():
    with pytest.raises(ValueError):
        tactical_points(_row(), "MID", "parking_the_bus")
