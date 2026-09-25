"""
test_scoring_rules.py — tests for Data/scoring_rules.py's GET /scoring-rules.

The endpoint's whole reason to exist is that the frontend used to restate these
numbers in JavaScript, where nothing could catch them disagreeing with the
scorer. Asserting the response against a table of literals here would rebuild
exactly that problem one layer down -- the test would agree with itself while
both drifted away from Results/scoring.py.

So the substantive tests below drive the REAL scoring functions with synthetic
stat rows and check that what the endpoint publishes is what the scorer actually
pays. A wrong constant fails these; renumbering a rule deliberately does not,
because the scorer moves with it.

No database and no auth: the route is in PUBLIC_ROUTES and the handler only
reads module constants.
"""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from main import app
from Results.tactical_scoring import general_points
from Game_logic.dream11_scoring import calculate_dream11_points

client = TestClient(app)

POSITIONS = ("GK", "DEF", "MID", "FWD")


@pytest.fixture(scope="module")
def rules():
    resp = client.get("/scoring-rules")
    assert resp.status_code == 200, resp.text
    return resp.json()


def _classic_row(position, **stats):
    """A player who did nothing, plus whatever the caller sets.

    Phase 4c: this now feeds Results/tactical_scoring.general_points, which
    reads the same columns. `bonus` is kept in the row deliberately -- the
    tactical game removes FPL bonus entirely, and leaving the column present
    proves general_points ignores it rather than merely not being given it.
    """
    base = dict(
        position=position, minutes=0, goals_scored=0, assists=0, clean_sheets=0,
        goals_conceded=0, saves=0, bonus=0, yellow_cards=0, red_cards=0,
        own_goals=0, penalties_saved=0, penalties_missed=0,
        defensive_contributions=0, total_points=0,
    )
    base.update(stats)
    return SimpleNamespace(**base)


def _d11_row(**stats):
    base = dict(
        minutes=0, goals_scored=0, assists=0, goals_conceded=0, saves=0,
        yellow_cards=0, red_cards=0, own_goals=0, penalties_saved=0,
        penalties_missed=0,
    )
    base.update(stats)
    return SimpleNamespace(**base)


# --------------------------------------------------- the route is reachable


def test_scoring_rules_needs_no_credential(rules):
    """Public by decision (see PUBLIC_ROUTES). If this ever starts 401ing, the
    "How Points Work" screens break for a signed-out visitor."""
    assert set(rules) == {"classic", "tactical", "dream11"}


def test_both_rulesets_arrive_in_one_response(rules):
    """The Dream11 screen exists to say where the two games differ, which is
    only sound if both halves came from the same read."""
    assert rules["classic"]["assist"] != rules["dream11"]["assist"]


# ------------------------------------- published values match classic scoring


@pytest.mark.parametrize("position", POSITIONS)
def test_published_goal_value_is_what_the_scorer_pays(rules, position):
    scored = general_points(_classic_row(position, goals_scored=1), position)
    assert scored == rules["classic"]["goal"][position]


@pytest.mark.parametrize("position", POSITIONS)
def test_published_assist_value_is_what_the_scorer_pays(rules, position):
    scored = general_points(_classic_row(position, assists=1), position)
    assert scored == rules["classic"]["assist"]


@pytest.mark.parametrize("position", POSITIONS)
def test_published_clean_sheet_value_and_threshold_match(rules, position):
    minutes = rules["classic"]["clean_sheet_minutes"]
    earned = general_points(_classic_row(position, minutes=minutes, clean_sheets=1), position)
    # One minute short must NOT pay the clean sheet -- that's the threshold
    # being real rather than decorative.
    missed = general_points(_classic_row(position, minutes=minutes - 1, clean_sheets=1), position)
    appearance = rules["classic"]["appearance"]

    assert earned - appearance["full"] == rules["classic"]["clean_sheet"][position]
    assert missed == appearance["partial"]


def test_published_appearance_values_match(rules):
    """Phase 4c: this used to need a spare bonus point in each row.

    The classic _component_score had a fallback -- a row with minutes and
    nothing else deferred to FPL's own total_points instead of computing -- so
    appearance points were only reachable by seeding some other stat and
    netting it out. The tactical engine has no fallback: the point table IS the
    rule, so minutes alone is enough and the test says what it means."""
    appearance = rules["classic"]["appearance"]

    partial = general_points(_classic_row("MID", minutes=1), "MID")
    full = general_points(_classic_row("MID", minutes=appearance["full_minutes"]), "MID")

    assert partial == appearance["partial"]
    assert full == appearance["full"]
    assert appearance["partial"] < appearance["full"]


@pytest.mark.parametrize(
    "stat,key",
    [
        ("yellow_cards", "yellow_card"),
        ("red_cards", "red_card"),
        ("own_goals", "own_goal"),
        ("penalties_missed", "penalty_missed"),
    ],
)
def test_published_deductions_match_and_stay_negative(rules, stat, key):
    scored = general_points(_classic_row("MID", **{stat: 1}), "MID")
    assert scored == rules["classic"][key]
    # Guards the sign convention: these are stored negative, and a refactor
    # that flipped one would otherwise still "match" a flipped constant.
    assert rules["classic"][key] < 0


def test_published_saves_tier_matches(rules):
    saves = rules["classic"]["saves"]
    # Integer division: one short of the divisor pays nothing.
    assert general_points(_classic_row("GK", saves=saves["per"] - 1), "GK") == 0
    assert general_points(_classic_row("GK", saves=saves["per"]), "GK") == saves["points"]


def test_published_goals_conceded_tier_matches(rules):
    conceded = rules["classic"]["goals_conceded"]
    assert general_points(_classic_row("DEF", goals_conceded=conceded["per"] - 1), "DEF") == 0
    assert (
        general_points(_classic_row("DEF", goals_conceded=conceded["per"]), "DEF")
        == conceded["points"]
    )
    # GK/DEF only -- a midfielder is untouched by concessions.
    assert general_points(_classic_row("MID", goals_conceded=conceded["per"]), "MID") == 0


def test_published_penalty_saved_matches(rules):
    scored = general_points(_classic_row("GK", penalties_saved=1), "GK")
    assert scored == rules["classic"]["penalty_saved"]


def test_published_defcon_threshold_matches_per_position(rules):
    points = rules["classic"]["defensive_contribution"]
    thresholds = rules["classic"]["defensive_contribution_threshold"]

    for position in ("DEF", "MID", "FWD"):
        threshold = thresholds[position]
        at = general_points(_classic_row(position, defensive_contributions=threshold), position)
        below = general_points(_classic_row(position, defensive_contributions=threshold - 1), position)
        assert at == points, position
        assert below == 0, position


def test_goalkeepers_are_published_as_ineligible_for_defcon(rules):
    """GK is served as 0 to mean "no threshold reaches it". The scorer must
    genuinely pay a keeper nothing however many actions they rack up."""
    assert rules["classic"]["defensive_contribution_threshold"]["GK"] == 0
    assert general_points(_classic_row("GK", defensive_contributions=99), "GK") == 0


def test_published_tactical_values_match_the_rules_module(rules):
    from Shared.rules import (
        ATTACK_POINTS,
        BALANCED_GOAL_OR_ASSIST_POINTS,
        CREATIVITY_TIERS,
        DC_TIERS,
        DEFENCE_CLEAN_SHEET_POINTS,
        FIXTURE_DURATION_MIN,
        FREE_TRANSFER_BANK_CAP,
        FREE_TRANSFERS_EARNED_PER_GAMEWEEK,
        RULES_VERSION,
    )

    tactical = rules["tactical"]
    assert tactical["rules_version"] == RULES_VERSION
    assert tactical["bonus_player_count"] == 2
    assert tactical["tactical_swap_limit"] == 2
    assert tactical["auto_sub_slots"] == [12, 13]
    assert tactical["tactical_sub_slots"] == [14, 15]
    assert tactical["fixture_duration_minutes"] == FIXTURE_DURATION_MIN
    assert tactical["free_transfers_per_gameweek"] == FREE_TRANSFERS_EARNED_PER_GAMEWEEK
    assert tactical["free_transfer_bank_cap"] == FREE_TRANSFER_BANK_CAP

    assert tactical["tactics"]["attack"]["eligible_position"] == "FWD"
    assert tactical["tactics"]["attack"]["goal"] == ATTACK_POINTS["goal"]
    assert tactical["tactics"]["attack"]["assist"] == ATTACK_POINTS["assist"]

    assert tactical["tactics"]["defence"]["eligible_position"] == "DEF"
    assert tactical["tactics"]["defence"]["clean_sheet"] == DEFENCE_CLEAN_SHEET_POINTS
    assert tactical["tactics"]["defence"]["defensive_contribution_tiers"] == [
        {"threshold": threshold, "points": points} for threshold, points in DC_TIERS
    ]

    assert tactical["tactics"]["balanced"]["eligible_position"] == "MID"
    assert tactical["tactics"]["balanced"]["goal_or_assist"] == BALANCED_GOAL_OR_ASSIST_POINTS
    assert tactical["tactics"]["balanced"]["creativity_tiers"] == [
        {"threshold": float(threshold), "points": points} for threshold, points in CREATIVITY_TIERS
    ]


# ------------------------------------- published values match Dream11 scoring


@pytest.mark.parametrize("position", POSITIONS)
def test_published_d11_goal_value_matches(rules, position):
    scored = calculate_dream11_points(_d11_row(goals_scored=1), position)
    assert scored == rules["dream11"]["goal"][position]


@pytest.mark.parametrize("position", POSITIONS)
def test_published_d11_assist_value_matches(rules, position):
    scored = calculate_dream11_points(_d11_row(assists=1), position)
    assert scored == rules["dream11"]["assist"]


def test_d11_assist_really_is_the_headline_divergence(rules):
    """The Dream11 screen badges this row as differing from classic. If the two
    ever converge, that badge becomes a lie -- fail here rather than ship it."""
    assert rules["dream11"]["assist"] != rules["classic"]["assist"]
    assert rules["dream11"]["assist"] > max(rules["dream11"]["goal"].values())


@pytest.mark.parametrize("position", POSITIONS)
def test_published_d11_clean_sheet_threshold_matches(rules, position):
    minutes = rules["dream11"]["clean_sheet_minutes"]
    earned = calculate_dream11_points(_d11_row(minutes=minutes), position)
    missed = calculate_dream11_points(_d11_row(minutes=minutes - 1), position)

    assert earned == rules["dream11"]["clean_sheet"][position]
    # Dream11 pays no appearance points, so a sub-threshold shutout is a clean 0.
    assert missed == 0


def test_d11_clean_sheet_threshold_differs_from_classic(rules):
    """The other badged divergence: 54 minutes, not 60."""
    assert rules["dream11"]["clean_sheet_minutes"] != rules["classic"]["clean_sheet_minutes"]


@pytest.mark.parametrize(
    "stat,key",
    [
        ("yellow_cards", "yellow_card"),
        ("red_cards", "red_card"),
        ("own_goals", "own_goal"),
        ("penalties_missed", "penalty_missed"),
    ],
)
def test_published_d11_deductions_match(rules, stat, key):
    scored = calculate_dream11_points(_d11_row(**{stat: 1}), "MID")
    assert scored == rules["dream11"][key]
    assert rules["dream11"][key] < 0


def test_published_d11_tiers_match(rules):
    saves = rules["dream11"]["saves"]
    conceded = rules["dream11"]["goals_conceded"]

    assert calculate_dream11_points(_d11_row(saves=saves["per"] - 1), "GK") == 0
    assert calculate_dream11_points(_d11_row(saves=saves["per"]), "GK") == saves["points"]
    assert (
        calculate_dream11_points(_d11_row(goals_conceded=conceded["per"]), "DEF")
        == conceded["points"]
    )


def test_published_dream11_multipliers_are_wired_to_the_right_fields(rules):
    """Multipliers are applied against a whole squad in score_contest/
    score_gameweek, which need a database, so these check the wiring rather
    than the arithmetic: that each published field carries the constant it
    claims to. Catches a captain/vice transposition, which is invisible to a
    literal-table test because both values would still be "present"."""
    from Game_logic.dream11_scoring import CAPTAIN_MULTIPLIER, VICE_CAPTAIN_MULTIPLIER

    assert rules["dream11"]["captain_multiplier"] == CAPTAIN_MULTIPLIER
    assert rules["dream11"]["vice_captain_multiplier"] == VICE_CAPTAIN_MULTIPLIER
    assert "captain_multiplier" not in rules["classic"]
    assert "triple_captain_multiplier" not in rules["classic"]
    assert "transfer_hit" not in rules["classic"]
    assert "max_banked_free_transfers" not in rules["classic"]

    assert rules["dream11"]["vice_captain_multiplier"] < rules["dream11"]["captain_multiplier"]


def test_published_squad_shape_matches_the_rules_module(rules):
    """The FPL rules publish the 15-player squad shape; Dream11's team is the
    starting 11 only."""
    from Shared.rules import BENCH_SIZE, SQUAD_SIZE, STARTING_XI_SIZE

    assert rules["classic"]["squad_size"] == SQUAD_SIZE
    assert rules["classic"]["starting_xi_size"] == STARTING_XI_SIZE
    assert rules["classic"]["bench_size"] == BENCH_SIZE
    assert (
        rules["classic"]["starting_xi_size"] + rules["classic"]["bench_size"]
        == rules["classic"]["squad_size"]
    )
    # Dream11 has no bench field at all -- its team IS the starting eleven.
    assert "bench_size" not in rules["dream11"]
    assert rules["dream11"]["team_size"] == rules["classic"]["starting_xi_size"]
