"""
tactical_scoring.py — the pure tactical scoring engine (Phase 2).

PURE. No SQL, no I/O, no environment, no clock. Inputs are plain data: one
selection snapshot, per-fixture stat rows keyed by player, and a position map.
That is what lets every rule be tested on a hand-built gameweek instead of a
seeded database, and it is why this module sits beside scoring.py rather than
inside it -- scoring.py owns the queries, this owns the arithmetic.

NOT WIRED IN YET. Phase 2 adds this module and its tests; Phase 4 points
score_gameweek at it and removes the classic path. Until then the live scorer
in scoring.py is untouched and still authoritative, and the two disagree on
purpose -- this one has no captain, no chips, no hits and no FPL bonus.

SCORES ONE SELECTION. Batching over a league's managers belongs to Phase 4,
which must do it in batches of a few hundred (IMPLEMENTATION_PLAN.md section
10 -- the server is a 1 GB e2-micro with a single-task worker).

ORDER OF OPERATIONS, from section 6 of the plan. The order is the specification,
not an implementation detail, and the steps are numbered in score_selection
below because several of them are only correct in this sequence:

  1. Per-fixture General Points, summed per player. "Appeared" means total
     minutes across all his fixtures > 0.
  2. Resolve Tactical swaps FIRST. Each swapped slot yields the outgoing
     player's points AND the incoming player's. Mark those slots ineligible
     for Auto Sub cover -- this is why swaps come before autosubs rather than
     after: a swapped-out player who never appeared would otherwise look
     exactly like a no-show needing cover.
  3. GK cover: the backup GK takes the slot if the starting GK did not appear
     and the backup did.
  4. Outfield cover: the slot-13 Auto Sub, if he appeared, replaces the
     LOWEST-slot non-appearing outfield starter whose replacement keeps the
     formation legal (1 GK, >=3 DEF, >=3 MID, >=1 FWD). One Auto Sub, so at
     most one cover. A starter actually replaced in step 3 or 4 is reported
     with role 'auto_sub_replaced' and stops counting; one who did not play
     but was NOT replaced stays a plain 'starter' and still counts, at 0.
  5. Tactical Points: for each Bonus Player who appeared HIMSELF, apply the
     tactic's events per fixture and sum. Never inherited by a cover.
  6. Sub Bonus: +1 per executed swap where the incoming player's full-gameweek
     General Points strictly exceed the outgoing player's.
  7. Total = General of every scoring slot + Tactical + Sub Bonus.

DECIMAL, NOT FLOAT. ml.player_gw_stats.creativity is numeric(6,1) and arrives
from psycopg2 as Decimal. tier_points refuses floats outright rather than
coercing them, so a boundary like "39.9 earns +1, 40.0 earns +3" can never be
decided by a binary repr. See the guard in tier_points.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from Shared.rules import (
    APPEARANCE_FULL_MINUTES,
    APPEARANCE_FULL_POINTS,
    APPEARANCE_POINTS,
    ASSIST_POINTS,
    ATTACK_POINTS,
    BALANCED_GOAL_OR_ASSIST_POINTS,
    CLEAN_SHEET_MINUTES_THRESHOLD,
    CLEAN_SHEET_POINTS,
    CREATIVITY_TIERS,
    DC_TIERS,
    DEF_CONTRIBUTION_POINTS,
    DEF_CONTRIBUTION_THRESHOLD,
    DEFENCE_CLEAN_SHEET_POINTS,
    FORMATION_MIN,
    GOAL_POINTS,
    GOALS_CONCEDED_PER,
    GOALS_CONCEDED_POINTS,
    MID_FWD_CONTRIBUTION_THRESHOLD,
    OWN_GOAL_POINTS,
    PENALTY_MISSED_POINTS,
    PENALTY_SAVED_POINTS,
    RED_CARD_POINTS,
    SAVES_PER,
    SAVES_POINTS,
    STARTING_XI_SIZE,
    TACTICS,
    YELLOW_CARD_POINTS,
)

# Roles reported in the breakdown. The dashboard renders these directly.
ROLE_STARTER = "starter"
ROLE_SWAPPED_OUT = "swapped_out"
ROLE_SWAPPED_IN = "swapped_in"
ROLE_AUTO_SUB_COVER = "auto_sub_cover"
# D2, closing ambiguity A1: a starter who did not play AND was replaced by an
# Auto Sub. Distinct from a plain 'starter' who did not play and was NOT
# replaced -- that one is still a scoring slot contributing 0, whereas this one
# has stopped counting entirely. The dashboard needs to tell them apart.
ROLE_AUTO_SUB_REPLACED = "auto_sub_replaced"
ROLE_BENCH_UNUSED = "bench_unused"


# ---- inputs ----------------------------------------------------------------


@dataclass(frozen=True)
class Slot:
    """One of the 15 rows of a selection. Mirrors a starting_xi row."""
    position_slot: int
    player_id: int
    is_bonus: bool = False


@dataclass(frozen=True)
class Swap:
    """One planned Tactical Sub. Mirrors a tactical_swaps row."""
    player_out_id: int
    player_in_id: int


@dataclass(frozen=True)
class Selection:
    tactic: str
    slots: list
    swaps: list = field(default_factory=list)


# ---- outputs ---------------------------------------------------------------


@dataclass
class PlayerLine:
    """Per-player breakdown. `counted` is whether his General Points reached
    the total: a starter replaced by an Auto Sub keeps role 'starter' and his
    own points, but counted is False."""
    player_id: int
    position: str
    position_slot: int
    role: str
    minutes: int
    general_points: int
    tactical_points: int
    counted: bool
    is_bonus: bool = False
    covers_player_id: int = None


@dataclass
class SwapLine:
    player_out_id: int
    player_in_id: int
    general_out: int
    general_in: int
    sub_bonus: int


@dataclass
class SelectionScore:
    raw_points: int        # General Points of every scoring slot
    tactical_points: int
    sub_bonus: int
    total: int
    players: list
    swaps: list


# ---- field access ----------------------------------------------------------


def _get(row, field_name, default=0):
    """Read a field from a dict OR an object with attributes.

    Tests hand-build dicts; Phase 4 will pass SQLAlchemy Row objects straight
    from a query. Supporting both here costs one function and saves converting
    every row at the call site -- which is where a silent typo would otherwise
    turn into a zero."""
    if hasattr(row, "keys"):
        value = row.get(field_name, default)
    else:
        value = getattr(row, field_name, default)
    return default if value is None else value


# ---- pure rules ------------------------------------------------------------


def tier_points(value, tiers):
    """Points for the HIGHEST tier `value` reaches. Tiers are (minimum, points),
    ascending, and are never stacked.

    Floats are rejected rather than coerced. A tier boundary is an exact
    question -- does 39.9 clear 40? -- and a float answers it with whatever
    binary rounding produced the value, which is not a rule anyone wrote down.
    Callers pass int (defensive actions) or Decimal (creativity, numeric(6,1)
    in the database). Coercing silently via Decimal(str(x)) would paper over a
    caller that had already lost precision upstream."""
    if isinstance(value, float):
        raise TypeError(
            f"tier_points refuses float ({value!r}): tier boundaries must be "
            f"exact. Pass an int, or a Decimal for numeric columns like "
            f"creativity."
        )
    earned = 0
    for minimum, points in tiers:
        if value >= minimum:
            earned = points
    return earned


def general_points(row, position):
    """General Points for ONE player-fixture row, per section 1.

    No FPL bonus, no captain multiplier -- both are removed from this game, so
    a `bonus` column on the row is deliberately ignored.

    Unlike scoring.py's _component_score there is NO fallback to the archive's
    total_points when every component is zero. That fallback exists because the
    classic scorer had to reproduce FPL's own number including bonus; here the
    point table IS the rule, and a genuinely blank row genuinely scores its
    appearance points and nothing else."""
    minutes = _get(row, "minutes")
    points = 0

    if minutes > 0:
        points += (APPEARANCE_FULL_POINTS if minutes >= APPEARANCE_FULL_MINUTES
                   else APPEARANCE_POINTS)

    points += _get(row, "goals_scored") * GOAL_POINTS[position]
    points += _get(row, "assists") * ASSIST_POINTS

    if minutes >= CLEAN_SHEET_MINUTES_THRESHOLD and _get(row, "clean_sheets"):
        points += CLEAN_SHEET_POINTS[position]

    if position == "GK":
        points += (_get(row, "saves") // SAVES_PER) * SAVES_POINTS
        points += _get(row, "penalties_saved") * PENALTY_SAVED_POINTS

    if position in ("GK", "DEF"):
        points += (_get(row, "goals_conceded") // GOALS_CONCEDED_PER) * GOALS_CONCEDED_POINTS

    # Goalkeepers are not eligible for defensive contributions at all.
    if position != "GK":
        threshold = (DEF_CONTRIBUTION_THRESHOLD if position == "DEF"
                     else MID_FWD_CONTRIBUTION_THRESHOLD)
        if _get(row, "defensive_contributions") >= threshold:
            points += DEF_CONTRIBUTION_POINTS

    points += _get(row, "yellow_cards") * YELLOW_CARD_POINTS
    points += _get(row, "red_cards") * RED_CARD_POINTS
    points += _get(row, "own_goals") * OWN_GOAL_POINTS
    points += _get(row, "penalties_missed") * PENALTY_MISSED_POINTS

    return points


def tactical_points(row, position, tactic):
    """Tactical Points for ONE player-fixture row under `tactic`.

    Per fixture on purpose: in a double gameweek the tiers are evaluated on
    each fixture and the results summed, never on the gameweek's total. Two
    fixtures of 25 creativity are two first-tier payments, not one second-tier
    one."""
    if tactic not in TACTICS:
        raise ValueError(f"unknown tactic {tactic!r}; expected one of {TACTICS}")

    if tactic == "attack":
        return (_get(row, "goals_scored") * ATTACK_POINTS["goal"]
                + _get(row, "assists") * ATTACK_POINTS["assist"])

    if tactic == "defence":
        points = 0
        if (_get(row, "minutes") >= CLEAN_SHEET_MINUTES_THRESHOLD
                and _get(row, "clean_sheets")):
            points += DEFENCE_CLEAN_SHEET_POINTS
        return points + tier_points(_get(row, "defensive_contributions"), DC_TIERS)

    # balanced
    involvements = _get(row, "goals_scored") + _get(row, "assists")
    creativity = _get(row, "creativity", Decimal("0"))
    return (involvements * BALANCED_GOAL_OR_ASSIST_POINTS
            + tier_points(creativity, CREATIVITY_TIERS))


# ---- breakdowns, for display -----------------------------------------------
#
# The dashboard has to tell a manager WHICH rules produced a player's points,
# not just the total. These mirror the two functions above rule for rule, and
# read the same constants, so they cannot drift in VALUE. They can drift in
# COVERAGE -- a new rule added above and forgotten here -- which is what
# test_every_breakdown_sums_to_its_total exists to catch: it asserts
# sum(breakdown) == the function's own answer over a matrix of rows.


def general_points_breakdown(row, position):
    """[{"rule": str, "points": int}, ...] for one player-fixture row.

    Only non-zero contributions are listed: a defender who conceded nothing
    should not be shown a "goals conceded: 0" line."""
    minutes = _get(row, "minutes")
    out = []

    def add(rule, points):
        if points:
            out.append({"rule": rule, "points": points})

    if minutes > 0:
        if minutes >= APPEARANCE_FULL_MINUTES:
            add("appearance_60_plus", APPEARANCE_FULL_POINTS)
        else:
            add("appearance_under_60", APPEARANCE_POINTS)

    add("goals", _get(row, "goals_scored") * GOAL_POINTS[position])
    add("assists", _get(row, "assists") * ASSIST_POINTS)

    if minutes >= CLEAN_SHEET_MINUTES_THRESHOLD and _get(row, "clean_sheets"):
        add("clean_sheet", CLEAN_SHEET_POINTS[position])

    if position == "GK":
        add("saves", (_get(row, "saves") // SAVES_PER) * SAVES_POINTS)
        add("penalties_saved", _get(row, "penalties_saved") * PENALTY_SAVED_POINTS)

    if position in ("GK", "DEF"):
        add("goals_conceded",
            (_get(row, "goals_conceded") // GOALS_CONCEDED_PER) * GOALS_CONCEDED_POINTS)

    if position != "GK":
        threshold = (DEF_CONTRIBUTION_THRESHOLD if position == "DEF"
                     else MID_FWD_CONTRIBUTION_THRESHOLD)
        if _get(row, "defensive_contributions") >= threshold:
            add("defensive_contribution", DEF_CONTRIBUTION_POINTS)

    add("yellow_card", _get(row, "yellow_cards") * YELLOW_CARD_POINTS)
    add("red_card", _get(row, "red_cards") * RED_CARD_POINTS)
    add("own_goal", _get(row, "own_goals") * OWN_GOAL_POINTS)
    add("penalty_missed", _get(row, "penalties_missed") * PENALTY_MISSED_POINTS)

    return out


def tactical_points_breakdown(row, position, tactic):
    """[{"rule": str, "points": int}, ...] for one player-fixture row under
    `tactic`. Empty when the tactic pays nothing for this row."""
    if tactic not in TACTICS:
        raise ValueError(f"unknown tactic {tactic!r}; expected one of {TACTICS}")
    out = []

    def add(rule, points):
        if points:
            out.append({"rule": rule, "points": points})

    if tactic == "attack":
        add("attack_goals", _get(row, "goals_scored") * ATTACK_POINTS["goal"])
        add("attack_assists", _get(row, "assists") * ATTACK_POINTS["assist"])
        return out

    if tactic == "defence":
        if (_get(row, "minutes") >= CLEAN_SHEET_MINUTES_THRESHOLD
                and _get(row, "clean_sheets")):
            add("defence_clean_sheet", DEFENCE_CLEAN_SHEET_POINTS)
        add("defence_contribution_tier",
            tier_points(_get(row, "defensive_contributions"), DC_TIERS))
        return out

    add("balanced_goal_or_assist",
        (_get(row, "goals_scored") + _get(row, "assists")) * BALANCED_GOAL_OR_ASSIST_POINTS)
    add("balanced_creativity_tier",
        tier_points(_get(row, "creativity", Decimal("0")), CREATIVITY_TIERS))
    return out


# ---- the engine ------------------------------------------------------------


def _formation_is_legal(counts):
    """Minimums only, per D1: 1 GK, >=3 DEF, >=3 MID, >=1 FWD.

    No upper bound is checked because none can be breached. An Auto Sub of
    position P is only on the bench when the XI holds fewer than the squad's
    allocation of P (2 GK / 5 DEF / 5 MID / 3 FWD), so bringing him on reaches
    that allocation at most -- a 5-defender XI leaves no spare defender to make
    it 6. An earlier FORMATION_MAX encoded that as a rule and was checked here;
    it was unreachable, and D1 settles that maxima are implied by the squad
    rather than stated."""
    return all(counts.get(p, 0) >= minimum for p, minimum in FORMATION_MIN.items())


def score_selection(selection, stats, positions):
    """Score one selection. See the module docstring for the 7 steps.

    selection : Selection with 15 Slots, a tactic and 0-2 Swaps.
    stats     : {player_id: [row, ...]} -- one row per fixture. A player absent
                from the map, or mapped to [], simply has no fixture data and
                scores 0; partial/live scoring is normal, not an error.
    positions : {player_id: 'GK'|'DEF'|'MID'|'FWD'}. The four codes are the
                only values ml.players.position can hold (players_position_check).

    A player missing from `positions` raises KeyError rather than scoring 0 --
    an unknown position is a bug in the caller, and silently returning zero
    would hide it inside a plausible-looking total."""
    if selection.tactic not in TACTICS:
        raise ValueError(f"unknown tactic {selection.tactic!r}")

    by_slot = {s.position_slot: s for s in selection.slots}
    position_of = {s.player_id: positions[s.player_id] for s in selection.slots}

    # ---- step 1: per-fixture General Points, summed per player
    general = {}
    minutes = {}
    for slot in selection.slots:
        rows = stats.get(slot.player_id) or []
        general[slot.player_id] = sum(general_points(r, position_of[slot.player_id])
                                      for r in rows)
        minutes[slot.player_id] = sum(_get(r, "minutes") for r in rows)

    def appeared(player_id):
        return minutes.get(player_id, 0) > 0

    # ---- step 2: swaps, BEFORE any autosub decision
    swapped_out = {}     # player_id -> Swap
    swapped_in = {}
    for swap in selection.swaps:
        swapped_out[swap.player_out_id] = swap
        swapped_in[swap.player_in_id] = swap

    starters = [by_slot[n] for n in range(1, STARTING_XI_SIZE + 1) if n in by_slot]
    bench = [by_slot[n] for n in sorted(by_slot) if n > STARTING_XI_SIZE]

    # Slots that count towards the total. Swapped-out starters stay in (their
    # points are banked) and swapped-in bench players join.
    counted = {s.player_id for s in starters}
    counted |= set(swapped_in)

    # ---- steps 3 and 4: Auto Subs
    # The live formation, as it stands after swaps. A swapped-in player takes
    # the outgoing player's place for formation purposes only -- the rules
    # require both to be the same position, so the counts are unaffected.
    formation = {}
    for s in starters:
        formation[position_of[s.player_id]] = formation.get(position_of[s.player_id], 0) + 1

    covers = {}          # bench player_id -> starter player_id he replaces
    replaced = set()

    # step 3: GK cover. The backup GK covers only the starting GK.
    gk_starters = [s for s in starters if position_of[s.player_id] == "GK"]
    backup_gk = next((b for b in bench if position_of[b.player_id] == "GK"), None)
    for gk in gk_starters:
        if (not appeared(gk.player_id)
                and gk.player_id not in swapped_out
                and backup_gk is not None
                and backup_gk.player_id not in swapped_in
                and appeared(backup_gk.player_id)):
            covers[backup_gk.player_id] = gk.player_id
            replaced.add(gk.player_id)

    # step 4: one outfield Auto Sub, covering the LOWEST slot that stays legal.
    auto_outfield = next((b for b in bench
                          if b.position_slot == 13 and position_of[b.player_id] != "GK"),
                         None)
    # A cover whose own position is not on record cannot be judged legal, so
    # he does not come on (ambiguity E6). Better a starter scoring 0 than a
    # substitution made on a guess about the shape it produces.
    if (auto_outfield is not None
            and auto_outfield.player_id not in swapped_in
            and position_of[auto_outfield.player_id] in FORMATION_MIN
            and appeared(auto_outfield.player_id)):
        sub_position = position_of[auto_outfield.player_id]
        for starter in starters:                     # already in slot order
            if position_of[starter.player_id] == "GK":
                continue
            if appeared(starter.player_id):
                continue
            # A slot involved in a swap is NEVER Auto Sub covered.
            if starter.player_id in swapped_out:
                continue
            if starter.player_id in replaced:
                continue
            trial = dict(formation)
            trial[position_of[starter.player_id]] -= 1
            trial[sub_position] = trial.get(sub_position, 0) + 1
            if _formation_is_legal(trial):
                covers[auto_outfield.player_id] = starter.player_id
                replaced.add(starter.player_id)
                formation = trial
                break

    counted -= replaced
    counted |= set(covers)

    # ---- step 5: Tactical Points, Bonus Players who appeared THEMSELVES
    tactical = {}
    for slot in selection.slots:
        if not slot.is_bonus:
            continue
        if slot.player_id in replaced or not appeared(slot.player_id):
            # A no-show Bonus Player loses Bonus status for the gameweek, and
            # it does NOT pass to whoever covers him.
            continue
        rows = stats.get(slot.player_id) or []
        tactical[slot.player_id] = sum(
            tactical_points(r, position_of[slot.player_id], selection.tactic)
            for r in rows
        )

    # ---- step 6: Sub Bonus
    swap_lines = []
    for swap in selection.swaps:
        out_points = general.get(swap.player_out_id, 0)
        in_points = general.get(swap.player_in_id, 0)
        bonus = 1 if in_points > out_points else 0
        swap_lines.append(SwapLine(
            player_out_id=swap.player_out_id,
            player_in_id=swap.player_in_id,
            general_out=out_points,
            general_in=in_points,
            sub_bonus=bonus,
        ))

    # ---- the breakdown
    lines = []
    for slot in selection.slots:
        pid = slot.player_id
        if pid in swapped_out:
            role = ROLE_SWAPPED_OUT
        elif pid in swapped_in:
            role = ROLE_SWAPPED_IN
        elif pid in covers:
            role = ROLE_AUTO_SUB_COVER
        elif pid in replaced:
            role = ROLE_AUTO_SUB_REPLACED
        elif slot.position_slot <= STARTING_XI_SIZE:
            role = ROLE_STARTER
        else:
            role = ROLE_BENCH_UNUSED
        lines.append(PlayerLine(
            player_id=pid,
            position=position_of[pid],
            position_slot=slot.position_slot,
            role=role,
            minutes=minutes.get(pid, 0),
            general_points=general.get(pid, 0),
            tactical_points=tactical.get(pid, 0),
            counted=pid in counted,
            is_bonus=slot.is_bonus,
            covers_player_id=covers.get(pid),
        ))

    # ---- step 7: totals
    raw_points = sum(general.get(pid, 0) for pid in counted)
    tactical_total = sum(tactical.values())
    sub_bonus_total = sum(s.sub_bonus for s in swap_lines)

    return SelectionScore(
        raw_points=raw_points,
        tactical_points=tactical_total,
        sub_bonus=sub_bonus_total,
        total=raw_points + tactical_total + sub_bonus_total,
        players=lines,
        swaps=swap_lines,
    )
