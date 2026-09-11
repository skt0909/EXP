$ErrorActionPreference = "Stop"

$env:ENVIRONMENT = "simulation"
$env:DATABASE_URL = "postgresql://postgres:postgres@localhost:55432/fantasy_simulation"
$env:REDIS_URL = "redis://localhost:56379/0"
$env:JWT_SECRET = "simulation-secret"

docker compose -f simulation\docker-compose.simulation.yml up -d --build
.\venv\Scripts\python.exe -m alembic upgrade heads

$checks = @(
  "worker-mid-task-crash",
  "beat-real-tick-restart",
  "api-deadline-race",
  "real-multi-worker-event-race",
  "task-replay",
  "leaderboard",
  "matchday",
  "season38"
)

foreach ($check in $checks) {
  .\venv\Scripts\python.exe -m simulation.real_stack_checks $check
}

.\venv\Scripts\python.exe -m simulation.real_stack_checks load --exact-level 1
.\venv\Scripts\python.exe -m simulation.real_stack_checks load --exact-level 2
