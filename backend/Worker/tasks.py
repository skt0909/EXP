"""
tasks.py — Celery tasks for the ML prediction pipeline and gameweek
scoring.

run_ml_pipeline and schedule_predictions are currently DISABLED
(commented out below): predictions are backfilled, and dropping them
keeps xgboost out of the worker and Beat processes. The description of
both below is kept for when they are re-enabled.

run_ml_pipeline reuses the exact same building blocks as
Predict/predict_gameweek.ipynb and Context_assembler/main.py:
build_features -> predict_points (Predict/predictor.py) -> build_tiers,
then writes the result into ml.ml_predictions (tier_or_label column added
via ALTER TABLE -- no source DDL file exists in this repo to keep in sync,
confirmed with the user). Re-running for the same (season, gameweek) is
idempotent: existing rows for that season/gameweek/model_version are
deleted before the new ones are inserted, in the same transaction.

compute_gw_scores wraps Results/scoring.py's score_gameweek -- pure
function, all the actual scoring/autosub/captaincy logic lives there,
not here. Unlike run_ml_pipeline, a failure scoring one user does NOT
fail the whole task -- score_gameweek isolates per-user errors
internally and returns them in its summary; this task's own
try/except+retry only covers a failure before any per-user work starts
(e.g. the initial DB query itself failing).

compute_league_standings wraps Results/standings.py's
compute_league_standings (imported here under the alias
compute_standings to avoid shadowing this task's own name) -- same
per-league isolation as compute_gw_scores' per-user isolation; run
compute_gw_scores first for a given (season, gameweek) since this task
reads gw_scores, it doesn't compute it.

lock_expired_gameweeks / refresh_active_gameweeks / schedule_predictions
are the Celery Beat automation layer (see Worker/celery_app.py's
beat_schedule for intervals). Same shape as compute_league_standings:
each wraps a pure function re-exported by Worker/beat_registry.py
(imported under a leading-underscore alias to avoid shadowing the task's
own name), so the actual logic is testable without celery installed at
all. refresh_active_gameweeks calls the existing score_gameweek/
compute_league_standings pure functions per active gameweek -- it is
only the automatic trigger, not new scoring logic. schedule_predictions
calls run_ml_pipeline (defined below) directly as a plain function
call once it resolves which (season, gameweek) needs predictions --
celery tasks remain directly callable in-process this way, executing
synchronously within schedule_predictions' own task run rather than
dispatching a second async task.

poll_due_fixtures is the one live-polling task, for BOTH game modes. Beat
runs it every 60s; it asks the database which fixtures have a checkpoint
due (Data/live_poll.py's find_due_checkpoints: kickoff+50, kickoff+115,
and 'final' once ml.fixtures.finished is TRUE), fetches each gameweek's
live payload ONCE, writes every due fixture's stats from it, rescores the
Quick 11 contests on each fixture and the gameweek's tactical scores and
league tables, and records the checkpoint as done. Nothing is booked ahead
in Redis, so nothing can be lost or duplicated there: a checkpoint missed
while the worker was down is simply due on the next tick. It replaced
three tasks -- poll_and_score_dream11 (two ETAs booked per contest at
creation), schedule_fixture_polls and poll_and_score_fpl_fixture (ETAs
booked per fixture) -- whose bookings a Redis restart silently dropped,
and which, beyond Redis's 1-hour visibility_timeout, redelivered.

refresh_fixtures keeps ml.fixtures current (scores, kickoffs,
postponements, the finished flag 'final' waits on). It is on Beat every
15 minutes but calls the FPL API only when fixtures_refresh_reason says
so. refresh_player_prices re-pulls every player's price daily.

On failure, the full exception is logged and the task retries up to 3
times (60s apart) via Celery's built-in retry mechanism -- for transient
blips (e.g. a momentary DB connection drop). Once retries are exhausted,
Celery re-raises the original exception and marks the task FAILED --
never swallowed, since Celery's own retry/monitoring depends on failures
being visible as failures.

HEARTBEATS. Each Beat-scheduled task (everything in
Worker/celery_app.py's beat_schedule) calls record_task_heartbeat
(Worker/task_health.py) once on every execution -- success at its return
point(s), failure in its except block, right before self.retry -- so
GET /health/scheduled-tasks can tell "Beat stopped firing this" apart
from "Beat is fine but this keeps failing." The failure-path call
re-acquires the engine via get_engine() rather than reusing the try
block's local, since the one thing that can make even engine
acquisition itself fail is the case the heartbeat most needs to record.
The tasks that are NOT on Beat's schedule (compute_gw_scores,
compute_league_standings -- manually triggered) do not call it: there is
no configured interval for a one-off task to go stale against.

See Worker/celery_app.py for how to actually run Beat locally.
"""

import logging
import sys
from collections import defaultdict
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PROJECT_ROOT = _ROOT.parent
sys.path.insert(0, str(_PROJECT_ROOT))
sys.path.insert(0, str(_ROOT / "Feature_engineering"))
sys.path.insert(0, str(_ROOT / "Predict"))
sys.path.insert(0, str(_ROOT / "Data"))

from sqlalchemy import text

from Worker.celery_app import app
from Shared.db_utils import get_engine
# ML pipeline imports are disabled along with run_ml_pipeline and
# schedule_predictions below -- predictor pulls in xgboost, and every
# process that imports this module (the worker AND Beat) paid for it.
# from feature_builder import build_features
# from predictor import predict_points
# from tier_builder import build_tiers
# Phase 4a: the batched tactical job. Same name, same signature, same
# {"scored": [...], "failed": [...]} shape, so every call site below is
# unchanged; it additionally returns "skipped_reason".
from Results.scoring_job import score_gameweek_tactical as score_gameweek
from Results.standings import compute_league_standings as compute_standings
# Every Beat-scheduled function comes in through the registry, which is
# the one place that records what Beat runs and which module owns it --
# see Worker/beat_registry.py. The aliases avoid shadowing the task
# functions of the same name defined below.
from Worker.beat_registry import (
    lock_expired_gameweeks as _lock_expired_gameweeks,
    carry_forward_selections as _carry_forward_selections,
    lock_started_contests as _lock_started_contests,
    refresh_active_gameweeks as _refresh_active_gameweeks,
    find_next_gameweek_needing_predictions,
    find_due_checkpoints,
    mark_checkpoint_done,
    fixtures_refresh_reason,
    refresh_current_season_fixtures as _refresh_current_season_fixtures,
    refresh_player_prices as _refresh_player_prices,
    find_contests_needing_finalization as _find_contests_needing_finalization,
    finalize_dream11_contest as _finalize_dream11_contest,
    void_unplayable_contests as _void_unplayable_contests,
)
from Game_logic.dream11_scoring import score_dream11_contest, finalize_dream11_contest
from live_poll import poll_fixture_checkpoint
from fpl_ingest import fetch_json
from Worker.task_health import record_task_heartbeat

logger = logging.getLogger(__name__)

MODEL_VERSION = "xgboost_v1"


# DISABLED: run_ml_pipeline. Predictions are backfilled into
# ml.ml_predictions by hand with Predict/backfill_predictions.py (same
# steps, same write), so nothing needs to generate them live, and keeping
# this task meant loading xgboost into the worker and Beat. Re-enable
# together with the three imports at the top of this file and
# schedule_predictions below (which calls this directly).
# @app.task(bind=True, max_retries=3, default_retry_delay=60, name="run_ml_pipeline")
# def run_ml_pipeline(self, season: str, target_gameweek: int) -> dict:
#     try:
#         engine = get_engine()
#
#         features = build_features(engine, season, target_gameweek)
#         predictions = predict_points(features)
#         tiers = build_tiers(engine, season, features, predictions)
#
#         rows = tiers[["player_id", "predicted_points", "tier_or_label"]].copy()
#         rows["season"] = season
#         rows["gameweek"] = target_gameweek
#         rows["model_version"] = MODEL_VERSION
#
#         with engine.begin() as conn:
#             conn.execute(
#                 text(
#                     "DELETE FROM ml.ml_predictions "
#                     "WHERE season = :season AND gameweek = :gameweek AND model_version = :mv"
#                 ),
#                 {"season": season, "gameweek": target_gameweek, "mv": MODEL_VERSION},
#             )
#             rows.to_sql("ml_predictions", conn, schema="ml", if_exists="append", index=False)
#
#         return {
#             "season": season,
#             "gameweek": target_gameweek,
#             "rows_written": len(rows),
#             "tier_distribution": tiers["tier_or_label"].value_counts().to_dict(),
#         }
#     except Exception as exc:
#         logger.exception(
#             "run_ml_pipeline failed for season=%s, target_gameweek=%s (attempt %d/%d)",
#             season, target_gameweek, self.request.retries + 1, self.max_retries + 1,
#         )
#         raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="compute_gw_scores")
def compute_gw_scores(self, season: str, gameweek: int) -> dict:
    try:
        engine = get_engine()
        summary = score_gameweek(engine, season, gameweek)

        if summary["failed"]:
            failures = "; ".join(f"user_id={uid}: {err}" for uid, err in summary["failed"])
            logger.warning(
                "compute_gw_scores: season=%s gameweek=%s -- scored %d user(s), %d FAILED: %s",
                season, gameweek, len(summary["scored"]), len(summary["failed"]), failures,
            )
        else:
            logger.info(
                "compute_gw_scores: season=%s gameweek=%s -- scored %d user(s), 0 failed",
                season, gameweek, len(summary["scored"]),
            )

        return summary
    except Exception as exc:
        logger.exception(
            "compute_gw_scores failed for season=%s, gameweek=%s (attempt %d/%d)",
            season, gameweek, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="compute_league_standings")
def compute_league_standings(self, season: str, gameweek: int) -> dict:
    try:
        engine = get_engine()
        summary = compute_standings(engine, season, gameweek)

        if summary["failed"]:
            failures = "; ".join(f"league_id={lid}: {err}" for lid, err in summary["failed"])
            logger.warning(
                "compute_league_standings: season=%s gameweek=%s -- processed %d league(s), %d FAILED: %s",
                season, gameweek, len(summary["processed"]), len(summary["failed"]), failures,
            )
        else:
            logger.info(
                "compute_league_standings: season=%s gameweek=%s -- processed %d league(s), 0 failed",
                season, gameweek, len(summary["processed"]),
            )

        return summary
    except Exception as exc:
        logger.exception(
            "compute_league_standings failed for season=%s, gameweek=%s (attempt %d/%d)",
            season, gameweek, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="lock_expired_gameweeks")
def lock_expired_gameweeks(self) -> dict:
    try:
        engine = get_engine()
        summary = _lock_expired_gameweeks(engine)

        logger.info(
            "lock_expired_gameweeks: locked %d gameweek(s), skipped %d (no deadline yet)%s",
            len(summary["locked"]), len(summary["skipped_no_deadline"]),
            f": {summary['skipped_no_deadline']}" if summary["skipped_no_deadline"] else "",
        )
        record_task_heartbeat(engine, "lock_expired_gameweeks", success=True)
        return summary
    except Exception as exc:
        logger.exception("lock_expired_gameweeks failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "lock_expired_gameweeks", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="carry_forward_selections")
def carry_forward_selections(self) -> dict:
    try:
        engine = get_engine()
        summary = _carry_forward_selections(engine)

        logger.info(
            "carry_forward_selections: season=%s gameweek=%s carried %d, skipped %d (squad changed), skipped %d (no previous selection)",
            summary["season"], summary["gameweek"],
            len(summary["carried"]), len(summary["skipped_changed_squad"]), len(summary["skipped_no_previous"]),
        )
        record_task_heartbeat(engine, "carry_forward_selections", success=True)
        return summary
    except Exception as exc:
        logger.exception("carry_forward_selections failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "carry_forward_selections", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="lock_dream11_contests")
def lock_dream11_contests(self) -> dict:
    try:
        engine = get_engine()
        summary = _lock_started_contests(engine)

        logger.info(
            "lock_dream11_contests: locked %d contest(s), skipped %d (no kickoff_time yet)%s",
            len(summary["locked"]), len(summary["skipped_no_kickoff"]),
            f": {summary['skipped_no_kickoff']}" if summary["skipped_no_kickoff"] else "",
        )
        record_task_heartbeat(engine, "lock_dream11_contests", success=True)
        return summary
    except Exception as exc:
        logger.exception("lock_dream11_contests failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "lock_dream11_contests", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="refresh_active_gameweeks")
def refresh_active_gameweeks(self) -> dict:
    try:
        engine = get_engine()
        summary = _refresh_active_gameweeks(engine)

        if summary["failed"]:
            logger.warning(
                "refresh_active_gameweeks: refreshed %d gameweek(s), %d FAILED: %s",
                len(summary["refreshed"]), len(summary["failed"]), summary["failed"],
            )
        else:
            logger.info("refresh_active_gameweeks: refreshed %d gameweek(s), 0 failed", len(summary["refreshed"]))
        record_task_heartbeat(engine, "refresh_active_gameweeks", success=True)
        return summary
    except Exception as exc:
        logger.exception("refresh_active_gameweeks failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "refresh_active_gameweeks", success=False, error=str(exc))
        raise self.retry(exc=exc)



# DISABLED with run_ml_pipeline above, which it calls directly. Its
# schedule-predictions-weekly Beat entry is commented out in
# Worker/celery_app.py.
# @app.task(bind=True, max_retries=3, default_retry_delay=60, name="schedule_predictions")
# def schedule_predictions(self) -> dict:
#     try:
#         engine = get_engine()
#         next_gw = find_next_gameweek_needing_predictions(engine)
#
#         if next_gw is None:
#             logger.info("schedule_predictions: no upcoming gameweek needs predictions -- nothing to do")
#             record_task_heartbeat(engine, "schedule_predictions", success=True)
#             return {"triggered": None}
#
#         season, gameweek = next_gw
#         logger.info("schedule_predictions: triggering run_ml_pipeline for season=%s gameweek=%s", season, gameweek)
#         result = run_ml_pipeline(season, gameweek)
#         record_task_heartbeat(engine, "schedule_predictions", success=True)
#         return {"triggered": {"season": season, "gameweek": gameweek}, "result": result}
#     except Exception as exc:
#         logger.exception("schedule_predictions failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
#         record_task_heartbeat(get_engine(), "schedule_predictions", success=False, error=str(exc))
#         raise self.retry(exc=exc)


@app.task(bind=True, max_retries=0, name="poll_due_fixtures")
def poll_due_fixtures(self) -> dict:
    """Run every live-poll checkpoint that is due, for both game modes.

    One fixture failing (FPL down, a bad row) does not stop the others and
    is not marked done, so the next tick retries it -- which is also why
    this task never uses self.retry: the 60s Beat cadence IS the retry. In
    steady state (no match on) it is one small query and no API call.

    Order per gameweek: fetch the live payload once; per due fixture,
    write its stats, then rescore its Quick 11 contests ('final' finalizes
    them); then rescore the gameweek's tactical scores and league tables
    once; only then mark each fixture's checkpoint done. A crash before the
    mark leaves it due, and every step is safe to repeat.
    """
    engine = get_engine()
    try:
        by_gameweek = defaultdict(list)
        for f in find_due_checkpoints(engine):
            by_gameweek[(f.season, f.gameweek)].append(f)

        done, failed = [], []
        for (season, gameweek), fixtures in by_gameweek.items():
            try:
                live = fetch_json(f"event/{gameweek}/live/")
            except Exception as e:
                logger.exception("poll_due_fixtures: live fetch failed for %s GW%s", season, gameweek)
                failed.extend((f.id, f.checkpoint, f"live fetch: {e}") for f in fixtures)
                continue

            polled = []
            for f in fixtures:
                try:
                    poll = poll_fixture_checkpoint(engine, f.id, f.checkpoint, live=live)
                    contests = _score_fixture_contests(engine, f.id, f.checkpoint)
                    logger.info(
                        "poll_due_fixtures: fixture_id=%s %s -- %d updated, %d already settled, "
                        "%d unresolved, %d held (double gameweek); contests %s",
                        f.id, f.checkpoint, len(poll["updated"]), len(poll["already_settled"]),
                        len(poll["unresolved"]), len(poll["held"]), contests,
                    )
                    polled.append(f)
                except Exception as e:
                    logger.exception("poll_due_fixtures: fixture_id=%s %s failed", f.id, f.checkpoint)
                    failed.append((f.id, f.checkpoint, f"{type(e).__name__}: {e}"))

            if not polled:
                continue
            try:
                summary = score_gameweek(engine, season, gameweek)
                compute_standings(engine, season, gameweek)
                if summary and summary.get("failed"):
                    logger.error(
                        "poll_due_fixtures: %s GW%s -- %d manager(s) failed to score: %s",
                        season, gameweek, len(summary["failed"]), summary["failed"],
                    )
            except Exception as e:
                # Stats are written but the gameweek wasn't rescored; leave the
                # checkpoints due so the next tick does both again.
                logger.exception("poll_due_fixtures: rescoring %s GW%s failed", season, gameweek)
                failed.extend((f.id, f.checkpoint, f"rescore: {e}") for f in polled)
                continue

            for f in polled:
                mark_checkpoint_done(engine, f.id, f.checkpoint)
                done.append((f.id, f.checkpoint))

        if done or failed:
            logger.info("poll_due_fixtures: %d checkpoint(s) done %s, %d failed %s", len(done), done, len(failed), failed)
        record_task_heartbeat(
            engine, "poll_due_fixtures", success=not failed,
            error="; ".join(f"fixture {fid} {cp}: {err}" for fid, cp, err in failed) or None,
        )
        return {"done": done, "failed": failed}
    except Exception as exc:
        logger.exception("poll_due_fixtures failed")
        record_task_heartbeat(get_engine(), "poll_due_fixtures", success=False, error=str(exc))
        raise


# Open contests on one fixture -- a finalized (or voided) contest is frozen.
_OPEN_CONTESTS_ON_FIXTURE_QUERY = text(
    "SELECT id FROM dream11.contests WHERE fixture_id = :fixture_id AND finalized_at IS NULL ORDER BY id"
)


def _score_fixture_contests(engine, fixture_id: int, checkpoint: str) -> dict:
    """Rescore the fixture's open Quick 11 contests from the stats just
    written; at 'final', also finalize them (finalize_dream11_contest itself
    refuses unless the fixture is finished and every team scored).

    Raises if any contest failed, so the fixture's checkpoint stays due and
    the next tick retries it."""
    with engine.connect() as conn:
        contest_ids = [r.id for r in conn.execute(_OPEN_CONTESTS_ON_FIXTURE_QUERY, {"fixture_id": fixture_id})]
    result = {"scored": 0, "finalized": 0}
    failed = []
    for cid in contest_ids:
        try:
            if checkpoint == "final" and finalize_dream11_contest(engine, cid)["finalized"]:
                result["finalized"] += 1
            elif checkpoint != "final":
                score_dream11_contest(engine, cid)
                result["scored"] += 1
            else:
                result["scored"] += 1  # finished-gate or a team failed; the sweep retries
        except Exception as e:
            logger.exception("poll_due_fixtures: contest_id=%s failed to score", cid)
            failed.append((cid, str(e)))
    if failed:
        raise RuntimeError(f"{len(failed)} contest(s) failed: {failed}")
    return result


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="finalize_dream11_contests")
def finalize_dream11_contests(self) -> dict:
    """Void contests that can't finish, then freeze every contest whose
    match is over.

    VOIDING FIRST. A contest on a postponed fixture (kickoff removed) or an
    abandoned one (not finished a week after kickoff) would otherwise wait
    forever, since finalization needs fixtures.finished -- see
    Game_logic/dream11_scoring.py's void_unplayable_contests.

    THIS SWEEP IS THE GUARANTEE, poll_due_fixtures' 'final' checkpoint the
    fast path. 'final' finalizes a fixture's contests as soon as it runs,
    but finalize_dream11_contest refuses while any team fails to score;
    this sweep retries such a contest. It is idempotent by construction --
    a finalized contest no longer matches find_contests_needing_finalization
    -- and in steady state it finds nothing, which the partial index makes
    cheap.
    """
    try:
        engine = get_engine()
        voided = _void_unplayable_contests(engine)
        if voided:
            logger.info("finalize_dream11_contests: voided %d contest(s) %s", len(voided), voided)

        contest_ids = _find_contests_needing_finalization(engine)

        finalized, deferred = [], []
        for contest_id in contest_ids:
            try:
                result = _finalize_dream11_contest(engine, contest_id)
                if result["finalized"]:
                    finalized.append(contest_id)
                else:
                    deferred.append((contest_id, result["reason"]))
            except Exception as e:
                # One bad contest must not stop the rest -- same per-item
                # isolation as score_dream11_contest's per-team try/except.
                logger.exception("finalize_dream11_contests: contest_id=%s failed", contest_id)
                deferred.append((contest_id, f"{type(e).__name__}: {e}"))

        if finalized or deferred:
            logger.info(
                "finalize_dream11_contests: %d finalized %s, %d deferred %s",
                len(finalized), finalized, len(deferred), deferred,
            )
        record_task_heartbeat(engine, "finalize_dream11_contests", success=True)
        return {"voided": voided, "finalized": finalized, "deferred": deferred}
    except Exception as exc:
        logger.exception(
            "finalize_dream11_contests failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1
        )
        record_task_heartbeat(get_engine(), "finalize_dream11_contests", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="refresh_fixtures")
def refresh_fixtures(self) -> dict:
    """Re-pull the current season's fixture list: scores, rescheduled or
    postponed kickoffs, and the finished flag -- which is what makes a
    fixture's 'final' checkpoint due in poll_due_fixtures.

    On Beat every 15 minutes, but it only calls the API when
    fixtures_refresh_reason says it's needed: a fixture has kicked off and
    isn't finished yet, or nothing was refreshed in the last day. Between
    match days that is one API call a day.
    """
    try:
        engine = get_engine()
        reason = fixtures_refresh_reason(engine)
        if reason is None:
            record_task_heartbeat(engine, "refresh_fixtures", success=True)
            return {"refreshed": False, "reason": "not needed"}

        result = _refresh_current_season_fixtures(engine)
        if not result["refreshed"]:
            logger.info("refresh_fixtures: nothing to do -- %s", result.get("reason"))
        else:
            logger.info("refresh_fixtures: refreshed fixtures for season=%s (%s)", result["season"], reason)
        record_task_heartbeat(engine, "refresh_fixtures", success=True)
        return result
    except Exception as exc:
        logger.exception("refresh_fixtures failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "refresh_fixtures", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=300, name="refresh_player_prices")
def refresh_player_prices(self) -> dict:
    """Daily: every player's now_cost (GW mode's buy/sell price) and any
    player new to the game, from bootstrap-static. A new player has no
    backfilled prediction until the next backfill, so /chat shows them as
    New/Insufficient Data until then -- expected, not an error."""
    try:
        engine = get_engine()
        result = _refresh_player_prices(engine)
        logger.info("refresh_player_prices: %s", result)
        record_task_heartbeat(engine, "refresh_player_prices", success=True)
        return result
    except Exception as exc:
        logger.exception("refresh_player_prices failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "refresh_player_prices", success=False, error=str(exc))
        raise self.retry(exc=exc)
