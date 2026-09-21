"""Phase 3: validate_selection, the pure selection validator.

No database, no network -- see this directory's conftest.py.

It COLLECTS every error rather than raising on the first, which is the existing
422 pattern in this codebase: a manager fixing a lineup should be told
everything that is wrong in one response, not made to resubmit five times.

Positions are resolved by POSITION, never by slot (decision A4): slot 1 is not
assumed to be the goalkeeper, and neither is slot 12. The validator requires
exactly 1 GK among slots 1-11 and a GK in slot 12, but it learns which players
those are from the position map.
"""
from datetime import datetime, timedelta, timezone

import pytest

from Gameplay.selection_rules import SelectionInput, SwapInput, validate_selection

T0 = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)

# 15 ids, one squad: 2 GK / 5 DEF / 5 MID / 3 FWD.
GK = [1, 12]
DEF = [2, 3, 4, 5, 13]
MID = [6, 7, 8, 9, 14]
FWD = [10, 11, 15]
SQUAD = set(GK + DEF + MID + FWD)
POSITIONS = ({p: "GK" for p in GK} | {p: "DEF" for p in DEF}
             | {p: "MID" for p in MID} | {p: "FWD" for p in FWD})

# Everyone kicks off at the same time unless a test says otherwise.
FIXTURES = {pid: [T0] for pid in SQUAD}


def _sel(**kw):
    """1-4-4-2: XI = GK 1, DEF 2-5, MID 6-9, FWD 10-11.
    Bench = 12 GK, 13 DEF, 14 MID, 15 FWD. Balanced, Bonus = MID 6 and 7."""
    base = dict(
        season="2026-27",
        gameweek=6,
        tactic="balanced",
        player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
        bench_order=[12, 13, 14, 15],
        bonus_player_ids=[6, 7],
        swaps=[],
    )
    base.update(kw)
    return SelectionInput(**base)


def _errors(sel=None, squad=None, positions=None, fixtures=None):
    return validate_selection(
        sel or _sel(),
        squad if squad is not None else SQUAD,
        positions if positions is not None else POSITIONS,
        fixtures if fixtures is not None else FIXTURES,
    )


def _has(errors, fragment):
    return any(fragment in e for e in errors)


# ---- the happy path --------------------------------------------------------

def test_a_valid_selection_produces_no_errors():
    assert _errors() == []


@pytest.mark.parametrize("tactic,bonus", [
    ("attack", [10, 11]),
    ("defence", [2, 3]),
    ("balanced", [6, 7]),
])
def test_each_tactic_accepts_its_own_bonus_positions(tactic, bonus):
    assert _errors(_sel(tactic=tactic, bonus_player_ids=bonus)) == []


# ---- squad membership and slots -------------------------------------------

def test_the_fifteen_must_be_the_managers_own_squad():
    errors = _errors(_sel(player_ids=[99, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]))
    assert _has(errors, "not in")


def test_a_player_cannot_be_named_twice():
    errors = _errors(_sel(player_ids=[1, 2, 2, 4, 5, 6, 7, 8, 9, 10, 11]))
    assert _has(errors, "duplicate")


def test_the_xi_must_hold_exactly_eleven():
    assert _has(_errors(_sel(player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10])), "11")


def test_the_bench_must_hold_exactly_four():
    assert _has(_errors(_sel(bench_order=[12, 13, 14])), "4")


def test_a_player_cannot_be_on_the_bench_and_in_the_xi():
    errors = _errors(_sel(player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 12],
                          bench_order=[11, 13, 14, 15]))
    # 12 is a GK in the XI alongside GK 1 -> two keepers, and the shape breaks.
    assert errors


# ---- formation, by position not by slot (A4) -------------------------------

def test_exactly_one_goalkeeper_in_the_starting_xi():
    # Two keepers in the XI, none on the bench.
    errors = _errors(_sel(player_ids=[1, 12, 3, 4, 5, 6, 7, 8, 9, 10, 11],
                          bench_order=[2, 13, 14, 15]))
    assert _has(errors, "exactly 1 GK")


def test_no_goalkeeper_in_the_starting_xi_is_also_rejected():
    errors = _errors(_sel(player_ids=[13, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11],
                          bench_order=[12, 1, 14, 15]))
    assert _has(errors, "exactly 1 GK")


def test_the_midfield_floor_is_three():
    # 5-2-3, the one formation decision D1 removes.
    errors = _errors(_sel(player_ids=[1, 2, 3, 4, 5, 13, 6, 7, 10, 11, 15],
                          bench_order=[12, 8, 9, 14],
                          bonus_player_ids=[6, 7]))
    assert _has(errors, "MID")


def test_three_midfielders_is_accepted():
    # 4-3-3 is legal.
    errors = _errors(_sel(player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 10, 11, 15],
                          bench_order=[12, 13, 9, 14],
                          bonus_player_ids=[6, 7]))
    assert errors == []


def test_the_defence_floor_is_three():
    errors = _errors(_sel(player_ids=[1, 2, 3, 6, 7, 8, 9, 14, 10, 11, 15],
                          bench_order=[12, 4, 5, 13],
                          bonus_player_ids=[6, 7]))
    assert _has(errors, "DEF")


def test_at_least_one_forward_is_required():
    errors = _errors(_sel(player_ids=[1, 2, 3, 4, 5, 13, 6, 7, 8, 9, 14],
                          bench_order=[12, 10, 11, 15],
                          bonus_player_ids=[6, 7]))
    assert _has(errors, "FWD")


def test_slot_twelve_must_be_a_goalkeeper():
    # Bench reordered so the keeper sits at slot 13 instead of 12.
    errors = _errors(_sel(bench_order=[13, 12, 14, 15]))
    assert _has(errors, "slot 12")


def test_slots_thirteen_to_fifteen_must_be_outfield():
    errors = _errors(_sel(bench_order=[12, 13, 14, 15]))
    assert errors == []          # control: the keeper is at 12 and only there


# ---- tactic ----------------------------------------------------------------

def test_an_unknown_tactic_is_rejected():
    assert _has(_errors(_sel(tactic="parking_the_bus")), "tactic")


def test_attack_needs_two_forwards_in_the_xi():
    # 4-5-1: only one forward, so Attack is unavailable.
    errors = _errors(_sel(tactic="attack",
                          player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 10],
                          bench_order=[12, 13, 11, 15],
                          bonus_player_ids=[10, 11]))
    assert _has(errors, "Attack")


def test_attack_is_available_with_two_forwards():
    assert _errors(_sel(tactic="attack", bonus_player_ids=[10, 11])) == []


# ---- bonus players ---------------------------------------------------------

def test_exactly_two_bonus_players_are_required():
    assert _has(_errors(_sel(bonus_player_ids=[6])), "exactly 2")
    assert _has(_errors(_sel(bonus_player_ids=[6, 7, 8])), "exactly 2")


def test_the_two_bonus_players_must_be_different():
    assert _has(_errors(_sel(bonus_player_ids=[6, 6])), "distinct")


def test_a_bonus_player_must_be_a_starter():
    # 14 is a midfielder, but on the bench.
    assert _has(_errors(_sel(bonus_player_ids=[6, 14])), "starting XI")


def test_bonus_players_must_match_the_tactics_position():
    assert _has(_errors(_sel(tactic="balanced", bonus_player_ids=[2, 3])), "MID")
    assert _has(_errors(_sel(tactic="defence", bonus_player_ids=[6, 7])), "DEF")
    assert _has(_errors(_sel(tactic="attack", bonus_player_ids=[6, 7])), "FWD")


def test_a_goalkeeper_can_never_be_a_bonus_player():
    assert _errors(_sel(bonus_player_ids=[1, 6])) != []


# ---- swaps -----------------------------------------------------------------

def _fixtures(**overrides):
    f = {pid: [T0] for pid in SQUAD}
    f.update(overrides)
    return f


def test_a_legal_swap_is_accepted():
    # MID 9 out, MID 14 in. 14 kicks off after 9's fixture has ended.
    fixtures = _fixtures()
    fixtures[9] = [T0]
    fixtures[14] = [T0 + timedelta(minutes=120)]
    assert _errors(_sel(swaps=[SwapInput(9, 14)]), fixtures=fixtures) == []


def test_more_than_two_swaps_are_rejected():
    # D7: the endpoint is where this is caught, not the engine.
    fixtures = _fixtures()
    for pid in (13, 14, 15):
        fixtures[pid] = [T0 + timedelta(minutes=120)]
    errors = _errors(_sel(swaps=[SwapInput(9, 14), SwapInput(11, 15), SwapInput(8, 13)]),
                     fixtures=fixtures)
    assert _has(errors, "at most 2")


def test_the_outgoing_player_must_be_a_starter():
    fixtures = _fixtures()
    fixtures[14] = [T0 + timedelta(minutes=120)]
    assert _has(_errors(_sel(swaps=[SwapInput(13, 14)]), fixtures=fixtures), "starter")


def test_the_outgoing_player_must_not_be_a_bonus_player():
    fixtures = _fixtures()
    fixtures[14] = [T0 + timedelta(minutes=120)]
    assert _has(_errors(_sel(swaps=[SwapInput(6, 14)]), fixtures=fixtures), "Bonus")


def test_the_incoming_player_must_be_in_slot_fourteen_or_fifteen():
    fixtures = _fixtures()
    fixtures[13] = [T0 + timedelta(minutes=120)]
    assert _has(_errors(_sel(swaps=[SwapInput(9, 13)]), fixtures=fixtures), "Tactical Sub")


def test_both_players_must_share_a_position():
    fixtures = _fixtures()
    fixtures[15] = [T0 + timedelta(minutes=120)]
    # MID 9 out, FWD 15 in.
    assert _has(_errors(_sel(swaps=[SwapInput(9, 15)]), fixtures=fixtures), "same position")


def test_no_player_may_be_used_in_two_swaps():
    fixtures = _fixtures()
    fixtures[14] = [T0 + timedelta(minutes=120)]
    fixtures[15] = [T0 + timedelta(minutes=120)]
    errors = _errors(_sel(swaps=[SwapInput(9, 14), SwapInput(9, 15)]), fixtures=fixtures)
    assert _has(errors, "more than one swap")


def test_both_players_need_a_fixture_this_gameweek():
    fixtures = _fixtures()
    fixtures[14] = []                       # no fixture at all
    assert _has(_errors(_sel(swaps=[SwapInput(9, 14)]), fixtures=fixtures), "fixture")

    fixtures = _fixtures()
    fixtures[9] = []
    fixtures[14] = [T0 + timedelta(minutes=120)]
    assert _has(_errors(_sel(swaps=[SwapInput(9, 14)]), fixtures=fixtures), "fixture")


def test_the_incoming_kickoff_must_be_after_the_outgoing_fixture_ends():
    # FIXTURE_DURATION_MIN is 115, so a kickoff 114 minutes later is too early
    # and 115 minutes later is exactly on the boundary and still too early --
    # it must be strictly after the end.
    for offset, ok in ((114, False), (115, False), (116, True)):
        fixtures = _fixtures()
        fixtures[9] = [T0]
        fixtures[14] = [T0 + timedelta(minutes=offset)]
        errors = _errors(_sel(swaps=[SwapInput(9, 14)]), fixtures=fixtures)
        assert (errors == []) is ok, f"offset {offset} expected ok={ok}, got {errors}"


def test_a_double_gameweek_treats_each_players_fixtures_as_one_block():
    # The outgoing player plays twice; the incoming player must come after his
    # LAST fixture ends, not his first.
    fixtures = _fixtures()
    fixtures[9] = [T0, T0 + timedelta(days=3)]
    fixtures[14] = [T0 + timedelta(minutes=200)]      # after the first, not the last
    assert _has(_errors(_sel(swaps=[SwapInput(9, 14)]), fixtures=fixtures), "kickoff")

    fixtures[14] = [T0 + timedelta(days=3, minutes=116)]
    assert _errors(_sel(swaps=[SwapInput(9, 14)]), fixtures=fixtures) == []


# ---- collect-all -----------------------------------------------------------

def test_every_error_is_collected_in_one_response():
    # A submission wrong in four independent ways.
    sel = _sel(
        tactic="attack",                       # needs 2 FWD...
        player_ids=[1, 2, 3, 4, 5, 6, 7, 8, 9, 14, 10],   # ...but only 1 is named
        bench_order=[13, 12, 11, 15],          # keeper not at slot 12
        bonus_player_ids=[6],                  # not two, and not forwards
    )
    errors = _errors(sel)
    assert len(errors) >= 4
    assert _has(errors, "Attack")
    assert _has(errors, "slot 12")
    assert _has(errors, "exactly 2")


def test_errors_are_plain_strings_so_the_endpoint_can_return_them_as_a_422_detail():
    errors = _errors(_sel(tactic="nonsense"))
    assert errors and all(isinstance(e, str) for e in errors)
