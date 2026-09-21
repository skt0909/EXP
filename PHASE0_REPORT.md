# Phase 0 — Verification report

Read-only pass over the two draft migrations, the live dev schema and the codebase.
Nothing was migrated, written or deleted. The only files created are
`fpl_game_backup.sql` (step 1) and this report.

**Rules of evidence.** Every schema claim below was produced by running a command
against the live `fpl_game` database on 2026-09-20 and is quoted. Claims I could
not execute are labelled **UNVERIFIED** and say why.

**Verdict: the drafts must not be run as written.** One statement aborts the whole
migration, and one silently does nothing while leaving the plan's stated scoring
contract impossible to satisfy. Details in §2.

---

## 1. Backup (step 1) — done

```
pg_dump -U postgres fpl_game > fpl_game_backup.sql
  backup OK: 3032080 bytes, 20/09/2026 21:58:53
```

Server: `PostgreSQL 18.4 on x86_64-windows, compiled by msvc-19.44.35227, 64-bit`.

### Alembic state — the drafts have not been applied

```
=== alembic_version APPLIED in fpl_game ===
a06f58d93f5a
c2f6a83e91d4

=== alembic heads (files) ===
d58b3f10a7c2 (head)
```

The database sits at exactly the two heads `c41a9e27d06b` merges, so both drafts are
pending. `alembic heads` reports a single head only because the draft *files* now
exist on disk. `fpl_game_test` is at the same two revisions (§6).

---

## 2. Draft vs live schema — mismatches

### BLOCKER 1 — the TRUNCATE aborts

`cancelled_transfers` has a foreign key into `transfers`, and it is **not** in the
TRUNCATE list:

```
   referencing_table   |               conname                | references_table
-----------------------+--------------------------------------+------------------
 cancelled_transfers   | cancelled_transfers_transfer_id_fkey | transfers
```

Draft 1 truncates `leaderboard_snapshots, gw_scores, starting_xi, gw_selections,
chips, transfers` with `RESTART IDENTITY` and deliberately no `CASCADE`. PostgreSQL
refuses to truncate a table referenced by a foreign key from a table that is not
itself being truncated. This is exactly the case the draft's own comment anticipates
("Postgres will error and name it. Add that table to the list below instead of
cascading blindly").

`cancelled_transfers` holds **9 rows**. It is an append-only record of Free-Hit
cancellations — a concept the rewrite removes entirely.

> **UNVERIFIED:** I did not execute the TRUNCATE to observe the error, because Phase 0
> forbids writes. The foreign key's existence is verified above; the resulting abort is
> documented PostgreSQL behaviour, not something I ran.

**Fix:** add `cancelled_transfers` to the TRUNCATE list, first (or drop the table
outright — see §9). `starting_xi → gw_selections` is fine because both are in the list.

### BLOCKER 2 — `rules_version` is a smallint, not a string

```
=== public.gw_scores columns ===
  column_name   |     data_type     | is_nullable | column_default
----------------+-------------------+-------------+----------------
 rules_version  | smallint          | NO          |
```

Draft 2 ends with:

```sql
ADD COLUMN IF NOT EXISTS rules_version VARCHAR(20)
```

The column already exists, so `IF NOT EXISTS` makes this a **silent no-op** — the
migration succeeds and the column stays `smallint NOT NULL`. But the plan (§2 and
Phase 4) specifies `rules_version = "tactical-v1"`. Writing that string into a
`smallint` raises at runtime, in the scorer, long after the migration "passed".

This is the dangerous kind of mismatch: nothing fails at migration time.

Note also: the column is `NOT NULL` with **no default**, so every `INSERT` into
`gw_scores` must supply it. `Results/scoring.py` already does.

**Fix — pick one, explicitly:**
- **(a)** keep it numeric: bump the existing integer convention, and change the plan's
  `RULES_VERSION` from `"tactical-v1"` to the next integer; or
- **(b)** convert the column: `ALTER TABLE gw_scores ALTER COLUMN rules_version TYPE
  VARCHAR(20) USING rules_version::text`, then drop the `IF NOT EXISTS` line so the
  intent is visible in the diff.

I have not chosen between these — (a) and (b) have different consequences for the
existing 990 `gw_scores` rows and for `c9a04e7b53d1`'s stated purpose, and this is a
product decision.

### MISMATCH 3 — the bench is already at slots 12–15

Step 2 asks where the bench is stored today. Answer: **already in `starting_xi`**, and
the slot check is **already 1–15**, not 1–11:

```
 starting_xi | starting_xi_position_slot_check | CHECK (((position_slot >= 1) AND (position_slot <= 15)))
```

Consequences:

- The **name matches**, so draft 2's `DROP CONSTRAINT IF EXISTS
  starting_xi_position_slot_check` will work. The draft's stated worry ("if the live
  name differs, the DROP silently does nothing") does **not** materialise.
- The draft's comment "The old inline CHECK (slot 1-11)" is **wrong** and should be
  corrected, or the next reader will re-derive the wrong mental model.
- Replacing it with `ck_starting_xi_slot CHECK (position_slot BETWEEN 1 AND 15)` is a
  rename, not a widening. Harmless, but it is not doing what the draft says it is.

### MISMATCH 4 — draft 2's `downgrade()` destroys the bench

```sql
DELETE FROM starting_xi WHERE position_slot > 11
ALTER TABLE starting_xi ADD CONSTRAINT starting_xi_position_slot_check CHECK (position_slot BETWEEN 1 AND 11)
```

Because live is already 1–15, this does not "restore" anything — it deletes every
bench row and imposes a constraint the database never had. A downgrade should return
the schema to its *previous* state, which is 1–15.

**Fix:** drop the `DELETE`, and recreate the check as `BETWEEN 1 AND 15`.

### MISMATCH 5 — `tactic` is `NOT NULL` with no default

```sql
ALTER TABLE gw_selections ADD COLUMN tactic VARCHAR(10) NOT NULL ...
```

This only works on an empty table. `gw_selections` currently holds **106 rows**, so
draft 2 depends entirely on draft 1's TRUNCATE having run — which currently aborts
(Blocker 1). The two revisions are coupled more tightly than they look; if anyone runs
`upgrade` one step at a time, or the guard in §2.6 refuses, draft 2 fails.

**Fix:** either state the dependency explicitly in draft 2's docstring, or give the
column a temporary default and drop it after backfill.

### Statements that are CORRECT against live

Verified present, so these will do what they say:

| Draft statement | Live evidence |
|---|---|
| `DROP TRIGGER enforce_chip_limit ON chips` | trigger `enforce_chip_limit` exists on `chips` |
| `DROP FUNCTION enforce_chip_limit_fn()` | function `enforce_chip_limit_fn` exists |
| `DROP TABLE chips` | `chips` exists, 1 row |
| `gw_selections DROP captain_id, vice_captain_id, chip_used` | all three exist |
| `starting_xi DROP is_captain, is_vice_captain` | both exist |
| `gw_scores DROP transfer_hits, hit_deductions` | both exist |
| `UPDATE league_members SET season_points, rank, last_gw_points` | all three columns exist |
| `user_squads.total_transfers no longer exists` | confirmed — 6 columns, absent |
| new triggers / `tactical_swaps` | 0 name collisions in live |

Dropping `chip_used` also drops `gw_selections_chip_used_check` automatically — no
separate statement needed.

---

## 3. `ml.player_gw_stats` columns (step 3) — both present

```
       column_name       | data_type
-------------------------+-----------
 creativity              | numeric
 defensive_contributions | smallint
```

Resolves open item §8 of the plan ("`creativity` column existence … is unverified") and
confirms the plural spelling in the database. `creativity` is `numeric`, so the
Balanced tier thresholds (15/30/50) compare cleanly without integer rounding.

---

## 4. Dependency scan (step 4)

Counts are line-level references, excluding `Migrations/`.

| Term | backend | tests | frontend | files |
|---|---:|---:|---:|---:|
| `captain_id` | 31 | 91 | 12 | 20 |
| `vice_captain_id` | 31 | 93 | 12 | 20 |
| `chip_used` | 47 | 79 | 6 | 15 |
| `is_captain` | 24 | 15 | 6 | 15 |
| `is_vice_captain` | 24 | 14 | 4 | 14 |
| `transfer_hits` | 11 | 17 | 0 | 9 |
| `hit_deductions` | 13 | 19 | 1 | 10 |
| `chips` | 26 | 27 | 4 | 13 |
| `revert_free_hits` | 14 | 6 | 0 | 7 |
| `free_hit` | 83 | 113 | 3 | 17 |
| `/chips` | 6 | 10 | 2 | 10 |

### The trap in these numbers: Dream11 has its own captain

**Dream11 is out of scope (plan §2) but shares the vocabulary.** A large share of the
`captain_id` / `is_captain` hits are Dream11's, and its captain is a different feature
with different rules (2× *and* 1.5× vice, both unconditional — ARCHITECTURE §06):

- `backend/Game_logic/dream11.py`, `dream11_scoring.py`
- `frontend/src/api/dream11.js`, `pages/Dream11/PickTeamPage.jsx`,
  `components/OpponentTeam/OpponentTeamPanel.jsx`
- `backend/Tests/test_dream11.py` — **106 references**, all out of scope

A blanket "remove captain" pass would break Dream11. The classic-side files are:

**Backend (classic only)**
- `Gameplay/starting_xi.py` — captain/vice/chip writes, chip validation (heaviest)
- `Gameplay/lineup.py` — returns captain/vice/chip in `GET /gw_selection`
- `Gameplay/transfers.py` — `chip_used`, free-hit/wildcard uncapping
- `Gameplay/chips.py` — the whole module (`GET /chips/used`)
- `Results/scoring.py` — captain multiplier, chip effects, hits
- `Results/team_dashboard.py` — captain display, `transfer_hits`, `hit_deductions`
- `GameEngine/free_hit_revert.py` — the whole module
- `Shared/rules.py` — chip constants
- `Worker/tasks.py`, `Worker/celery_app.py`, `Worker/beat_registry.py` —
  `revert_free_hits` task, schedule entry, registry export
- `Context_assembler/main.py` — captain/vice in the chat prompt
- `Tools/fpl_sim.py` — simulation harness

**Frontend (classic only)**
- `api/gwSelection.js`, `api/chips.js`
- `pages/StartingXI/StartingXIPage.jsx` — captain/vice/chip UI
- `pages/DashboardPage/DashboardPage.jsx` — captain badge, `hit_deductions`
- `pages/Transfers/TransfersPage.jsx` — free-hit messaging

**Tests needing rework (classic):** `test_gameweek_lifecycle.py` (119),
`test_starting_xi.py` (79), `test_beat_scheduling.py` (38),
`test_full_season_scenario.py` (38), `test_team_dashboard.py` (32),
`test_scoring.py` (23), `test_transfers.py` (23), `test_celery_wiring.py` (8),
`test_auth_enforcement.py` (6), `test_context_assembler.py` (4), `test_health.py` (4),
`test_leagues.py` (2), `test_h2h_endpoint.py` (2).

`test_auth_enforcement.py` matters disproportionately: it walks the live FastAPI app
and fails if any route is in neither its authenticated nor its public table. Removing
`GET /chips/used` requires removing it from that table in the same commit.

---

## 5. ML write scan (step 5) — game layer is read-only on `ml`

Every `INSERT`/`UPDATE`/`DELETE` against `ml.*` outside `Migrations/` and `Tests/`:

```
Data/fpl_ingest.py:461  INSERT INTO ml.teams
Data/fpl_ingest.py:502  INSERT INTO ml.players
Data/fpl_ingest.py:581  INSERT INTO ml.fixtures
Data/fpl_ingest.py:667  INSERT INTO ml.fixtures
Data/fpl_ingest.py:771  INSERT INTO ml.player_gw_stats
Data/live_poll.py:91    INSERT INTO ml.fixture_poll_schedule
Data/live_poll.py:134   INSERT INTO ml.player_gw_stats
Worker/tasks.py:164     DELETE FROM ml.ml_predictions
```

No writer in `Gameplay/`, `Results/` or `GameEngine/`. This matches ML_LAYERS.md §01's
writer table exactly. **Neither draft touches `ml`** — confirmed by reading both files.

---

## 6. How `fpl_game_test` gets its schema (step 6)

**Alembic, not fixtures and not `baseline_schema.sql`.**

- `backend/Tests/README.md:33` — `DATABASE_URL="…/fpl_game_test" python -m alembic upgrade head`
- `.github/workflows/backend-tests.yml:60` — `run: python -m alembic upgrade head`
- `Tests/conftest.py` creates **no** schema. It wipes `ml.*` fixture data per test under
  `TEST_SEASON = "9999-00"` and inserts `public` rows per test file, but issues no DDL.

Current state:

```
=== alembic_version in fpl_game_test ===
a06f58d93f5a
c2f6a83e91d4
```

**What must change:** nothing in the test harness itself — `alembic upgrade head` will
carry `fpl_game_test` through both new revisions once they are fixed. Two consequences:

1. Draft 1's `_refuse_to_wipe_real_data()` guard counts rows in `gw_selections`,
   `gw_scores`, `transfers`, `leaderboard_snapshots`. On a **freshly created** test DB
   those are 0 and it passes silently. On a **reused** one (CI reuses within a job; a
   developer's local `fpl_game_test` accumulates) it will **refuse**, and `alembic
   upgrade head` fails with the guard's RuntimeError. CI would need
   `ALLOW_GAMEPLAY_WIPE=fpl_game_test`, or the guard needs a test-database exemption.
   **This will break CI as written.**
2. Blocker 1 applies to `fpl_game_test` identically — `cancelled_transfers` exists there
   too (it is created by the same migration chain).

---

## 7. Readers of the affected tables, and post-TRUNCATE dashboard state (step 7)

### Endpoints

| Table | Read by | Endpoints |
|---|---|---|
| `user_gameweek_finance` | `Results/team_dashboard.py` (read), `Results/scoring.py:200` (**writer**) | `GET /team` |
| `gw_scores` | `team_dashboard.py`, `leagues.py`, `standings.py`, `gameweek_finalize` | `GET /team`, `GET /leagues`, `GET /leagues/{id}/table`, `GET /leagues/{id}/h2h` |
| `league_members` | `leagues.py`, `standings.py` | `GET /leagues`, `GET /leagues/{id}/table`, `POST /leagues`, `POST /leagues/join` |
| `leaderboard_snapshots` | `standings.py` (writer) | none directly — read only via standings |
| `transfers` | `Gameplay/transfers.py`, `scoring.py`, `squad_selection.py`, `starting_xi.py` | `GET /transfers/used`, `POST /transfers` |

### What the dashboard shows after each truncate

| Table | Rows now | After | Dashboard effect | Recommendation |
|---|---:|---|---|---|
| `gw_scores` | 990 | empty | `GET /team` shows 0 points, no season total. Honest — scoring rules changed. | **Wipe** |
| `gw_selections` | 106 | empty | No lineup; `has_lineup=False` "not set yet" path. Correct. | **Wipe** |
| `starting_xi` | 1466 | empty | Cascades with the above. | **Wipe** |
| `transfers` | 459 | empty | Free-transfer bank recomputes from zero — correct under the new cap of 2. | **Wipe** |
| `chips` | 1 | dropped | `/chips/used` removed with it. | **Drop** |
| `leaderboard_snapshots` | 1461 | empty | League history gone. Under new scoring it would be misleading anyway. | **Wipe** |
| `user_gameweek_finance` | 97 | **kept** | ⚠️ **See below** | **Keep, but read this** |

> **⚠️ `user_gameweek_finance` becomes inconsistent, and the plan's reasoning is
> incomplete.** The plan keeps it because the dashboard shows bank and team value from
> it. That is correct — `team_dashboard.py:164` reads `bank, team_value` from it. But
> it is **written by `Results/scoring.py:200`**, one row per scored gameweek. Truncating
> `gw_scores` while keeping its 97 rows leaves finance rows for gameweeks that no longer
> have a score. `GET /team` for a past gameweek would then show a real bank and team
> value beside 0 points.
>
> The response model has a `team_value_available` flag whose comment says it is "False
> only for a 'final' gameweek with no `user_gameweek_finance` row". After the wipe the
> inverse case appears — a finance row with no score — which that flag does not
> describe. **Not a migration failure; a display inconsistency to decide on.** Options:
> keep as-is and accept it, or truncate only rows for wiped gameweeks. I have not
> chosen. `user_gameweek_finance` must NOT be truncated wholesale, per your instruction.

Also not in the TRUNCATE list, and worth a decision: **`league_h2h_fixtures` holds
10,184 rows** of H2H match results computed from `gw_scores` rows that are about to be
deleted. Leaving it gives `GET /leagues/{id}/h2h` a full fixture history with
`points_1`/`points_2` derived from scores that no longer exist. See §9.

---

## 8. Deployment configuration (step 8) — **none in the repo**

Searched the whole repo (excluding `venv/`, `node_modules/`) for `Dockerfile*`,
`docker-compose*`, `*.service`, `*.sh`, `Procfile`, `nginx*`, `Makefile`:

```
./.env.example
./.github/workflows/backend-tests.yml
./simulation/Dockerfile.worker
./simulation/docker-compose.simulation.yml
```

- The two `simulation/` files are the **local simulation harness** — a compose stack of
  `postgres:18`, `redis:8`, a Celery `worker` and `beat`. Not a deployment.
- `.github/workflows/backend-tests.yml` is CI only.
- `.env.example` names `ALLOWED_ORIGINS`, `API_FOOTBALL_DATA_KEY`, `DATABASE_URL`,
  `GROQ_API_KEY`, `JWT_SECRET`, `REDIS_URL`. No deployment target.
- `grep -rn "pitchside_db"` across the repo returns **nothing**.
- ARCHITECTURE.md §04 says the five dev processes are "started by hand", and §08 lists
  "Nothing is supervised" as an open gap — "no process supervision (systemd/supervisord/
  Docker)".

**UNVERIFIED — and unverifiable from here:** how the app runs on the e2-micro, which
processes run there, and how migrations are applied to `pitchside_db`. The repo records
none of it, and I cannot reach the server. **Before Phase 1 touches anything deployed,
this has to be answered by someone with access.** In particular: whether `pitchside_db`
is at the same two Alembic heads, and whether anything runs `alembic upgrade` there at
all.

---

## 9. Live-only objects (step 9)

| Object | Exact name | Rows | FK into a truncated/dropped/altered table? | Recommendation |
|---|---|---:|---|---|
| Free-Hit snapshots | **`free_hit_squads`** (not `free_hit_squad_snapshots`) | 0 | No — FK is `user_id → users(id)` only | **Drop.** Free Hit is removed; `GameEngine/free_hit_revert.py` and the `revert_free_hits` Beat task go with it. Empty, so no data loss. |
| Transfer cancellations | `cancelled_transfers` | 9 | **YES — `transfer_id → transfers(id)`** | **Drop** (preferred) or add to the TRUNCATE list. It exists only to un-count Free-Hit transfers; with no Free Hit it has no purpose. **This is Blocker 1.** |
| Transfer cart | `transfer_drafts` | 1 | No — `user_id → users(id)` only | **Truncate.** Staged swaps predate the new rules. The table itself stays — `GET/PUT/DELETE /transfer-drafts` survive the rewrite. |
| Dream11 team players | **`dream11.team_players`** — in the `dream11` schema, not `public` | — | No | **Keep untouched.** Out of scope. |
| Hand-applied baseline | revision `9fb0900550db` | — | n/a | **Keep.** It is the root of the chain; both drafts descend from it. |
| `user_squads.total_transfers` | — | — | n/a | **Already gone.** Confirmed: `user_squads` has 6 columns and no `total_transfers`. Draft 1's comment is correct and needs no change. |
| `gw_scores.rules_version` | present as `smallint NOT NULL` | 990 | n/a | **Blocker 2.** See §2. |
| H2H fixtures | `league_h2h_fixtures` | 10184 | No FK to a truncated table (`league_id → mini_leagues`, `user_id_1/2 → users`) | **Decide.** Not currently in any list. Its results derive from `gw_scores` rows being deleted. Recommend **truncating** it alongside, or `GET /leagues/{id}/h2h` serves orphaned results. |

---

## 10. Proposed fixes, in order

**Draft 1 (`c41a9e27d06b`)**
1. Resolve `cancelled_transfers` — drop the table, or add it as the first entry in the
   TRUNCATE list. *(Blocker 1)*
2. Decide on `league_h2h_fixtures` and `transfer_drafts` — add to the TRUNCATE list or
   document why not.
3. Add a test-database exemption to `_refuse_to_wipe_real_data()`, or set
   `ALLOW_GAMEPLAY_WIPE` in CI. *(Otherwise CI breaks — §6)*
4. Optionally drop `free_hit_squads` here, with the chip.

**Draft 2 (`d58b3f10a7c2`)**
5. Resolve `rules_version`: choose numeric-or-varchar and make the statement explicit
   rather than `IF NOT EXISTS`. *(Blocker 2)*
6. Fix the `downgrade()` — remove `DELETE FROM starting_xi WHERE position_slot > 11` and
   restore the check as `BETWEEN 1 AND 15`.
7. Correct the "old inline CHECK (slot 1-11)" comment; live is already 1–15.
8. Document that `tactic NOT NULL` requires draft 1's truncate to have run.

**Before Phase 1**
9. Answer §8 — how `pitchside_db` is migrated and what runs on the server.
10. Confirm the `user_gameweek_finance` / `gw_scores` consistency decision (§7).

---

## 11. Open questions I did not answer

- **`rules_version` numeric vs string** — a product decision with consequences for the
  990 existing rows and for `c9a04e7b53d1`'s intent.
- **`user_gameweek_finance` orphan rows** — keep and accept, or partially truncate.
- **`league_h2h_fixtures`** — 10,184 rows derived from scores being deleted.
- **Server state** — unverifiable from here (§8).

Nothing in Phase 1 should start until these are settled. Awaiting "confirmed".
