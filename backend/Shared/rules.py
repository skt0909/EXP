"""
rules.py — pure FPL game rules: constants and calculations with no
database, no I/O, and no side effects.

Nothing in this module may import from another project module, open a
connection, or read the environment. That constraint is what makes it
importable from anywhere (endpoints, scoring, Celery tasks, tests)
without dragging a stack behind it, and what lets the rules be tested on
hand-built data instead of a seeded database.

WHY THIS LIVES IN Shared/. It sits underneath every box rather than
inside one. Gameplay reads it for squad and transfer validation, Results
reads it for scoring and standings, and it imports nothing back -- so
putting it in any single package would have made two of them depend on a
third for what is really a constant table. Its zero-import rule above is
what makes that safe, and it is the same reasoning that moved
deadlines.py here alongside it.

Rules live here; the code that READS THE DATABASE to feed them stays in
its own module. transfers.py's free_transfers_available(conn, ...) is
the pattern: it fetches each prior gameweek's consumption and hands a
plain dict to _free_transfers_available() below.

WHAT IS DELIBERATELY NOT HERE:

  * starting_xi.py's chip and formation constants. That file is three
    concerns in one (lineup, chips, Free Hit snapshots) and its rules
    are entangled with the validation that applies them; it needs its
    own change, not a constant move.
  * dream11_scoring.py's point weightings. Dream11 is a separate game
    mode with its own rulebook -- its CAPTAIN_MULTIPLIER is 2.0 with a
    1.5 vice bonus, deliberately unlike classic FPL's 2/3. Merging the
    two sets would conflate two games rather than centralise one.
  * leagues.py's LEAGUE_TYPES / DEFAULT_MAX_MEMBERS. Those are product
    configuration, not rules of the game.
"""

# --- rules version ----------------------------------------------------
#
# Which generation of the rules in this module is currently in force.
# Stamped onto every gw_scores row at write time so a gameweek scored
# under one rule set can be told apart from one scored under another --
# see migration c9a04e7b53d1 for why history is frozen rather than
# rescored.
#
#   1  The original scoring: gw_scores took ml.player_gw_stats.total_points
#      wholesale, free transfers were a flat 1 per gameweek with no
#      banking, chips were one set per season, and sell price was frozen
#      at purchase price.
#
#   2  Current. Component-based scoring (including defensive
#      contributions), free-transfer banking capped at 5, two half-season
#      chip sets, and the half-profit sell price.
#
# BUMP THIS BY HAND when shipping a change to scoring, banking or pricing
# that alters what a manager's points would be -- and only then. It is
# deliberately manual rather than derived from a migration hash or git
# SHA: "does this change what a score would come out as?" is a semantic
# judgement no automation can make, and a version that moved on every
# unrelated commit would mean nothing. Bumping it is the same kind of
# deliberate act as writing the migration itself.
#
# Bumping does NOT rescore anything. Existing rows keep the version they
# were written under; only gameweeks scored after the bump carry the new
# one.
CURRENT_RULES_VERSION = 2


# --- free transfers ---------------------------------------------------

FREE_TRANSFERS_EARNED_PER_GAMEWEEK = 1
MAX_BANKED_FREE_TRANSFERS = 5

# The gameweek the recurrence starts from. A manager whose first transfer
# is in, say, gameweek 20 replays 19 empty gameweeks and simply arrives at
# the cap -- this app has no "joined at gameweek N" concept, and the
# allowance is a property of the season rather than of when someone
# started, so arriving at 5 is the correct reading of "unused free
# transfers accumulate up to 5".
FIRST_GAMEWEEK = 1

# Points charged per transfer beyond the free allowance.
HIT_COST = 4

MAX_TRANSFERS_PER_GAMEWEEK = 20

# Active this gameweek -> every transfer is free, and the per-gameweek
# cap above does not apply.
FREE_CHIPS = {"wildcard", "free_hit"}


def _free_transfers_available(used_by_gameweek: dict[int, int], gameweek: int) -> int:
    """The banking recurrence, as a pure function of prior consumption.

    used_by_gameweek maps a gameweek to how many of ITS allowance were
    spent (its is_free = TRUE row count). Gameweeks absent from the map
    spent nothing, which is how a manager who sat out several gameweeks
    accumulates.

    Kept pure and separate from the query that feeds it so the rule can
    be tested directly on hand-built histories -- including ones that
    would take a whole season of HTTP calls to reach, like "banked 5 and
    stayed there".
    """
    available = min(MAX_BANKED_FREE_TRANSFERS, FREE_TRANSFERS_EARNED_PER_GAMEWEEK)
    for gw in range(FIRST_GAMEWEEK, gameweek):
        # max(0, ...) first: overspending a gameweek costs points, it does
        # not leave a debt for the next one to pay off.
        carried = max(0, available - used_by_gameweek.get(gw, 0))
        available = min(
            MAX_BANKED_FREE_TRANSFERS, carried + FREE_TRANSFERS_EARNED_PER_GAMEWEEK
        )
    return available


# --- pricing ----------------------------------------------------------


def _selling_price(purchase_price: int, current_price: int) -> int:
    """What a manager gets back for a player, in tenths of £m.

    Rises are only half returned, rounded DOWN to the nearest £0.1m --
    buy at £7.0m and sell at £7.1m and you still get £7.0m; at £7.2m you
    get £7.1m. Falls are absorbed in full and immediately: there is no
    protection on the way down.

    Integer division is the rounding rule, not an approximation of one.
    Prices are held in tenths throughout (see BUDGET_CAP), so
    `profit // 2` IS "half the rise, rounded down to the nearest £0.1m".

    Pure: the caller supplies both prices. transfers.py reads the current
    one from ml.players.now_cost and the purchase price from the
    squad_players row, and calls this twice per transfer -- once to
    validate the budget, once to record the price actually paid. Those
    two must agree, which is the reason this is one function rather than
    an expression written out at each site.
    """
    if current_price <= purchase_price:
        return current_price
    profit = current_price - purchase_price
    return purchase_price + (profit // 2)


# --- squad composition ------------------------------------------------

SQUAD_SIZE = 15
POSITION_REQUIREMENTS = {"GK": 2, "DEF": 5, "MID": 5, "FWD": 3}
BUDGET_CAP = 1000  # tenths of £m, e.g. 1000 = £100.0m -- matches ml.players.cost_start's scale
MAX_PER_CLUB = 3

# The 15 split into a starting XI and a 4-man bench. Also the slot
# boundary: starting_xi.position_slot 1-11 is the XI, 12-15 the bench in
# substitution priority order.
STARTING_XI_SIZE = 11
BENCH_SIZE = 4


# --- chips ------------------------------------------------------------
#
# Two sets per season, one per half, with the boundary at the gameweek
# below: a chip unused in the first half is forfeited rather than carried
# over. Enforced in application code (Gameplay/chips.py, and the
# validation still in starting_xi.py) and independently by the
# enforce_chip_limit_fn trigger.

RESTRICTED_CHIPS = {"bench_boost", "triple_captain", "free_hit"}  # 1 use per half-season
VALID_CHIPS = RESTRICTED_CHIPS | {"wildcard"}  # wildcard: 1 use per half-season
FIRST_HALF_LAST_GAMEWEEK = 19


# --- captaincy --------------------------------------------------------
#
# These existed verbatim in BOTH scoring.py and team_dashboard.py, which
# had to agree or the dashboard's multiplier display would contradict the
# points it was explaining. Confirmed identical (2 and 3) at the time they
# were merged -- they had not drifted.

CAPTAIN_MULTIPLIER = 2
TRIPLE_CAPTAIN_MULTIPLIER = 3


# --- scoring point values ---------------------------------------------

GOAL_POINTS = {"GK": 10, "DEF": 6, "MID": 5, "FWD": 4}
CLEAN_SHEET_POINTS = {"GK": 4, "DEF": 4, "MID": 1, "FWD": 0}

# Defensive contributions: a defender needs 10 combined actions to earn
# the +2, a midfielder or forward 12. Previously inline literals in
# scoring.py's _component_score. Goalkeepers are not eligible at all.
DEF_CONTRIBUTION_THRESHOLD = 10
MID_FWD_CONTRIBUTION_THRESHOLD = 12
DEF_CONTRIBUTION_POINTS = 2

# The rest of _component_score's values, extracted for the same reason the
# DefCon thresholds above were: they were inline literals, and the moment
# anything other than the scorer had to state them -- GET /scoring-rules,
# which tells managers how points are earned -- a second copy would have
# been able to disagree with the first without any test noticing. Naming
# them makes the scorer and the endpoint read the same constant, so the
# published rules cannot drift from the arithmetic that applies them.
#
# Deductions are stored NEGATIVE rather than as magnitudes subtracted at
# the call site. `points += YELLOW_CARD_POINTS` cannot be got backwards;
# `points -= YELLOW_CARD_POINTS` with a positive constant can, and the
# sign convention here now matches dream11_scoring.py's, which already
# stored them this way.
APPEARANCE_POINTS = 1  # played at all
APPEARANCE_FULL_POINTS = 2  # played the threshold below or more
APPEARANCE_FULL_MINUTES = 60

ASSIST_POINTS = 3

# A clean sheet needs BOTH the shutout and 60 minutes. Dream11's
# equivalent threshold is 54, deliberately -- see dream11_scoring.py.
CLEAN_SHEET_MINUTES_THRESHOLD = 60

# Integer division, so 1 goal conceded costs nothing and 3 cost the same
# as 2. GK/DEF only.
GOALS_CONCEDED_POINTS = -1
GOALS_CONCEDED_PER = 2

# Also integer division: 2 saves score nothing. GK only.
SAVES_POINTS = 1
SAVES_PER = 3
PENALTY_SAVED_POINTS = 5

YELLOW_CARD_POINTS = -1
RED_CARD_POINTS = -3
OWN_GOAL_POINTS = -2
PENALTY_MISSED_POINTS = -2


# --- head-to-head league standings ------------------------------------

WIN_POINTS = 3
DRAW_POINTS = 1
LOSS_POINTS = 0
BYE_POINTS = 3

# An ASSUMPTION, not a derived value: this repo has no season-length
# constant anywhere else, and a real Premier League season is 38
# gameweeks. standings.py uses it purely to know how many gameweek-slots
# to fill when generating an H2H round-robin schedule upfront; if the
# cycle is shorter than the gameweeks remaining, the same shuffled cycle
# repeats rather than being reshuffled. See standings.py's module
# docstring for the full reasoning.
SEASON_LENGTH_GAMEWEEKS = 38
