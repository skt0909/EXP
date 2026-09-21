"""Executable checks for real simulation-stack behavior.

These checks assume the Docker compose stack is running and the simulation
database has been migrated. They intentionally use the real Redis broker and
Celery worker where worker behavior is under test.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, text

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))
for subdir in ("Context_assembler", "Data", "Game_logic", "Gameplay", "Shared", "Worker"):
    path = BACKEND / subdir
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

os.environ.setdefault("ENVIRONMENT", "simulation")
os.environ.setdefault("DATABASE_URL", "postgresql://postgres:postgres@localhost:55432/fantasy_simulation")
os.environ.setdefault("REDIS_URL", "redis://localhost:56379/0")
os.environ.setdefault("JWT_SECRET", "simulation-secret")

# Phase 4c: the tactical job replaces the deleted Results/scoring.py.
from Results.scoring_job import score_gameweek_tactical as score_gameweek
from Results.standings import compute_league_standings
from Worker.tasks import (
    crashable_simulate_match_task,
    lock_expired_gameweeks,
    refresh_active_gameweeks,
    simulate_gameweek_task,
    simulate_match_task,
)
from simulation.db import create_fixture, create_player_pool, create_teams, reset_simulation_data, seed_basic_gameweek
from simulation.events import Assist, Goal, MatchEvent, MatchEventType, YellowCard
from simulation.gameweek import simulate_gameweek
from simulation.load import LOAD_LEVELS, run_load_levels
from simulation.match import simulate_match
from simulation.matchday import simulate_matchday
from simulation.season import simulate_season


COMPOSE = ["docker", "compose", "-f", "simulation/docker-compose.simulation.yml"]


def engine():
    return create_engine(os.environ["DATABASE_URL"], pool_pre_ping=True)


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=ROOT, text=True, capture_output=True, check=True)


def _run_json_check(name: str) -> dict:
    out = _run([sys.executable, "-m", "simulation.real_stack_checks", name])
    return json.loads(out.stdout)


def _flush_redis() -> None:
    _run(COMPOSE + ["exec", "-T", "redis", "redis-cli", "FLUSHDB"])


def _fixture_with_players(season: str = "CHKBASE"):
    eng = engine()
    reset_simulation_data(eng, season)
    teams = create_teams(eng, season, 2)
    players = create_player_pool(eng, season, teams, 40)
    fixture_id = create_fixture(eng, season, 1, datetime.now(timezone.utc), teams[0], teams[1])
    return eng, fixture_id, list(players.keys())


def worker_restart_check() -> dict:
    eng, fixture_id, players = _fixture_with_players("CHKWRK")
    player = players[0]
    event_payload = [
        asdict(MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="CHK-WRK-KO")),
        asdict(Goal(player, 12, "CHK-WRK-GOAL")),
        asdict(Goal(player, 12, "CHK-WRK-GOAL")),
        asdict(MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="CHK-WRK-FT")),
    ]

    _run(COMPOSE + ["stop", "worker"])
    result = simulate_match_task.apply_async(args=[fixture_id, event_payload])
    _run(COMPOSE + ["up", "-d", "worker"])
    output = result.get(timeout=90)
    replay = simulate_match_task.apply_async(args=[fixture_id, event_payload]).get(timeout=90)

    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT s.goals_scored, count(e.*) AS event_rows "
                "FROM ml.player_gw_stats s "
                "JOIN ml.players p ON p.id = s.player_id "
                "LEFT JOIN simulation.football_events e ON e.provider_event_id = 'CHK-WRK-GOAL' "
                "WHERE p.season = 'CHKWRK' AND p.fpl_id = :player "
                "GROUP BY s.goals_scored"
            ),
            {"player": player},
        ).first()
    return {"task": output, "replay": replay, "goals_scored": row.goals_scored, "event_rows": row.event_rows}


def worker_mid_task_crash_check() -> dict:
    eng, fixture_id, players = _fixture_with_players("CHKCRASH")
    player = players[0]
    event_payload = [
        asdict(MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="CHK-CRASH-KO")),
        asdict(Goal(player, 12, "CHK-CRASH-GOAL")),
        asdict(MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="CHK-CRASH-FT")),
    ]
    _run(COMPOSE + ["stop", "beat"])
    _run(COMPOSE + ["stop", "worker"])
    _flush_redis()
    _run(COMPOSE + ["up", "-d", "--scale", "worker=1", "worker"])
    result = crashable_simulate_match_task.apply_async(args=[fixture_id, event_payload, 20])

    saw_durable_event = False
    for _ in range(40):
        with eng.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM simulation.football_events WHERE season='CHKCRASH'")).scalar()
        if count:
            saw_durable_event = True
            break
        time.sleep(0.25)
    if not saw_durable_event:
        raise RuntimeError("crashable task did not durably write events before timeout")

    worker_id = _run(COMPOSE + ["ps", "-q", "worker"]).stdout.strip().splitlines()[0]
    _run(["docker", "kill", "--signal=KILL", worker_id])
    _run(COMPOSE + ["up", "-d", "worker"])
    output = result.get(timeout=120)

    replay = simulate_match_task.apply_async(args=[fixture_id, event_payload]).get(timeout=90)
    with eng.connect() as conn:
        row = conn.execute(
            text(
                "SELECT s.goals_scored, count(e.*) AS event_rows, count(*) FILTER (WHERE e.processed_at IS NOT NULL) AS processed_rows "
                "FROM ml.player_gw_stats s "
                "JOIN ml.players p ON p.id = s.player_id "
                "LEFT JOIN simulation.football_events e ON e.provider_event_id = 'CHK-CRASH-GOAL' "
                "WHERE p.season = 'CHKCRASH' AND p.fpl_id = :player "
                "GROUP BY s.goals_scored"
            ),
            {"player": player},
        ).first()
        fixture = conn.execute(text("SELECT finished FROM ml.fixtures WHERE id=:id"), {"id": fixture_id}).first()
    return {
        "task": output,
        "replay": replay,
        "killed_worker": worker_id,
        "goals_scored": row.goals_scored,
        "event_rows": row.event_rows,
        "processed_rows": row.processed_rows,
        "fixture_finished": fixture.finished,
    }


def beat_restart_check() -> dict:
    eng = engine()
    before = _heartbeat_count(eng)
    _run(COMPOSE + ["restart", "beat"])
    time.sleep(8)
    after_restart = _heartbeat_count(eng)
    first = lock_expired_gameweeks.apply_async().get(timeout=60)
    second = lock_expired_gameweeks.apply_async().get(timeout=60)
    after_tasks = _heartbeat_count(eng)
    return {"heartbeat_rows_before": before, "after_restart": after_restart, "after_tasks": after_tasks, "first": first, "second": second}


def beat_real_tick_restart_check() -> dict:
    eng = engine()
    _run(COMPOSE + ["stop", "worker"])
    _run(COMPOSE + ["stop", "beat"])
    _flush_redis()
    _run(COMPOSE + ["up", "-d", "--scale", "worker=1", "worker", "beat"])
    seed_basic_gameweek(eng, "CHKBEAT", 1, users=1, seed=503, kickoff_time=datetime.now(timezone.utc))
    with eng.connect() as conn:
        before = conn.execute(
            text("SELECT last_success_at FROM task_heartbeats WHERE task_name='lock_expired_gameweeks'")
        ).scalar()

    def _heartbeat_after(previous):
        for _ in range(40):
            with eng.connect() as conn:
                row = conn.execute(
                    text("SELECT last_success_at FROM task_heartbeats WHERE task_name='lock_expired_gameweeks'")
                ).scalar()
            if row is not None and (previous is None or row > previous):
                return row
            time.sleep(0.5)
        raise RuntimeError("lock_expired_gameweeks did not heartbeat from a real Beat tick")

    first_tick = _heartbeat_after(before)
    with eng.connect() as conn:
        locked_after_first = conn.execute(text("SELECT COUNT(*) FROM gw_selections WHERE season='CHKBEAT' AND is_locked=TRUE")).scalar()

    _run(COMPOSE + ["restart", "beat"])
    second_tick = _heartbeat_after(first_tick)
    with eng.connect() as conn:
        locked_after_restart = conn.execute(text("SELECT COUNT(*) FROM gw_selections WHERE season='CHKBEAT' AND is_locked=TRUE")).scalar()
        selection_rows = conn.execute(text("SELECT COUNT(*) FROM gw_selections WHERE season='CHKBEAT'")).scalar()

    return {
        "heartbeat_before": before,
        "first_tick": first_tick,
        "second_tick_after_restart": second_tick,
        "locked_after_first_tick": locked_after_first,
        "locked_after_restart_tick": locked_after_restart,
        "selection_rows": selection_rows,
    }


def _heartbeat_count(eng) -> int:
    with eng.connect() as conn:
        return conn.execute(text("SELECT COUNT(*) FROM task_heartbeats")).scalar()


def deadline_race_check() -> dict:
    # Uses production DB lock function and selection-lock trigger at the exact
    # DB boundary by setting kickoff_time=now() inside a transaction.
    eng = engine()
    result = simulate_gameweek(eng, 1, users=1, season="CHKRACE", seed=501, accelerated=False)
    with eng.begin() as conn:
        conn.execute(text("UPDATE ml.fixtures SET kickoff_time = now() WHERE season='CHKRACE' AND gameweek=1"))
    locked = lock_expired_gameweeks.apply_async().get(timeout=60)
    with eng.connect() as conn:
        selection = conn.execute(text("SELECT id, is_locked FROM gw_selections WHERE season='CHKRACE' LIMIT 1")).first()
    try:
        with eng.begin() as conn:
            conn.execute(text("UPDATE gw_selections SET chip_used='bench_boost' WHERE id=:id"), {"id": selection.id})
        late_update = "accepted"
    except Exception as exc:
        late_update = f"rejected:{type(exc).__name__}"
    return {"setup": result.score_summary, "locked": locked, "selection_locked": selection.is_locked, "late_update": late_update}


def api_deadline_race_check() -> dict:
    from fastapi.testclient import TestClient
    from Data.auth import create_access_token
    from main import app

    eng = engine()
    season = "CHKAPI"
    world = seed_basic_gameweek(eng, season, 1, users=2, seed=509, kickoff_time=datetime.now(timezone.utc))
    users = world["users"]
    client = TestClient(app)

    def _payload(user, captain_shift=0):
        xi = user.player_fpl_ids[:11]
        bench = user.player_fpl_ids[11:]
        return {
            "season": season,
            "gameweek": 1,
            "player_ids": xi,
            "bench_order": bench,
            "captain_id": xi[captain_shift],
            "vice_captain_id": xi[1 if captain_shift != 1 else 2],
            "chip_used": None,
        }

    headers = {user.user_id: {"Authorization": f"Bearer {create_access_token(user.user_id)}"} for user in users}
    with eng.begin() as conn:
        conn.execute(text("UPDATE ml.fixtures SET kickoff_time = now() + interval '91 minutes' WHERE season=:season"), {"season": season})
        conn.execute(text("DELETE FROM gw_selections WHERE season=:season"), {"season": season})
    accepted = client.post("/gw_selection", json=_payload(users[0]), headers=headers[users[0].user_id])

    with eng.begin() as conn:
        conn.execute(text("UPDATE ml.fixtures SET kickoff_time = now() + interval '90 minutes' WHERE season=:season"), {"season": season})

    def _change_existing():
        response = client.post("/gw_selection", json=_payload(users[0], captain_shift=2), headers=headers[users[0].user_id])
        return {"operation": "change_existing", "status": response.status_code, "body": response.json()}

    def _submit_new():
        response = client.post("/gw_selection", json=_payload(users[1]), headers=headers[users[1].user_id])
        return {"operation": "submit_new", "status": response.status_code, "body": response.json()}

    def _lock():
        return {"operation": "lock", "body": lock_expired_gameweeks.apply_async().get(timeout=60)}

    with ThreadPoolExecutor(max_workers=3) as pool:
        race_results = list(pool.map(lambda fn: fn(), [_change_existing, _submit_new, _lock]))

    with eng.connect() as conn:
        selections = conn.execute(
            text("SELECT user_id, captain_id, is_locked FROM gw_selections WHERE season=:season ORDER BY user_id"),
            {"season": season},
        ).all()
    return {
        "initial_submit_status": accepted.status_code,
        "race_results": race_results,
        "selection_rows": [tuple(r) for r in selections],
    }


def multi_worker_event_concurrency_check() -> dict:
    eng, fixture_id, players = _fixture_with_players("CHKCONC")
    p1, p2 = players[0], players[1]
    event_sets = [
        [MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="CHK-CONC-KO"), Goal(p1, 10, "CHK-CONC-GOAL")],
        [Goal(p1, 10, "CHK-CONC-GOAL"), Assist(p2, 10, "CHK-CONC-AST")],
        [Assist(p2, 10, "CHK-CONC-AST"), YellowCard(p2, 40, "CHK-CONC-YC"), MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="CHK-CONC-FT")],
    ]
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda evs: simulate_match(eng, fixture_id, evs).__dict__, event_sets))
    with eng.connect() as conn:
        stats = conn.execute(
            text(
                "SELECT p.fpl_id, s.goals_scored, s.assists, s.yellow_cards "
                "FROM ml.player_gw_stats s JOIN ml.players p ON p.id=s.player_id "
                "WHERE p.season='CHKCONC' ORDER BY p.fpl_id"
            )
        ).all()
        event_count = conn.execute(text("SELECT COUNT(*) FROM simulation.football_events WHERE season='CHKCONC'")).scalar()
    return {"results": results, "event_count": event_count, "stats": [tuple(r) for r in stats]}


def real_multi_worker_event_race_check() -> dict:
    _run(COMPOSE + ["stop", "beat"])
    _run(COMPOSE + ["stop", "worker"])
    _flush_redis()
    _run(COMPOSE + ["up", "-d", "--scale", "worker=2", "worker"])
    eng, fixture_id, players = _fixture_with_players("CHKMWORK")
    p1, p2 = players[0], players[1]
    event_sets = [
        [
            asdict(MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="CHK-MW-KO")),
            asdict(Goal(p1, 10, "CHK-MW-GOAL")),
        ],
        [
            asdict(Goal(p1, 10, "CHK-MW-GOAL")),
            asdict(Assist(p2, 10, "CHK-MW-AST")),
        ],
        [
            asdict(Assist(p2, 10, "CHK-MW-AST")),
            asdict(YellowCard(p2, 40, "CHK-MW-YC")),
            asdict(MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="CHK-MW-FT")),
        ],
    ]
    tasks = [simulate_match_task.apply_async(args=[fixture_id, events]) for events in event_sets]
    results = [task.get(timeout=120) for task in tasks]
    with eng.connect() as conn:
        stats = conn.execute(
            text(
                "SELECT p.fpl_id, s.goals_scored, s.assists, s.yellow_cards "
                "FROM ml.player_gw_stats s JOIN ml.players p ON p.id=s.player_id "
                "WHERE p.season='CHKMWORK' ORDER BY p.fpl_id"
            )
        ).all()
        event_count = conn.execute(text("SELECT COUNT(*) FROM simulation.football_events WHERE season='CHKMWORK'")).scalar()
        worker_count = len(_run(COMPOSE + ["ps", "-q", "worker"]).stdout.strip().splitlines())
    return {"worker_containers": worker_count, "results": results, "event_count": event_count, "stats": [tuple(r) for r in stats]}


def task_replay_check() -> dict:
    eng = engine()
    result = simulate_gameweek(eng, 1, users=3, season="CHKREPLY", seed=601, accelerated=True)
    score_1 = score_gameweek(eng, "CHKREPLY", 1)
    score_2 = score_gameweek(eng, "CHKREPLY", 1)
    standings_1 = compute_league_standings(eng, "CHKREPLY", 1)
    standings_2 = compute_league_standings(eng, "CHKREPLY", 1)
    refresh_1 = refresh_active_gameweeks.apply_async().get(timeout=60)
    refresh_2 = refresh_active_gameweeks.apply_async().get(timeout=60)
    with eng.connect() as conn:
        scores = conn.execute(text("SELECT COUNT(*), SUM(total_points) FROM gw_scores WHERE season='CHKREPLY'")).first()
        snaps = conn.execute(text("SELECT COUNT(*) FROM leaderboard_snapshots WHERE season='CHKREPLY'")).scalar()
    return {
        "initial": result.score_summary,
        "score_1": score_1,
        "score_2": score_2,
        "standings_1": standings_1,
        "standings_2": standings_2,
        "refresh_1": refresh_1,
        "refresh_2": refresh_2,
        "gw_score_rows": scores[0],
        "total_points_sum": int(scores[1] or 0),
        "snapshot_rows": snaps,
    }


def leaderboard_check() -> dict:
    eng = engine()
    result = simulate_gameweek(eng, 1, users=4, season="CHKLDR", seed=701, accelerated=True)
    standings_retry = compute_league_standings(eng, "CHKLDR", 1)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id, points_this_gw, total_points, rank "
                "FROM leaderboard_snapshots WHERE season='CHKLDR' ORDER BY rank, user_id"
            )
        ).all()
        dupes = conn.execute(
            text(
                "SELECT COUNT(*) FROM (SELECT league_id,user_id,season,gameweek,COUNT(*) c "
                "FROM leaderboard_snapshots WHERE season='CHKLDR' GROUP BY 1,2,3,4 HAVING COUNT(*)>1) d"
            )
        ).scalar()
    return {"simulation": result.standings_summary, "retry": standings_retry, "rows": [tuple(r) for r in rows], "duplicate_snapshot_keys": dupes}


def matchday_check() -> dict:
    eng, fixture_id, players = _fixture_with_players("CHKDAY")
    result = simulate_matchday(
        eng,
        {
            fixture_id: [
                MatchEvent(MatchEventType.KICKOFF, minute=0, provider_event_id="CHK-DAY-KO"),
                Goal(players[0], 7, "CHK-DAY-GOAL"),
                Assist(players[1], 7, "CHK-DAY-AST"),
                MatchEvent(MatchEventType.MATCH_FINISHED, minute=90, provider_event_id="CHK-DAY-FT"),
            ]
        },
    )
    with eng.connect() as conn:
        stats_rows = conn.execute(text("SELECT COUNT(*) FROM ml.player_gw_stats s JOIN ml.players p ON p.id=s.player_id WHERE p.season='CHKDAY'")).scalar()
    return {"result": asdict(result), "stats_rows": stats_rows}


def load_check(max_level: int, exact_level: int | None = None) -> dict:
    eng = engine()
    started = time.perf_counter()
    try:
        if exact_level is None:
            result = run_load_levels(eng, max_level=max_level, season_prefix="LD", seed=801)
            payload = asdict(result)
        else:
            users = LOAD_LEVELS[exact_level]
            run = simulate_gameweek(eng, gameweek_id=exact_level, users=users, season=f"LE{exact_level}", seed=801 + exact_level, accelerated=True)
            payload = {"levels": {exact_level: users}, "metrics": [run.metrics], "score": run.score_summary, "standings": run.standings_summary}
        return {"status": "ok", "elapsed_seconds": time.perf_counter() - started, "result": payload}
    except Exception as exc:
        return {"status": "failed", "elapsed_seconds": time.perf_counter() - started, "error": f"{type(exc).__name__}: {exc}"}


def final_season_check() -> dict:
    eng = engine()
    result = simulate_season(eng, season_id="CHK38", gameweeks=38, users=3, seed=901)
    with eng.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT user_id, season_points, rank FROM league_members "
                "WHERE league_id IN (SELECT id FROM mini_leagues WHERE season='CHK38') ORDER BY rank, user_id"
            )
        ).all()
        score_rows = conn.execute(text("SELECT COUNT(*), MAX(gameweek) FROM gw_scores WHERE season='CHK38'")).first()
    return {
        "gameweeks": len(result.results),
        "last": asdict(result.results[-1]),
        "final_standings": [tuple(r) for r in rows],
        "gw_score_rows": score_rows[0],
        "max_gameweek": score_rows[1],
    }


CHECKS = {
    "worker-restart": worker_restart_check,
    "worker-mid-task-crash": worker_mid_task_crash_check,
    "beat-restart": beat_restart_check,
    "beat-real-tick-restart": beat_real_tick_restart_check,
    "deadline-race": deadline_race_check,
    "api-deadline-race": api_deadline_race_check,
    "event-concurrency": multi_worker_event_concurrency_check,
    "real-multi-worker-event-race": real_multi_worker_event_race_check,
    "task-replay": task_replay_check,
    "leaderboard": leaderboard_check,
    "matchday": matchday_check,
    "season38": final_season_check,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=sorted(set(CHECKS) | {"load"}))
    parser.add_argument("--max-level", type=int, default=1, choices=sorted(LOAD_LEVELS))
    parser.add_argument("--exact-level", type=int, choices=sorted(LOAD_LEVELS))
    args = parser.parse_args()
    output = load_check(args.max_level, args.exact_level) if args.check == "load" else CHECKS[args.check]()
    print(json.dumps(output, default=str, sort_keys=True))


if __name__ == "__main__":
    main()
