"""
fpl_sim.py — DEV-ONLY manual driver for the classic-FPL gameweek
lifecycle. Lets you walk a whole season by hand -- open gameweek, make
transfers, trip the deadline, score, advance -- without running Celery
Beat, Redis, or a worker.

WHY THIS EXISTS INSTEAD OF COMMENTING OUT THE BEAT LOGIC
--------------------------------------------------------
Nothing in the request path consults Celery. The only thing Beat
contributes to the FPL rules is *when* two DB flags flip:

    gw_selections.is_locked   <- Worker/tasks.py lock_expired_gameweeks
    dream11.contests.is_locked <- Worker/tasks.py lock_dream11_contests

Both tasks are thin wrappers around pure functions in
GameEngine/gameweek_lock.py and Game_logic/dream11_locking.py, and Beat
is a SEPARATE OS process from the
API and the worker (see Worker/celery_app.py's docstring). So simply
not starting `celery beat` already gives you a frozen clock: no
gameweek ever auto-locks, every deadline stays open indefinitely, and
uvicorn behaves exactly as it does in production otherwise. Commenting
out beat_schedule would change nothing that not-running-beat doesn't
already change, and would leave edited code to un-edit later.

This script is the other half: the manual trigger for those same
transitions, calling the SAME pure functions Beat's tasks call, so what
you exercise here is production logic, not a test double.

USAGE (run from the backend/ directory, venv active)
----------------------------------------------------
    python Tools/fpl_sim.py status      --season 2024-25 --gw 1 --user 1
    python Tools/fpl_sim.py lock        --season 2024-25 --gw 1
    python Tools/fpl_sim.py unlock      --season 2024-25 --gw 1
    python Tools/fpl_sim.py score       --season 2024-25 --gw 1
    python Tools/fpl_sim.py beat lock_expired_gameweeks
    python Tools/fpl_sim.py beat refresh_active_gameweeks
    python Tools/fpl_sim.py beat lock_dream11_contests
    python Tools/fpl_sim.py beat revert_free_hits --buffer-hours -9999

`lock` / `unlock` are the deliberately-artificial pair: they set the
flag directly for a chosen gameweek so you control the deadline
regardless of what ml.fixtures.kickoff_time actually says. `beat ...`
is the honest path -- it runs the real lock function against the
real kickoff times, which is what you want once you're testing whether
the *rule* fires, not just what happens after it has.

unlock has no production counterpart at all: gw_selections has a BEFORE
UPDATE trigger (enforce_selection_lock, see Migrations/baseline_schema.sql)
that RAISES on any UPDATE of an already-locked row, so TRUE -> FALSE is
impossible through normal SQL by design. This command disables that
trigger for the single statement and re-enables it, which needs table
ownership on public.gw_selections. That is a test-harness escape hatch
and nothing else -- never wire it into an endpoint.

This module imports no celery and no ML code, so it runs with just
SQLAlchemy + the DB reachable.
"""

import argparse
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from sqlalchemy import text

from Shared.db_utils import get_engine, safe_url
# Phase 4b: the simulation harness drives the NEW scorer, passing
# allow_sim_seasons=True because its seasons (SIM38OK, SIM38TST, SIMSMOKE)
# are exactly the ones the production filter refuses. This is the only
# caller in the repo that passes it.
from Results.scoring_job import score_gameweek_tactical
from Results.standings import compute_league_standings
from GameEngine.free_hit_revert import revert_expired_free_hits
from GameEngine.gameweek_finalize import refresh_active_gameweeks
from GameEngine.gameweek_lock import lock_expired_gameweeks
from Shared.deadlines import resolve_gameweek_deadline
from Game_logic.dream11_locking import lock_started_contests

# Direct flag writes. LOCK_STMT is the same FALSE -> TRUE direction
# LOCK_GAMEWEEK_STMT in gameweek_lock.py uses, so the trigger permits it
# untouched; UNLOCK_STMT is the direction the trigger exists to forbid.
LOCK_STMT = text(
    "UPDATE gw_selections SET is_locked = TRUE "
    "WHERE season = :season AND gameweek = :gameweek AND is_locked = FALSE"
)
UNLOCK_STMT = text(
    "UPDATE gw_selections SET is_locked = FALSE "
    "WHERE season = :season AND gameweek = :gameweek AND is_locked = TRUE"
)
DISABLE_LOCK_TRIGGER = text("ALTER TABLE public.gw_selections DISABLE TRIGGER enforce_selection_lock")
ENABLE_LOCK_TRIGGER = text("ALTER TABLE public.gw_selections ENABLE TRIGGER enforce_selection_lock")

SELECTION_STATE_QUERY = text(
    """
    SELECT user_id, is_locked, chip_used, captain_id, vice_captain_id, submitted_at
    FROM gw_selections
    WHERE season = :season AND gameweek = :gameweek
      AND (:user_id IS NULL OR user_id = :user_id)
    ORDER BY user_id
    """
)

# transfers_made replaces the old user_squads.total_transfers column,
# which was dropped: it never excluded Free-Hit-cancelled transfers, so
# it drifted from reality the moment a chip was cancelled. Counted live
# from `transfers` with the same cancelled_transfers exclusion the
# banking arithmetic uses, so this dump agrees with what the app charges.
SQUAD_STATE_QUERY = text(
    """
    SELECT us.user_id, us.budget_remaining,
           COUNT(*) FILTER (WHERE sp.is_active) AS active_players,
           COUNT(*) AS total_rows,
           (SELECT COUNT(*) FROM transfers t
             WHERE t.user_id = us.user_id AND t.season = us.season
               AND NOT EXISTS (
                   SELECT 1 FROM cancelled_transfers c WHERE c.transfer_id = t.id
               )
           ) AS transfers_made
    FROM user_squads us
    LEFT JOIN squad_players sp ON sp.user_squad_id = us.id
    WHERE us.season = :season AND (:user_id IS NULL OR us.user_id = :user_id)
    GROUP BY us.user_id, us.season, us.budget_remaining
    ORDER BY us.user_id
    """
)

TRANSFERS_STATE_QUERY = text(
    """
    SELECT gameweek,
           COUNT(*) FILTER (WHERE is_free) AS free_transfers,
           COUNT(*) FILTER (WHERE NOT is_free) AS paid_transfers
    FROM transfers
    WHERE season = :season AND (:user_id IS NULL OR user_id = :user_id)
    GROUP BY gameweek
    ORDER BY gameweek
    """
)

CHIPS_STATE_QUERY = text(
    """
    SELECT chip_type, gameweek_used
    FROM chips
    WHERE season = :season AND (:user_id IS NULL OR user_id = :user_id)
    ORDER BY gameweek_used, chip_type
    """
)

# Collapsed to one row per snapshot -- 15 player rows per free hit would
# bury everything else in the status dump.
FREE_HIT_STATE_QUERY = text(
    """
    SELECT gameweek, COUNT(*) AS players_snapshotted,
           MIN(budget_remaining) AS budget_to_restore,
           MAX(reverted_at) AS reverted_at
    FROM free_hit_squads
    WHERE season = :season AND (:user_id IS NULL OR user_id = :user_id)
    GROUP BY gameweek
    ORDER BY gameweek
    """
)

SCORES_STATE_QUERY = text(
    """
    SELECT gameweek, raw_points, final_points, transfer_hits, hit_deductions,
           total_points, season_total
    FROM gw_scores
    WHERE season = :season AND (:user_id IS NULL OR user_id = :user_id)
    ORDER BY gameweek
    """
)

# How many player_gw_stats rows exist for this gameweek -- scoring reads
# these via COALESCE(..., 0), so "everyone scored 0" is almost always
# this being empty rather than a scoring bug.
STATS_COVERAGE_QUERY = text(
    """
    SELECT COUNT(*) AS rows_present,
           COUNT(*) FILTER (WHERE minutes > 0) AS rows_with_minutes
    FROM ml.player_gw_stats
    WHERE season = :season AND gameweek = :gameweek
    """
)


def _print_rows(title: str, rows, empty_note: str) -> None:
    print(f"\n{title}")
    if not rows:
        print(f"  (none) -- {empty_note}")
        return
    for r in rows:
        print("  " + "  ".join(f"{k}={v}" for k, v in r._mapping.items()))


def cmd_status(args) -> None:
    engine = get_engine()
    params = {"season": args.season, "user_id": args.user}

    print(f"DB: {safe_url()}")
    print(f"season={args.season}  gameweek={args.gw}  user={args.user if args.user else 'ALL'}")

    deadline = resolve_gameweek_deadline(engine, args.season, args.gw)
    print(f"\nGW{args.gw} deadline (MIN ml.fixtures.kickoff_time): {deadline or 'NOT INGESTED YET'}")

    with engine.connect() as conn:
        coverage = conn.execute(STATS_COVERAGE_QUERY, {"season": args.season, "gameweek": args.gw}).first()
        print(
            f"ml.player_gw_stats for GW{args.gw}: {coverage.rows_present} row(s), "
            f"{coverage.rows_with_minutes} with minutes > 0"
        )

        selections = conn.execute(
            SELECTION_STATE_QUERY, {**params, "gameweek": args.gw}
        ).all()
        squads = conn.execute(SQUAD_STATE_QUERY, params).all()
        transfers = conn.execute(TRANSFERS_STATE_QUERY, params).all()
        chips = conn.execute(CHIPS_STATE_QUERY, params).all()
        free_hits = conn.execute(FREE_HIT_STATE_QUERY, params).all()
        scores = conn.execute(SCORES_STATE_QUERY, params).all()

    _print_rows(
        f"gw_selections (GW{args.gw}) -- is_locked is THE transfer/XI gate",
        selections,
        "no selection submitted for this gameweek, which counts as UNLOCKED",
    )
    _print_rows("user_squads (season-wide)", squads, "no squad -- POST /squad/select first")
    _print_rows("transfers by gameweek", transfers, "no transfers made yet")
    _print_rows("chips used", chips, "no chips played yet")
    _print_rows(
        "free hit snapshots -- reverted_at=None means the pre-chip squad is still owed back",
        free_hits,
        "no free hit activated",
    )
    _print_rows("gw_scores", scores, "nothing scored yet -- run `score`")


def cmd_lock(args) -> None:
    engine = get_engine()
    with engine.begin() as conn:
        result = conn.execute(LOCK_STMT, {"season": args.season, "gameweek": args.gw})
    print(f"locked {result.rowcount} gw_selections row(s) for season={args.season} gameweek={args.gw}")
    if result.rowcount == 0:
        print("  (already locked, or no gw_selections row exists for that gameweek)")


def cmd_unlock(args) -> None:
    engine = get_engine()
    try:
        with engine.begin() as conn:
            conn.execute(DISABLE_LOCK_TRIGGER)
            result = conn.execute(UNLOCK_STMT, {"season": args.season, "gameweek": args.gw})
            conn.execute(ENABLE_LOCK_TRIGGER)
    except Exception as e:
        print(f"unlock failed: {type(e).__name__}: {e}")
        print(
            "  ALTER TABLE ... DISABLE TRIGGER requires ownership of public.gw_selections. "
            "Connect as the table's owner (or a superuser) to use unlock."
        )
        raise SystemExit(1)
    print(f"unlocked {result.rowcount} gw_selections row(s) for season={args.season} gameweek={args.gw}")


def cmd_score(args) -> None:
    engine = get_engine()
    score_summary = score_gameweek_tactical(
        engine, args.season, args.gw, allow_sim_seasons=True
    )
    print(f"score_gameweek: scored {len(score_summary['scored'])} user(s) {score_summary['scored']}")
    for user_id, err in score_summary["failed"]:
        print(f"  FAILED user_id={user_id}: {err}")

    standings_summary = compute_league_standings(engine, args.season, args.gw)
    print(f"compute_league_standings: processed {len(standings_summary['processed'])} league(s)")
    for league_id, err in standings_summary["failed"]:
        print(f"  FAILED league_id={league_id}: {err}")


# Only the recurring FPL-side Beat tasks whose pure functions are
# celery-free. run_ml_pipeline / schedule_predictions / the poll tasks
# are deliberately absent: they pull in the ML stack or the live-poll
# HTTP client, neither of which belongs in a rules walkthrough.
BEAT_TASKS = {
    "lock_expired_gameweeks": lock_expired_gameweeks,
    "lock_dream11_contests": lock_started_contests,
    "refresh_active_gameweeks": refresh_active_gameweeks,
    "revert_free_hits": revert_expired_free_hits,
}


def cmd_beat(args) -> None:
    engine = get_engine()
    # revert_free_hits is the one task that waits for a gameweek to END
    # rather than start, so on a frozen clock it would never fire during
    # a hand-run test. A negative buffer moves its "this fixture can't
    # still be in progress" cutoff into the future, which forces the
    # revert immediately -- the only reason this override exists.
    kwargs = {}
    if args.task == "revert_free_hits" and args.buffer_hours is not None:
        kwargs["buffer_hours"] = args.buffer_hours
    summary = BEAT_TASKS[args.task](engine, **kwargs)
    print(f"{args.task}: {summary}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[1], allow_abbrev=False)
    sub = parser.add_subparsers(dest="command", required=True)

    def add_gw_args(p, need_gw=True):
        p.add_argument("--season", required=True, help="e.g. 2024-25")
        p.add_argument("--gw", type=int, required=need_gw, help="gameweek number")

    p_status = sub.add_parser("status", help="dump every piece of FPL state for a season/gameweek")
    add_gw_args(p_status)
    p_status.add_argument("--user", type=int, default=None, help="restrict to one user_id (default: all)")
    p_status.set_defaults(func=cmd_status)

    p_lock = sub.add_parser("lock", help="force a gameweek's deadline to have passed")
    add_gw_args(p_lock)
    p_lock.set_defaults(func=cmd_lock)

    p_unlock = sub.add_parser("unlock", help="reopen a locked gameweek (test-harness only)")
    add_gw_args(p_unlock)
    p_unlock.set_defaults(func=cmd_unlock)

    p_score = sub.add_parser("score", help="run score_gameweek + compute_league_standings")
    add_gw_args(p_score)
    p_score.set_defaults(func=cmd_score)

    p_beat = sub.add_parser("beat", help="run one Beat task's pure function against real kickoff times")
    p_beat.add_argument("task", choices=sorted(BEAT_TASKS))
    p_beat.add_argument(
        "--buffer-hours",
        type=int,
        default=None,
        help="revert_free_hits only: hours after kickoff before a fixture counts as over "
             "(default 3). Pass a large negative value, e.g. -9999, to force an immediate revert.",
    )
    p_beat.set_defaults(func=cmd_beat)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
