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
from Results.scoring import _component_score
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

    `bonus` and `defensive_contributions` are real columns _component_score
    reads; every field must be present because it accesses them directly.
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
    assert set(rules) == {"classic", "dream11"}


def test_both_rulesets_arrive_in_one_response(rules):
    """The Dream11 screen exists to say where the two games differ, which is
    only sound if both halves came from the same read."""
    assert rules["classic"]["assist"] != rules["dream11"]["assist"]


# ------------------------------------- published values match classic scoring


@pytest.mark.parametrize("position", POSITIONS)
def test_published_goal_value_is_what_the_scorer_pays(rules, position):
    scored = _component_score(_classic_row(position, goals_scored=1))
    assert scored == rules["classic"]["goal"][position]


@pytest.mark.parametrize("position", POSITIONS)
def test_published_assist_value_is_what_the_scorer_pays(rules, position):
    scored = _component_score(_classic_row(position, assists=1))
    assert scored == rules["classic"]["assist"]


@pytest.mark.parametrize("position", POSITIONS)
def test_published_clean_sheet_value_and_threshold_match(rules, position):
    minutes = rules["classic"]["clean_sheet_minutes"]
    earned = _component_score(_classic_row(position, minutes=minutes, clean_sheets=1))
    # One minute short must NOT pay the clean sheet -- that's the threshold
    # being real rather than decorative.
    missed = _component_score(_classic_row(position, minutes=minutes - 1, clean_sheets=1))
    appearance = rules["classic"]["appearance"]

    assert earned - appearance["full"] == rules["classic"]["clean_sheet"][position]
    assert missed == appearance["partial"]


def test_published_appearance_values_match(rules):
    """Appearance points are only reachable through the component path, which
    needs some other stat present to engage (see _component_score's
    component_signal -- a row with minutes alone defers to FPL's total_points
    instead). So each case carries one bonus point and the assertion nets it
    out; seeding minutes alone would test the fallback, not the rule."""
    appearance = rules["classic"]["appearance"]

    partial = _component_score(_classic_row("MID", minutes=1, bonus=1))
    full = _component_score(_classic_row("MID", minutes=appearance["full_minutes"], bonus=1))

    assert partial - 1 == appearance["partial"]
    assert full - 1 == appearance["full"]
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
    scored = _component_score(_classic_row("MID", **{stat: 1}))
    assert scored == rules["classic"][key]
    # Guards the sign convention: these are stored negative, and a refactor
    # that flipped one would otherwise still "match" a flipped constant.
    assert rules["classic"][key] < 0


def test_published_saves_tier_matches(rules):
    saves = rules["classic"]["saves"]
    # Integer division: one short of the divisor pays nothing.
    assert _component_score(_classic_row("GK", saves=saves["per"] - 1)) == 0
    assert _component_score(_classic_row("GK", saves=saves["per"])) == saves["points"]


def test_published_goals_conceded_tier_matches(rules):
    conceded = rules["classic"]["goals_conceded"]
    assert _component_score(_classic_row("DEF", goals_conceded=conceded["per"] - 1)) == 0
    assert (
        _component_score(_classic_row("DEF", goals_conceded=conceded["per"]))
        == conceded["points"]
    )
    # GK/DEF only -- a midfielder is untouched by concessions.
    assert _component_score(_classic_row("MID", goals_conceded=conceded["per"])) == 0


def test_published_penalty_saved_matches(rules):
    scored = _component_score(_classic_row("GK", penalties_saved=1))
    assert scored == rules["classic"]["penalty_saved"]


def test_published_defcon_threshold_matches_per_position(rules):
    points = rules["classic"]["defensive_contribution"]
    thresholds = rules["classic"]["defensive_contribution_threshold"]

    for position in ("DEF", "MID", "FWD"):
        threshold = thresholds[position]
        at = _component_score(_classic_row(position, defensive_contributions=threshold))
        below = _component_score(_classic_row(position, defensive_contributions=threshold - 1))
        assert at == points, position
        assert below == 0, position


def test_goalkeepers_are_published_as_ineligible_for_defcon(rules):
    """GK is served as 0 to mean "no threshold reaches it". The scorer must
    genuinely pay a keeper nothing however many actions they rack up."""
    assert rules["classic"]["defensive_contribution_threshold"]["GK"] == 0
    assert _component_score(_classic_row("GK", defensive_contributions=99)) == 0


def test_transfer_hit_is_published_as_the_manager_experiences_it(rules):
    """Shared.rules stores HIT_COST as a positive cost the scorer subtracts;
    the endpoint negates it so the screen can render it beside the other
    signed values without inventing the minus itself."""
    from Shared.rules import HIT_COST

    assert rules["classic"]["transfer_hit"] == -HIT_COST
    assert rules["classic"]["transfer_hit"] < 0


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


def test_published_multipliers_are_wired_to_the_right_fields(rules):
    """Multipliers are applied against a whole squad in score_contest/
    score_gameweek, which need a database, so these check the wiring rather
    than the arithmetic: that each published field carries the constant it
    claims to. Catches a captain/vice transposition, which is invisible to a
    literal-table test because both values would still be "present"."""
    from Game_logic.dream11_scoring import CAPTAIN_MULTIPLIER, VICE_CAPTAIN_MULTIPLIER
    from Shared.rules import CAPTAIN_MULTIPLIER as CLASSIC_CAPTAIN
    from Shared.rules import TRIPLE_CAPTAIN_MULTIPLIER

    assert rules["dream11"]["captain_multiplier"] == CAPTAIN_MULTIPLIER
    assert rules["dream11"]["vice_captain_multiplier"] == VICE_CAPTAIN_MULTIPLIER
    assert rules["classic"]["captain_multiplier"] == CLASSIC_CAPTAIN
    assert rules["classic"]["triple_captain_multiplier"] == TRIPLE_CAPTAIN_MULTIPLIER

    # The ordering that makes the armbands mean anything.
    assert rules["dream11"]["vice_captain_multiplier"] < rules["dream11"]["captain_multiplier"]
    assert rules["classic"]["captain_multiplier"] < rules["classic"]["triple_captain_multiplier"]


def test_published_squad_shape_matches_the_rules_module(rules):
    """The classic screen's Bench Boost note says "11 starters and 4 bench
    subs"; Dream11's says the starting 11 is final with no bench at all."""
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
