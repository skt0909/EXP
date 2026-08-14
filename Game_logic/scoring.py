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
  client submitted player_ids/bench_order (Game_logic/starting_xi.py),
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

from sqlalchemy import text

logger = logging.getLogger(__name__)

CAPTAIN_MULTIPLIER = 2
TRIPLE_CAPTAIN_MULTIPLIER = 3
HIT_COST = 4

GW_SELECTIONS_QUERY = text(
    "SELECT id AS gw_selection_id, user_id, chip_used FROM gw_selections "
    "WHERE season = :season AND gameweek = :gameweek"
)

# minutes/total_points default to 0 via COALESCE when no player_gw_stats
# row exists yet (match not played/ingested) -- partial/live scoring is
# allowed by design, not an error.
STARTING_XI_STATS_QUERY = text(
    """
    SELECT sx.position_slot, sx.is_captain, sx.is_vice_captain,
           mp.position,
           COALESCE(pgs.minutes, 0) AS minutes,
           COALESCE(pgs.total_points, 0) AS total_points
    FROM starting_xi sx
    JOIN ml.players mp ON mp.fpl_id = sx.player_id AND mp.season = :season
    LEFT JOIN ml.player_gw_stats pgs
        ON pgs.player_id = mp.id AND pgs.season = :season AND pgs.gameweek = :gameweek
    WHERE sx.gw_selection_id = :gw_selection_id
    """
)

TRANSFER_HITS_QUERY = text(
    "SELECT COUNT(*) FROM transfers "
    "WHERE user_id = :user_id AND season = :season AND gameweek = :gameweek AND is_free = FALSE"
)

SEASON_TOTAL_PRIOR_QUERY = text(
    "SELECT COALESCE(SUM(total_points), 0) FROM gw_scores "
    "WHERE user_id = :user_id AND season = :season AND gameweek < :gameweek"
)

UPSERT_GW_SCORE_STMT = text(
    """
    INSERT INTO gw_scores
        (user_id, season, gameweek, raw_points, final_points, transfer_hits, hit_deductions, total_points, season_total)
    VALUES
        (:user_id, :season, :gameweek, :raw_points, :final_points, :transfer_hits, :hit_deductions, :total_points, :season_total)
    ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
        raw_points = EXCLUDED.raw_points,
        final_points = EXCLUDED.final_points,
        transfer_hits = EXCLUDED.transfer_hits,
        hit_deductions = EXCLUDED.hit_deductions,
        total_points = EXCLUDED.total_points,
        season_total = EXCLUDED.season_total
    """
)


def _formation_legal(effective_starters: dict) -> bool:
    counts = Counter(r.position for r in effective_starters.values())
    return 3 <= counts.get("DEF", 0) <= 5 and 2 <= counts.get("MID", 0) <= 5 and 1 <= counts.get("FWD", 0) <= 3


def _resolve_autosubs(starters: dict, bench: dict) -> list:
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

    if len(rows) != 15:
        raise ValueError(f"expected 15 starting_xi rows for gw_selection_id={gw_selection_id}, got {len(rows)}")

    starters = {r.position_slot: r for r in rows if r.position_slot <= 11}
    bench = {r.position_slot: r for r in rows if r.position_slot > 11}

    if chip_used == "bench_boost":
        effective = list(rows)  # all 15, skip autosub entirely -- moot when everyone counts
    else:
        effective = _resolve_autosubs(starters, bench)

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
