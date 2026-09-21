# Phase 1 report — migrations

Scope: fix the two draft migrations, rehearse on a scratch database, resolve the
H2H question, apply to `fpl_game_test` only, commit, stop. **`fpl_game` and the
server database are untouched and stay on their current schema until Phase 4.**

**Rules of evidence.** Every claim about database or Alembic state is followed by
the command and its quoted output. Claims I could not verify are labelled
*unverified*.

This revision supersedes the first Phase 1 report. **Round 2 corrected one claim the
first version got wrong — see §2.1.**

---

## 1. Result summary

| Step | Outcome |
|---|---|
| 1. `league_h2h_fixtures` investigation | derived columns are safely resettable — **reset added**, §2 |
| 2. Decision 1 evidence (fresh user) | **handled**, HTTP 200 with zeroes, §3 — *one earlier claim corrected* |
| 3. Rehearsal on a NEW scratch copy | guard refusal, schema, round trip, suite all pass, §4 |
| 4. Applied to `fpl_game_test` only | **ALL CHECKS PASSED**, 179 EXPECTED / 3 UNEXPECTED, §5 |
| 5. `fpl_game` and server untouched | **verified**, §8 |
| 6. `ALLOW_GAMEPLAY_WIPE` placement | **absent from `.env`**, §6 |
| 7. Commit | `2f0a8f8`, three files only, §7 |

---

## 2. `league_h2h_fixtures` — investigation and decision

### 2.0 Full column list

```
                                     Table "public.league_h2h_fixtures"
  Column   |         Type          | Nullable |                     Default
-----------+-----------------------+----------+-------------------------------------------------
 id        | integer               | not null | nextval('league_h2h_fixtures_id_seq'::regclass)
 league_id | integer               | not null |
 season    | character varying(9)  | not null |
 gameweek  | smallint              | not null |
 user_id_1 | integer               | not null |
 user_id_2 | integer               |          |
 points_1  | smallint              |          |
 points_2  | smallint              |          |
 result    | character varying(10) |          |
Check constraints:
    "league_h2h_fixtures_result_check" CHECK (result::text = ANY (ARRAY['win_1','win_2','draw','bye']))
```

**Pairing columns** (the schedule): `league_id, season, gameweek, user_id_1, user_id_2`.
**Derived columns**: `points_1, points_2, result`. No others.

### 2.1 Which columns are derived from `gw_scores` — and the `'bye'` trap

`Results/standings.py::_process_h2h_league` is the only writer. It reads every
fixture for the gameweek and overwrites all three, without skipping already-resolved
rows:

```python
points_1 = gw_score_rows[fx.user_id_1].total_points if fx.user_id_1 in gw_score_rows else 0
if fx.user_id_2 is None:
    points_2, result = None, "bye"
else:
    points_2 = gw_score_rows[fx.user_id_2].total_points if fx.user_id_2 in gw_score_rows else 0
    if points_1 > points_2:   result = "win_1"
    elif points_2 > points_1: result = "win_2"
    else:                     result = "draw"
conn.execute(RESOLVE_FIXTURE_STMT, {...})   # standings.py:304-320
```

and the insert at generation time writes **only** the pairing:

```sql
INSERT INTO league_h2h_fixtures (league_id, season, gameweek, user_id_1, user_id_2)
VALUES (:league_id, :season, :gameweek, :user_id_1, :user_id_2)   -- standings.py:117-122
```

So `points_1`/`points_2` are `gw_scores.total_points` and nothing else, and `result`
is a comparison of those two.

**The one thing worth checking carefully was `'bye'`**, because a bye is a property of
the *schedule*, not of anyone's score — if it lived only in `result`, resetting the
column would destroy pairing information. It does not. It is derived from
`user_id_2 IS NULL` at both ends:

- writer — `if fx.user_id_2 is None: result = "bye"` (`standings.py:310-311`)
- reader — `is_bye = row.user_id_2 is None` (`leagues.py:525`), which the API uses
  rather than reading `result`

The data agrees. `'bye'` occurs **only** where there is no opponent, and 1,224 rows
are already in the "bye pairing, not yet resolved" state:

```
 no_opponent | marked_bye | count
-------------+------------+-------
 f           | f          |   467
 f           |            |  8425
 t           | t          |    68     <- every 'bye' has no opponent
 t           |            |  1224     <- unplayed byes already look like this
```

The three derived columns are also perfectly correlated, so resetting them together
produces a state that already exists in bulk rather than a new hybrid one:

```
 result_null | points_null | count
-------------+-------------+-------
 f           | f           |   535
 t           | t           |  9649
```

### 2.2 How H2H standings are computed

**From the fixtures table, not recomputed from `gw_scores`** — and gated on
`result IS NOT NULL`:

```sql
SELECT user_id_1 AS user_id,
       CASE result WHEN 'win_1' THEN :win WHEN 'draw' THEN :draw
                   WHEN 'bye' THEN :bye ELSE :loss END AS match_points
FROM league_h2h_fixtures
WHERE league_id = :league_id AND season = :season AND result IS NOT NULL
UNION ALL ...                                      -- standings.py:138-153
```

A row with `result IS NULL` therefore contributes nothing at all. Resetting cannot
corrupt a standing; it zeroes it, which is the correct answer once `gw_scores` is
empty. `Results/leagues.py`'s `H2H_SEASON_RECORDS_QUERY` applies the same
`f.result IS NOT NULL` filter, with the reason stated in the source: *"without it
every member would read as having lost every gameweek still to come."*

### 2.3 What the API and screens return when `result IS NULL`

The endpoint models three states deliberately (`leagues.py:209-215`): unplayed,
bye, played. Called live against a league that has both kinds of round:

```
league_id=7  resolved_gw=1  unplayed_gw=3

--- RESOLVED round: GET /leagues/7/h2h?gameweek=1 -> HTTP 200
  {"fixture_id": 1, "side_1": {"user_id": 417, "points": 10, "is_current_user": true},
                    "side_2": {"user_id": 416, "points": 10, "is_current_user": false},
   "result": "draw", "is_bye": false, "outcome_for_current_user": "draw"}

--- UNPLAYED round: GET /leagues/7/h2h?gameweek=3 -> HTTP 200
  {"fixture_id": 5, "side_1": {"user_id": 417, "points": null, "is_current_user": true},
                    "side_2": {"user_id": 419, "points": null, "is_current_user": false},
   "result": null, "is_bye": false, "outcome_for_current_user": null}
```

HTTP 200 either way. The unplayed round renders as a scheduled fixture with null
points and a null result — both sides still named, because the pairing is intact.
`is_provisional` is defined as `status != "final" and any(f.result is not None ...)`,
so an all-null round is correctly *not* provisional: *"An unplayed gameweek is not
provisional — it has nothing to be provisional about."*

### 2.4 Verdict and the change made

Every derived column can be reset without changing who plays whom, so per your
instruction the reset is in `c41a9e27d06b`, and only those three columns move:

```sql
UPDATE league_h2h_fixtures
   SET points_1 = NULL, points_2 = NULL, result = NULL
 WHERE points_1 IS NOT NULL OR points_2 IS NOT NULL OR result IS NOT NULL
```

The guard now counts resolved rows — **conditionally**, which is the one subtlety
worth naming: an unplayed row loses nothing to this revision, so counting all 10,184
would make the guard fire on databases that have nothing to lose.

```python
# league_h2h_fixtures is counted CONDITIONALLY, unlike the tables above:
# this revision resets only its three derived columns and leaves the
# pairings alone, so an unplayed row loses nothing and must not trip the
# guard. A resolved row (result IS NOT NULL) does lose data, so it counts.
rows += conn.execute(
    sa.text("SELECT COUNT(*) FROM league_h2h_fixtures WHERE result IS NOT NULL")
).scalar() or 0
```

**Proof the pairings survive.** I fingerprinted the schedule before the migration and
re-checked it after, on both databases:

```
scratch   BEFORE: md5 2c4e184d3bde9472576304a4a4f10585  10184 rows  1292 byes  535 resolved
scratch   AFTER : PASS pairing fingerprint unchanged  '2c4e184d3bde9472576304a4a4f10585'
                  PASS total rows unchanged            10184
                  PASS bye pairings unchanged           1292
                  PASS rows with a result                  0
                  PASS rows with points                    0
```

Byte-identical. The same check passed on `fpl_game_test` against its own fingerprint
(`0d3bc00e…`, 50,084 rows, 6,840 byes) — §5.

**Downgrade cannot restore the results.** They are data, not structure, same as the
truncated tables. Recovery is the backup.

---

## 3. Decision 1 evidence — the fresh user

### 3.1 A correction to the previous report

The first Phase 1 report stated this state returns `team_value_available: false`.
**That was wrong, and the call it came from was malformed.** `GET /team` requires
both `season` and `gameweek`; without them it returns 422, not a dashboard:

```
--- GET /team -> HTTP 422
    detail  [{'type': 'missing', 'loc': ['query', 'season'], ...},
             {'type': 'missing', 'loc': ['query', 'gameweek'], ...}]
```

Here is the real output, on a user seeded with no rows in any of the three tables:

```
seeded user_id=4320; confirming the three tables are empty FOR THIS USER:
  gw_scores                0 rows
  user_gameweek_finance    0 rows
  gw_selections            0 rows

--- GET /team?season=9999-00&gameweek=1 -> HTTP 200
    user_id                  4320          season                   '9999-00'
    username                 'gate_dd72a9e02a'                      gameweek 1
    team_name                'Gate FC'      deadline                None
    has_lineup               False          chip_used               None
    captain_multiplier       2              gw_points               0
    gw_average               23.2           season_total            0
    has_score                False          raw_points              0
    captain_bonus            0              transfer_hits           0
    hit_deductions           0              final_total             0
    live_status              'upcoming'     overall_rank            None
    overall_rank_total       None           team_value_available    True
    team_value               0.0            bank                    0.0
    lineup                   <dict, len=4>  bench                   <list, len=0>
```

### 3.2 Is the state handled?

**Yes.** HTTP 200, no error, `has_lineup: False`, and every points and money figure
zero rather than stale. The concern that drove reversing the "keep finance" decision —
a real bank displayed beside 0 points — does not arise: `bank` and `team_value` are
both `0.0` precisely because the finance rows are gone.

**One cosmetic wart, flagged not fixed:** `team_value_available` is `True` while
`team_value` is `0.0`, so the field claims the figure is available and reports £0.0m
rather than "unknown". That is a display nuance for Phase 2, not a failure of this
state — nothing crashes and nothing stale is shown. The migration comment records the
accurate behaviour.

---

## 4. Rehearsal on a NEW scratch copy

Dropped and recreated from a fresh dump, as instructed:

```
pg_dump -U postgres fpl_game -f fpl_game_backup.sql      # 2,994,855 bytes
dropdb -U postgres --if-exists fpl_game_scratch
createdb -U postgres fpl_game_scratch
psql -U postgres -d fpl_game_scratch -f fpl_game_backup.sql

 current_database | gw_scores | finance |  h2h  | h2h_resolved
------------------+-----------+---------+-------+--------------
 fpl_game_scratch |       990 |      97 | 10184 |          535

 version_num
--------------
 a06f58d93f5a
 c2f6a83e91d4
```

### 4.1 Guard refusal

```
=== guard refusal test: ALLOW_GAMEPLAY_WIPE unset ===
RuntimeError: Refusing to run: database 'fpl_game_scratch' holds 3657 gameplay rows
and this revision would wipe them. If this is a dev database you are happy to empty,
re-run with ALLOW_GAMEPLAY_WIPE=fpl_game_scratch

=== revision unchanged after refusal ===
c2f6a83e91d4
a06f58d93f5a
```

**3,657 = 3,122 + 535.** The delta is exactly the resolved H2H rows, confirming the
conditional count includes them and correctly ignores the 9,649 unplayed. The
revision did not move.

### 4.2 Upgrade and schema verification

```
INFO  Running upgrade a06f58d93f5a, c2f6a83e91d4 -> c41a9e27d06b, ...
INFO  Running upgrade c41a9e27d06b -> d58b3f10a7c2, ...
d58b3f10a7c2 (head)
```

Every item asserted individually — **ALL CHECKS PASSED**. Added: `tactic`,
`is_bonus`, `role`, `tactical_points`, `sub_bonus`, `tactical_swaps`, 6 constraints,
4 triggers. Dropped: `captain_id`, `vice_captain_id`, `chip_used`, `is_captain`,
`is_vice_captain`, `transfer_hits`, `hit_deductions`, table `chips`, trigger
`enforce_chip_limit` + its function. Deliberately unchanged:

```
starting_xi_position_slot_check :: CHECK (((position_slot >= 1) AND (position_slot <= 15)))
ck_starting_xi_slot NOT created   0
gw_scores.rules_version type      'smallint'
tactic is_nullable                'NO'
tactic column_default             None      <- DEFAULT dropped
```

### 4.3 Downgrade round trip

```
=== downgrade -1 ===
INFO  Running downgrade d58b3f10a7c2 -> c41a9e27d06b
c41a9e27d06b (mergepoint)

=== a second -1 (expected to fail at the mergepoint) ===
ERROR [alembic.util.messaging] Ambiguous walk
FAILED: Ambiguous walk

=== downgrade a06f58d93f5a (the working form) ===
INFO  Running downgrade c41a9e27d06b -> a06f58d93f5a, c2f6a83e91d4
c2f6a83e91d4
a06f58d93f5a
```

Then `upgrade head` again → `d58b3f10a7c2 (head)`, and verification reports
**ALL CHECKS PASSED** a second time, pairing fingerprint still
`2c4e184d3bde9472576304a4a4f10585`.

### 4.4 Full suite on the migrated scratch schema

```
156 failed, 471 passed, 4 skipped, 3 warnings, 27 errors in 28.17s
```

658 tests, 183 failures+errors: **179 EXPECTED, 4 UNEXPECTED** (all four pre-existing,
§4.5).

| EXPECTED cause | Count |
|---|---|
| removed table `chips` | 66 |
| removed column `chip_used` | 53 + 4 masked = 57 |
| removed column `captain_id` | 34 |
| removed column `transfer_hits` | 22 |

The "masked" four are `test_gameweek_lifecycle` tests that assert only
`assert 500 == 200`. The app's handler turns a `ProgrammingError` into a generic 500,
so the assertion hides the cause; the captured server log names it:

```
ERROR Gameplay.transfers:transfers.py:678 Database write failed: ProgrammingError:
(psycopg2.errors.UndefinedColumn) column "chip_used" does not exist
LINE 1: SELECT is_locked, chip_used FROM gw_selections WHERE user_id...
```

**No failure is caused by a new object** — not `tactic`'s NOT NULL, not the triggers,
not `tactical_swaps`, not the H2H reset.

### 4.5 The four pre-existing failures, by name and reason

Proven by downgrading this same copy to the pre-migration revisions and re-running
them on identical data — all four fail there too (`4 failed in 0.86s`):

| Test | Reason it fails |
|---|---|
| `test_auth_enforcement::test_the_endpoint_table_covers_every_route_in_the_app` | `AssertionError: these routes are in neither ENDPOINTS nor PUBLIC_ROUTES, so nothing asserts whether they require a credential: [('PATCH', '/dream11/contests/{contest_id}/team')]` — the route was added in `aafe3ca` without being added to the auth table. Dream11, out of scope per Decision 7. |
| `test_auth_enforcement::test_every_dream11_route_is_in_the_authenticated_table` | Same root cause: `Extra items in the left set: ('PATCH', '/dream11/contests/{contest_id}/team')`. |
| `test_beat_scheduling::test_schedule_predictions_no_op_when_nothing_needs_predicting` | `AssertionError: assert ('2026-27', 6) is None`. `find_next_gameweek_needing_predictions` scans **all** seasons, but the test seeds only `TEST_SEASON` and expects `None`. The scratch copy carries real ingested `2026-27` fixtures with no predictions, so the function correctly finds one. A test-isolation gap that only shows on a database holding real fixture data — **it passes on `fpl_game_test`** (§5.3), which confirms the diagnosis. |
| `test_fpl_live_polling::test_ignores_fixtures_whose_kickoff_is_already_past` | `assert 2610 not in {2610}`. The test seeds its own fixture at `NOW() - 1h` with `finished=False` and expects `find_fixtures_needing_poll_schedule` to exclude it; the query returns it. A genuine behavioural mismatch, pre-existing and unrelated to Phase 1. It also fails on `fpl_game_test` (§5.3), and the id differs per database because it is the test's own freshly-inserted row. |

---

## 5. Applied to `fpl_game_test`

### 5.1 Backup, then the abort check

```
=== 4a. back up fpl_game_test FIRST ===
  wrote fpl_game_test_backup.sql, bytes: 6890045

=== 4b. ABORT CHECK: current_database() via the exact URL alembic will use ===
  URL              : postgresql://postgres:***@localhost:5432/fpl_game_test
  current_database : fpl_game_test
  VERDICT          : PROCEED
```

State before: 50,084 H2H rows (6,840 byes, 2,635 resolved), fingerprint
`0d3bc00e7f313236d1dbea470fbd9a6a`, guard total 25,297, at
`a06f58d93f5a` + `c2f6a83e91d4`.

### 5.2 Upgrade

`ALLOW_GAMEPLAY_WIPE=fpl_game_test` was set for this one command and cleared
immediately after:

```
INFO  Running upgrade a06f58d93f5a, c2f6a83e91d4 -> c41a9e27d06b, ...
INFO  Running upgrade c41a9e27d06b -> d58b3f10a7c2, ...
=== override cleared from this shell: '' ===
d58b3f10a7c2 (head)
```

Schema verification: **ALL CHECKS PASSED**, including every preserved table compared
to its own pre-migration count (not merely "> 0"):

```
=== PRESERVED tables (row count must be unchanged) ===
  PASS  league_h2h_fixtures  50084      PASS  users            17838
  PASS  user_squads            556      PASS  squad_players     4764
  PASS  mini_leagues          2292      PASS  transfer_drafts     54
  PASS  free_hit_squads          0

=== league_h2h_fixtures: pairings intact, derived columns reset ===
  PASS  pairing fingerprint unchanged   '0d3bc00e7f313236d1dbea470fbd9a6a'
  PASS  total rows unchanged             50084
  PASS  bye pairings unchanged            6840
  PASS  rows with a result                   0
  PASS  rows with points                     0
```

### 5.3 Full suite on `fpl_game_test`

```
155 failed, 471 passed, 5 skipped, 3 warnings, 27 errors in 29.34s
```

658 tests, 182 failures+errors: **179 EXPECTED, 3 UNEXPECTED.**

| EXPECTED cause | Count |
|---|---|
| removed table `chips` | 66 |
| removed column `chip_used` | 53 + 4 masked = 57 |
| removed column `captain_id` | 34 |
| removed column `transfer_hits` | 22 |

The four masked ones confirmed here too, same query:

```
ERROR Gameplay.transfers:transfers.py:678 Database write failed: ProgrammingError:
(psycopg2.errors.UndefinedColumn) column "chip_used" does not exist
LINE 1: SELECT is_locked, chip_used FROM gw_selections WHERE user_id...
```

**UNEXPECTED: 3**, all from §4.5's pre-existing list —
`test_the_endpoint_table_covers_every_route_in_the_app`,
`test_every_dream11_route_is_in_the_authenticated_table`,
`test_ignores_fixtures_whose_kickoff_is_already_past`.

The fourth is **absent here**, as predicted:

```
=== does test_beat_scheduling's predictions test pass here? ===
1 passed, 1 warning in 0.81s
```

confirming it was the dev database's real `2026-27` fixture data, not the migration.

**Zero migration-caused unexpected failures on either database.** No code was fixed.

---

## 6. `ALLOW_GAMEPLAY_WIPE` placement

```
=== item 6: is ALLOW_GAMEPLAY_WIPE in the main .env? ===
  absent from .env (correct)
  --- where it IS set ---
  .env.test:9: ALLOW_GAMEPLAY_WIPE=fpl_game_test
  .github\workflows\backend-tests.yml:52: ALLOW_GAMEPLAY_WIPE: fpl_game_test
```

Two places, both naming the test database. Neither names `fpl_game` or `pitchside_db`.

---

## 7. Commit, and the `.sql` files

Commit `2f0a8f8` on `tactical-game`, exactly three files:

```
IMPLEMENTATION_PLAN.md
backend/Migrations/versions/c41a9e27d06b_drop_classic_only_objects.py
backend/Migrations/versions/d58b3f10a7c2_add_tactical_game_structures.py
 3 files changed, 127 insertions(+), 28 deletions(-)
```

### The `.sql` files are NOT git-ignored

```
=== git check-ignore on the .sql artefacts ===
  NOT ignored  fpl_game_backup.sql
  NOT ignored  schema_dump.sql
  NOT ignored  fpl_game_test_backup.sql
=== any .sql rule in .gitignore? ===
  (no matches)
```

They stayed out of the commit only because I named the three files explicitly. They
remain untracked and visible to any future `git add -A`:

```
?? PHASE0_REPORT.md   ?? PHASE1_REPORT.md   ?? fpl_game_backup.sql
?? fpl_game_test_backup.sql                 ?? schema_dump.sql
```

`fpl_game_backup.sql` is 2.9 MB and `fpl_game_test_backup.sql` 6.9 MB of real user
data. **I have not added a `.gitignore` rule**, because item 7 limited the commit to
three files. Say the word and I will add one.

### One uncommitted change worth flagging

`.github/workflows/backend-tests.yml` carries the Decision 8 change
(`ALLOW_GAMEPLAY_WIPE: fpl_game_test`) and is **not** in this commit, since item 7
did not list it. **Consequence: once these migrations reach CI without it, the guard
will refuse and the CI job will fail**, because the CI database accumulates gameplay
rows. It needs to land in the same merge. Tell me whether to commit it.

---

## 8. What was and was not touched

| Database | State | Evidence |
|---|---|---|
| `fpl_game` | **untouched**, still `a06f58d93f5a` + `c2f6a83e91d4` | only ever read: `pg_dump`, and the `rules_version` / column-list queries |
| `fpl_game_test` | migrated to `d58b3f10a7c2 (head)` | §5, backed up first |
| `fpl_game_scratch` | at head, throwaway | §4 |
| `pitchside_db` (server) | **untouched**, unreachable | no command was issued against it — *unverified by instruction* |

Backups on disk: `fpl_game_backup.sql` (2,994,855 bytes),
`fpl_game_test_backup.sql` (6,890,045 bytes).

---

## 9. Stopping here

Phase 2 not started. Three things are open for you:

1. **`.gitignore` for the `.sql` dumps** — not ignored today (§7).
2. **The CI workflow change** — uncommitted, and CI will fail without it (§7).
3. **`team_value_available: true` beside `team_value: 0.0`** — cosmetic, noted for
   Phase 2 (§3.2).
