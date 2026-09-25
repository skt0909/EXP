# B1 anchor — option B: `ruleset_epochs`

**Rules of evidence.** Commands and their output are quoted.

---

## 1. Result

The anchor is now **stored, never derived**. Migration `e7c4d81b3a95`
(`down_revision = "d58b3f10a7c2"`, the head I was asked to report and confirmed
with `alembic heads`) creates an append-only `ruleset_epochs` table and writes
one row per database at release time.

| Step | Outcome |
|---|---|
| tests first | 10 of 11 failed before the table existed |
| migration + trigger + no FKs | done |
| epoch computed on the scratch copy | `season='2026-27' rules_version=3 first_gameweek=6` |
| verified against a manual `ml.fixtures` query | **matches** |
| round trip: upgrade → downgrade → upgrade | clean |
| applied to `fpl_game_test` | done; no fixtures, so nothing inserted (correct) |
| full suite | 866 collected, 122 failures+errors, **3 UNEXPECTED — unchanged** |

---

## 2. Two definitions I had to pin down, and one discrepancy

### The deadline — matches the app exactly

`Shared/deadlines.py`:

```python
DEADLINE_OFFSET_MINUTES = 90
_DEADLINE_EXPR = f"MIN(kickoff_time) - interval '{DEADLINE_OFFSET_MINUTES} minutes'"
```

The migration uses the same expression, restated as a literal rather than
imported — a migration is a historical record and must keep applying the same
way after the constant moves on. It is kept character-identical so the two can
be diffed by eye.

### Current season — a genuine discrepancy, reported not papered over

**The app's own source cannot be used.** `Data/fpl_ingest.py:187`:

```python
def current_live_season() -> str | None:
    """The season FPL's API is serving right now..."""
    return _season_from_events(fetch_json("bootstrap-static/").get("events"))
```

It asks the FPL API over the network. **A migration must not do that** — there
is no network in CI, and a schema change must not depend on a third party being
up. So the migration uses the database-side equivalent:

```sql
SELECT season FROM ml.fixtures
WHERE kickoff_time > now() AND season ~ '^[0-9]{4}-[0-9]{2}$'
ORDER BY kickoff_time LIMIT 1
```

The regex is doing real work. The scratch copy holds six seasons:

```
  season  | fixtures |           first_ko            |            last_ko
----------+----------+-------------------------------+-------------------------------
 2025-26  |       40 | 2025-08-15 20:00:00+01        | 2025-09-14 16:30:00+01
 2026-27  |      380 | 2026-08-21 20:00:00+01        | 2027-05-30 16:00:00+01
 9998-00  |        1 | 2026-01-10 15:00:00+00        | 2026-01-10 15:00:00+00
 SIM38OK  |       38 | 2026-09-11 18:26:05.561703+01 | 2026-09-11 18:26:07.374816+01
 SIM38TST |       38 | 2026-09-11 18:25:23.864116+01 | 2026-09-11 18:25:25.51583+01
 SIMSMOKE |        1 | 2026-09-11 18:25:17.166711+01 | 2026-09-11 18:25:17.166711+01
```

`SIM38OK`, `SIM38TST` and `SIMSMOKE` belong to the `simulation/` harness and
the plan says never to treat them as game data. **Nothing in the code filters
them today** — `git grep` for `SIM38|SIMSMOKE` across `backend/**/*.py` returns
no matches, so the guardrail lived only in the plan until now. The season-format
regex is how this migration honours it. The past-dated `2025-26` and `9998-00`
are excluded by the `kickoff_time > now()` clause rather than by the regex.

**This is a second definition of "current season" in the codebase.** It agrees
with the API one while a season is running and disagrees between seasons
(the API returns `None`; this returns the upcoming season as soon as its
fixtures are ingested). For choosing a release epoch that is the behaviour you
want, but it is a divergence worth knowing about.

---

## 3. The computed epoch, verified independently

Migration output on the scratch copy of `fpl_game`:

```
NOTICE: ruleset_epochs -- computed season='2026-27' rules_version=3 first_gameweek=6.
Stored row: season='2026-27' rules_version=3 first_gameweek=6
created_at=2026-09-21 15:29:40.669109+01:00
```

Checked against `ml.fixtures` by hand, with the database clock at
`2026-09-21 15:26`:

```
 gameweek |        deadline        | still_ahead
----------+------------------------+-------------
        4 | 2026-09-12 13:30:00+01 | f
        5 | 2026-09-18 18:30:00+01 | f
        6 | 2026-10-10 11:00:00+01 | t
        7 | 2026-10-17 11:00:00+01 | t
```

Gameweek 5's deadline has passed, Gameweek 6's has not. **6 is correct under
rule D5.**

Round trip:

```
Running upgrade d58b3f10a7c2 -> e7c4d81b3a95   (first_gameweek=6)
Running downgrade e7c4d81b3a95 -> d58b3f10a7c2
d58b3f10a7c2
Running upgrade d58b3f10a7c2 -> e7c4d81b3a95   (first_gameweek=6)
```

The append-only guard, on the scratch copy:

```
ERROR:  ruleset_epochs is append-only: UPDATE on (season=2026-27, rules_version=3)
is not allowed. The first gameweek of a ruleset is a historical fact -- correcting
it means inserting a new rules_version, not rewriting this one.
```

### `fpl_game_test`

```
Running upgrade d58b3f10a7c2 -> e7c4d81b3a95
NOTICE: ruleset_epochs -- no future fixture in any real season, so no current
season to anchor. Inserting nothing; this season will be read as starting under
these rules at gameweek 1. Expected on a fresh CI database and between seasons.
e7c4d81b3a95 (head)
```

Correct: the test database's fixtures are created and torn down per test, so at
migration time there were none. **`fpl_game` and the server were not migrated**
— their epochs must be computed at their own release time, which is the whole
point of storing it per database.

---

## 4. How the code reads it

`Gameplay.transfers.ruleset_first_gameweek(conn, season)` returns the stored
`first_gameweek` for `(season, RULES_VERSION)`, or `FIRST_GAMEWEEK` when there
is no row. A missing row is **not** an error: it means the database has never
known another ruleset, which is true of CI and of any season that began under
these rules.

Transfers recorded **before** the epoch are dropped from the recurrence and
logged:

```python
logger.warning(
    "user_id=%s season=%s: %s transfer(s) recorded in gameweek(s) %s, "
    "before the ruleset epoch (first_gameweek=%s). Ignored by the free-transfer "
    "recurrence. This should not happen: the ruleset starts between gameweeks.",
    ...)
```

They cannot consume an allowance that did not exist yet, and they should not
exist at all, so each one is a data-integrity signal rather than a silent skip.

The recurrence itself stays pure — `_tactical_free_transfers_available` takes
the start Gameweek as a parameter and touches no database.

---

## 5. Tests

11 new, in `backend/Tests/test_ruleset_epochs.py`, all passing. 10 of the 11
failed before the table existed.

| Asked for | Test |
|---|---|
| no epoch row → `FIRST_GAMEWEEK` behaviour | `test_no_epoch_row_falls_back_to_first_gameweek`, `test_without_an_epoch_the_allowance_behaves_as_it_did_before` |
| epoch 7 → 1 at GW7 | `test_an_epoch_of_seven_gives_one_at_gameweek_seven` |
| epoch 7 → 2 at GW8, nothing used | `test_an_epoch_of_seven_gives_two_at_gameweek_eight_with_nothing_used` |
| earlier transfer ignored and warned | `test_a_transfer_before_the_epoch_is_ignored_and_warned` |
| UPDATE and DELETE raise | `test_updating_a_row_raises`, `test_deleting_a_row_raises` |
| deleting a user does not move anyone's allowance | `test_deleting_a_user_does_not_move_any_other_managers_allowance` |
| re-running creates no duplicate | `test_reinserting_the_same_epoch_is_a_no_op_not_a_duplicate` |

Plus `test_the_table_has_no_foreign_keys_so_nothing_cascades_into_it`, which
pins the structural property the whole option rests on.

**`test_deleting_a_user_does_not_move_any_other_managers_allowance` is the point
of the change.** It seeds a victim holding the earliest `gw_selections` row,
reads a second manager's allowance, deletes the victim, and reads it again.
Under the rejected anchor the second read would have differed. It does not.

---

## 6. Suite

| | Before | Now |
|---|---|---|
| collected | 855 | 866 |
| failures + errors | 122 | **122** |
| EXPECTED | 119 | **119** |
| **UNEXPECTED** | **3** | **3** |

+11 tests, all passing. No new failures, no new UNEXPECTED.

---

## 7. Things worth knowing

**C1. The epoch depends on when you run the migration.** It is computed from
`now()`, so migrating outside the release window stores a Gameweek that is
already under way. The table is append-only, so that cannot be corrected in
place — it needs a new `rules_version`. The release checklist in section 10 now
says to migrate inside the window and to check the row afterwards, but the
migration itself cannot enforce it.

**C2. A second "current season" definition now exists**, as described in §2. It
agrees with the API one during a season and differs between seasons.

**C3. The test fixture disables the trigger to clean up.** `test_ruleset_epochs.py`'s
`epoch` fixture runs `ALTER TABLE ... DISABLE TRIGGER` to delete its own rows.
That is a demonstration that ordinary code cannot remove them, but it does mean
the test suite holds the one piece of code that can. It needs table-owner
rights, which the test role has.

**C4. `downgrade()` loses the epoch.** It is data, not schema. Re-upgrading
recomputes it from whatever fixtures exist at that later moment, which will not
be the same answer. Downgrading past this revision after a real release loses
the record of when the ruleset began — noted in the migration's own docstring.
