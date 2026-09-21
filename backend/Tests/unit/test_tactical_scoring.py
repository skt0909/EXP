"""Phase 2: score_selection, the 7-step order of operations from section 6.

No database, no network -- see this directory's conftest.py. Every scenario is
a hand-built gameweek: plain dicts in, a breakdown out.

Squad layout used throughout (_squad below), 1-4-4-2:
    slots 1-11  starters     ids 1..11   (1 GK, 2-5 DEF, 6-9 MID, 10-11 FWD)
    slot 12     backup GK    id 12
    slot 13     outfield Auto Sub  id 13 (DEF)
    slots 14-15 Tactical Subs      ids 14 (MID) and 15 (FWD)

The bench is deliberately NOT ordered GK-DEF-MID-FWD by accident of id: slot 12
is the only goalkeeper and slot 13 the only Auto Sub, which is what the rules
say, and no test may pass by assuming a different arrangement.
"""
from decimal import Decimal

import pytest

from Results.tactical_scoring import Selection, Slot, Swap, score_selection

POSITIONS = {
    1: "GK",
    2: "DEF", 3: "DEF", 4: "DEF", 5: "DEF",
    6: "MID", 7: "MID", 8: "MID", 9: "MID",
    10: "FWD", 11: "FWD",
    12: "GK", 13: "DEF", 14: "MID", 15: "FWD",
}


def _row(**kw):
    base = dict(minutes=90, goals_scored=0, assists=0, clean_sheets=0,
                goals_conceded=0, saves=0, penalties_saved=0,
                penalties_missed=0, own_goals=0, yellow_cards=0, red_cards=0,
                defensive_contributions=0, creativity=Decimal("0"))
    base.update(kw)
    return base


def _squad(tactic="balanced", bonus=(6, 7), swaps=()):
    slots = [Slot(position_slot=i, player_id=i, is_bonus=(i in bonus)) for i in range(1, 16)]
    return Selection(
        tactic=tactic,
        slots=slots,
        swaps=[Swap(player_out_id=o, player_in_id=i) for o, i in swaps],
    )


def _stats(**overrides):
    """Every one of the 15 plays 90 minutes and does nothing, unless overridden."""
    stats = {pid: [_row()] for pid in range(1, 16)}
    for pid, rows in overrides.items():
        stats[pid] = rows if isinstance(rows, list) else [rows]
    return stats


def _line(result, player_id):
    return next(p for p in result.players if p.player_id == player_id)


def _covered(result):
    """The starters who were actually replaced by an Auto Sub.

    Not the same as `counted is False`: a no-show starter nobody covers is
    still a scoring slot, he just scores 0. Only a REPLACED starter stops
    counting, so "was he covered" has to be read from the cover's own line."""
    return {p.covers_player_id for p in result.players if p.covers_player_id is not None}


# ---- baseline --------------------------------------------------------------

def test_eleven_starters_score_and_the_bench_does_not():
    result = score_selection(_squad(), _stats(), POSITIONS)
    # 11 starters x 2 appearance points.
    assert result.raw_points == 22
    assert result.tactical_points == 0
    assert result.sub_bonus == 0
    assert result.total == 22
    for pid in (12, 13, 14, 15):
        assert _line(result, pid).role == "bench_unused"
        assert _line(result, pid).counted is False


def test_the_breakdown_names_every_one_of_the_fifteen():
    result = score_selection(_squad(), _stats(), POSITIONS)
    assert {p.player_id for p in result.players} == set(range(1, 16))
    assert all(p.position == POSITIONS[p.player_id] for p in result.players)


def test_total_is_general_plus_tactical_plus_sub_bonus():
    # Step 7.
    stats = _stats()
    stats[6] = [_row(goals_scored=1, creativity=Decimal("40"))]
    result = score_selection(_squad(tactic="balanced", bonus=(6, 7)), stats, POSITIONS)
    # general: 10 players x 2, plus player 6 with 2 + 5 = 7  -> 27
    # tactical: player 6 is Bonus -> 1 (goal) + 3 (creativity tier) = 4
    assert result.raw_points == 27
    assert result.tactical_points == 4
    assert result.total == 31


# ---- step 2: swaps ---------------------------------------------------------

def test_a_swap_banks_the_outgoing_points_and_adds_the_incoming_ones():
    stats = _stats()
    stats[9] = [_row(goals_scored=1)]    # outgoing MID: 2 + 5 = 7
    stats[14] = [_row(goals_scored=1)]   # incoming MID: 2 + 5 = 7
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert _line(result, 9).role == "swapped_out"
    assert _line(result, 9).general_points == 7
    assert _line(result, 9).counted is True     # banked, not discarded
    assert _line(result, 14).role == "swapped_in"
    assert _line(result, 14).counted is True
    # 10 ordinary starters x 2, plus 7 banked, plus 7 incoming.
    assert result.raw_points == 20 + 7 + 7


def test_up_to_thirteen_players_can_score_in_one_gameweek():
    result = score_selection(_squad(swaps=((8, 14), (11, 15))), _stats(), POSITIONS)
    counted = [p for p in result.players if p.counted]
    assert len(counted) == 13
    assert result.raw_points == 13 * 2


def test_an_outgoing_player_who_never_appeared_still_swaps_and_scores_zero():
    stats = _stats()
    stats[9] = [_row(minutes=0)]
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert _line(result, 9).role == "swapped_out"
    assert _line(result, 9).general_points == 0
    assert _line(result, 14).counted is True


def test_an_incoming_player_who_never_appeared_scores_zero_with_no_cover():
    stats = _stats()
    stats[14] = [_row(minutes=0)]
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert _line(result, 14).general_points == 0
    # The Auto Sub must NOT step in to rescue the failed swap.
    assert _line(result, 13).role == "bench_unused"


def test_a_slot_involved_in_a_swap_is_never_auto_sub_covered():
    # Player 9 is swapped out AND did not appear. Without the rule, the outfield
    # Auto Sub would see a 0-minute starter and cover him.
    stats = _stats()
    stats[9] = [_row(minutes=0)]
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert _line(result, 13).role == "bench_unused"
    assert _line(result, 13).counted is False


def test_an_unused_tactical_sub_stays_on_the_bench_and_scores_nothing():
    stats = _stats()
    stats[15] = [_row(goals_scored=3)]   # a hat-trick that must not count
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert _line(result, 15).role == "bench_unused"
    assert _line(result, 15).counted is False
    assert result.raw_points == 20 + 2 + 2


# ---- steps 3 and 4: auto subs ----------------------------------------------

def test_the_backup_gk_covers_only_the_starting_gk():
    stats = _stats()
    stats[1] = [_row(minutes=0)]
    stats[12] = [_row(saves=3)]          # 2 + 1 = 3
    result = score_selection(_squad(), stats, POSITIONS)
    assert _line(result, 12).role == "auto_sub_cover"
    assert _line(result, 12).general_points == 3
    assert _line(result, 12).covers_player_id == 1
    assert _line(result, 1).role == "auto_sub_replaced"
    assert _line(result, 1).counted is False
    assert result.raw_points == 20 + 3


def test_a_backup_gk_who_also_did_not_play_cannot_cover():
    stats = _stats()
    stats[1] = [_row(minutes=0)]
    stats[12] = [_row(minutes=0)]
    result = score_selection(_squad(), stats, POSITIONS)
    assert _line(result, 12).role == "bench_unused"
    assert result.raw_points == 20


def test_the_outfield_auto_sub_covers_the_lowest_slot_that_keeps_the_formation_legal():
    # Two no-shows: slot 5 (DEF) and slot 9 (MID). Lowest is 5. Replacing a DEF
    # with the DEF on slot 13 keeps 1-4-4-2, so slot 5 is the one covered.
    stats = _stats()
    stats[5] = [_row(minutes=0)]
    stats[9] = [_row(minutes=0)]
    stats[13] = [_row(goals_scored=1)]   # DEF: 2 + 6 = 8
    result = score_selection(_squad(), stats, POSITIONS)
    assert _line(result, 13).role == "auto_sub_cover"
    assert _line(result, 13).covers_player_id == 5
    assert _line(result, 5).role == "auto_sub_replaced"
    assert _line(result, 5).counted is False
    # Only one outfield Auto Sub exists, so slot 9 is NOT covered. He keeps the
    # plain 'starter' role, remains a scoring slot, and simply contributes 0 --
    # which is exactly the distinction auto_sub_replaced exists to draw.
    assert _covered(result) == {5}
    assert _line(result, 9).role == "starter"
    assert _line(result, 9).counted is True
    assert _line(result, 9).general_points == 0
    assert result.raw_points == 9 * 2 + 8


def _thin_defence():
    """1 GK / 3 DEF / 4 MID / 3 FWD, with a FORWARD as the outfield Auto Sub.

    Three defenders is the legal minimum, so losing one and covering him with
    anything other than a defender is illegal -- which is what makes the block
    testable at all. The 1-4-4-2 default cannot show this: it has a spare
    defender.
    """
    positions = {1: "GK",
                 2: "DEF", 3: "DEF", 4: "DEF",
                 5: "MID", 6: "MID", 7: "MID", 8: "MID",
                 9: "FWD", 10: "FWD", 11: "FWD",
                 12: "GK", 13: "FWD", 14: "MID", 15: "FWD"}
    selection = Selection(
        tactic="balanced",
        slots=[Slot(position_slot=i, player_id=i, is_bonus=(i in (5, 6)))
               for i in range(1, 16)],
        swaps=[],
    )
    return selection, positions


def test_a_formation_that_would_become_illegal_blocks_the_cover():
    selection, positions = _thin_defence()
    stats = _stats()
    stats[2] = [_row(minutes=0)]         # a defender, and only 3 were named
    stats[13] = [_row(goals_scored=1)]   # the Auto Sub is a forward
    result = score_selection(selection, stats, positions)
    # Covering a DEF with a FWD leaves 2 defenders, below the minimum of 3.
    assert _line(result, 13).role == "bench_unused"
    assert _line(result, 13).counted is False
    assert _covered(result) == set()          # nobody was replaced
    assert result.raw_points == 10 * 2


def test_the_auto_sub_skips_an_illegal_slot_and_covers_a_later_legal_one():
    # Same thin defence, but now a forward ALSO fails to appear. Slot 2 is the
    # lower slot and is tried first; covering it is illegal, so the Auto Sub
    # moves on to slot 9, where replacing a FWD with a FWD is fine.
    selection, positions = _thin_defence()
    stats = _stats()
    stats[2] = [_row(minutes=0)]
    stats[9] = [_row(minutes=0)]
    stats[13] = [_row(goals_scored=1)]   # FWD: 2 + 4 = 6
    result = score_selection(selection, stats, positions)
    assert _line(result, 13).role == "auto_sub_cover"
    assert _line(result, 13).covers_player_id == 9
    assert _covered(result) == {9}               # slot 2 was skipped, not covered
    assert result.raw_points == 9 * 2 + 6


def test_the_midfield_floor_is_three_so_a_third_midfielder_cannot_be_covered_away():
    # D1: the minimum is 3 MID, not 2. A 3-MID XI that loses a midfielder
    # cannot be covered by a non-midfielder -- that would leave 2.
    positions = {1: "GK",
                 2: "DEF", 3: "DEF", 4: "DEF", 5: "DEF",
                 6: "MID", 7: "MID", 8: "MID",
                 9: "FWD", 10: "FWD", 11: "FWD",
                 12: "GK", 13: "DEF", 14: "MID", 15: "FWD"}
    selection = Selection(
        tactic="balanced",
        slots=[Slot(position_slot=i, player_id=i, is_bonus=(i in (6, 7)))
               for i in range(1, 16)],
        swaps=[],
    )
    stats = _stats()
    stats[8] = [_row(minutes=0)]          # a midfielder, and only 3 were named
    stats[13] = [_row(goals_scored=1)]    # the Auto Sub is a defender
    result = score_selection(selection, stats, positions)
    assert _line(result, 13).role == "bench_unused"
    assert _covered(result) == set()
    assert _line(result, 8).role == "starter"     # not replaced
    assert result.raw_points == 10 * 2


def test_five_two_three_is_no_longer_a_legal_shape_to_arrive_at():
    # D1 removes exactly one formation: 5-2-3. Reaching it by Auto Sub must be
    # refused -- here a 5-3-2 XI loses a midfielder and the only Auto Sub is a
    # forward, which would produce 5 DEF / 2 MID / 3 FWD.
    positions = {1: "GK",
                 2: "DEF", 3: "DEF", 4: "DEF", 5: "DEF", 6: "DEF",
                 7: "MID", 8: "MID", 9: "MID",
                 10: "FWD", 11: "FWD",
                 12: "GK", 13: "FWD", 14: "MID", 15: "MID"}
    selection = Selection(
        tactic="balanced",
        slots=[Slot(position_slot=i, player_id=i, is_bonus=(i in (7, 8)))
               for i in range(1, 16)],
        swaps=[],
    )
    stats = _stats()
    stats[9] = [_row(minutes=0)]
    stats[13] = [_row(goals_scored=1)]
    result = score_selection(selection, stats, positions)
    assert _line(result, 13).role == "bench_unused"
    assert _covered(result) == set()


# ---- D2: the sixth role ----------------------------------------------------

def test_a_replaced_starter_is_labelled_auto_sub_replaced_and_stops_counting():
    stats = _stats()
    stats[5] = [_row(minutes=0)]          # DEF no-show
    stats[13] = [_row(goals_scored=1)]    # DEF Auto Sub: 2 + 6 = 8
    result = score_selection(_squad(), stats, POSITIONS)

    replaced = _line(result, 5)
    assert replaced.role == "auto_sub_replaced"
    assert replaced.general_points == 0
    assert replaced.counted is False

    cover = _line(result, 13)
    assert cover.role == "auto_sub_cover"
    assert cover.covers_player_id == 5
    assert cover.counted is True

    # The replaced starter contributes nothing; the cover contributes instead.
    assert result.raw_points == 10 * 2 + 8


def test_the_six_roles_are_the_only_ones_the_breakdown_ever_reports():
    stats = _stats()
    stats[5] = [_row(minutes=0)]          # will be replaced
    stats[13] = [_row()]                  # the cover
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert {p.role for p in result.players} == {
        "starter", "swapped_out", "swapped_in", "auto_sub_cover",
        "auto_sub_replaced", "bench_unused",
    }


def test_only_a_replaced_starter_gets_the_new_role_not_an_uncovered_no_show():
    # The whole point of A1's fix: "did not play" and "was replaced" are
    # different states and must be told apart.
    stats = _stats()
    stats[5] = [_row(minutes=0)]
    stats[13] = [_row(minutes=0)]         # the Auto Sub did not play either
    result = score_selection(_squad(), stats, POSITIONS)
    assert _line(result, 5).role == "starter"
    assert _line(result, 5).counted is True
    assert _line(result, 13).role == "bench_unused"


# ---- step 5: tactical points ----------------------------------------------

def test_only_bonus_players_earn_tactical_points():
    stats = _stats()
    stats[8] = [_row(goals_scored=1, creativity=Decimal("40"))]   # not a Bonus Player
    result = score_selection(_squad(tactic="balanced", bonus=(6, 7)), stats, POSITIONS)
    assert result.tactical_points == 0
    assert _line(result, 8).tactical_points == 0


def test_a_bonus_player_who_did_not_appear_earns_no_tactical_points():
    stats = _stats()
    stats[6] = [_row(minutes=0, creativity=Decimal("40"))]
    result = score_selection(_squad(bonus=(6, 7)), stats, POSITIONS)
    assert _line(result, 6).tactical_points == 0
    assert result.tactical_points == 0


def test_bonus_status_does_not_pass_to_the_auto_sub_who_covers_him():
    # The cover earns General Points only, never the Bonus Player's tactic.
    stats = _stats()
    stats[6] = [_row(minutes=0)]                      # Bonus MID no-show
    stats[13] = [_row(goals_scored=1, creativity=Decimal("40"))]
    positions = dict(POSITIONS)
    positions[13] = "MID"                             # so the cover is legal
    sel = Selection(tactic="balanced",
                    slots=[Slot(i, i, i in (6, 7)) for i in range(1, 16)],
                    swaps=[])
    result = score_selection(sel, stats, positions)
    assert _line(result, 13).role == "auto_sub_cover"
    assert _line(result, 13).general_points == 7
    assert _line(result, 13).tactical_points == 0
    assert result.tactical_points == 0


def test_tactical_tiers_are_evaluated_per_fixture_then_summed_in_a_double_gameweek():
    # Two fixtures of 25 creativity each. Per fixture that is tier 1 -> 1 + 1 = 2.
    # Summing first (50) and then tiering would give 3, which is the bug.
    stats = _stats()
    stats[6] = [_row(creativity=Decimal("25")), _row(creativity=Decimal("25"))]
    result = score_selection(_squad(bonus=(6, 7)), stats, POSITIONS)
    assert _line(result, 6).tactical_points == 2


def test_defensive_contribution_tiers_are_also_per_fixture():
    stats = _stats()
    stats[2] = [_row(defensive_contributions=5), _row(defensive_contributions=5)]
    result = score_selection(_squad(tactic="defence", bonus=(2, 3)), stats, POSITIONS)
    # 5 and 5 are each below the first tier of 8. Summing to 10 would pay 3.
    assert _line(result, 2).tactical_points == 0


def test_general_points_are_summed_across_a_double_gameweek():
    stats = _stats()
    stats[10] = [_row(goals_scored=1), _row(goals_scored=1)]   # FWD: (2+4) x 2
    result = score_selection(_squad(), stats, POSITIONS)
    assert _line(result, 10).general_points == 12


def test_a_player_appeared_if_total_minutes_across_his_fixtures_exceed_zero():
    stats = _stats()
    stats[6] = [_row(minutes=0), _row(minutes=30, creativity=Decimal("20"))]
    result = score_selection(_squad(bonus=(6, 7)), stats, POSITIONS)
    assert _line(result, 6).tactical_points == 1      # he did appear


# ---- step 6: sub bonus -----------------------------------------------------

def test_sub_bonus_is_one_per_swap_where_the_incoming_player_beats_the_outgoing():
    stats = _stats()
    stats[9] = [_row()]                   # 2
    stats[14] = [_row(goals_scored=1)]    # 7
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert result.sub_bonus == 1
    assert result.swaps[0].sub_bonus == 1


def test_a_sub_bonus_tie_gives_nothing():
    # Strictly greater, so equal scores pay 0.
    result = score_selection(_squad(swaps=((9, 14),)), _stats(), POSITIONS)
    assert result.sub_bonus == 0


def test_sub_bonus_caps_at_two_because_there_are_only_two_tactical_subs():
    stats = _stats()
    stats[14] = [_row(goals_scored=1)]
    stats[15] = [_row(goals_scored=1)]
    result = score_selection(_squad(swaps=((8, 14), (11, 15))), stats, POSITIONS)
    assert result.sub_bonus == 2


def test_sub_bonus_when_the_outgoing_player_never_appeared_needs_only_a_positive_score():
    # The accepted small giveaway named in section 1.
    stats = _stats()
    stats[9] = [_row(minutes=0)]          # 0
    stats[14] = [_row(minutes=1)]         # 1
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert result.sub_bonus == 1


def test_sub_bonus_compares_general_points_only_not_tactical():
    # Player 9 is a Bonus Player... which the schema forbids as an outgoing
    # player, so this asserts the comparison basis using a non-Bonus pair and
    # a tactic that would otherwise inflate the incoming player.
    stats = _stats()
    stats[9] = [_row(goals_scored=1)]     # general 7
    stats[14] = [_row(creativity=Decimal("40"))]   # general 2, tactical would be 3
    result = score_selection(_squad(swaps=((9, 14),)), stats, POSITIONS)
    assert result.sub_bonus == 0


# ---- missing data ----------------------------------------------------------

def test_a_player_with_no_stat_rows_at_all_scores_zero_and_did_not_appear():
    stats = _stats()
    del stats[6]
    result = score_selection(_squad(bonus=(6, 7)), stats, POSITIONS)
    assert _line(result, 6).general_points == 0
    assert _line(result, 6).tactical_points == 0


def test_an_unknown_position_is_rejected_rather_than_silently_scoring_zero():
    positions = dict(POSITIONS)
    positions[6] = "SWEEPER"
    with pytest.raises(KeyError):
        score_selection(_squad(), _stats(), positions)
