"""
scoring.py — pure gameweek scoring: gw_selections -> gw_scores.

score_gameweek(engine, season, gameweek) processes every user who has a
gw_selections row for (season, gameweek) -- no row means no submission,
skipped entirely, by design (not an error, not a failure). Each user is
scored independently in its own try/except so one broken row can't
abort the whole batch; failures are collected and returned, not raised.

Autosub / captaincy rules (all decided, not re-litigated here):
- Nothing guarantees position_slot 1 is the GK, for either the starting
  XI or the bench -- position_slot is assigned in whatever order the
  client submitted player_ids/bench_order (Gameplay/starting_xi.py),
  not sorted by position. So GK identity is ALWAYS resolved via
  ml.players.position, never via a hardcoded slot number, for both the
  starting GK and the bench GK -- same principle already decided for
  bench GK, applied consistently here to the starting GK too.
- chip_used == 'bench_boost': autosub is skipped entirely (moot --
  all 15 rows count once, at their own total_points).
- GK autosub: starting GK has minutes == 0 AND bench GK has
  minutes > 0 -> swap.
- Outfield autosub: for each 0-minute outfield starter (in
  position_slot order), try bench outfield candidates (bench slots
  excluding whichever is the GK) in bench-priority order, skipping any
  bench player already used earlier in this same run. A candidate with
  minutes == 0 is skipped outright -- they didn't play either, so
  subbing them on achieves nothing (the same "must have actually
  played" logic the GK rule states explicitly, applied consistently
  here rather than left as a special case). A candidate is only
  committed if the resulting formation (DEF 3-5, MID 2-5, FWD 1-3)
  stays legal; otherwise the next candidate is tried. If nothing works,
  the starter simply stays at 0 points -- no swap, no error.
- The captain multiplier looks at the ORIGINAL starting_xi
  is_captain/is_vice_captain flags (captaincy is a designation on a
  specific player, unaffected by autosub) and requires minutes > 0 to
  qualify: captain first, vice-captain as fallback, no multiplier at
  all if neither actually played.
- Multiplier is 3x under chip_used == 'triple_captain', 2x otherwise
  -- independent of bench_boost, which only changes the effective set,
  not the multiplier.

Re-runnable: season_total is recomputed fresh from gw_scores each run
(summed over gameweek < this one) rather than trusting a stored running
total, and the gw_scores write is an upsert keyed on
(user_id, season, gameweek) -- so re-running for the same
(season, gameweek) with unchanged upstream data is idempotent, not
additive. This matters because partial/live scoring means this may run
several times as a gameweek progresses (more player_gw_stats rows
arriving as matches finish).
"""

import logging
from collections import Counter
from types import SimpleNamespace

from sqlalchemy import text

# Every scoring rule this module applies lives in rules.py, so the point
# weightings have exactly one definition. HIT_COST in particular is the
# price of a rule owned elsewhere (transfers.py stamps is_free; this
# module only counts the unstamped rows), and the captain multipliers
# were previously duplicated verbatim in team_dashboard.py.
from Shared.rules import (
    APPEARANCE_FULL_MINUTES,
    APPEARANCE_FULL_POINTS,
    APPEARANCE_POINTS,
    ASSIST_POINTS,
    CAPTAIN_MULTIPLIER,
    CURRENT_RULES_VERSION,
    CLEAN_SHEET_MINUTES_THRESHOLD,
    CLEAN_SHEET_POINTS,
    DEF_CONTRIBUTION_POINTS,
    DEF_CONTRIBUTION_THRESHOLD,
    GOAL_POINTS,
    GOALS_CONCEDED_PER,
    GOALS_CONCEDED_POINTS,
    HIT_COST,
    MID_FWD_CONTRIBUTION_THRESHOLD,
    OWN_GOAL_POINTS,
    PENALTY_MISSED_POINTS,
    PENALTY_SAVED_POINTS,
    RED_CARD_POINTS,
    SAVES_PER,
    SAVES_POINTS,
    TRIPLE_CAPTAIN_MULTIPLIER,
    YELLOW_CARD_POINTS,
)

logger = logging.getLogger(__name__)

GW_SELECTIONS_QUERY = text(
    "SELECT id AS gw_selection_id, user_id, chip_used FROM gw_selections "
    "WHERE season = :season AND gameweek = :gameweek"
)

# minutes/total_points default to 0 via COALESCE when no player_gw_stats
# row exists yet (match not played/ingested) -- partial/live scoring is
# allowed by design, not an error.
STARTING_XI_STATS_QUERY = text(
    """
    SELECT sx.player_id, sx.position_slot, sx.is_captain, sx.is_vice_captain,
           mp.position,
           COALESCE(pgs.minutes, 0) AS minutes,
           COALESCE(pgs.goals_scored, 0) AS goals_scored,
           COALESCE(pgs.assists, 0) AS assists,
           COALESCE(pgs.clean_sheets, 0) AS clean_sheets,
           COALESCE(pgs.goals_conceded, 0) AS goals_conceded,
           COALESCE(pgs.saves, 0) AS saves,
           COALESCE(pgs.bonus, 0) AS bonus,
           COALESCE(pgs.yellow_cards, 0) AS yellow_cards,
           COALESCE(pgs.red_cards, 0) AS red_cards,
           COALESCE(pgs.own_goals, 0) AS own_goals,
           COALESCE(pgs.penalties_saved, 0) AS penalties_saved,
           COALESCE(pgs.penalties_missed, 0) AS penalties_missed,
           COALESCE(pgs.defensive_contributions, 0) AS defensive_contributions,
           COALESCE(pgs.total_points, 0) AS total_points
    FROM starting_xi sx
    JOIN ml.players mp ON mp.fpl_id = sx.player_id AND mp.season = :season
    LEFT JOIN ml.player_gw_stats pgs
        ON pgs.player_id = mp.id AND pgs.season = :season AND pgs.gameweek = :gameweek
    WHERE sx.gw_selection_id = :gw_selection_id
    """
)

# Deliberately knows nothing about free-transfer banking. Game_logic/
# transfers.py decides how many of a gameweek's transfers were covered by
# the allowance and stamps is_free on each row at submission time, so
# counting the unstamped ones here yields 4 * max(0, made - available) for
# free of charge -- whatever `available` happened to be. Keep it that way:
# reintroducing an allowance constant here would put the banking rule in
# two places, and this is the copy that turns into points.
#
# The cancelled_transfers exclusion is the same rule transfers.py applies
# to its three counting queries: a transfer a Free Hit cancellation
# reversed did not happen, so it cannot be charged for. Today every
# transfer made under an active Free Hit is is_free = TRUE and so could
# never have been charged anyway -- this keeps the four queries agreeing
# rather than leaving one that would start charging for reversed moves
# the moment a paid transfer became cancellable.
TRANSFER_HITS_QUERY = text(
    "SELECT COUNT(*) FROM transfers t "
    "WHERE t.user_id = :user_id AND t.season = :season AND t.gameweek = :gameweek "
    "AND t.is_free = FALSE "
    "AND NOT EXISTS (SELECT 1 FROM cancelled_transfers c WHERE c.transfer_id = t.id)"
)

SEASON_TOTAL_PRIOR_QUERY = text(
    "SELECT COALESCE(SUM(total_points), 0) FROM gw_scores "
    "WHERE user_id = :user_id AND season = :season AND gameweek < :gameweek"
)

# rules_version is stamped on the re-score arm too, not just the insert:
# re-running a gameweek recomputes it under whatever rules are in force
# NOW, so the row must say so. A row keeps an old version only while
# nothing rescores it -- which, since refresh_active_gameweeks only
# revisits a 5-day window, is what freezes history in practice.
UPSERT_GW_SCORE_STMT = text(
    """
    INSERT INTO gw_scores
        (user_id, season, gameweek, raw_points, final_points, transfer_hits, hit_deductions,
         total_points, season_total, rules_version)
    VALUES
        (:user_id, :season, :gameweek, :raw_points, :final_points, :transfer_hits, :hit_deductions,
         :total_points, :season_total, :rules_version)
    ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
        raw_points = EXCLUDED.raw_points,
        final_points = EXCLUDED.final_points,
        transfer_hits = EXCLUDED.transfer_hits,
        hit_deductions = EXCLUDED.hit_deductions,
        total_points = EXCLUDED.total_points,
        season_total = EXCLUDED.season_total,
        rules_version = EXCLUDED.rules_version
    """
)


def _formation_legal(effective_starters: dict) -> bool:
    counts = Counter(r.position for r in effective_starters.values())
    return 3 <= counts.get("DEF", 0) <= 5 and 2 <= counts.get("MID", 0) <= 5 and 1 <= counts.get("FWD", 0) <= 3




def _component_score(row) -> int:
    # `minutes` is deliberately NOT in this list, and it looks like an omission
    # until you try adding it. The fallback below is not a last resort -- it is
    # the rule for "we hold no component detail for this player", and in that
    # case FPL's own total_points is the better authority than a formula run
    # over columns we don't have. A row with minutes and nothing else is exactly
    # that case: total_points already includes the appearance points, so
    # deferring is correct and recomputing would be us guessing. Adding
    # "minutes" here makes any such row score from components alone and drops
    # whatever else FPL counted -- it fails ten tests in test_scoring.py for
    # precisely that reason.
    component_signal = any(
        getattr(row, field, 0)
        for field in (
            "goals_scored",
            "assists",
            "clean_sheets",
            "goals_conceded",
            "saves",
            "bonus",
            "yellow_cards",
            "red_cards",
            "own_goals",
            "penalties_saved",
            "penalties_missed",
            "defensive_contributions",
        )
    )
    if not component_signal:
        return int(row.total_points or 0)

    points = 0
    if row.minutes > 0:
        points += APPEARANCE_FULL_POINTS if row.minutes >= APPEARANCE_FULL_MINUTES else APPEARANCE_POINTS
    points += row.goals_scored * GOAL_POINTS[row.position]
    points += row.assists * ASSIST_POINTS
    if row.minutes >= CLEAN_SHEET_MINUTES_THRESHOLD and row.clean_sheets:
        points += CLEAN_SHEET_POINTS[row.position]
    if row.position == "GK":
        points += (row.saves // SAVES_PER) * SAVES_POINTS
        points += row.penalties_saved * PENALTY_SAVED_POINTS
    if row.position in ("GK", "DEF"):
        points += (row.goals_conceded // GOALS_CONCEDED_PER) * GOALS_CONCEDED_POINTS
    threshold = (
        DEF_CONTRIBUTION_THRESHOLD
        if row.position == "DEF"
        else MID_FWD_CONTRIBUTION_THRESHOLD
    )
    if row.position in ("DEF", "MID", "FWD") and row.defensive_contributions >= threshold:
        points += DEF_CONTRIBUTION_POINTS
    points += row.bonus
    # `+=` throughout: the card/own-goal/missed-penalty constants are stored
    # negative (see Shared.rules), so subtracting them here would silently
    # award points for a red card.
    points += row.yellow_cards * YELLOW_CARD_POINTS
    points += row.red_cards * RED_CARD_POINTS
    points += row.own_goals * OWN_GOAL_POINTS
    points += row.penalties_missed * PENALTY_MISSED_POINTS
    return points


def _aggregate_selection_rows(rows: list) -> list:
    grouped: dict[tuple[int, int], list] = {}
    for row in rows:
        grouped.setdefault((row.position_slot, row.player_id), []).append(row)

    aggregated = []
    for (slot, player_id), player_rows in grouped.items():
        first = player_rows[0]
        aggregated.append(
            SimpleNamespace(
                player_id=player_id,
                position_slot=slot,
                is_captain=first.is_captain,
                is_vice_captain=first.is_vice_captain,
                position=first.position,
                minutes=sum(r.minutes for r in player_rows),
                total_points=sum(_component_score(r) for r in player_rows),
            )
        )
    return aggregated


def resolve_autosubs(starters: dict, bench: dict) -> list:
    """starters: {position_slot (1-11): row}. bench: {position_slot (12-15): row}.
    Returns the effective 11 (list of rows) after GK + outfield autosubs.
    """
    starters = dict(starters)  # local mutable copy -- caller's dict untouched

    bench_gk_slot = next(slot for slot, r in bench.items() if r.position == "GK")
    bench_outfield_slots = [slot for slot in sorted(bench.keys()) if slot != bench_gk_slot]
    used_bench_slots: set[int] = set()

    gk_slot = next(slot for slot, r in starters.items() if r.position == "GK")
    bench_gk = bench[bench_gk_slot]
    if starters[gk_slot].minutes == 0 and bench_gk.minutes > 0:
        starters[gk_slot] = bench_gk
        used_bench_slots.add(bench_gk_slot)

    outfield_starter_slots = sorted(slot for slot, r in starters.items() if r.position != "GK")
    for oslot in outfield_starter_slots:
        if starters[oslot].minutes > 0:
            continue
        for cslot in bench_outfield_slots:
            if cslot in used_bench_slots:
                continue
            candidate = bench[cslot]
            if candidate.minutes == 0:
                continue
            tentative = dict(starters)
            tentative[oslot] = candidate
            if _formation_legal(tentative):
                starters[oslot] = candidate
                used_bench_slots.add(cslot)
                break

    return list(starters.values())


def _score_one_user(
    engine, season: str, gameweek: int, user_id: int, gw_selection_id: int, chip_used: str | None
) -> None:
    with engine.connect() as conn:
        rows = conn.execute(
            STARTING_XI_STATS_QUERY,
            {"gw_selection_id": gw_selection_id, "season": season, "gameweek": gameweek},
        ).all()

    rows = _aggregate_selection_rows(rows)

    if len(rows) != 15:
        raise ValueError(f"expected 15 selected players for gw_selection_id={gw_selection_id}, got {len(rows)}")

    starters = {r.position_slot: r for r in rows if r.position_slot <= 11}
    bench = {r.position_slot: r for r in rows if r.position_slot > 11}

    if chip_used == "bench_boost":
        effective = list(rows)  # all 15, skip autosub entirely -- moot when everyone counts
    else:
        effective = resolve_autosubs(starters, bench)

    raw_points = sum(r.total_points for r in effective)

    captain_row = next((r for r in rows if r.is_captain), None)
    vice_row = next((r for r in rows if r.is_vice_captain), None)
    multiplier = TRIPLE_CAPTAIN_MULTIPLIER if chip_used == "triple_captain" else CAPTAIN_MULTIPLIER

    extra_points = 0
    if captain_row is not None and captain_row.minutes > 0:
        extra_points = (multiplier - 1) * captain_row.total_points
    elif vice_row is not None and vice_row.minutes > 0:
        extra_points = (multiplier - 1) * vice_row.total_points
    # else: neither played -- no multiplier applied to anyone this gameweek

    final_points = raw_points + extra_points

    with engine.connect() as conn:
        transfer_hits = conn.execute(
            TRANSFER_HITS_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).scalar()
        season_total_prior = conn.execute(
            SEASON_TOTAL_PRIOR_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
        ).scalar()

    hit_deductions = transfer_hits * HIT_COST
    total_points_this_gw = final_points - hit_deductions
    season_total = season_total_prior + total_points_this_gw

    with engine.begin() as conn:
        conn.execute(
            UPSERT_GW_SCORE_STMT,
            {
                "user_id": user_id,
                "season": season,
                "gameweek": gameweek,
                "raw_points": raw_points,
                "final_points": final_points,
                "transfer_hits": transfer_hits,
                "hit_deductions": hit_deductions,
                "total_points": total_points_this_gw,
                "season_total": season_total,
                "rules_version": CURRENT_RULES_VERSION,
            },
        )


def score_gameweek(engine, season: str, gameweek: int) -> dict:
    """Score every user with a gw_selections row for (season, gameweek).

    Returns {"scored": [user_id, ...], "failed": [(user_id, error_message), ...]}.
    Never raises for a single user's bad data -- only a failure before
    any per-user processing starts (e.g. the initial query itself
    failing) would propagate.
    """
    with engine.connect() as conn:
        selections = conn.execute(GW_SELECTIONS_QUERY, {"season": season, "gameweek": gameweek}).all()

    scored = []
    failed = []
    for sel in selections:
        try:
            _score_one_user(engine, season, gameweek, sel.user_id, sel.gw_selection_id, sel.chip_used)
            scored.append(sel.user_id)
        except Exception as e:
            logger.error(
                "score_gameweek: failed to score user_id=%s (season=%s, gameweek=%s): %s: %s",
                sel.user_id, season, gameweek, type(e).__name__, e,
            )
            failed.append((sel.user_id, str(e)))

    return {"scored": scored, "failed": failed}
