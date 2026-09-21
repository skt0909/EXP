"""
tasks.py — Celery tasks for the ML prediction pipeline and gameweek
scoring.

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

poll_and_score_dream11 is a ONE-OFF task (scheduled via .apply_async's
eta parameter from Game_logic/dream11.py's create_contest, NOT a
beat_schedule entry -- Beat is for recurring intervals, this fires
exactly twice per contest at fixed offsets from kickoff_time). Calls
poll_fixture_checkpoint (Data/live_poll.py -- relocated from
under Dream11 since it was never Dream11-specific) then
score_dream11_contest (Game_logic/dream11_scoring.py) in sequence,
same "wrap the pure functions, retry/log here" shape as every other
task in this file.

schedule_fixture_polls / poll_and_score_fpl_fixture are the classic-FPL
counterpart to the Dream11 pair above, added once ingest_upcoming_fixtures
(Data/fpl_ingest.py) started giving ml.fixtures real
future-dated rows to schedule against. schedule_fixture_polls IS a
beat_schedule entry (recurring, unlike Dream11's one-off-at-creation
trigger) since there's no single HTTP request that "creates" a classic
fixture the way Game_logic/dream11.py's create_contest does -- Beat has
to be the thing that notices a newly-ingested upcoming fixture and
schedules its two checkpoints. poll_and_score_fpl_fixture reuses the
SAME poll_fixture_checkpoint as Dream11, then calls the existing
score_gameweek (Results/scoring.py) as-is -- no new scoring logic,
same "recompute broadly and cheaply, idempotent by design" philosophy
as refresh_active_gameweeks.

On failure, the full exception is logged and the task retries up to 3
times (60s apart) via Celery's built-in retry mechanism -- for transient
blips (e.g. a momentary DB connection drop). Once retries are exhausted,
Celery re-raises the original exception and marks the task FAILED --
never swallowed, since Celery's own retry/monitoring depends on failures
being visible as failures.

HEARTBEATS. Each of the eight Beat-scheduled tasks (everything in
Worker/celery_app.py's beat_schedule) calls record_task_heartbeat
(Worker/task_health.py) once on every execution -- success at its return
point(s), failure in its except block, right before self.retry -- so
GET /health/scheduled-tasks can tell "Beat stopped firing this" apart
from "Beat is fine but this keeps failing." The failure-path call
re-acquires the engine via get_engine() rather than reusing the try
block's local, since the one thing that can make even engine
acquisition itself fail is the case the heartbeat most needs to record.
The five tasks that are NOT on Beat's schedule (run_ml_pipeline,
compute_gw_scores, compute_league_standings, poll_and_score_dream11,
poll_and_score_fpl_fixture -- one-off or manually-triggered) do not call
it: there is no configured interval for a one-off task to go stale
against.

See Worker/celery_app.py for how to actually run Beat locally.
"""

import logging
import os
import sys
import time
from datetime import timedelta
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
from feature_builder import build_features
from predictor import predict_points
from tier_builder import build_tiers
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
    lock_started_contests as _lock_started_contests,
    refresh_active_gameweeks as _refresh_active_gameweeks,
    revert_expired_free_hits as _revert_expired_free_hits,
    find_next_gameweek_needing_predictions,
    find_fixtures_needing_poll_schedule,
    mark_fixture_polls_scheduled,
    refresh_current_season_fixtures as _refresh_current_season_fixtures,
    find_contests_needing_finalization as _find_contests_needing_finalization,
    finalize_dream11_contest as _finalize_dream11_contest,
)
from Game_logic.dream11_scoring import score_dream11_contest, finalize_dream11_contest
from live_poll import poll_fixture_checkpoint
from Worker.task_health import record_task_heartbeat
from simulation.events import MatchEvent, MatchEventType
from simulation.event_store import insert_events
from simulation.gameweek import simulate_gameweek as _simulate_gameweek
from simulation.match import _fixture_context as _simulation_fixture_context
from simulation.match import simulate_match as _simulate_match
from simulation.season import simulate_season as _simulate_season

logger = logging.getLogger(__name__)

MODEL_VERSION = "xgboost_v1"


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="run_ml_pipeline")
def run_ml_pipeline(self, season: str, target_gameweek: int) -> dict:
    try:
        engine = get_engine()

        features = build_features(engine, season, target_gameweek)
        predictions = predict_points(features)
        tiers = build_tiers(engine, season, features, predictions)

        rows = tiers[["player_id", "predicted_points", "tier_or_label"]].copy()
        rows["season"] = season
        rows["gameweek"] = target_gameweek
        rows["model_version"] = MODEL_VERSION

        with engine.begin() as conn:
            conn.execute(
                text(
                    "DELETE FROM ml.ml_predictions "
                    "WHERE season = :season AND gameweek = :gameweek AND model_version = :mv"
                ),
                {"season": season, "gameweek": target_gameweek, "mv": MODEL_VERSION},
            )
            rows.to_sql("ml_predictions", conn, schema="ml", if_exists="append", index=False)

        return {
            "season": season,
            "gameweek": target_gameweek,
            "rows_written": len(rows),
            "tier_distribution": tiers["tier_or_label"].value_counts().to_dict(),
        }
    except Exception as exc:
        logger.exception(
            "run_ml_pipeline failed for season=%s, target_gameweek=%s (attempt %d/%d)",
            season, target_gameweek, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


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


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="revert_free_hits")
def revert_free_hits(self) -> dict:
    try:
        engine = get_engine()
        summary = _revert_expired_free_hits(engine)

        if summary["failed"]:
            logger.warning(
                "revert_free_hits: reverted %d free hit(s), %d FAILED: %s",
                len(summary["reverted"]), len(summary["failed"]), summary["failed"],
            )
        else:
            logger.info("revert_free_hits: reverted %d free hit(s), 0 failed", len(summary["reverted"]))
        record_task_heartbeat(engine, "revert_free_hits", success=True)
        return summary
    except Exception as exc:
        logger.exception("revert_free_hits failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "revert_free_hits", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="schedule_predictions")
def schedule_predictions(self) -> dict:
    try:
        engine = get_engine()
        next_gw = find_next_gameweek_needing_predictions(engine)

        if next_gw is None:
            logger.info("schedule_predictions: no upcoming gameweek needs predictions -- nothing to do")
            record_task_heartbeat(engine, "schedule_predictions", success=True)
            return {"triggered": None}

        season, gameweek = next_gw
        logger.info("schedule_predictions: triggering run_ml_pipeline for season=%s gameweek=%s", season, gameweek)
        result = run_ml_pipeline(season, gameweek)
        record_task_heartbeat(engine, "schedule_predictions", success=True)
        return {"triggered": {"season": season, "gameweek": gameweek}, "result": result}
    except Exception as exc:
        logger.exception("schedule_predictions failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "schedule_predictions", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="poll_and_score_dream11")
def poll_and_score_dream11(self, fixture_id: int, contest_id: int, checkpoint: str) -> dict:
    try:
        engine = get_engine()
        poll_summary = poll_fixture_checkpoint(engine, fixture_id, checkpoint)

        if checkpoint == "fulltime":
            # Try to freeze the result here, but only TRY: finalization is
            # gated on the fixture actually reporting finished, and at
            # kickoff+115min it usually has not yet (FPL's finished flag
            # lags until bonus points confirm). When that gate says no this
            # still scores the contest and leaves it open, and the
            # finalize_dream11_contests Beat sweep picks it up later.
            # That sweep, not this task, is what guarantees finalization
            # happens at all -- see its docstring.
            final_summary = finalize_dream11_contest(engine, contest_id)
            score_summary = final_summary["score"] or {"scored": [], "failed": []}
        else:
            final_summary = {"finalized": False, "reason": "halftime checkpoint"}
            score_summary = score_dream11_contest(engine, contest_id)

        logger.info(
            "poll_and_score_dream11: fixture_id=%s contest_id=%s checkpoint=%s -- "
            "polled %d player(s) (%d already settled, %d unresolved), scored %d team(s), %d failed, finalized=%s",
            fixture_id, contest_id, checkpoint,
            len(poll_summary["updated"]), len(poll_summary["already_settled"]), len(poll_summary["unresolved"]),
            len(score_summary["scored"]), len(score_summary["failed"]), final_summary["finalized"],
        )
        return {"poll": poll_summary, "score": score_summary, "finalize": final_summary}
    except Exception as exc:
        logger.exception(
            "poll_and_score_dream11 failed for fixture_id=%s, contest_id=%s, checkpoint=%s (attempt %d/%d)",
            fixture_id, contest_id, checkpoint, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


# Offsets from kickoff_time, not from actual match events -- same
# fixed-offset approach Game_logic/dream11.py's create_contest already
# uses for its own two checkpoints. 50min covers kickoff + a 45min half +
# some stoppage without waiting for full-time; 115min covers a full
# 90min match + a typical half-time break + stoppage, well before FPL's
# own bonus-point confirmation lag (see GameEngine/gameweek_finalize.py's
# module docstring).
HALFTIME_OFFSET_MINUTES = 50
FULLTIME_OFFSET_MINUTES = 115


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="finalize_dream11_contests")
def finalize_dream11_contests(self) -> dict:
    """Freeze the result of every contest whose match is over.

    WHY THIS IS A BEAT TASK AND NOT JUST THE kickoff+115min CHECKPOINT.
    poll_and_score_dream11 does attempt finalization at full-time, but it
    cannot be relied on to achieve it, for two independent reasons:

      1. Finalization is gated on ml.fixtures.finished, and at kickoff+115
         that flag is usually still FALSE -- FPL does not set it until
         bonus points are confirmed, hours later. So the checkpoint's
         attempt legitimately fails most of the time and something has to
         come back later.

      2. Those checkpoints are one-off ETAs booked at contest creation by
         create_contest, whose scheduling is explicitly best-effort: a
         broker outage there is caught and logged so contest creation
         still succeeds. A contest created during an outage has NO
         checkpoints booked at all, and before this sweep existed nothing
         would ever have scored it.

    So this is the guarantee and the checkpoint is the optimisation. It is
    idempotent by construction -- a finalized contest no longer matches
    find_contests_needing_finalization -- and in steady state it finds
    nothing, which the partial index makes cheap.
    """
    try:
        engine = get_engine()
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
        return {"finalized": finalized, "deferred": deferred}
    except Exception as exc:
        logger.exception(
            "finalize_dream11_contests failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1
        )
        record_task_heartbeat(get_engine(), "finalize_dream11_contests", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="refresh_fixtures")
def refresh_fixtures(self) -> dict:
    """Re-pull the current season's fixture list: scores, rescheduled
    kickoffs, and the finished flag.

    Runs BEFORE schedule_fixture_polls in the Beat schedule's ordering of
    concerns -- that task books polls for fixtures this one ingests, so
    without this it can only ever schedule fixtures a human last ingested
    by hand. That was the actual state: ml.fixtures had not been written
    since a single manual run, leaving played matches stuck at
    finished=FALSE indefinitely.
    """
    try:
        engine = get_engine()
        result = _refresh_current_season_fixtures(engine)

        if not result["refreshed"]:
            logger.info("refresh_fixtures: nothing to do -- %s", result.get("reason"))
        else:
            logger.info("refresh_fixtures: refreshed fixtures for season=%s", result["season"])
        record_task_heartbeat(engine, "refresh_fixtures", success=True)
        return result
    except Exception as exc:
        logger.exception("refresh_fixtures failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "refresh_fixtures", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="schedule_fixture_polls")
def schedule_fixture_polls(self) -> dict:
    try:
        engine = get_engine()
        fixtures = find_fixtures_needing_poll_schedule(engine)

        scheduled = []
        for f in fixtures:
            poll_and_score_fpl_fixture.apply_async(
                args=[f.id, "halftime"],
                eta=f.kickoff_time + timedelta(minutes=HALFTIME_OFFSET_MINUTES),
            )
            poll_and_score_fpl_fixture.apply_async(
                args=[f.id, "fulltime"],
                eta=f.kickoff_time + timedelta(minutes=FULLTIME_OFFSET_MINUTES),
            )
            mark_fixture_polls_scheduled(engine, f.id)
            scheduled.append(f.id)

        logger.info(
            "schedule_fixture_polls: scheduled polling for %d fixture(s): %s",
            len(scheduled), scheduled,
        )
        record_task_heartbeat(engine, "schedule_fixture_polls", success=True)
        return {"scheduled": scheduled}
    except Exception as exc:
        logger.exception("schedule_fixture_polls failed (attempt %d/%d)", self.request.retries + 1, self.max_retries + 1)
        record_task_heartbeat(get_engine(), "schedule_fixture_polls", success=False, error=str(exc))
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="poll_and_score_fpl_fixture")
def poll_and_score_fpl_fixture(self, fixture_id: int, checkpoint: str) -> dict:
    try:
        engine = get_engine()
        poll_summary = poll_fixture_checkpoint(engine, fixture_id, checkpoint)

        with engine.connect() as conn:
            fixture_row = conn.execute(
                text("SELECT season, gameweek FROM ml.fixtures WHERE id = :fixture_id"),
                {"fixture_id": fixture_id},
            ).first()
        score_summary = score_gameweek(engine, fixture_row.season, fixture_row.gameweek)

        logger.info(
            "poll_and_score_fpl_fixture: fixture_id=%s checkpoint=%s -- "
            "polled %d player(s) (%d already settled, %d unresolved), scored %d user(s), %d failed",
            fixture_id, checkpoint,
            len(poll_summary["updated"]), len(poll_summary["already_settled"]), len(poll_summary["unresolved"]),
            len(score_summary["scored"]), len(score_summary["failed"]),
        )
        return {"poll": poll_summary, "score": score_summary}
    except Exception as exc:
        logger.exception(
            "poll_and_score_fpl_fixture failed for fixture_id=%s, checkpoint=%s (attempt %d/%d)",
            fixture_id, checkpoint, self.request.retries + 1, self.max_retries + 1,
        )
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="simulation.simulate_match")
def simulate_match_task(self, fixture_id: int, events: list[dict]) -> dict:
    try:
        engine = get_engine()
        typed_events = [
            MatchEvent(
                event_type=MatchEventType(event["event_type"]),
                player_id=event.get("player_id"),
                minute=event.get("minute", 0),
                provider_event_id=event.get("provider_event_id"),
                related_player_id=event.get("related_player_id"),
                metadata=event.get("metadata") or {},
            )
            for event in events
        ]
        result = _simulate_match(engine, fixture_id, typed_events)
        return result.__dict__
    except Exception as exc:
        logger.exception("simulation.simulate_match failed for fixture_id=%s", fixture_id)
        raise self.retry(exc=exc)


@app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=2,
    name="simulation.crashable_match",
    acks_late=True,
    reject_on_worker_lost=True,
)
def crashable_simulate_match_task(self, fixture_id: int, events: list[dict], sleep_seconds: int = 20) -> dict:
    """Simulation-only task used to prove mid-task worker loss is recoverable.

    It durably writes the provider events, sleeps long enough for the harness
    to SIGKILL the worker, and then reuses the normal match simulator on retry.
    """
    if os.getenv("ENVIRONMENT") != "simulation":
        raise RuntimeError("simulation.crashable_match can only run with ENVIRONMENT=simulation")
    try:
        engine = get_engine()
        typed_events = [
            MatchEvent(
                event_type=MatchEventType(event["event_type"]),
                player_id=event.get("player_id"),
                minute=event.get("minute", 0),
                provider_event_id=event.get("provider_event_id"),
                related_player_id=event.get("related_player_id"),
                metadata=event.get("metadata") or {},
            )
            for event in events
        ]
        season, gameweek = _simulation_fixture_context(engine, fixture_id)
        insert_events(engine, fixture_id, season, gameweek, sorted(typed_events, key=lambda item: item.minute))
        time.sleep(sleep_seconds)
        result = _simulate_match(engine, fixture_id, typed_events)
        return result.__dict__
    except Exception as exc:
        logger.exception("simulation.crashable_match failed for fixture_id=%s", fixture_id)
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="simulation.simulate_gameweek")
def simulate_gameweek_task(self, gameweek: int, users: int = 100, season: str = "SIM-2026", seed: int = 12345) -> dict:
    try:
        engine = get_engine()
        result = _simulate_gameweek(engine, gameweek, users=users, season=season, seed=seed, accelerated=True)
        return result.__dict__
    except Exception as exc:
        logger.exception("simulation.simulate_gameweek failed for season=%s gameweek=%s", season, gameweek)
        raise self.retry(exc=exc)


@app.task(bind=True, max_retries=3, default_retry_delay=60, name="simulation.simulate_season")
def simulate_season_task(self, season: str = "SIM-2026", gameweeks: int = 38, users: int = 100, seed: int = 12345) -> dict:
    try:
        engine = get_engine()
        result = _simulate_season(engine, season_id=season, gameweeks=gameweeks, users=users, seed=seed)
        return {"season": result.season, "gameweeks": result.gameweeks, "users": result.users}
    except Exception as exc:
        logger.exception("simulation.simulate_season failed for season=%s", season)
        raise self.retry(exc=exc)
