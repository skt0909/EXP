Last verified against the live codebase: 3 September 2026. Re-verify significant claims (route counts, row counts, trigger list) before trusting this for anything beyond orientation, since the codebase will have changed since this was written.

*Update, 4 September 2026: the "CORS is fully open" and "Nothing is supervised" rows in Known Gaps (08) were revised to reflect work completed since the snapshot above. The rest of this document is untouched and still dated 3 September 2026.*

---

# How Pitchside fits together

System reference · read from the database and the source, 3 Sep 2026

Two fantasy games sharing one FastAPI backend, one Postgres instance and one Celery worker: what crosses the wire, where every row lives, which process wakes up when, and how the gameweek engine and the scoring engine divide the work.

**44** backend modules · **35** HTTP routes · **3** Postgres schemas, **28** tables · **11** enforcement triggers · **8** scheduled tasks · **524** tests

---

## 01 — The gateway: one function every request goes through

The browser never talks to the backend directly. Fourteen small API modules — `squad.js`, `transfers.js`, `dream11.js` and so on — all call a single `request()` in `frontend/src/api/client.js`, and that function is the entire gateway. It is deliberately the only place that knows the token exists.

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

> **Dev-only setting still in place.** CORS is `allow_origins=["*"]` so the Vite dev server on its own port can reach the API. The comment beside it says not to carry that into anything touching real user auth — it is now touching real user auth.

---

## 02 — The API surface

Twelve routers mounted onto one FastAPI app in `Context_assembler/main.py`. Thirty-five routes, of which seven answer without a credential and the rest do not.

**Identity & reference — Data/**

| Route | Method | Purpose |
|---|---|---|
| `/auth/register` · `/auth/login` | POST | bcrypt hash, returns a token. Public by necessity. |
| `/auth/me` | GET | Who the bearer token names. |
| `/players` | GET | Season player catalogue. Public by decision — identical for everyone. |
| `/fixtures` | GET | Fixtures with derived status and the caller's contest performance. |

**Squad & matchday — Gameplay/**

| Route | Method | Purpose |
|---|---|---|
| `/squad` · `/squad/select` | GET POST | The 15-man squad: 2 GK, 5 DEF, 5 MID, 3 FWD, £100.0m, max 3 per club. |
| `/gw_selection` | GET POST | Starting XI, bench order, captain, vice, chip. |
| `/transfers` · `/transfers/used` | POST GET | Batch swaps, free-transfer allowance, hits. |
| `/transfer-drafts` | GET PUT DELETE | A staged cart — planned swaps that haven't been committed. |
| `/chips/used` | GET | Which chips are spent, per half-season. |

**Results — Results/**

| Route | Method | Purpose |
|---|---|---|
| `/team` | GET | Dashboard: lineup, live points, autosubs, status. |
| `/leagues` · `/leagues/join` | GET POST | Mini-leagues, classic or head-to-head. |
| `/leagues/{id}/table` · `/h2h` | GET | Standings, and one gameweek's H2H matchups. |

**Dream11 — Game_logic/ · router-level auth**

| Route | Method | Purpose |
|---|---|---|
| `/dream11/contests` | GET POST | Your contests; create one on a fixture (prices frozen at creation). |
| `/dream11/contests/join` | POST | Join by 7-character code. |
| `/dream11/contests/{id}` | GET | Detail, with the caller's own membership flags. |
| `/dream11/contests/{id}/players` | GET | The priced pool — identical for every caller. |
| `/dream11/contests/{id}/leaderboard` | GET | Every member's standing. |
| `/dream11/contests/{id}/team` | GET POST | Submit 11 picks; read a team (yours always, a rival's only after lock). |
| `/dream11/fixtures/{id}/contests` | GET | Your contests on one match. |

**Advice — Context_assembler/**

| Route | Method | Purpose |
|---|---|---|
| `/chat` | POST | Assembles squad + ML tiers into a prompt, calls Groq. |

---

## 03 — How the data is stored

One Postgres instance, three schemas, and the split is by *ownership* rather than by feature. Row counts below are live from the dev database.

| Schema | Holds | Notable tables | Rows |
|---|---|---|---:|
| `ml` | Everything ingested from outside, plus model output. Nothing a user writes. | `players` · `teams` · `fixtures` · `player_gw_stats` · `ml_predictions` | 5,985 |
| `public` | The classic game: who plays, what they picked, what they scored. | `users` · `user_squads` · `squad_players` · `gw_selections` · `starting_xi` · `transfers` · `chips` · `gw_scores` · `mini_leagues` · `league_members` · `league_h2h_fixtures` · `leaderboard_snapshots` | 15,081 |
| `dream11` | The contest game, entirely self-contained. | `contests` · `contest_members` · `player_prices` · `teams` · `team_players` | 76 |

### Two meanings of "player id", and the one query that converts

This is the single most load-bearing convention in the schema, and the easiest thing to get silently wrong.

- **Classic FPL tables store the raw `fpl_id`**, never translated: `squad_players.player_id`, `starting_xi.player_id`, `transfers.player_in_id`.
- **Dream11 tables store `ml.players.id`**, the internal serial — and so does `ml.player_gw_stats.player_id`, which is why Dream11's scoring joins line up without translation.

Exactly one query crosses between them: `PLAYERS_LOOKUP_QUERY`, which selects `p.id AS internal_id` keyed by `p.fpl_id`. Everything a client sends or receives is in fpl-id space; everything that reaches Dream11 storage is in internal-id space. The translation is only well-defined *within a season* — `ml.players` holds one row per `(fpl_id, season)`, so the same footballer has a different internal id each year.

Unifying the two is an open decision, not an oversight: it would mean migrating live data on one side or the other.

### Money, and other scales

Prices are held in **tenths of £m** as integers throughout — `BUDGET_CAP = 1000` is £100.0m. That is what makes the selling-price rule exact rather than approximate: half a price rise, rounded down to the nearest £0.1m, is literally `profit // 2`. Dream11 doesn't use money at all — it uses *credits*, floats in `[6.0, 11.0]` rounded to the nearest 0.5, against a cap of 100.

### Eleven triggers doing the work application code shouldn't be trusted with

The pattern throughout is belt-and-braces: application code checks, and the database refuses. Every one of these exists because the check alone was judged insufficient.

| Table | Trigger | Fires | Prevents |
|---|---|---|---|
| `ml.player_gw_stats` | `enforce_gw_stats_immutability` | BEFORE UPDATE | Changing a settled gameweek's stats (`is_live = FALSE`). |
| `ml.players` | `enforce_player_identity` | BEFORE UPDATE | A player's identity columns moving under existing picks. |
| `ml.fixtures` | `enforce_fixture_identity` | BEFORE UPDATE | Re-pointing a fixture at different clubs. Scores and `finished` stay updatable. |
| `ml.ml_predictions` | `enforce_ml_prediction_immutability` | BEFORE UPDATE | Rewriting a prediction after the fact. |
| `public.gw_selections` | `enforce_selection_lock` | BEFORE UPDATE | Editing a lineup after the deadline. |
| `public.transfers` | `enforce_transfers_immutability` | BEFORE UPDATE/DELETE | Un-making a transfer. Cancellation is a separate append-only table. |
| `public.chips` | `enforce_chip_limit` | BEFORE INSERT | A second use of a chip within the same half-season. |
| `public.leaderboard_snapshots` | `enforce_snapshot_immutability` | BEFORE UPDATE/DELETE | Editing frozen league history. Append-only. |
| `dream11.player_prices` | `enforce_price_immutability` | BEFORE UPDATE | Any price change after contest creation. |
| `dream11.teams` | `enforce_contest_lock` | BEFORE INSERT | Submitting a team after kickoff. |
| `dream11.contest_members` | `enforce_contest_result_immutability` | BEFORE UPDATE | Moving points or rank on a finalized contest. |

Schema changes go through Alembic — ten hand-authored revisions, none autogenerated, because there are no ORM models to diff against. The chain currently ends at `b3f7c1e08d52` (Dream11 contest finalization).

---

## 04 — The services, and which one wakes up when

Five processes in development. Nothing is containerised or supervised yet — each is started by hand.

| Process | Kind | Notes |
|---|---|---|
| **Vite dev server** | sync | React 18 + React Router. Serves the SPA on its own port, which is why CORS is open. |
| **uvicorn** (`uvicorn main:app`) | sync | The whole HTTP surface. Handlers are plain `def`, not `async def` — they block on the DB and FastAPI runs them in a threadpool. |
| **Postgres** | store | One instance, three schemas. A separate `fpl_game_test` database backs the test suite; `TEST_DATABASE_URL` always wins over `DATABASE_URL` so a test run can never touch dev data. |
| **Redis** | async | Celery broker *and* result backend, one URL for both. Not a cache — nothing in the app reads or writes it directly. |
| **Celery worker** (`celery -A Worker.celery_app worker --pool=solo`) | async | `--pool=solo` is required on Windows: the default prefork pool needs `os.fork`. One process, one task at a time. |
| **Celery Beat** (`celery -A Worker.celery_app beat`) | async | A *separate* process that only schedules. Worker with no Beat means nothing fires on a timer; Beat with no worker means tasks queue and never run. |

*Legend: sync = request path · async = scheduled / background · store = persistence*

### Configuration resolves the same way everywhere

Every service finds its own config by walking up from its file to the nearest `.env`, preferring a `TEST_`-prefixed variable, and **raising if neither exists** rather than falling back to a localhost default. That last part is deliberate: Redis was once hardcoded to `redis://localhost:6379/0`, which meant a misconfigured deploy failed opaquely instead of saying so.

`get_engine()` is `lru_cache`'d, so each process holds exactly one connection pool. It used to exist as five identical copies in five packages; a worker that imported two of them held two independent pools.

### What Beat runs

Every scheduled function is re-exported through `Worker/beat_registry.py` — an index with no logic in it, so "what does Beat call, and who owns it?" has one answer. The retry/logging wrappers live in `Worker/tasks.py`; the behaviour lives in each function's own module.

| Task | Every | Owner module | What it does |
|---|---|---|---|
| `lock_expired_gameweeks` | 5 min | `GameEngine/gameweek_lock` | Flips `gw_selections.is_locked` once the deadline passes. |
| `lock_dream11_contests` | 5 min | `Game_logic/dream11_locking` | Locks each contest at *its own* fixture's kickoff. The only writer of that flag. |
| `refresh_active_gameweeks` | 15 min | `GameEngine/gameweek_finalize` | Scores every gameweek inside the 5-day active window, then recomputes standings. |
| `revert_free_hits` | 15 min | `GameEngine/free_hit_revert` | Restores the pre-Free-Hit squad once that gameweek is over. |
| `refresh_fixtures` | 15 min | `Data/fpl_ingest` | Re-pulls the season's fixture list so scores, kickoff changes and `finished` stay current. |
| `finalize_dream11_contests` | 15 min | `Game_logic/dream11_scoring` | Freezes the result of every contest whose match has finished. |
| `schedule_fixture_polls` | 15 min | `Data/live_poll` | Books each new fixture's two live-poll checkpoints. |
| `schedule_predictions` | Tue 06:00 | `Predict/prediction_scheduling` | Finds the next gameweek with no predictions and runs the ML pipeline. |

### The one-off tasks Beat doesn't own

Two tasks fire on an ETA rather than an interval. When a Dream11 contest is created, `create_contest` books `poll_and_score_dream11` for **kickoff + 50 min** and **kickoff + 115 min**. The classic side does the same per fixture via `schedule_fixture_polls`.

That scheduling happens in a FastAPI `BackgroundTask`, after the response is sent, and is wrapped in its own try/except. The reason is measured, not theoretical: with Redis stopped, an inline `.apply_async()` hung the POST for ~100 seconds and then poisoned the Celery app instance for the whole process. A bounded retry policy cut that to ~2 seconds, and moving it off the request path made it 0.

> **The consequence of best-effort scheduling.** A contest created while Redis is down gets no checkpoints booked at all, and the failure is logged rather than raised so contest creation still succeeds. That is why `finalize_dream11_contests` exists as a recurring sweep: the ETA tasks are the optimisation, the sweep is the guarantee.

---

## 05 — Game Engine × Score Engine

The split is clean once you see it: **GameEngine decides *when*, Results decides *how much*.** Neither imports the other's concerns, and both read their constants from `Shared/rules.py`, which imports nothing at all.

**Game Engine** — `backend/GameEngine/` — time and state transitions

- **gameweek_lock** — deadline is `MIN(kickoff_time) − 90 min`, *derived, never stored*. Asking twice can legitimately differ if fixtures are re-ingested.
- **gameweek_finalize** — decides which gameweeks are "active" and calls the scorer. Active is a **5-day window from first kickoff**, not a flag.
- **free_hit_revert** — restores the pre-chip squad once every fixture is done.

**Score Engine** — `backend/Results/` — arithmetic over frozen selections

- **scoring** — `gw_selections → gw_scores`. Autosubs, captaincy, chip effects, transfer hits.
- **standings** — `gw_scores → league_members + leaderboard_snapshots`. Classic and H2H from separate sources.
- **team_dashboard / leagues** — read models over the same rows.

### The handshake

Every 15 minutes, `refresh_active_gameweeks` asks one question — which gameweeks started within the last five days? — and for each one runs **score, then standings, in that order, never parallel**, because `compute_league_standings` reads the `gw_scores` rows `score_gameweek` has just written.

Both sides isolate failures per item: one user who fails to score doesn't abort the batch, and one broken league doesn't stop the others. Failures are collected and returned, not raised.

Re-running is expected, not exceptional. `score_gameweek` upserts on `(user_id, season, gameweek)` and recomputes `season_total` fresh from stored prior rows rather than trusting a running total — so as more match data arrives mid-gameweek, rescoring converges instead of accumulating.

> **Why "active" is a window and not a flag.** Neither `fixtures.finished` nor `player_gw_stats.is_live` is a usable recency signal here: the ingest path writes a whole gameweek's rows in one atomic batch, so those columns flip together rather than incrementally as matches end. A real match window (Friday through Monday) plus FPL's 24–48h bonus-point confirmation lag fits comfortably inside five days. **That window is also what freezes history** — once a gameweek falls out of it, nothing revisits the row, and `gw_scores.rules_version` records which generation of the rules produced it.

### The race that exists and currently costs nothing

Locking and scoring run on different intervals and can overlap. The analysis is written into both modules' source rather than left implicit, and it concludes the race is harmless — but by two accidents of the current design rather than by an invariant: the scoring query doesn't filter on `is_locked` at all, and the endpoints that must refuse late edits gate on the deadline instead. The 90-minute deadline offset also gives locking a head start over scoring, which is why changing that number invalidates the analysis.

---

## 06 — The game logic itself

Two rulebooks, deliberately not merged. `Shared/rules.py` holds classic FPL's constants as pure data with a zero-import rule; Dream11's live in `dream11_scoring.py` because it is a different game, not a variant.

| Rule | Classic FPL | Dream11 |
|---|---|---|
| Scope | A whole gameweek, all 10 matches | **One fixture** |
| Squad | 15 (2/5/5/3), pick an XI + 4 bench | 11 picked directly — no squad, no bench, no transfers |
| Budget | £100.0m, max 3 per club | 100 credits, max 7 per club |
| Formation | XI must stay legal after autosubs | 1 GK, 3–5 DEF, 3–5 MID, 1–3 FWD → 7 legal shapes |
| Goal points | GK 10 · DEF 6 · MID 5 · FWD 4 | GK 10 · DEF 6 · MID 5 · FWD 4 |
| Assist | 3 | 20 |
| Clean sheet | 60-minute threshold | **54-minute threshold**, recomputed from minutes + goals conceded, never read from the precomputed column |
| Captain | 2× (3× on Triple Captain); vice is a *fallback* only if the captain didn't play | 2× captain *and* 1.5× vice, both unconditional and simultaneous, applied as additive bonuses |
| Result | Frozen by falling out of the 5-day window; `rules_version` stamped | Frozen explicitly by `finalized_at`, then never recomputed |

### Classic: the parts that carry state across gameweeks

- **Free transfers bank.** One earned per gameweek, unused ones accumulate to a cap of 5. Implemented as a pure recurrence over prior consumption, so it can be tested on hand-built histories that would take a season of HTTP calls to reach. Overspending costs points; it does not leave a debt.
- **Hits** cost 4 points per transfer beyond the allowance — charged by the scorer, which counts rows the transfer endpoint stamped `is_free = FALSE`.
- **Selling price** returns only *half* a price rise, rounded down; falls are absorbed in full. One function, called twice per transfer — once to validate the budget, once to record what was paid — because those two must agree.
- **Chips** come in two sets, one per half-season, boundary at gameweek 19. An unused first-half chip is forfeited, not carried. Wildcard and Free Hit additionally make every transfer free and lift the per-gameweek cap.
- **Autosubs** replace 0-minute starters with bench players in priority order, skipping candidates who also didn't play, and only if the resulting formation stays legal. GK identity is always resolved from `ml.players.position`, never from a slot number — nothing guarantees slot 1 is the keeper.

### Dream11: everything happens once

A contest is created against one fixture. At that moment the player pool — *every* player from both clubs, not a predicted XI — is priced from each player's mean points over their last ≤5 completed gameweeks, min-max normalised within that pool alone into `[6.0, 11.0]`. Those prices are inserted once and a trigger refuses to update them, so every member builds against identical numbers.

Joining uses a 7-character code. Submission is insert-only — one team per user per contest, no edit path — and a trigger on the teams row blocks it once the contest locks at kickoff. Scoring runs at two checkpoints, and finalization freezes the result once the fixture actually reports finished.

> **A detail worth knowing before touching any Dream11 query.** Because a contest is one match, its scoring joins filter on the contest's own `fixture_id` — not just the gameweek. `ml.player_gw_stats` is unique on `(player_id, season, gameweek, COALESCE(fixture_id, -1))`, so in a double gameweek a player legitimately has two rows. Classic FPL deliberately sums across both, because an FPL gameweek score *is* the total from every match played. The two joins look almost identical and must not be reconciled.

---

## 07 — Where the numbers come from

Two ingest paths, one prediction pipeline, and an LLM at the end of it.

1. **source** — FPL API. Plus a community archive mirror for completed seasons. These are the only two external data sources.
2. **Data/** — `fpl_ingest` · `live_poll`. Bulk backfill and per-fixture checkpoint polling, sharing one column list so both write the same shape.
3. **ml schema** — `player_gw_stats`. 3,211 rows. Half-time writes stay editable; full-time settles the row and the trigger seals it.
4. **Feature_engineering → Predict** — features → XGBoost → tiers. Rolling means feed `model.json`; raw output becomes tiers, never a number shown to a user.
5. **Context_assembler** — `/chat` → Groq. Squad + tiers + captain become a prompt. The model is told never to state a predicted-points figure.

The tier step exists because raw model output has two problems on its own: players with no history all get a near-identical ~7.5 prediction, an artifact of how XGBoost routes missing values rather than a real assessment; and the model has no concept of whether someone will actually play. So `build_tiers` filters both into explicit labels — *New/Insufficient Data*, *Doubtful/Injured* — and only assigns a percentile tier to players who are both known and available. The prompt then instructs the model to treat a tier as a **floor**, since the predictor is known to underestimate hauls.

Chat refuses to answer at all until gameweek 1 data exists, because before that every player would show *New/Insufficient Data* — technically functional, practically useless.

---

## 08 — Known gaps

Things this reference would be dishonest to leave out.

| Gap | Status | Detail |
|---|---|---|
| Four Dream11 scoring categories don't exist | blocked | Shots on target, chances created, passes completed and tackles/interceptions have no data source. FPL's API doesn't expose them, and API-Football's free tier was verified against a live key: it covers seasons 2022–2024 only, so the current season is out of reach. The scoring code does not fake them — it simply omits those rules. |
| CORS is fully open | fixed (4 Sep 2026) | Was `allow_origins=["*"]` alongside real JWT auth. Now reads `ALLOWED_ORIGINS` from the environment (comma-separated), with no code-level localhost fallback -- same raise-if-unset pattern as `JWT_SECRET`/`DATABASE_URL`/`REDIS_URL`. |
| Nothing is supervised | partially addressed (4 Sep 2026) | Worker and Beat are still started by hand in separate terminals -- that part is unchanged. What's new: `GET /health/scheduled-tasks` and a `task_heartbeats` table now expose, per Beat-scheduled task, its last success/failure and a staleness verdict against its configured interval -- "Beat stopped firing this" is detectable rather than silent. Still manual to check, and still no process supervision (systemd/supervisord/Docker) or alerting on top of the signal -- both remain the Observability project's scope. |
| Two abandoned cross-provider hooks | inert | `ml.season_stats` (0 rows, no writer) and `ml.players.fbref_name` (0 of 1,466 populated) are fossils of an FBref integration that was scoped and dropped. `feature_builder` already runs with those features as NaN. |
| Dream11 UI has no "final" state | backend done | The API now returns `is_finalized`, but the contest screens still render a binary Open / Locked badge from `is_locked`, which never returns to false — so a contest that ended months ago looks like one that kicked off a minute ago. |

---

## How this was assembled

Read from the source and from the live dev database on 3 September 2026 — module inventory, route table, schema and row counts, and the trigger list are all queried or grepped rather than recalled. Row counts are dev data and will not match production. Nothing in the codebase was modified to produce this document.
