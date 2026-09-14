# Fantasy Simulation

This directory contains a deterministic simulation harness for match,
gameweek, and season testing. It is deliberately isolated from the current
application files, but imports production scoring and standings code at
runtime.

## Safety

Simulation commands refuse to run unless:

- `ENVIRONMENT=simulation`
- the database URL contains `fantasy_simulation` or `test`
- the database URL does not look like production

Use `--force` only for local throwaway databases.

## Commands

```powershell
python manage.py simulate_match --match-id 123 --sample-events --force
python manage.py simulate_gameweek --gameweek 5 --users 10000 --seed 123 --force
python manage.py simulate_season --gameweeks 38 --users 10000 --seed 123 --force
python manage.py advance_game_clock --minutes 30 --force
python manage.py generate_test_users --users 10000 --season SIM2026 --seed 123 --force

python -m simulation.cli simulate-gameweek --gameweek 5 --users 10000 --seed 123
python -m simulation.cli simulate-season --gameweeks 38 --users 10000 --seed 123
python -m simulation.cli advance-game-clock --minutes 30
```

## Principle

Only inputs are simulated:

1. fixtures, users, squads, and starting XI rows are seeded deterministically
2. simulated match events write production-shaped `ml.player_gw_stats`
3. production `Results.scoring.score_gameweek` calculates fantasy points
4. production `Results.standings.compute_league_standings` updates tables

Duplicate provider events are skipped inside the simulator before stats are
materialized, and production scoring remains idempotent through its existing
`gw_scores` upsert.

## Celery/Redis Mode

Fast tests can use Celery eager mode through the existing `Worker.tasks`
wrappers. A real worker/Beat/Redis/Postgres topology is described in:

```powershell
docker compose -f simulation/docker-compose.simulation.yml up --build
```

Validate the compose file without starting containers:

```powershell
docker compose -f simulation/docker-compose.simulation.yml config
```

## Verification

```powershell
.\venv\Scripts\python.exe -m pytest simulation\test_simulation_contract.py simulation\test_production_scoring_contract.py simulation\test_simulation_db_integration.py backend\Tests\test_worker.py backend\Tests\test_beat_scheduling.py::test_every_beat_task_name_is_actually_registered
.\venv\Scripts\python.exe -m compileall simulation backend\Worker\tasks.py manage.py
python manage.py simulate_gameweek --gameweek 1 --users 3 --season SIMSMOKE --seed 7 --force
python manage.py simulate_season --gameweeks 38 --users 1 --season SIM38OK --seed 31 --force
```

## Current Limits

- Real crash/restart tests require starting the Docker stack and killing the
  worker process; the repo now provides the container topology but the automated
  kill/restart orchestration is not included.
- Production tables such as `transfers` and `leaderboard_snapshots` are
  intentionally append-only/immutable. Repeated large simulations should use a
  fresh simulation season/database rather than disabling those triggers.
- Large load levels up to 1,000,000 users are exposed by `load-test`; they are
  not run in the default suite because they are environment-capacity tests, not
  fast CI tests.
