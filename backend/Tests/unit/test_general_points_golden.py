"""Phase 2: the General Points golden table.

48 hand-checked player-fixture rows, one or more per rule in section 1's
General Points table. Every `expected` was worked out by hand from that table
and the arithmetic is written out in the comment beside it, so a future change
to the point values fails here with a row that says what it was meant to be.

No database, no network -- see this directory's conftest.py. The archive check
at the bottom is the one exception and it is skipped unless an environment
variable asks for it.
"""
import os
from decimal import Decimal

import pytest

from Results.tactical_scoring import general_points


def _r(position, expected, note, **stats):
    base = dict(minutes=90, goals_scored=0, assists=0, clean_sheets=0,
                goals_conceded=0, saves=0, penalties_saved=0,
                penalties_missed=0, own_goals=0, yellow_cards=0, red_cards=0,
                defensive_contributions=0, creativity=Decimal("0"))
    base.update(stats)
    return pytest.param(base, position, expected, id=note)


GOLDEN = [
    # ---- appearance
    _r("GK",  0, "0min-nothing",               minutes=0),
    _r("GK",  1, "1min-1pt",                   minutes=1),
    _r("GK",  1, "59min-1pt",                  minutes=59),
    _r("GK",  2, "60min-2pts",                 minutes=60),
    _r("MID", 2, "90min-2pts"),

    # ---- goals, by position
    _r("GK",  12, "gk-goal-2+10",              goals_scored=1),
    _r("DEF", 8,  "def-goal-2+6",              goals_scored=1),
    _r("MID", 7,  "mid-goal-2+5",              goals_scored=1),
    _r("FWD", 6,  "fwd-goal-2+4",              goals_scored=1),
    _r("FWD", 14, "fwd-hattrick-2+12",         goals_scored=3),

    # ---- assists
    _r("MID", 5, "mid-assist-2+3",             assists=1),
    _r("FWD", 8, "fwd-two-assists-2+6",        assists=2),

    # ---- clean sheets, by position and the 60-minute gate
    _r("GK",  6, "gk-cs-2+4",                  clean_sheets=1),
    _r("DEF", 6, "def-cs-2+4",                 clean_sheets=1),
    _r("MID", 3, "mid-cs-2+1",                 clean_sheets=1),
    _r("FWD", 2, "fwd-cs-worth-nothing",       clean_sheets=1),
    _r("DEF", 1, "def-cs-at-59min-denied",     minutes=59, clean_sheets=1),
    _r("DEF", 6, "def-cs-at-60min-allowed",    minutes=60, clean_sheets=1),

    # ---- defensive contributions: DEF needs 10, MID/FWD need 12, GK never
    _r("DEF", 2, "def-dc-9-below",             defensive_contributions=9),
    _r("DEF", 4, "def-dc-10-earns-2",          defensive_contributions=10),
    _r("MID", 2, "mid-dc-11-below",            defensive_contributions=11),
    _r("MID", 4, "mid-dc-12-earns-2",          defensive_contributions=12),
    _r("FWD", 2, "fwd-dc-11-below",            defensive_contributions=11),
    _r("FWD", 4, "fwd-dc-12-earns-2",          defensive_contributions=12),
    _r("GK",  2, "gk-dc-never-eligible",       defensive_contributions=20),

    # ---- goals conceded: -1 per 2, GK/DEF only, integer division
    _r("GK",  2, "gk-conceded-1-free",         goals_conceded=1),
    _r("GK",  1, "gk-conceded-2-minus1",       goals_conceded=2),
    _r("GK",  1, "gk-conceded-3-still-minus1", goals_conceded=3),
    _r("GK",  0, "gk-conceded-4-minus2",       goals_conceded=4),
    _r("DEF", 1, "def-conceded-2-minus1",      goals_conceded=2),
    _r("MID", 2, "mid-conceded-exempt",        goals_conceded=4),
    _r("FWD", 2, "fwd-conceded-exempt",        goals_conceded=4),

    # ---- saves: +1 per 3, GK only
    _r("GK",  2, "gk-2-saves-nothing",         saves=2),
    _r("GK",  3, "gk-3-saves-plus1",           saves=3),
    _r("GK",  4, "gk-6-saves-plus2",           saves=6),
    _r("DEF", 2, "def-saves-exempt",           saves=6),

    # ---- penalties
    _r("GK",  7, "gk-pen-saved-2+5",           penalties_saved=1),
    _r("MID", 0, "mid-pen-missed-2-2",         penalties_missed=1),
    _r("FWD", -2, "fwd-two-pens-missed",       penalties_missed=2),

    # ---- cards and own goals
    _r("MID", 1,  "mid-yellow-2-1",            yellow_cards=1),
    _r("MID", -1, "mid-red-2-3",               red_cards=1),
    _r("DEF", 0,  "def-own-goal-2-2",          own_goals=1),
    _r("DEF", -1, "def-60min-yellow-and-og",   minutes=60, yellow_cards=1, own_goals=1),

    # ---- realistic combinations, several rules at once
    _r("GK",  7,  "gk-cs-and-4-saves",         clean_sheets=1, saves=4),
    _r("DEF", 14, "def-goal-cs-and-dc",        goals_scored=1, clean_sheets=1,
                                               defensive_contributions=10),
    _r("MID", 13, "mid-goal-assist-cs-dc",     goals_scored=1, assists=1,
                                               clean_sheets=1, defensive_contributions=12),
    _r("FWD", 12, "fwd-brace-assist-yellow",   goals_scored=2, assists=1, yellow_cards=1),
    _r("GK",  7,  "gk-busy-losing-game",       goals_conceded=3, saves=5, penalties_saved=1),
]


@pytest.mark.parametrize("row,position,expected", GOLDEN)
def test_general_points_golden_table(row, position, expected):
    assert general_points(row, position) == expected


def test_the_golden_table_is_as_large_as_the_spec_asks():
    # Section 8 of the Phase 2 brief: at least 40 rows.
    assert len(GOLDEN) >= 40


# ---- optional: the real archive --------------------------------------------
#
# Skipped by default. The suite must run with no network, and this downloads a
# season of CSVs. Set FPL_ARCHIVE_GOLDEN=1 to opt in.
#
# What it asserts: recomputed General Points == the archive's total_points MINUS
# its bonus, because the tactical game removes FPL bonus and changes nothing
# else in the General table. Rows before defensive contributions existed are
# excluded -- that stat only starts in 2025-26, and FPL's own totals for earlier
# seasons cannot contain points for a rule that did not yet exist.

ARCHIVE_ENV = "FPL_ARCHIVE_GOLDEN"


@pytest.mark.skipif(
    not os.environ.get(ARCHIVE_ENV),
    reason=f"set {ARCHIVE_ENV}=1 to check against the downloaded FPL archive "
           f"(needs network; the default suite must not)",
)
def test_general_points_matches_the_archive_minus_bonus():
    import csv
    import io
    import urllib.request

    season = os.environ.get("FPL_ARCHIVE_SEASON", "2025-26")
    url = ("https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/"
           f"master/data/{season}/gws/merged_gw.csv")
    with urllib.request.urlopen(url, timeout=60) as fh:
        text = fh.read().decode("utf-8", errors="replace")

    position_of = {"GK": "GK", "GKP": "GK", "DEF": "DEF", "MID": "MID", "FWD": "FWD"}
    checked = mismatches = 0
    examples = []

    for rec in csv.DictReader(io.StringIO(text)):
        position = position_of.get((rec.get("position") or "").strip().upper())
        if position is None:
            continue
        if "defensive_contribution" not in rec and "defensive_contributions" not in rec:
            pytest.skip(f"{season} has no defensive contribution column")

        def num(field, default=0):
            raw = rec.get(field)
            if raw in (None, ""):
                return default
            return int(float(raw))

        row = dict(
            minutes=num("minutes"),
            goals_scored=num("goals_scored"),
            assists=num("assists"),
            clean_sheets=num("clean_sheets"),
            goals_conceded=num("goals_conceded"),
            saves=num("saves"),
            penalties_saved=num("penalties_saved"),
            penalties_missed=num("penalties_missed"),
            own_goals=num("own_goals"),
            yellow_cards=num("yellow_cards"),
            red_cards=num("red_cards"),
            defensive_contributions=num("defensive_contribution") or num("defensive_contributions"),
            creativity=Decimal("0"),
        )
        expected = num("total_points") - num("bonus")
        got = general_points(row, position)
        checked += 1
        if got != expected:
            mismatches += 1
            if len(examples) < 10:
                examples.append(f"{rec.get('name')} gw{rec.get('GW')} "
                                f"{position}: got {got}, archive {expected}, row={row}")

    # Printed, not just asserted: "it passed" does not say how much was
    # actually compared, and a silently-shrinking sample would still pass.
    print(f"\narchive {season}: {checked} rows checked, {mismatches} mismatches")

    assert checked > 1000, f"only {checked} rows parsed -- the CSV shape changed?"
    assert mismatches == 0, (
        f"{mismatches} of {checked} rows disagree with the archive:\n"
        + "\n".join(examples)
    )
