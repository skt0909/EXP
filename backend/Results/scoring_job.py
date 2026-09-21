"""
scoring_job.py — the live scoring job, running the tactical engine.

Replaces the per-manager loop in Results/scoring.py for production callers.
scoring.py itself is left in place for Phase 4b: the dashboard still imports
resolve_autosubs from it, and unpicking that is 4b's job, not this one.

WHAT THIS PRESERVES, DELIBERATELY AND EXACTLY
  * the idempotent upsert on (user_id, season, gameweek) -- re-running a
    gameweek with unchanged upstream data rewrites the same row rather than
    adding one, which matters because partial/live scoring runs this several
    times as a gameweek progresses;
  * season_total recomputed fresh from gw_scores each run rather than trusting
    a stored running total;
  * the user_gameweek_finance snapshot (scoring.py:198-207), same columns, same
    upsert, same meaning of team_value -- what was PAID for the active squad,
    not what it could be sold for;
  * the return shape {"scored": [...], "failed": [...]} and per-manager error
    isolation, so one bad row cannot abort the batch.

WHAT IS NEW
  * BATCHING. The old job opened a connection and ran several queries PER
    MANAGER. This loads managers in batches of SCORING_BATCH_SIZE and runs ONE
    stats query for every player in the batch, so memory and round trips are
    bounded by the batch rather than by the league. The server is a 1 GB
    e2-micro with a single-task worker (IMPLEMENTATION_PLAN.md section 10).
  * The tactical engine. raw_points is General Points, plus tactical_points and
    sub_bonus; there are no hits, so total_points == final_points.
  * A SEASON FILTER. Only real seasons are scored. SIM38OK / SIM38TST /
    SIMSMOKE belong to the simulation harness and must never be treated as game
    data.
  * AN EPOCH GATE. A gameweek before the ruleset's first_gameweek is not scored
    at all.
  * VALIDATION IS ADVISORY (decision A7). An invalid stored selection is
    LOGGED and STILL SCORED, so a data bug never leaves a manager with no
    score.

DECIMAL, NOT FLOAT. creativity is numeric(6,1) and psycopg2 returns Decimal.
It is passed through untouched -- tier_points refuses floats outright, so a
stray float() here would fail loudly rather than silently deciding a boundary.
"""
import logging
import re
from types import SimpleNamespace
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from Gameplay.selection_rules import SelectionInput, SwapInput, validate_selection
from Results.tactical_scoring import Selection, Slot, Swap, score_selection
from Shared.rules import FIRST_GAMEWEEK, RULES_VERSION

logger = logging.getLogger(__name__)

# How many managers are held in memory at once. A module constant rather than a
# parameter default alone so the worker can be tuned without a code change at
# every call site.
SCORING_BATCH_SIZE = 200

# A real season is exactly YYYY-YY. Anything else -- SIM38OK, SIM38TST,
# SIMSMOKE -- belongs to the simulation harness and is never game data.
REAL_SEASON_RE = re.compile(r"^\d{4}-\d{2}$")


SELECTIONS_QUERY = text(
    "SELECT id AS gw_selection_id, user_id, tactic FROM gw_selections "
    "WHERE season = :season AND gameweek = :gameweek ORDER BY user_id"
)

EPOCH_QUERY = text(
    "SELECT first_gameweek FROM ruleset_epochs "
    "WHERE season = :season AND rules_version = :rules_version"
)

SLOTS_QUERY = text(
    "SELECT gw_selection_id, player_id, position_slot, is_bonus FROM starting_xi "
    "WHERE gw_selection_id = ANY(:selection_ids) ORDER BY gw_selection_id, position_slot"
)

SWAPS_QUERY = text(
    "SELECT gw_selection_id, player_out_id, player_in_id FROM tactical_swaps "
    "WHERE gw_selection_id = ANY(:selection_ids) ORDER BY gw_selection_id, id"
)

# ONE query per batch, for every player any manager in the batch holds.
#
# LEFT JOIN, not JOIN: a player with no stats row yet has not played (or has
# not been ingested), which is normal during live scoring and must come back as
# a zero rather than dropping the player out of the squad entirely.
#
# One row per fixture, so a double gameweek arrives as two rows for the same
# player and the engine tiers each fixture separately -- which is the rule.
# The join is on ml.players.id while starting_xi stores the FPL id, so fpl_id
# is selected back out to key the result.
BATCH_STATS_QUERY = text(
    """
    SELECT mp.fpl_id AS player_id,
           mp.position,
           pgs.minutes, pgs.goals_scored, pgs.assists, pgs.clean_sheets,
           pgs.goals_conceded, pgs.saves, pgs.penalties_saved,
           pgs.penalties_missed, pgs.own_goals, pgs.yellow_cards,
           pgs.red_cards, pgs.defensive_contributions, pgs.creativity
    FROM ml.players mp
    LEFT JOIN ml.player_gw_stats pgs
           ON pgs.player_id = mp.id
          AND pgs.season = :season
          AND pgs.gameweek = :gameweek
    WHERE mp.season = :season
      AND mp.fpl_id = ANY(:player_ids)
    """
)

# BOUNDED BELOW BY THE EPOCH (ambiguity E5, now closed). season_total used to
# sum every earlier gameweek, including rows written under rules_version 1 or
# 2, so a season that switched rulesets mid-way carried a cumulative total
# spanning two scoring systems. :first_gameweek is the ruleset epoch, or
# FIRST_GAMEWEEK when there is no epoch row -- in which case this is exactly
# the old behaviour, because the season has only ever known these rules.
SEASON_TOTAL_PRIOR_QUERY = text(
    "SELECT user_id, COALESCE(SUM(total_points), 0) AS prior FROM gw_scores "
    "WHERE season = :season "
    "  AND gameweek >= :first_gameweek AND gameweek < :gameweek "
    "  AND user_id = ANY(:user_ids) "
    "GROUP BY user_id"
)

# Same computation as scoring.py's TEAM_VALUE_AND_BANK_QUERY, widened to a
# batch. team_value is the sum of what was PAID for the active squad.
BATCH_FINANCE_QUERY = text(
    """
    SELECT us.user_id, us.budget_remaining,
           COALESCE(SUM(sp.purchase_price) FILTER (WHERE sp.is_active), 0) AS team_value_tenths
    FROM user_squads us
    LEFT JOIN squad_players sp ON sp.user_squad_id = us.id
    WHERE us.season = :season AND us.user_id = ANY(:user_ids)
    GROUP BY us.user_id, us.budget_remaining
    """
)

UPSERT_GW_SCORE_STMT = text(
    """
    INSERT INTO gw_scores
        (user_id, season, gameweek, raw_points, tactical_points, sub_bonus,
         final_points, total_points, season_total, rules_version)
    VALUES
        (:user_id, :season, :gameweek, :raw_points, :tactical_points, :sub_bonus,
         :final_points, :total_points, :season_total, :rules_version)
    ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
        raw_points = EXCLUDED.raw_points,
        tactical_points = EXCLUDED.tactical_points,
        sub_bonus = EXCLUDED.sub_bonus,
        final_points = EXCLUDED.final_points,
        total_points = EXCLUDED.total_points,
        season_total = EXCLUDED.season_total,
        rules_version = EXCLUDED.rules_version
    """
)

# Copied verbatim from scoring.py:198-207 -- the snapshot must not change
# shape just because the scorer behind it did.
UPSERT_GW_FINANCE_STMT = text(
    """
    INSERT INTO user_gameweek_finance (user_id, season, gameweek, bank, team_value)
    VALUES (:user_id, :season, :gameweek, :bank, :team_value)
    ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
        bank = EXCLUDED.bank,
        team_value = EXCLUDED.team_value,
        captured_at = now()
    """
)

# A selected player with no ml.players row for this season (ambiguity E6).
# He is given a position that matches NO formation minimum, so he counts
# towards none of them -- see _score_one's warning for the full reasoning.
UNKNOWN_POSITION = "UNKNOWN"

_STAT_FIELDS = (
    "minutes", "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "saves", "penalties_saved", "penalties_missed", "own_goals",
    "yellow_cards", "red_cards", "defensive_contributions",
)


def ruleset_first_gameweek(conn, season: str) -> int:
    """The first gameweek played under this ruleset, from ruleset_epochs.

    A missing row means this database has never known another ruleset, so the
    answer is FIRST_GAMEWEEK. See migration e7c4d81b3a95 for why the anchor is
    stored rather than derived from gameplay data."""
    stored = conn.execute(
        EPOCH_QUERY, {"season": season, "rules_version": RULES_VERSION}
    ).scalar()
    return FIRST_GAMEWEEK if stored is None else int(stored)


def _stat_row(row) -> dict:
    """One player-fixture row in the shape the engine reads.

    creativity is passed through as the Decimal psycopg2 returned. NEVER
    float() it: the tier boundaries are exact comparisons and tier_points
    rejects floats precisely so that a lost-precision value cannot decide
    whether 39.9 clears 40."""
    out = {f: (getattr(row, f) or 0) for f in _STAT_FIELDS}
    creativity = row.creativity
    out["creativity"] = Decimal("0") if creativity is None else creativity
    return out


def _chunks(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def score_gameweek_tactical(engine, season: str, gameweek: int,
                            batch_size: int = SCORING_BATCH_SIZE,
                            allow_sim_seasons: bool = False) -> dict:
    """Score every manager with a gw_selections row for (season, gameweek).

    Returns {"scored": [...], "failed": [(user_id, msg), ...],
             "skipped_reason": None | str}.

    Never raises for one manager's bad data; only a failure before any
    per-manager work (the driving query itself) propagates.

    allow_sim_seasons (ambiguity E1, now closed) exists for ONE caller:
    Tools/fpl_sim.py, whose whole purpose is to drive the SIM38OK / SIM38TST /
    SIMSMOKE seasons the filter otherwise refuses. It is an explicit ARGUMENT
    rather than an environment variable on purpose -- an env var is ambient and
    could be set on the production worker by accident, where an argument has to
    be typed at the one call site that wants it. The production entry points
    never pass it, and test_the_production_task_never_allows_sim_seasons pins
    that.
    """
    if allow_sim_seasons and not REAL_SEASON_RE.match(season or ""):
        logger.warning(
            "score_gameweek_tactical: scoring NON-REAL season %r because "
            "allow_sim_seasons=True. This is the simulation harness; production "
            "must never reach this line.",
            season,
        )
    elif not REAL_SEASON_RE.match(season or ""):
        reason = (f"not a real season: {season!r} does not match "
                  f"{REAL_SEASON_RE.pattern} -- simulation seasons are never scored")
        logger.info("score_gameweek_tactical: %s", reason)
        return {"scored": [], "failed": [], "skipped_reason": reason}

    with engine.connect() as conn:
        first_gameweek = ruleset_first_gameweek(conn, season)
        if gameweek < first_gameweek:
            reason = (f"gameweek {gameweek} precedes the ruleset epoch "
                      f"(first_gameweek={first_gameweek}) for season {season}")
            logger.info("score_gameweek_tactical: %s", reason)
            return {"scored": [], "failed": [], "skipped_reason": reason}

        selections = conn.execute(
            SELECTIONS_QUERY, {"season": season, "gameweek": gameweek}
        ).all()
    # first_gameweek is carried into the batches below: it bounds season_total.

    scored, failed = [], []
    for batch in _chunks(selections, max(1, batch_size)):
        try:
            done, bad = _score_batch(engine, season, gameweek, batch, first_gameweek)
        except Exception as exc:                       # noqa: BLE001
            # A whole-batch failure is still isolated: the remaining batches
            # run. Every manager in it is reported, so nothing disappears.
            logger.error(
                "score_gameweek_tactical: batch of %s failed (season=%s, gameweek=%s): %s: %s",
                len(batch), season, gameweek, type(exc).__name__, exc,
            )
            failed.extend((sel.user_id, str(exc)) for sel in batch)
            continue
        scored.extend(done)
        failed.extend(bad)

    return {"scored": scored, "failed": failed, "skipped_reason": None}


def _score_batch(engine, season: str, gameweek: int, batch, first_gameweek: int):
    """One batch: four reads, then one write transaction."""
    selection_ids = [s.gw_selection_id for s in batch]
    user_ids = [s.user_id for s in batch]

    with engine.connect() as conn:
        slot_rows = conn.execute(SLOTS_QUERY, {"selection_ids": selection_ids}).all()
        swap_rows = conn.execute(SWAPS_QUERY, {"selection_ids": selection_ids}).all()

        slots_by_selection, player_ids = {}, set()
        for r in slot_rows:
            slots_by_selection.setdefault(r.gw_selection_id, []).append(r)
            player_ids.add(r.player_id)
        swaps_by_selection = {}
        for r in swap_rows:
            swaps_by_selection.setdefault(r.gw_selection_id, []).append(r)

        # THE one stats query for the whole batch.
        stats_by_player, positions = {}, {}
        if player_ids:
            for r in conn.execute(
                BATCH_STATS_QUERY,
                {"season": season, "gameweek": gameweek, "player_ids": sorted(player_ids)},
            ):
                positions[r.player_id] = r.position
                if r.minutes is None and r.creativity is None and r.goals_scored is None:
                    # LEFT JOIN produced a no-stats row: the player exists but
                    # has not played. Record the position, add no fixture.
                    stats_by_player.setdefault(r.player_id, [])
                    continue
                stats_by_player.setdefault(r.player_id, []).append(_stat_row(r))

        prior = {
            r.user_id: r.prior
            for r in conn.execute(
                SEASON_TOTAL_PRIOR_QUERY,
                {"season": season, "gameweek": gameweek, "user_ids": user_ids,
                 "first_gameweek": first_gameweek},
            )
        }
        finance = {
            r.user_id: (r.budget_remaining, r.team_value_tenths)
            for r in conn.execute(
                BATCH_FINANCE_QUERY, {"season": season, "user_ids": user_ids},
            )
        }

    scored, failed, writes, finance_writes = [], [], [], []
    for sel in batch:
        try:
            result = _score_one(sel, slots_by_selection.get(sel.gw_selection_id, []),
                                swaps_by_selection.get(sel.gw_selection_id, []),
                                stats_by_player, positions, season, gameweek)
        except Exception as exc:                        # noqa: BLE001
            logger.error(
                "score_gameweek_tactical: failed to score user_id=%s (season=%s, gameweek=%s): %s: %s",
                sel.user_id, season, gameweek, type(exc).__name__, exc,
            )
            failed.append((sel.user_id, str(exc)))
            continue

        total = result.total
        writes.append({
            "user_id": sel.user_id, "season": season, "gameweek": gameweek,
            "raw_points": result.raw_points,
            "tactical_points": result.tactical_points,
            "sub_bonus": result.sub_bonus,
            "final_points": total,
            # E7: total_points and final_points are ALWAYS EQUAL under these
            # rules, because hits are the only thing that ever separated them
            # and hits are gone. Both are kept for now: the dashboard's
            # breakdown contract still names both, and dropping one is a
            # response-shape change that belongs after the frontend phase.
            # test_total_points_equals_final_points_because_there_are_no_hits
            # pins the equality so it cannot drift while both exist.
            "total_points": total,
            "season_total": (prior.get(sel.user_id, 0) or 0) + total,
            "rules_version": RULES_VERSION,
        })
        if sel.user_id in finance:
            bank, team_value = finance[sel.user_id]
            finance_writes.append({
                "user_id": sel.user_id, "season": season, "gameweek": gameweek,
                "bank": bank, "team_value": team_value,
            })
        scored.append(sel.user_id)

    if writes:
        _written, write_failures = _persist(engine, writes, finance_writes)
        if write_failures:
            lost = {uid for uid, _ in write_failures}
            scored = [uid for uid in scored if uid not in lost]
            failed.extend(write_failures)

    return scored, failed


def _execute_writes(conn, writes, finance_writes):
    """The actual statements. A seam, so the fallback below and the tests can
    drive exactly one write at a time."""
    conn.execute(UPSERT_GW_SCORE_STMT, writes)
    if finance_writes:
        conn.execute(UPSERT_GW_FINANCE_STMT, finance_writes)


def _persist(engine, writes, finance_writes):
    """Write a batch, falling back to one manager at a time if that fails.

    AMBIGUITY E4, now closed. The 4a job wrote the whole batch in one
    transaction, so a single bad row took all 200 managers with it -- worse
    than the per-manager job it replaced, whose blast radius was one. The batch
    write is still tried first because it is one round trip for 200 managers;
    only when it fails does this degrade to the old behaviour.

    The retry uses a SAVEPOINT per manager inside ONE transaction
    (begin_nested), so a manager who fails again rolls back alone and the rest
    still commit -- rather than opening 200 transactions.

    Returns (written_user_ids, [(user_id, error), ...]).
    """
    finance_by_user = {f["user_id"]: f for f in finance_writes}
    try:
        with engine.begin() as conn:
            _execute_writes(conn, writes, finance_writes)
        return [w["user_id"] for w in writes], []
    except SQLAlchemyError as exc:
        logger.error(
            "score_gameweek_tactical: batch write of %s manager(s) failed, "
            "falling back to one at a time: %s: %s",
            len(writes), type(exc).__name__, exc,
        )

    written, failures = [], []
    with engine.begin() as conn:
        for w in writes:
            fin = finance_by_user.get(w["user_id"])
            savepoint = conn.begin_nested()
            try:
                _execute_writes(conn, [w], [fin] if fin else [])
                savepoint.commit()
                written.append(w["user_id"])
            except SQLAlchemyError as exc:
                savepoint.rollback()
                logger.error(
                    "score_gameweek_tactical: write failed for user_id=%s "
                    "(season=%s, gameweek=%s): %s: %s",
                    w["user_id"], w["season"], w["gameweek"], type(exc).__name__, exc,
                )
                failures.append((w["user_id"], str(exc)))
    return written, failures


def _score_one(sel, slot_rows, swap_rows, stats_by_player, positions, season, gameweek):
    """Validate (advisory) and score one manager."""
    # E6: a player selected but absent from ml.players for this season used to
    # raise KeyError from the engine's position lookup and fail the WHOLE
    # manager. He now gets UNKNOWN_POSITION, which:
    #   * scores 0, because BATCH_STATS_QUERY returns no row for him either,
    #     so his fixture list is empty and general_points is never called;
    #   * counts towards NO formation minimum, because "UNKNOWN" matches none
    #     of FORMATION_MIN's keys. That is the honest reading -- we do not know
    #     he was a defender, so he cannot be credited with satisfying the
    #     defensive floor. It makes Auto Sub cover STRICTER, not looser: a line
    #     depending on him to reach its floor refuses the cover rather than
    #     allowing one on a guess;
    #   * cannot come on as an Auto Sub cover, guarded in the engine.
    missing = sorted({r.player_id for r in slot_rows} - set(positions))
    if missing:
        logger.warning(
            "data-integrity: user_id=%s season=%s gameweek=%s selected player_id(s) "
            "%s with no ml.players row for this season. They score 0, count towards "
            "no formation minimum, and cannot be used as an Auto Sub cover. The rest "
            "of the selection is scored normally.",
            sel.user_id, season, gameweek, missing,
        )
        positions = dict(positions)
        for pid in missing:
            positions[pid] = UNKNOWN_POSITION

    slots = [Slot(position_slot=r.position_slot, player_id=r.player_id,
                  is_bonus=bool(r.is_bonus)) for r in slot_rows]
    swaps = [Swap(player_out_id=r.player_out_id, player_in_id=r.player_in_id)
             for r in swap_rows]

    # Decision A7: advisory only. A selection that fails validation is STILL
    # scored -- a data bug must never leave a manager with no score. The
    # warning is the signal that something wrote a row the endpoint would have
    # refused.
    xi = [s.player_id for s in slots if s.position_slot <= 11]
    bench = [s.player_id for s in slots if s.position_slot > 11]
    errors = validate_selection(
        SelectionInput(
            season=season, gameweek=gameweek, tactic=sel.tactic,
            player_ids=xi, bench_order=bench,
            bonus_player_ids=[s.player_id for s in slots if s.is_bonus],
            swaps=[SwapInput(s.player_out_id, s.player_in_id) for s in swaps],
        ),
        set(xi) | set(bench),
        positions,
        {},
        check_timing=False,
    )
    if errors:
        logger.warning(
            "data-integrity: stored selection for user_id=%s (season=%s, gameweek=%s, "
            "gw_selection_id=%s) fails validation and is being scored anyway "
            "(decision A7): %s",
            sel.user_id, season, gameweek, sel.gw_selection_id, errors,
        )

    return score_selection(
        Selection(tactic=sel.tactic, slots=slots, swaps=swaps),
        stats_by_player,
        positions,
    )


# ---- single-manager entry point, for the dashboard -------------------------


def score_manager(conn, season: str, gameweek: int, gw_selection_id: int, tactic: str):
    """Score ONE manager and return the engine's full SelectionScore.

    The dashboard calls this per request, so a manager's per-player points are
    computed rather than stored (decision: no new tables). It reuses the same
    queries and the same row mapping as the batch job, which is the point --
    a second mapping here could disagree with the one that wrote gw_scores,
    and the dashboard would explain a number it did not produce.

    Takes a Connection, not an Engine: the dashboard already holds one open.
    """
    slot_rows = conn.execute(SLOTS_QUERY, {"selection_ids": [gw_selection_id]}).all()
    swap_rows = conn.execute(SWAPS_QUERY, {"selection_ids": [gw_selection_id]}).all()

    player_ids = sorted({r.player_id for r in slot_rows})
    stats_by_player, positions = {}, {}
    if player_ids:
        for r in conn.execute(
            BATCH_STATS_QUERY,
            {"season": season, "gameweek": gameweek, "player_ids": player_ids},
        ):
            positions[r.player_id] = r.position
            if r.minutes is None and r.creativity is None and r.goals_scored is None:
                stats_by_player.setdefault(r.player_id, [])
                continue
            stats_by_player.setdefault(r.player_id, []).append(_stat_row(r))

    sel = SimpleNamespace(user_id=None, gw_selection_id=gw_selection_id, tactic=tactic)
    result = _score_one(sel, slot_rows, swap_rows, stats_by_player, positions,
                        season, gameweek)
    return result, stats_by_player, positions
