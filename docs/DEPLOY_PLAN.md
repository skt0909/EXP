# Deploy plan: automatic scoring on a 1 GB e2-micro

Target: Ubuntu first, then a GCP e2-micro (1 GB RAM, 2 shared vCPU) running
the API, the Celery worker + Beat, Redis and Postgres on one box.

## What happens automatically

- **After every match, both modes:** a Beat job runs every minute and does each
  fixture's checkpoints as they fall due: kickoff+50 min, kickoff+115 min, and
  **final** once FPL marks the match finished. It fetches the gameweek's live
  stats once, writes them to `ml.player_gw_stats`, then rescores:
  - every Quick 11 contest on that fixture, finalizing them at the final
    checkpoint;
  - every user's GW (tactical) score for the gameweek, and the league tables.

  Nothing is booked ahead in Redis. A checkpoint missed while the worker was
  down is done on the next tick.
- **Fixture list:** checked every 15 minutes, but FPL is only called while a
  match is in play, and otherwise once a day. This picks up final scores, the
  finished flag, rescheduled kickoffs and postponements.
- **Credits (prices):** refreshed daily at 01:30 UTC, after FPL's overnight
  price changes. Players new to the game are added too.
- **Chat predictions:** backfilled by a daily systemd timer running as its own
  process. It never runs inside the worker (see Predictions below).

Tactical scoring starts at GW6 for 2026-27 (the ruleset's first gameweek), so
GW1–5 produce no GW-mode scores. That is by design.

## Status

### Done

- **ML pipeline:** commented out, not deleted (`run_ml_pipeline`,
  `schedule_predictions`, the weekly Beat entry). xgboost is no longer loaded by
  the worker or Beat: the worker's import went from 173 to 107 MB.
- **Backfill:** `backend/Predict/backfill_predictions.py` (by hand, or
  `--auto`), plus `deploy/systemd/fpl-backfill.{service,timer}`.
- **Connection pool:** capped at 3 + 2 per process (`DB_POOL_SIZE` and
  `DB_MAX_OVERFLOW` override it).
- **Redis:**
  - Tests use Redis db 1 and rate limits db 2; Celery stays on db 0.
  - The dev Redis is capped at 64 MB, with no eviction.
  - 28 stale queued tasks were purged.
- **Polling by fixture:**
  - The migration `d8c2e5a1f374` is applied to the dev and test databases.
  - New: `poll_due_fixtures`.
  - Removed: booking polls at contest creation, `poll_and_score_dream11`,
    `schedule_fixture_polls` and `poll_and_score_fpl_fixture`.
- **Only the final checkpoint settles stats.** Fulltime at kickoff+115 no longer
  locks numbers that FPL may still correct.
- **The poller writes creativity, influence, threat, ICT and xG.** Until now
  live-polled data never paid out the tactical creativity tier.
- **Double gameweeks:** a player's second fixture gets the gameweek total minus
  what's stored for their first. Once the second match kicks off, the first
  isn't re-polled from the total; its final checkpoint settles what it has.
- **Postponed fixtures:** their kickoff is cleared, and a rescheduled fixture
  moves to its new gameweek.
- **Void rule:** a Quick 11 contest on a postponed fixture, or on one that is
  still unfinished 7 days after kickoff, is voided. Voiding locks it, finalizes
  it, and sets `voided_at` and `void_reason`. The contest API returns
  `void_reason`. A rescheduled match does not revive the contest.
- **Finalizing a contest also locks it**, so a finished contest can't take
  team edits before the 5-minute lock sweep reaches it.
- **Teams lock at kickoff, in the database** (migration `e6a4b9d2c815`).
  `enforce_contest_lock_fn` refuses once the contest is locked *or* its
  fixture has kicked off, and it now also guards edits: `dream11.team_players`
  picks and captaincy, and `dream11.teams` entry names. This closes the
  5-minute window before the lock sweep, and the edit path, which only a
  handler check used to protect. Scoring's `final_points`/`final_minutes`
  writes, and cascade deletes, are unaffected.
- **Quick 11 screens show the real state:** Open → Live (from kickoff) →
  Completed, or Cancelled with "Match postponed/abandoned"
  (`frontend/src/data/contestStatus.js`). Edit, invite and delete controls hide
  from kickoff, and the team picker locks itself at kickoff if it's left open.
- **Daily price refresh:** the `refresh_player_prices` task.
- **Rate limits:** `backend/Shared/rate_limit.py`, backed by Redis. See the
  table below.
- **`/chat`:** messages are capped at 1,000 characters.
- **Deployment files:** all under `deploy/` (see Server setup).
- **GW5 catch-up:** ran through the real worker on dev.
  - GW5 marked finished, and its stats settled (659 rows).
  - GW4 was re-checked with nothing duplicated.
  - Contest 618 finalized.
  - GW6 predictions re-backfilled using the GW5 results (667 players; the 8 new
    ones are tagged New/Insufficient Data).
- **Tests:** 858 pass. The 3 that fail come from uncommitted work that predates
  this plan (see Open gaps).

### Rate limits

| Endpoint | Limit | Protects against |
|---|---|---|
| `POST /auth/login` | 5/min per IP, 10 per 15 min per email | bcrypt CPU; password guessing |
| `POST /auth/register` | 3/hour per IP | bcrypt CPU; fake accounts |
| `POST /auth/forgot-password` | 3/hour per IP and per email | reset-email spam |
| `POST /auth/reset-password` | 5 per 15 min per IP | reset-token guessing |
| `POST /chat` | 10/min and 100/day per user | Groq quota |
| `POST /dream11/contests`, `/join` | 10/min per user | contest spam |
| Any other write | 30/min per user | write floods |
| All `/api/` (nginx) | 10/s per IP, bursts up to 20 | general floods |

The limits key on the user when the request carries a valid token, and on the
IP otherwise. If the Redis counter store is down, requests are let through
rather than blocked.

## Decisions on the gaps

1. **GW5:** it catches up automatically once the fixtures refresh. This has now
   run.
2. **Double gameweeks:** the subtract-the-earlier-match fix is applied. FPL's
   per-fixture `explain` block can't be used instead: it omits zero-point stats
   and has no creativity.
3. **Postponed matches:** handled by the void rule above.
4. **Ownership (`selected`)** isn't in the live feed. It stays NULL for
   live-polled rows, and the model treats it as 0. Accepted.
5. **New mid-season players** have no prediction until the next backfill.
   Accepted.
6. **Monitoring:** later. `/health/scheduled-tasks` already reports stale
   tasks; point an external uptime check (for example UptimeRobot) at it.

## Server setup, in order

Everything assumes the app lives at `/opt/fpl`, with its venv at
`/opt/fpl/venv` and a system user `fpl`. Change the paths in `deploy/` if your
layout differs.

1. **Swap:** `sudo sh deploy/scripts/setup_swap.sh` (2 GB, swappiness 10).
2. **Postgres 18** from apt.postgresql.org, to match dev's 18.4:
   - Copy `deploy/postgres/fpl-tuning.conf` into `conf.d/`.
   - Restore a `pg_dump` of the dev database, without the ~2,026 test users
     or the `simulation` schema.
   - Then run `alembic upgrade heads`.
3. **Redis** from apt: append `deploy/redis/fpl.conf` to
   `/etc/redis/redis.conf`.
4. **App:**
   - Copy the code, create the venv and `pip install -r requirements.txt`.
   - Write `.env` from `.env.example` with a new `JWT_SECRET`, the real
     `ALLOWED_ORIGINS` and `RATE_LIMIT_STORAGE_URL=redis://localhost:6379/2`.
5. **Frontend:** run `npm run build` on your PC (not on the 1 GB box) and copy
   `frontend/dist` to `/opt/fpl/frontend/dist`.
6. **nginx:** install `deploy/nginx/fpl.conf`, then `sudo certbot --nginx`.
7. **systemd:** copy `deploy/systemd/*` to `/etc/systemd/system/`, then:
   - `sudo systemctl enable --now fpl-api fpl-worker fpl-backfill.timer`
8. **journald:** install `deploy/journald/fpl.conf`, which caps logs at 200 MB.
9. **Firewall:** allow only 22, 80 and 443 (GCP firewall and/or ufw). Use SSH
   keys only.
10. **Backups:** run `deploy/scripts/backup_db.sh gs://bucket/prefix` from the
    `fpl` user's crontab, and try one restore.
11. **Checks before relying on it:**
    - `curl -A "Mozilla/5.0" https://fantasy.premierleague.com/api/bootstrap-static/`
      from the server, since FPL blocks some cloud IPs.
    - Run the full test suite on the server's Python version.
    - Watch `free -m` and `journalctl -u fpl-worker -f` through a match window.

## Predictions: backfill, not a pipeline

`backend/Predict/backfill_predictions.py` runs the pipeline's steps and does the
same delete-then-insert write.

- `--season S --gw N` is a dry run by hand; add `--write` to save.
- `--auto` picks the next gameweek with no predictions and writes it, but only
  once the gameweek before is complete: every fixture finished, stats present,
  and none still live.
- `fpl-backfill.timer` runs `--auto` daily at 05:30 UTC as a separate process
  capped at 450 MB. A run takes about 7 s and peaks at about 270 MB, and all of
  it is released when the process exits.

`/chat` reads each player's latest backfilled tier, so a gameweek that isn't
backfilled yet falls back to the previous one.

## Expected memory on the e2-micro

| Process | RAM |
|---|---|
| OS + agents | ~150–200 MB |
| API (1 uvicorn process) | ~110–150 MB |
| Worker + embedded Beat (solo pool) | ~130–150 MB |
| Postgres (tuned) | ~80–120 MB |
| Redis | ~5–10 MB |
| nginx | ~10 MB |
| **Total** | **~500–650 MB of ~970 MB** |

Brief peaks: the backfill adds ~270 MB for about 7 s a day at most, and the
price refresh adds ~30–50 MB.

## Open gaps

- **Monitoring and alerts:** deferred, see above.
- **The FPL feed:** a 503 while FPL is updating is retried on the next tick for
  polls and fixture refreshes. Nothing yet backs off harder if FPL blocks the
  server's IP.
- **Tests failing before this work** (not caused by it):
  - `test_auth.py::test_registered_team_name_is_visible_to_team_dashboard` is
    missing an import of `TEST_SEASON`.
  - The `test_auth_enforcement.py` route table doesn't list the new saved-teams
    and `/transfers/history` routes.
- **Two Alembic heads:** `a3d7f92c1e60` (saved teams / entry name, uncommitted)
  and `d8c2e5a1f374`. Merge them, or at least use `alembic upgrade heads`
  (plural) on the server.
- **Uncommitted working tree:** this work is mixed in with earlier uncommitted
  edits and deletions. Commit it before copying the code to the server.
- **Windows dev:** Celery refuses `-B` there, so run `worker` and `beat` as two
  processes (see `backend/Worker/celery_app.py`).
