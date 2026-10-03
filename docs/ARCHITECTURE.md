Last verified against the live codebase: 27 September 2026 (migrations, polling and scoring snapshots updated 3 October 2026). Re-verify significant claims (route counts, row counts, trigger list) before trusting this for anything beyond orientation, since the codebase will have changed since this was written.

*Update, 3 October 2026: live checkpoints now also depend on the real match state (`started`, `finished_provisional`, `minutes` on `ml.fixtures`, persisted by `ingest_upcoming_fixtures`), so checkpoints follow the real match rather than kickoff + N minutes (halftime is disabled, fulltime waits for `finished_provisional`). Scoring breakdowns are frozen at settle time (`team_players.final_breakdown` for Quick 11; `gw_scores.player_breakdowns` for the tactical game, via `Results/tactical_breakdowns.py`) so finished results no longer change with today's constants. Production frontend releases are documented in `deploy/PRODUCTION.md`. Counts (902 tests, 31 migrations) re-checked; the table and route counts above were not re-derived.*

*Update, 27 September 2026: sections 02, 03, 04, 07 and 08 and the counts below were re-verified and rewritten for the fixture-driven live polling (`poll_due_fixtures`), Quick 11 changes (editing, saved teams, lock at kickoff, voiding), rate limiting, the ML pipeline replaced by backfilling, and the VM deployment files in `deploy/`. See `docs/DEPLOY_PLAN.md` for the deployment side.*

*Update, 4 September 2026: the "CORS is fully open" and "Nothing is supervised" rows in Known Gaps (08) were revised to reflect work completed since the snapshot above.*

*Update, 22 September 2026: the classic FPL game was converted IN PLACE into a "tactical" mode — captain, vice-captain, chips (Wildcard/Free Hit/Bench Boost/Triple Captain), transfer hits and FPL bonus points are gone; the game is now GK+XI formation, a per-gameweek tactic (`attack`/`defence`/`balanced`), exactly 2 Bonus Players, 4 fixed bench roles, and up to 2 planned Tactical Subs. This is a full rewrite of every section that described the old rules, not an addendum — see `IMPLEMENTATION_PLAN.md` for the complete rulebook and migration history. Dream11 (section 06's right-hand column) is untouched throughout.*

---

# How Pitchside fits together

System reference · read from the database and the source, 27 Sep 2026

Two fantasy games sharing one FastAPI backend, one Postgres instance and one Celery worker: what crosses the wire, where every row lives, which process wakes up when, and how the gameweek engine and the scoring engine divide the work. In the app the two games are called **Tactic mode** (below, "tactical", formerly classic FPL) and **Quick 11 mode** (in code and tables, "Dream11").

**48** backend modules · **44** HTTP routes · **3** Postgres schemas, **35** tables · **16** enforcement triggers · **8** scheduled tasks · **902** tests

---

## 01 — The gateway: one function every request goes through

The browser never talks to the backend directly. Thirteen small API modules — `squad.js`, `transfers.js`, `dream11.js` and so on — all call a single `request()` in `frontend/src/api/client.js`, and that function is the entire gateway. It is deliberately the only place that knows the token exists.

**Request flow:**

1. **browser** — Page component. Calls a typed helper, e.g. `fetchContest({contest_id})`.
2. **api/client.js** — `request()`. Reads the JWT from `localStorage`, attaches `Authorization: Bearer`, sets JSON headers.
3. **FastAPI** — CORS → router. Security dependency resolves *before* body validation, so a bad token is 401 whatever the payload.
4. **Endpoint** (Gameplay / Results / Game_logic) — Pydantic model in, Pydantic model out. Identity always from `current_user.id`.
5. **SQLAlchemy Core** — Postgres. Hand-written `text()` SQL with bound parameters. No ORM models anywhere.

Because `request()` is the only exit, a new endpoint client physically cannot forget to authenticate — and the response side gets the same treatment. HTTP status codes are translated once, into error classes the UI can branch on:

| Status | Error class | What the UI does with it |
|---|---|---|
| 401 | `AuthError` | A registered handler drops the session — a token the server rejects is not worth keeping. |
| 422 | `ValidationError` | Renders *every* failure at once: the backend collects them into a `detail: [...]` list rather than stopping at the first. |
| 422 | `LockedError` | A 422 whose message matches `/lock\|deadline\|has already started/`. Shown as a locked state, not an error toast — the deadline passing is a game state, not a fault. |
| 403 | `ForbiddenError` | Authenticated but refused. Dream11 hides rival XIs until kickoff; the server's own sentence is shown verbatim. |
| 404 | `NotFoundError` | Often expected, not broken — a contest member who simply never picked a team. |

### Identity

Auth is a 12-hour HS256 JWT signed with `JWT_SECRET` from `.env` — no default value, so a missing secret raises rather than silently signing with a guessable constant. `get_current_user` decodes the token and then **re-reads the users row on every request**, so a credential can't outlive the account it names.

The two game modes enforce it differently, and the difference is the point:

- **Classic FPL** declares `Depends(get_current_user)` per endpoint.
- **Dream11** declares it once on the `APIRouter` itself, so a route added later is authenticated by construction. That router previously authenticated one of nine endpoints and trusted a caller-supplied `user_id` everywhere else.

One endpoint still takes a `user_id` parameter — `GET /dream11/contests/{id}/team` — because there it names *whose* team to fetch rather than who is asking, and it is checked against `current_user.id` behind an explicit visibility rule.

### Rate limits

`Shared/rate_limit.py` adds a middleware in front of every router, registered *inside* CORS so a 429 still carries CORS headers. Limits key on the user when the bearer token verifies, on the client IP otherwise: login 5/min per IP plus 10 per 15 min per email (checked inside the handler, before any bcrypt work), register 3/hour, forgot-password 3/hour per IP and per email, reset-password 5 per 15 min, `/chat` 10/min and 100/day, contest create/join 10/min, every other write 30/min. Counters live in Redis db 2 (`RATE_LIMIT_STORAGE_URL`), falling back to in-process memory; a counter-store failure lets the request through rather than taking logins down. `RATE_LIMIT_ENABLED=0` turns it off, which the test suite does.

CORS reads an explicit `ALLOWED_ORIGINS` list from the environment and raises if it's unset (see section 08).

---

## 02 — The API surface

Twelve routers mounted onto one FastAPI app in `Context_assembler/main.py`, plus three routes defined in `main.py` itself (`/chat` and the two health checks). Forty-four routes, of which eight answer without a credential: register, login, forgot-password, reset-password, `/players`, `/scoring-rules`, `/health` and `/health/scheduled-tasks`.

**Identity & reference — Data/**

| Route | Method | Purpose |
|---|---|---|
| `/auth/register` · `/auth/login` | POST | bcrypt hash, returns a token. Public by necessity, and rate limited (section 01). |
| `/auth/forgot-password` · `/auth/reset-password` | POST | Emails a 30-minute reset link over SMTP (`Shared/mailer.py`; with no `SMTP_HOST` the link is only logged), then sets the new password. Public. |
| `/auth/me` | GET DELETE | Who the bearer token names; `DELETE` removes the account. |
| `/players` | GET | Season player catalogue. Public by decision — identical for everyone. |
| `/fixtures` | GET | Fixtures with derived status and the caller's contest performance. |

**Health — Context_assembler/**

| Route | Method | Purpose |
|---|---|---|
| `/health` | GET | Database reachability. Public. |
| `/health/scheduled-tasks` | GET | Per Beat task: last success/failure from `task_heartbeats`, and a staleness verdict against its configured interval (none for a crontab entry). Public. |

**Squad & matchday — Gameplay/**

| Route | Method | Purpose |
|---|---|---|
| `/squad` · `/squad/select` | GET POST | The 15-man squad: 2 GK, 5 DEF, 5 MID, 3 FWD, £100.0m, max 3 per club. |
| `/gw_selection` | GET POST | Starting XI, bench roles (12 backup GK, 13 Auto Sub, 14–15 Tactical Subs), `tactic`, exactly 2 `bonus_player_ids`, up to 2 `swaps`. No captain, vice-captain or chip fields — those columns no longer exist. |
| `/transfers` · `/transfers/used` · `/transfers/history` | POST GET GET | Batch swaps, free-transfer allowance (cap 2, no paid transfers — overspend is rejected, not charged), and the manager's past transfers. |
| `/transfer-drafts` · `/transfer-drafts/{id}` | GET PUT · DELETE | A staged cart — planned swaps that haven't been committed. |
| `/gameweeks/current` (`Game_logic/fixtures.py`) | GET | Which gameweek every other page should default to — see section 05's "current gameweek" note. |

**Results — Results/**

| Route | Method | Purpose |
|---|---|---|
| `/team` | GET | Dashboard: lineup, `tactic`, per-player `role`/`is_bonus`, `general_points`/`tactical_points`/`sub_bonus`, swap detail, `is_locked`, `provisional`, `scored`. |
| `/scoring-rules` (`Data/scoring_rules.py`) | GET | The tactic tier tables (tier thresholds, points) and general-points table, read by the frontend rules page rather than hardcoded there. |
| `/leagues` · `/leagues/join` | GET POST | Mini-leagues, classic or head-to-head. |
| `/leagues/{id}/table` · `/h2h` | GET | Standings, and one gameweek's H2H matchups. |

**Dream11 — Game_logic/ · router-level auth**

| Route | Method | Purpose |
|---|---|---|
| `/dream11/contests` | GET POST | Your contests; create one on a fixture (prices frozen at creation). Creation books nothing in Celery — see section 04. |
| `/dream11/contests/join` | POST | Join by 7-character code. |
| `/dream11/contests/{id}` | GET DELETE | Detail, with the caller's membership flags plus `is_finalized` and `void_reason`; the creator can delete an unlocked contest. |
| `/dream11/contests/{id}/players` | GET | The priced pool — identical for every caller. |
| `/dream11/contests/{id}/leaderboard` | GET | Every member's standing. |
| `/dream11/contests/{id}/team` | GET POST PATCH | Submit 11 picks; replace them (`PATCH`) until kickoff; read a team (yours always, a rival's only after lock). |
| `/dream11/fixtures/{id}/contests` · `/dream11/fixtures/{id}/players` | GET | Your contests on one match; that match's player pool, for building a team before any contest exists. |
| `/dream11/saved-teams` · `/dream11/saved-teams/{id}` | GET POST · DELETE | Reusable lineups scoped to a fixture, validated with the same rules as a submission. Never scored or locked. |

**Advice — Context_assembler/**

| Route | Method | Purpose |
|---|---|---|
| `/chat` | POST | Assembles squad + backfilled ML tiers into a prompt, calls Groq. Message capped at 1,000 characters; replies are rewritten to say "Tactic mode" / "Quick 11 mode", never "FPL" or "Dream11". |

---

## 03 — How the data is stored

One Postgres instance, three schemas, and the split is by *ownership* rather than by feature. Row counts below are live from the dev database (approximate, from Postgres statistics). The dev database also has a fourth, `simulation` schema (one table) used only by the `backend/Simulation/` harness; it is not part of the game and should not be copied to a server.

| Schema | Holds | Notable tables | Rows |
|---|---|---|---:|
| `ml` (8 tables) | Everything ingested from outside, plus model output. Nothing a user writes. | `players` · `teams` · `fixtures` · `player_gw_stats` · `ml_predictions` · `fixture_poll_schedule` · `player_gw_features` · `season_stats` | ~13,900 |
| `public` (20 tables, including `alembic_version`) | The tactical game: who plays, what they picked, what they scored. **`chips` and `free_hit_squads` are gone** — dropped, not just emptied. | `users` · `user_squads` · `squad_players` · `gw_selections` · `starting_xi` · `tactical_swaps` · `transfers` · `cancelled_transfers` · `gw_scores` · `ruleset_epochs` · `gameweeks` · `user_gameweek_finance` · `mini_leagues` · `league_members` · `league_h2h_fixtures` · `leaderboard_snapshots` · `transfer_drafts` · `task_heartbeats` · `password_reset_tokens` | ~14,300 |
| `dream11` (7 tables) | The contest game, self-contained. `saved_teams` / `saved_team_players` hold reusable fixture-scoped lineups. | `contests` · `contest_members` · `player_prices` · `teams` · `team_players` · `saved_teams` · `saved_team_players` | ~850 |

`ml.fixture_poll_schedule` records when each fixture's three live-poll checkpoints actually ran (`halftime_polled_at`, `fulltime_polled_at`, `final_polled_at`); a NULL means not done yet. Its older `halftime_scheduled`/`fulltime_scheduled` booleans belong to the ETA-booking design it replaced and are no longer read. `dream11.contests` gained `voided_at`/`void_reason` for contests whose match was postponed or abandoned.

### Two meanings of "player id", and the one query that converts

This is the single most load-bearing convention in the schema, and the easiest thing to get silently wrong.

- **Classic FPL tables store the raw `fpl_id`**, never translated: `squad_players.player_id`, `starting_xi.player_id`, `transfers.player_in_id`.
- **Dream11 tables store `ml.players.id`**, the internal serial — and so does `ml.player_gw_stats.player_id`, which is why Dream11's scoring joins line up without translation.

Exactly one query crosses between them: `PLAYERS_LOOKUP_QUERY`, which selects `p.id AS internal_id` keyed by `p.fpl_id`. Everything a client sends or receives is in fpl-id space; everything that reaches Dream11 storage is in internal-id space. The translation is only well-defined *within a season* — `ml.players` holds one row per `(fpl_id, season)`, so the same footballer has a different internal id each year.

Unifying the two is an open decision, not an oversight: it would mean migrating live data on one side or the other.

### Money, and other scales

Prices are held in **tenths of £m** as integers throughout — `BUDGET_CAP = 1000` is £100.0m. That is what makes the selling-price rule exact rather than approximate: half a price rise, rounded down to the nearest £0.1m, is literally `profit // 2`. Dream11 doesn't use money at all — it uses *credits*, floats in `[6.0, 11.0]` rounded to the nearest 0.5, against a cap of 100.

### Sixteen triggers doing the work application code shouldn't be trusted with

The pattern throughout is belt-and-braces: application code checks, and the database refuses. Every one of these exists because the check alone was judged insufficient. `enforce_chip_limit` (on the now-dropped `chips` table) is gone; five tactical triggers replaced it, doing more than it ever did.

| Table | Trigger | Fires | Prevents |
|---|---|---|---|
| `ml.player_gw_stats` | `enforce_gw_stats_immutability` | BEFORE UPDATE | Changing a settled gameweek's stats (`is_live = FALSE`). |
| `ml.players` | `enforce_player_identity` | BEFORE UPDATE | A player's identity columns moving under existing picks. |
| `ml.fixtures` | `enforce_fixture_identity` | BEFORE UPDATE | Re-pointing a fixture at different clubs. Scores and `finished` stay updatable. |
| `ml.ml_predictions` | `enforce_ml_prediction_immutability` | BEFORE UPDATE | Rewriting a prediction after the fact. |
| `public.gw_selections` | `enforce_selection_lock` | BEFORE UPDATE | Editing a lineup after the deadline. |
| `public.starting_xi` | `enforce_bonus_count` | AFTER INSERT/UPDATE/DELETE, DEFERRED to COMMIT | Committing an XI without **exactly 2** Bonus Players. Deferred so the app can delete-and-reinsert the whole XI in one transaction. |
| `public.starting_xi` | `enforce_starting_xi_lock` | BEFORE INSERT/UPDATE/DELETE | Changing a starter or bench slot once the parent selection is locked. |
| `public.tactical_swaps` | `enforce_swap_refs` | AFTER INSERT/UPDATE, DEFERRED | An outgoing player who isn't a non-Bonus starter, or an incoming player who isn't a Tactical Sub (slot 14/15) on the same selection. |
| `public.tactical_swaps` | `enforce_tactical_swaps_lock` | BEFORE INSERT/UPDATE/DELETE | Changing a planned swap once the selection is locked. |
| `public.ruleset_epochs` | `enforce_ruleset_epochs_immutability` | BEFORE UPDATE/DELETE | Moving or deleting the stored anchor for the free-transfer recurrence. Append-only, no FKs — nothing can cascade into it. |
| `public.transfers` | `enforce_transfers_immutability` | BEFORE UPDATE/DELETE | Un-making a transfer. Cancellation is a separate append-only table. |
| `public.leaderboard_snapshots` | `enforce_snapshot_immutability` | BEFORE UPDATE/DELETE | Editing frozen league history. Append-only. |
| `dream11.player_prices` | `enforce_price_immutability` | BEFORE UPDATE | Any price change after contest creation. |
| `dream11.teams` | `enforce_contest_lock` | BEFORE INSERT, UPDATE OF `contest_id`/`user_id`/`entry_name` | Submitting or renaming a team once the contest is locked **or its fixture has kicked off** — the kickoff check closes the up-to-5-minute gap before the lock sweep sets `is_locked`. |
| `dream11.team_players` | `enforce_contest_lock` | BEFORE INSERT, UPDATE OF `team_id`/`player_id`/`is_captain`/`is_vice_captain` | Editing picks or armbands after kickoff (the edit path replaces these rows). Scoring's `final_points`/`final_minutes` writes and cascade deletes are deliberately not covered. |
| `dream11.contest_members` | `enforce_contest_result_immutability` | BEFORE UPDATE | Moving points or rank on a finalized contest. |

Schema changes go through Alembic — 31 hand-authored revisions (including merge revisions), none autogenerated, because there are no ORM models to diff against. The chain currently ends at a single head, `c4d9a2e7f1b6` ("add scoring breakdown snapshots"), which follows `b7e3f1a9c2d4` ("add fixture match state") on top of `7c098b3189de` ("merge deploy heads"). The latest schema changes are `b7e3f1a9c2d4` (`ml.fixtures.started` / `finished_provisional` / `minutes`, the real match state from FPL's fixtures feed) and `c4d9a2e7f1b6` (nullable `dream11.team_players.final_breakdown` and `public.gw_scores.player_breakdowns`, frozen per-player scoring breakdowns; older rows stay NULL and readers fall back), before them `d8c2e5a1f374` (checkpoint `*_polled_at` columns, contest voiding) and `e6a4b9d2c815` (lock Quick 11 teams at kickoff, for edits too), which runs after the saved-teams / `entry_name` revisions. On a server, use `alembic upgrade heads`.

---

## 04 — The services, and which one wakes up when

In development each process is started by hand. On the Linux server (a 1 GB VM) they run under systemd from the files in `deploy/`, behind nginx — see `docs/DEPLOY_PLAN.md`.

| Process | Kind | Notes |
|---|---|---|
| **Vite dev server** (dev) / **nginx** (server) | sync | React 18 + React Router. In dev, Vite serves the SPA and proxies `/api/*` to uvicorn, stripping the prefix. On the server, nginx serves the built `frontend/dist`, proxies `/api/` the same way, caps request bodies at 64 KB and limits each IP to 10 requests/s. |
| **uvicorn** (`uvicorn main:app`) | sync | The whole HTTP surface. Handlers are plain `def`, not `async def` — they block on the DB and FastAPI runs them in a threadpool. On the server: one process, `--proxy-headers` (real client IPs for rate limiting), `--limit-concurrency 20`. |
| **Postgres** | store | One instance, three schemas. A separate `fpl_game_test` database backs the test suite; `TEST_DATABASE_URL` always wins over `DATABASE_URL` so a test run can never touch dev data. |
| **Redis** | async | Celery broker *and* result backend (db 0), plus rate-limit counters (db 2). Tests use db 1. Capped at 64 MB with `noeviction` and no persistence — nothing in it needs to survive a restart, because live-poll progress is tracked in Postgres. |
| **Celery worker** | async | `--pool=solo`: one process, one task at a time. On Linux it runs with Beat embedded (`worker -B --pool=solo`, `deploy/systemd/fpl-worker.service`); only one worker may ever have `-B`. Results expire after an hour. |
| **Celery Beat** | async | Only schedules. On Windows, Celery refuses `-B`, so dev runs `celery -A Worker.celery_app beat` as a separate process. Worker with no Beat means nothing fires on a timer; Beat with no worker means tasks queue and never run. |
| **Prediction backfill** (server) | async | Not a Celery task. `deploy/systemd/fpl-backfill.timer` runs `Predict/backfill_predictions.py --auto` daily at 05:30 UTC as its own short-lived process, so xgboost's ~270 MB peak is released when it exits — see section 07. |

*Legend: sync = request path · async = scheduled / background · store = persistence*

### Configuration resolves the same way everywhere

Every service finds its own config by walking up from its file to the nearest `.env`, preferring a `TEST_`-prefixed variable, and **raising if neither exists** rather than falling back to a localhost default. That last part is deliberate: Redis was once hardcoded to `redis://localhost:6379/0`, which meant a misconfigured deploy failed opaquely instead of saying so.

`get_engine()` is `lru_cache`'d, so each process holds exactly one connection pool. It used to exist as five identical copies in five packages; a worker that imported two of them held two independent pools. The pool is small on purpose — 3 connections plus 2 overflow per process (`DB_POOL_SIZE` / `DB_MAX_OVERFLOW` override it) — so the API and worker together stay inside a 1 GB server's `max_connections = 20`.

### What Beat runs

Every scheduled function is re-exported through `Worker/beat_registry.py` — an index with no logic in it, so "what does Beat call, and who owns it?" has one answer. The retry/logging wrappers live in `Worker/tasks.py`; the behaviour lives in each function's own module.

| Task | Every | Owner module | What it does |
|---|---|---|---|
| `poll_due_fixtures` | 1 min | `Data/live_poll` | Live polling for **both** modes — see below. |
| `lock_expired_gameweeks` | 5 min | `GameEngine/gameweek_lock` | Flips `gw_selections.is_locked` once the deadline passes. |
| `carry_forward_selections` | 5 min | `GameEngine/selection_carry_forward` | Copies a manager's last lineup into the next gameweek before they've touched Starting XI, so the dashboard's pitch view is populated. |
| `lock_dream11_contests` | 5 min | `Game_logic/dream11_locking` | Sets each contest's `is_locked` at *its own* fixture's kickoff. (Team edits are already refused from kickoff by the trigger in section 03; this flag is what the rest of the app reads.) |
| `refresh_active_gameweeks` | 15 min | `GameEngine/gameweek_finalize` | Scores every gameweek inside the 5-day active window (via the tactical engine), then recomputes standings. |
| `refresh_fixtures` | 15 min tick | `Data/fpl_ingest` | Re-pulls the season's fixture list — scores, kickoff changes, postponements (kickoff cleared), rescheduled gameweeks and `finished`. Calls FPL only while a fixture has kicked off and isn't finished, or once a day (`live_poll.fixtures_refresh_reason`). |
| `finalize_dream11_contests` | 15 min | `Game_logic/dream11_scoring` | Voids contests on postponed or abandoned fixtures, then freezes any finished contest the `final` checkpoint left open (e.g. a team that failed to score). |
| `refresh_player_prices` | daily 01:30 UTC | `Data/fpl_ingest` | `bootstrap-static`: every player's `now_cost` (GW mode's buy/sell price) and players new to the game. |

`revert_free_hits` went with the Free Hit chip. `schedule_fixture_polls` and `schedule_predictions` are gone too: the first was replaced by `poll_due_fixtures`, and the second by backfilling predictions outside Celery (section 07). The `run_ml_pipeline` / `schedule_predictions` task code is commented out in `Worker/tasks.py`, not deleted.

### Live polling: found in the database, not booked in Redis

`poll_due_fixtures` asks Postgres every minute which fixtures have a checkpoint due (`live_poll.find_due_checkpoints`), within 14 days of kickoff:

| Checkpoint | Due | Writes |
|---|---|---|
| `halftime` | **disabled** — no stored signal proves halftime was reached; the query carries the commented-out condition (`started` and `minutes >= 45`) to enable once fixture `minutes` is verified live | — |
| `fulltime` | once FPL reports `finished_provisional` (the final whistle) and the fixture isn't yet `finished` | `is_live = TRUE` |
| `final` | once `fixtures.finished` is TRUE | `is_live = FALSE` — the only checkpoint that settles |

It takes at most one checkpoint per fixture per tick (a finished fixture goes straight to `final`, skipping `fulltime`). It fetches each gameweek's `event/{gw}/live/` payload **once** for all its due fixtures, writes each fixture's stats, rescores the Quick 11 contests on it (`final` finalizes them), rescores that gameweek's tactical scores and league tables once, and only then records each checkpoint in `fixture_poll_schedule`. A fixture that fails is left due and retried on the next tick. With no match on, a tick is one small query and no API call.

This replaced ETA tasks booked in Redis — two per contest at creation (`poll_and_score_dream11`) and two per fixture (`schedule_fixture_polls` → `poll_and_score_fpl_fixture`). A Redis restart silently lost those bookings, a contest created while Redis was down got none, and an ETA further out than Redis's 1-hour `visibility_timeout` was redelivered over and over. Contest creation no longer touches Celery at all. `finalize_dream11_contests` stays as the sweep behind the `final` checkpoint.

---

## 05 — Game Engine × Score Engine

The split is clean once you see it: **GameEngine decides *when*, Results decides *how much*.** Neither imports the other's concerns, and both read their constants from `Shared/rules.py`, which imports nothing at all.

**Game Engine** — `backend/GameEngine/` — time and state transitions

- **gameweek_lock** — deadline is `MIN(kickoff_time) − 90 min`, *derived, never stored*. Asking twice can legitimately differ if fixtures are re-ingested.
- **gameweek_finalize** — decides which gameweeks are "active" and calls the scorer. Active is a **5-day window from first kickoff**, not a flag. Also writes `gameweeks.scored_at` once a gameweek's batch completes *and* every fixture in it is `finished` — the one state-based ("this will not change again") signal in the schema; everything else here is inferred from elapsed time or match state.
- **free_hit_revert — deleted.** The Free Hit chip it served no longer exists.

**Score Engine** — `backend/Results/` — arithmetic over frozen selections

- **tactical_scoring** — pure functions: General Points, Tactical Points (Bonus Players only, per the active tactic), Sub Bonus (executed Tactical Swaps). No captaincy, no chip effects, no hits — `total_points == final_points` always, by rule.
- **scoring_job** — `gw_selections → gw_scores`, batched (`SCORING_BATCH_SIZE = 200`), season-filtered (`Shared/seasons.py`, real seasons only — `SIM*`/sentinel seasons are never scored), epoch-gated (nothing scores before its ruleset's `first_gameweek`). Replaces the deleted `Results/scoring.py`.
- **standings** — `gw_scores → league_members + leaderboard_snapshots`. Classic and H2H from separate sources. Unchanged by the tactical conversion.
- **team_dashboard / leagues** — read models over the same rows. `team_dashboard` additionally computes each manager's autosubs and swaps live, at request time, rather than storing them.

### The handshake

Every 15 minutes, `refresh_active_gameweeks` asks one question — which gameweeks started within the last five days? — and for each one runs **score, then standings, in that order, never parallel**, because `compute_league_standings` reads the `gw_scores` rows `score_gameweek_tactical` has just written.

Both sides isolate failures per item: one user who fails to score doesn't abort the batch, and one broken league doesn't stop the others — and if a *batch* write itself fails, scoring falls back to writing that batch's managers one at a time (a per-manager SAVEPOINT each) rather than losing all of them. Failures are collected and returned, not raised.

Re-running is expected, not exceptional. `score_gameweek_tactical` upserts on `(user_id, season, gameweek)` and recomputes `season_total` fresh from stored prior rows — bounded below by the ruleset's epoch, so a season that changed rules mid-way never sums across two rule generations — rather than trusting a running total. As more match data arrives mid-gameweek, rescoring converges instead of accumulating.

> **Why "active" is a window and not a flag.** Neither `fixtures.finished` nor `player_gw_stats.is_live` is a usable recency signal for "is this gameweek still moving": the bulk ingest path writes a whole gameweek's rows in one atomic batch, and although live polling (section 04) now flips them match by match, a gameweek can still be rescored after its last `final` checkpoint if FPL corrects data. A real match window (Friday through Monday) plus FPL's 24–48h bonus-point confirmation lag fits comfortably inside five days. **That window is also what freezes history** — once a gameweek falls out of it, nothing revisits the row, `gameweeks.scored_at` is set (if every fixture finished), and `gw_scores.rules_version` records which generation of the rules produced it.

> **The "current gameweek" is deliberately not "the one with the soonest deadline."** `GET /gameweeks/current` (`Game_logic/fixtures.py`) answers "the earliest gameweek in the latest real season that is not yet scored" (`gameweeks.scored_at IS NULL`), falling back to deadline-ranking only when nothing matches (season not started, or every gameweek scored). The earlier, deadline-only rule flipped to the *next* gameweek the instant a deadline passed — while the one just played was still live and its score still moving. The season filter (`Shared/seasons.py`, `^20[0-9]{2}-[0-9]{2}$`) exists because the naive `^[0-9]{4}-[0-9]{2}$` pattern let the harness's `SIM*` seasons, and two literal sentinel rows (`9998-00`, `9999-00`) already in the dev database, win outright.

### The race that exists and currently costs nothing

Locking and scoring run on different intervals and can overlap. The analysis is written into both modules' source rather than left implicit, and it concludes the race is harmless — but by two accidents of the current design rather than by an invariant: the scoring query doesn't filter on `is_locked` at all, and the endpoints that must refuse late edits gate on the deadline instead. The 90-minute deadline offset also gives locking a head start over scoring, which is why changing that number invalidates the analysis.

---

## 06 — The game logic itself

Two rulebooks, deliberately not merged. `Shared/rules.py` holds the tactical game's constants as pure data with a zero-import rule; Dream11's live in `dream11_scoring.py` because it is a different game, not a variant.

| Rule | Tactical (formerly classic FPL) | Dream11 |
|---|---|---|
| Scope | A whole gameweek, all 10 matches | **One fixture** |
| Squad | 15 (2/5/5/3), pick an XI + 4 bench | 11 picked directly — no squad, no bench, no transfers |
| Budget | £100.0m, max 3 per club | 100 credits, max 7 per club |
| Formation | 1 GK, **≥3 DEF, ≥3 MID** (floor moved 2→3, removing exactly one shape: 5-2-3), ≥1 FWD; XI must stay legal after autosubs | 1 GK, 3–5 DEF, 3–5 MID, 1–3 FWD → 7 legal shapes |
| Goal points | GK 10 · DEF 6 · MID 5 · FWD 4 | GK 10 · DEF 6 · MID 5 · FWD 4 |
| Assist | 3 | 20 |
| Clean sheet | 60-minute threshold | **54-minute threshold**, recomputed from minutes + goals conceded, never read from the precomputed column |
| Captain / chips | **Gone.** No captain, vice-captain, or chips (Wildcard/Free Hit/Bench Boost/Triple Captain) — see the tactic/Bonus Player rules below instead | 2× captain *and* 1.5× vice, both unconditional and simultaneous, applied as additive bonuses |
| Transfer hits | **Gone.** No paid transfers — a transfer beyond the bank is rejected outright, not charged | n/a (no transfers) |
| Result | Frozen once `gameweeks.scored_at` is set (batch complete AND every fixture finished); `rules_version` stamped | Frozen explicitly by `finalized_at` (which also locks the contest), then never recomputed. A postponed or abandoned match voids the contest instead: no result. |

### Tactical: tactic, Bonus Players, and Tactical Subs — the parts that replaced captaincy and chips

- **Tactic.** Chosen every gameweek: `attack`, `defence` or `balanced`. Attack needs ≥2 starting FWD (the squad only holds 3, so an Attack manager can never make a forward Tactical Swap — accepted for the MVP).
- **Bonus Players.** Exactly 2 per gameweek, chosen from the starting XI, by *position* matching the tactic — Attack → FWD, Defence → DEF, Balanced → MID. A GK is never eligible. A Bonus Player who doesn't appear loses Bonus status for that gameweek; it does not pass to anyone else.
- **Tactical Points** (Bonus Players only, per fixture, summed across a double gameweek): Attack pays goal/assist directly; Defence and Balanced pay tiered thresholds (clean sheet + Defensive Contribution tiers for Defence; goal-or-assist + creativity tiers for Balanced) where **only the highest tier reached counts, never stacked**.
- **Bench roles are fixed, not ranked**: slot 12 is the backup GK (Auto Sub only, never a Bonus Player), 13 is the outfield Auto Sub, 14 and 15 are Tactical Subs. Auto Subs cover a 0-minute starter automatically, in slot order, only if the resulting formation stays legal — a Bonus Player covered this way earns General Points only, no Tactical Points.
- **Tactical Swaps** (0, 1 or 2, planned before the deadline): outgoing must be a non-Bonus starter, incoming must be a Tactical Sub, same position, and the incoming player's first kickoff must be strictly after the outgoing player's last fixture ends. Validated once at submission, never re-validated if a kickoff later moves. Both players' points count; the outgoing player's are banked.
- **Sub Bonus**: +1 per executed swap where the incoming player outscored the outgoing player on General Points, full gameweek, max +2 (the database caps it at 2 swaps structurally; the endpoint also rejects a third).
- **Free transfers bank.** One at the first gameweek under these rules (an anchor stored, never derived, in `ruleset_epochs` — see section 03), +1 per gameweek, capped at **2** (down from 5). Overspending is rejected outright rather than charged — there is no hit-points arithmetic anymore.
- **Selling price** returns only *half* a price rise, rounded down; falls are absorbed in full. Unchanged by the tactical conversion.
- **Autosubs** replace 0-minute starters with bench players in priority order, skipping candidates who also didn't play, and only if the resulting formation stays legal. GK identity is always resolved from `ml.players.position`, never from a slot number — nothing guarantees slot 1 is the keeper.

### Dream11: everything happens once

A contest is created against one fixture. At that moment the player pool — *every* player from both clubs, not a predicted XI — is priced from each player's mean points over their last ≤5 completed gameweeks, min-max normalised within that pool alone into `[6.0, 11.0]`. Those prices are inserted once and a trigger refuses to update them, so every member builds against identical numbers.

Joining uses a 7-character code. One team per user per contest: `POST` submits it and `PATCH` replaces the whole lineup, both refused from kickoff by the `enforce_contest_lock` trigger on `teams` and `team_players` (section 03). A team can also be started from a saved team. Scoring runs at `poll_due_fixtures`' fulltime checkpoint, and the `final` checkpoint finalizes the contest once the fixture actually reports finished. A contest whose fixture is postponed (kickoff cleared) or still unfinished 7 days after kickoff is voided by the `finalize_dream11_contests` sweep: locked, finalized, `void_reason` set, no result. A rescheduled match does not revive it.

The contest screens derive their badge from these fields (`frontend/src/data/contestStatus.js`): **Open** → **Live** (from kickoff, even before `is_locked` is set) → **Completed** (`is_finalized`) or **Cancelled** (`void_reason`). Edit, invite and delete controls are hidden from kickoff.

> **A detail worth knowing before touching any Dream11 query.** Because a contest is one match, its scoring joins filter on the contest's own `fixture_id` — not just the gameweek. `ml.player_gw_stats` is unique on `(player_id, season, gameweek, COALESCE(fixture_id, -1))`, so in a double gameweek a player legitimately has two rows. Classic FPL deliberately sums across both, because an FPL gameweek score *is* the total from every match played. The two joins look almost identical and must not be reconciled. For those per-fixture rows to be right at all, the live poller splits FPL's gameweek-summed stats: a player's second fixture stores the live total minus what's already stored for their first, and the first stops being re-polled from the total once the second kicks off (section 07).

---

## 07 — Where the numbers come from

Two ingest paths, one prediction step run as a backfill, and an LLM at the end of it.

1. **source** — FPL API. Plus a community archive mirror for completed seasons. These are the only two external data sources; no code calls API-Football on a schedule. About 4 FPL calls a day with no match on, and roughly 30–75 on a match day, mostly `bootstrap-static` + `fixtures` from `refresh_fixtures`.
2. **Data/** — `fpl_ingest` · `live_poll`. Bulk backfill (CLI) and per-fixture checkpoint polling (`poll_due_fixtures`), sharing one column list so both write the same shape. The poller also writes influence/creativity/threat/ICT/xG, which feed the tactical creativity tier. `refresh_player_prices` re-runs `ingest_bootstrap` daily, clearing the process-lifetime `bootstrap-static` cache first so the long-lived worker doesn't re-ingest day one's prices.
3. **ml schema** — `player_gw_stats`, ~7,500 rows. Halftime and fulltime rows stay editable (`is_live = TRUE`); only the `final` checkpoint, once FPL marks the fixture finished, settles the row, and the trigger then seals it. In a double gameweek each fixture's row holds only that match's share (section 06).
4. **Feature_engineering → Predict** — features → XGBoost → tiers, written to `ml.ml_predictions`. **No longer a Celery task.** `run_ml_pipeline` / `schedule_predictions` are commented out so the worker never loads xgboost (its import dropped from ~173 to ~107 MB). `Predict/backfill_predictions.py` runs the same three steps with the same delete-then-insert write: by hand (`--season S --gw N`, a dry run unless `--write`), or `--auto` from the daily systemd timer, which writes the next gameweek without predictions only once the gameweek before it is complete (every fixture finished, stats present, none still live). Raw output becomes tiers, never a number shown to a user.
5. **Context_assembler** — `/chat` → Groq. Squad + tiers become a prompt. For a gameweek not yet backfilled, each player's latest backfilled tier is used, so chat keeps working between backfills; only a player with no prediction ever (e.g. new mid-season) shows *New/Insufficient Data*. The model is told never to state a predicted-points figure, and to call the modes "Tactic mode" and "Quick 11 mode" — never "FPL" or "Dream11" — with replies also rewritten on the way out as a safety net. (The `[CURRENT CAPTAIN]` prompt tag and its `CAPTAIN_QUERY` were removed with the rest of captaincy — nothing in `ChatResponse` depended on it.)

The tier step exists because raw model output has two problems on its own: players with no history all get a near-identical ~7.5 prediction, an artifact of how XGBoost routes missing values rather than a real assessment; and the model has no concept of whether someone will actually play. So `build_tiers` filters both into explicit labels — *New/Insufficient Data*, *Doubtful/Injured* — and only assigns a percentile tier to players who are both known and available. The prompt then instructs the model to treat a tier as a **floor**, since the predictor is known to underestimate hauls.

Chat refuses to answer at all until gameweek 1 data exists, because before that every player would show *New/Insufficient Data* — technically functional, practically useless.

---

## 08 — Known gaps

Things this reference would be dishonest to leave out.

| Gap | Status | Detail |
|---|---|---|
| Four Dream11 scoring categories don't exist | blocked | Shots on target, chances created, passes completed and tackles/interceptions have no data source. FPL's API doesn't expose them, and API-Football's free tier was verified against a live key: it covers seasons 2022–2024 only, so the current season is out of reach. The scoring code does not fake them — it simply omits those rules. |
| CORS is fully open | fixed (4 Sep 2026) | Was `allow_origins=["*"]` alongside real JWT auth. Now reads `ALLOWED_ORIGINS` from the environment (comma-separated), with no code-level localhost fallback -- same raise-if-unset pattern as `JWT_SECRET`/`DATABASE_URL`/`REDIS_URL`. |
| Nothing is supervised | supervised on the server; no alerting (27 Sep 2026) | `deploy/systemd/` runs the API, the worker + Beat and the daily backfill under systemd with `Restart=always` and memory caps. In dev they're still started by hand. `GET /health/scheduled-tasks` and `task_heartbeats` expose each Beat task's last success/failure and staleness, but nothing alerts on it yet — an external uptime check on that endpoint is the planned next step. In September, dev Redis was down for 18 days unnoticed. |
| Two abandoned cross-provider hooks | inert | `ml.season_stats` (0 rows, no writer) and `ml.players.fbref_name` (0 of 1,466 populated) are fossils of an FBref integration that was scoped and dropped. `feature_builder` already runs with those features as NaN. |
| Dream11 UI has no "final" state | fixed (26 Sep 2026) | The contest screens show Open / Live / Completed / Cancelled (section 06), not a binary Open / Locked badge. |
| The server isn't migrated yet | deployed to a test VM (26 Sep 2026) | The app runs on a LAN VM (nginx + systemd) at the current `main`. Frontend changes need `npm run build` and a copy of `dist/` into nginx's root after `git pull` — pulling alone doesn't update the served site. A production `pitchside_db` migration is still to be done, with `alembic upgrade heads`. |
| Failing tests | 3 open | `test_auth.py::test_registered_team_name_is_visible_to_team_dashboard` is missing an import of `TEST_SEASON`; `test_auth_enforcement.py`'s route tables don't yet list the saved-teams and `/transfers/history` routes. The legacy classic-field tests listed here earlier are resolved: `test_team_dashboard.py` and `test_full_season_scenario.py` were deleted; `test_beat_scheduling.py` and `test_fpl_live_polling.py` were rewritten for the current schedule. |
| Ownership (`selected`) isn't in the live feed | accepted | Live-polled rows leave `selected` NULL, and the model treats it as 0. |

---

## How this was assembled

Originally read from the source and the live dev database on 3 September 2026. Re-verified and substantially rewritten on 22 September 2026 against the tactical conversion. Re-verified again on 27 September 2026: routes from the app's OpenAPI schema, tables, triggers and row counts from the dev database, the Beat schedule from `Worker/celery_app.py`, the migration head from `alembic heads`, and the test count from `pytest --collect-only`. Row counts are dev data and will not match production. Nothing in the codebase was modified to produce this document.
