"""
selection_rules.py — pure validation of a tactical gameweek selection.

PURE. No SQL, no I/O, no clock. The caller reads the manager's squad, the
position map and the gameweek's fixtures, and hands them in as plain data.
That is decision A7: the scoring engine validates nothing, and validation is a
separate pure function rather than something tangled into the endpoint.

COLLECTS ALL ERRORS. Every rule is checked and every failure appended; nothing
raises on the first problem. This is the existing 422 pattern in this codebase
(see the classic _validate_selection it replaces) and it exists so a manager
fixing a lineup is told everything that is wrong at once.

POSITION, NEVER SLOT (decision A4). Slot 1 is not assumed to be the goalkeeper
and neither is slot 12. The rules below require exactly 1 GK among slots 1-11
and a GK at slot 12, but which players those are is read from the position map.
This is the same principle scoring.py states for the scorer, applied to
validation -- position_slot is assigned in submission order, so a slot number
has never been evidence of a position.

WHAT THIS DOES NOT CHECK. The deadline. That is the caller's, and deliberately
so: it needs the database and a clock, and starting_xi.py already rejects a
late submission before any of this runs.
"""
from collections import Counter
from dataclasses import dataclass, field

from Shared.rules import (
    BENCH_SIZE,
    FIXTURE_DURATION_MIN,
    FORMATION_MIN,
    SQUAD_SIZE,
    STARTING_XI_SIZE,
    TACTICS,
)

# Which position each tactic's 2 Bonus Players must be. Literal positions, and
# a goalkeeper is never eligible under any tactic.
BONUS_POSITION = {"attack": "FWD", "defence": "DEF", "balanced": "MID"}

# The bench slot that must hold the backup goalkeeper, and the two that hold
# Tactical Subs. Slot 13 is the outfield Auto Sub.
BENCH_GK_SLOT = 12
AUTO_OUTFIELD_SLOT = 13
TACTICAL_SLOTS = (14, 15)

# Attack needs two forwards in the XI to have two Bonus forwards to name.
ATTACK_MIN_FORWARDS = 2


@dataclass(frozen=True)
class SwapInput:
    player_out_id: int
    player_in_id: int


@dataclass(frozen=True)
class SelectionInput:
    season: str
    gameweek: int
    tactic: str
    player_ids: list          # 11 ids, slots 1-11 in order
    bench_order: list         # 4 ids, slots 12-15 in order
    bonus_player_ids: list    # exactly 2
    swaps: list = field(default_factory=list)


def _last_fixture_end(kickoffs):
    """When the player's gameweek is over, in his own terms.

    A double gameweek is treated as ONE block: the end is the LAST kickoff plus
    the fixture duration, not the first. Swapping in someone who kicks off
    between a player's two fixtures would otherwise look legal while the
    outgoing player was still to play again."""
    from datetime import timedelta
    return max(kickoffs) + timedelta(minutes=FIXTURE_DURATION_MIN)


def validate_selection(sel, squad_ids, positions, fixtures_by_player,
                       *, check_timing: bool = True):
    """Returns a list of human-readable errors; empty means valid.

    sel               : SelectionInput
    squad_ids         : set of the manager's active 15 player ids
    positions         : {player_id: 'GK'|'DEF'|'MID'|'FWD'}
    fixtures_by_player: {player_id: [kickoff datetime, ...]} for this gameweek.
                        An empty list means no fixture, which is what blocks a
                        swap and is NOT an error on its own -- a manager may
                        field a player whose club is blank.
    check_timing      : KEYWORD-ONLY. False drops the two fixture-dependent
                        swap rules -- "both players have a fixture" and the
                        kickoff ordering -- and nothing else.

    WHY check_timing EXISTS (ambiguity E2, now closed). Decision D4: a swap is
    validated once at submission and NEVER re-validated, so a fixture that moved
    afterwards must not retrospectively invalidate it. The scoring job and the
    dashboard re-run this validator for its OTHER rules -- bonus count,
    formation, swap references -- and must not re-run the timing one. They pass
    check_timing=False and an empty fixture map.

    Both fixture rules go together because both read the same data the
    re-validating caller does not have. The previous workaround fabricated a
    notional fixture per player, with each swap's incoming player given a later
    one, purely to make the timing rule pass. That was inventing input to dodge
    a check; this states the intent instead.

    Keyword-only on purpose: a positional boolean in the fifth slot is exactly
    the kind of argument that gets passed in the wrong position."""
    errors = []

    # ---- the 15 -----------------------------------------------------------
    if len(sel.player_ids) != STARTING_XI_SIZE:
        errors.append(
            f"starting XI must contain exactly {STARTING_XI_SIZE} players, "
            f"got {len(sel.player_ids)}"
        )
    if len(sel.bench_order) != BENCH_SIZE:
        errors.append(
            f"bench must contain exactly {BENCH_SIZE} players, got {len(sel.bench_order)}"
        )

    all_ids = list(sel.player_ids) + list(sel.bench_order)
    counts = Counter(all_ids)
    duplicates = sorted(pid for pid, n in counts.items() if n > 1)
    if duplicates:
        errors.append(f"duplicate player_id(s): {duplicates}")

    not_in_squad = sorted(set(all_ids) - set(squad_ids))
    if not_in_squad:
        errors.append(f"player_id(s) not in your squad: {not_in_squad}")

    if len(set(all_ids)) == SQUAD_SIZE:
        missing = sorted(set(squad_ids) - set(all_ids))
        if missing:
            errors.append(f"every squad player must be given a slot -- missing: {missing}")

    unknown_position = sorted(pid for pid in set(all_ids) if pid not in positions)
    if unknown_position:
        errors.append(f"player_id(s) with no position on record: {unknown_position}")

    # Everything below reads positions, so stop if they are not all known --
    # otherwise one unknown id produces a cascade of misleading shape errors.
    if unknown_position:
        return errors

    # ---- formation, by position -------------------------------------------
    xi_positions = Counter(positions[pid] for pid in sel.player_ids)

    gk_in_xi = xi_positions.get("GK", 0)
    if gk_in_xi != FORMATION_MIN["GK"]:
        errors.append(f"starting XI must contain exactly 1 GK, got {gk_in_xi}")

    for position in ("DEF", "MID", "FWD"):
        minimum = FORMATION_MIN[position]
        have = xi_positions.get(position, 0)
        if have < minimum:
            errors.append(
                f"starting XI needs at least {minimum} {position}, got {have}"
            )

    # ---- bench slots ------------------------------------------------------
    # Slot order IS the bench meaning: 12 backup GK, 13 outfield Auto Sub,
    # 14 and 15 Tactical Subs.
    if len(sel.bench_order) == BENCH_SIZE:
        bench_by_slot = dict(zip(range(BENCH_GK_SLOT, BENCH_GK_SLOT + BENCH_SIZE),
                                 sel.bench_order))
        gk_slot_player = bench_by_slot[BENCH_GK_SLOT]
        if positions[gk_slot_player] != "GK":
            errors.append(
                f"bench slot 12 must be the backup goalkeeper, but player "
                f"{gk_slot_player} is a {positions[gk_slot_player]}"
            )
        for slot in (AUTO_OUTFIELD_SLOT,) + TACTICAL_SLOTS:
            pid = bench_by_slot[slot]
            if positions[pid] == "GK":
                errors.append(
                    f"bench slot {slot} must be an outfield player, but player "
                    f"{pid} is a GK"
                )

    # ---- tactic -----------------------------------------------------------
    if sel.tactic not in TACTICS:
        errors.append(f"tactic must be one of {sorted(TACTICS)}, got {sel.tactic!r}")
    elif sel.tactic == "attack":
        forwards = xi_positions.get("FWD", 0)
        if forwards < ATTACK_MIN_FORWARDS:
            errors.append(
                f"Attack needs at least {ATTACK_MIN_FORWARDS} FWD in the starting XI "
                f"to name 2 Bonus forwards, got {forwards}"
            )

    # ---- bonus players ----------------------------------------------------
    bonus = list(sel.bonus_player_ids)
    if len(bonus) != 2:
        errors.append(f"exactly 2 Bonus Players are required, got {len(bonus)}")
    if len(set(bonus)) != len(bonus):
        errors.append("the 2 Bonus Players must be distinct")

    for pid in bonus:
        if pid not in sel.player_ids:
            errors.append(f"Bonus Player {pid} must be in the starting XI")
            continue
        if sel.tactic in BONUS_POSITION:
            wanted = BONUS_POSITION[sel.tactic]
            if positions[pid] != wanted:
                errors.append(
                    f"Bonus Player {pid} is a {positions[pid]}, but the "
                    f"{sel.tactic} tactic requires {wanted}"
                )

    # ---- swaps ------------------------------------------------------------
    if len(sel.swaps) > len(TACTICAL_SLOTS):
        errors.append(
            f"at most {len(TACTICAL_SLOTS)} tactical swaps are allowed, got {len(sel.swaps)}"
        )

    tactical_players = set()
    if len(sel.bench_order) == BENCH_SIZE:
        bench_by_slot = dict(zip(range(BENCH_GK_SLOT, BENCH_GK_SLOT + BENCH_SIZE),
                                 sel.bench_order))
        tactical_players = {bench_by_slot[s] for s in TACTICAL_SLOTS}

    used = Counter()
    for swap in sel.swaps:
        used[swap.player_out_id] += 1
        used[swap.player_in_id] += 1
    reused = sorted(pid for pid, n in used.items() if n > 1)
    if reused:
        errors.append(f"player_id(s) used in more than one swap: {reused}")

    for swap in sel.swaps:
        out_id, in_id = swap.player_out_id, swap.player_in_id

        if out_id not in sel.player_ids:
            errors.append(f"swap: outgoing player {out_id} must be a starter")
        elif out_id in bonus:
            errors.append(f"swap: outgoing player {out_id} is a Bonus Player and cannot be swapped out")

        if in_id not in tactical_players:
            errors.append(
                f"swap: incoming player {in_id} must be a Tactical Sub (bench slot 14 or 15)"
            )

        if out_id in positions and in_id in positions and positions[out_id] != positions[in_id]:
            errors.append(
                f"swap: {out_id} ({positions[out_id]}) and {in_id} ({positions[in_id]}) "
                f"must be the same position"
            )

        if not check_timing:
            continue

        out_fixtures = fixtures_by_player.get(out_id) or []
        in_fixtures = fixtures_by_player.get(in_id) or []
        if not out_fixtures:
            errors.append(f"swap: outgoing player {out_id} has no fixture in gameweek {sel.gameweek}")
        if not in_fixtures:
            errors.append(f"swap: incoming player {in_id} has no fixture in gameweek {sel.gameweek}")

        if out_fixtures and in_fixtures:
            # Strictly after: a kickoff exactly as the previous fixture ends is
            # not "after" it, and the manager gains nothing from the tie.
            if min(in_fixtures) <= _last_fixture_end(out_fixtures):
                errors.append(
                    f"swap: incoming player {in_id}'s first kickoff must be after "
                    f"outgoing player {out_id}'s last fixture ends "
                    f"(kickoff + {FIXTURE_DURATION_MIN} minutes)"
                )

    return errors
