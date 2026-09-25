# Phase 4a report — the tactical engine in the live scoring job

Scope: re-pin the concurrency tests, then replace the per-manager scoring loop
with a batched job running `Results/tactical_scoring.py`. No migrations. The
dashboard, the frontend, Dream11 and the classic functions the dashboard
imports were not touched.

**Rules of evidence.** Commands and their output are quoted. Claims I could not
verify are labelled *unverified*.

---

## 1. Step 1 — the three concurrency tests, re-pinned first

All three now pass, unskipped. `5 passed` for the file.

They were rewritten rather than patched, because the rule they assumed is gone:

| Test | Before | Now |
|---|---|---|
| `..._cannot_both_spend_the_last_free_transfer` | both 200, one stamped paid | **one 200, one 422** — there is no paid fallback |
| `..._cannot_both_spend_the_same_budget` | gameweek 1 | **gameweek 3**, where the bank has reached 2, so both batches are within the allowance and race for the *budget* rather than being rejected for the allowance |
| `test_serialised_batches_are_unaffected_by_the_lock` | one free, one paid | **one free, one rejected** — the serial outcome the concurrent case must match |

The first is now **stricter** than before: a lock failure used to cost the
manager 4 points, and would now hand out a transfer that does not exist.

### They still pin the guarantee — proven by mutation

The advisory lock was replaced with `SELECT 1` and the file re-run:

```
=== tests WITH THE LOCK DISABLED (must fail) ===
FAILED ..._cannot_both_spend_the_last_free_transfer
FAILED ..._cannot_both_spend_the_same_budget
2 failed, 3 passed
```

Restored and verified byte-identical by SHA-1
(`97139678e5a06c2e7810f26a85fec039ab18ae82`).

---

## 2. Step 2 — the job as it was, with file and line

**Active window.** `GameEngine/gameweek_finalize.py:51`,
`DEFAULT_ACTIVE_WINDOW_DAYS = 5`; `find_active_gameweeks(engine, window_days)`
at `:128` and `refresh_active_gameweeks` at `:134`, which calls
`score_gameweek` at `:148` (now `:151`). A gameweek is active for a fixed
window after its first kickoff — a time window, not a flag. **Unchanged by 4a.**

**The upsert.** `Results/scoring.py:157-174`, `INSERT INTO gw_scores ... ON
CONFLICT (user_id, season, gameweek) DO UPDATE SET ...`. Idempotent by the
unique key, which matters because live scoring runs repeatedly as a gameweek
progresses. **Preserved exactly**, with the column list changed to the tactical
one.

**`season_total`.** `Results/scoring.py:147-150`:

```sql
SELECT COALESCE(SUM(total_points), 0) FROM gw_scores
WHERE user_id = :user_id AND season = :season AND gameweek < :gameweek
```

Recomputed fresh each run rather than trusting a stored running total.
**Preserved**, widened to a batch with `GROUP BY user_id`.

**`scoring.py:200` — the finance write.** The line itself is the `INSERT INTO
user_gameweek_finance` of `UPSERT_GW_FINANCE_STMT` (`:198-207`):

```sql
INSERT INTO user_gameweek_finance (user_id, season, gameweek, bank, team_value)
VALUES (:user_id, :season, :gameweek, :bank, :team_value)
ON CONFLICT (user_id, season, gameweek) DO UPDATE SET
    bank = EXCLUDED.bank, team_value = EXCLUDED.team_value, captured_at = now()
```

`bank` is `user_squads.budget_remaining`; `team_value` is
`SUM(squad_players.purchase_price) FILTER (WHERE is_active)` — **what was PAID
for the active squad, not what it could be sold for** (`:176-192`). Copied
verbatim into the new job, and pinned by two tests.

**Was it too tightly coupled to swap safely?** No, and the reason is worth
stating: `score_gameweek` is a thin loop over `_score_one_user`, and the four
production callers only read `summary["scored"]`. The new job keeps the name,
the signature and the return shape, so no caller changed.

---

## 3. Step 3 — the new job

`backend/Results/scoring_job.py`, `score_gameweek_tactical(engine, season,
gameweek, batch_size=SCORING_BATCH_SIZE)`. `SCORING_BATCH_SIZE = 200`.

**Callers repointed** — `GameEngine/gameweek_finalize.py:49` and
`Worker/tasks.py:117`, both as `score_gameweek_tactical as score_gameweek`, so
the three call sites are untouched.

### The one stats query per batch

```sql
SELECT mp.fpl_id AS player_id, mp.position,
       pgs.minutes, pgs.goals_scored, pgs.assists, pgs.clean_sheets,
       pgs.goals_conceded, pgs.saves, pgs.penalties_saved,
       pgs.penalties_missed, pgs.own_goals, pgs.yellow_cards,
       pgs.red_cards, pgs.defensive_contributions, pgs.creativity
FROM ml.players mp
LEFT JOIN ml.player_gw_stats pgs
       ON pgs.player_id = mp.id
      AND pgs.season = :season
      AND pgs.gameweek = :gameweek
WHERE mp.season = :season
  AND mp.fpl_id = ANY(:player_ids)
```

`LEFT JOIN`, not `JOIN`: a player with no stats row has not played, which is
normal mid-gameweek and must come back as a zero rather than dropping him out
of the squad. Positions come from `ml.players.position`. The join is on
`ml.players.id` while `starting_xi` stores the FPL id, so `fpl_id` is selected
back out to key the result — **these are not interchangeable**, and conflating
them produces a squad with no stats and a silent zero.

**`creativity` is passed through as the `Decimal` psycopg2 returned**, never
`float()`ed. `tier_points` refuses floats, so a lost-precision value would fail
loudly rather than quietly deciding whether 39.9 clears 40.

### What is written

`raw_points` = General Points, plus `tactical_points` and `sub_bonus`;
`final_points` = the sum; `total_points` = `final_points` (no hits);
`rules_version` = `RULES_VERSION` (3); `season_total` as before.

**Confirmed: everything downstream reads `total_points` only.** Outside the
scorer and the dashboard, `git grep` finds exactly one read of `gw_scores`:

```
backend/Results/standings.py:77:
    "SELECT user_id, total_points, season_total FROM gw_scores "
```

League tables, leaderboard snapshots and H2H all route through
`standings.py`, so none of them reads `raw_points`, `final_points` or the
dropped hit columns.

### Season filter, epoch gate, advisory validation

`REAL_SEASON_RE = ^\d{4}-\d{2}$`. `SIM38OK`, `SIM38TST` and `SIMSMOKE` return
`{"scored": [], "skipped_reason": ...}` without touching a row.

The epoch is read from `ruleset_epochs` for `(season, RULES_VERSION)`; a
gameweek before `first_gameweek` is not scored; **no row means score from
gameweek 1**.

`validate_selection` runs in warning-only mode. An invalid stored selection
**logs a data-integrity warning and is still scored** (decision A7).

---

## 4. Step 4 — tests

`13 passed` in `backend/Tests/test_scoring_job_tactical.py`. They failed first
on `ModuleNotFoundError: No module named 'Results.scoring_job'`.

| Asked for | Test |
|---|---|
| (a) golden gameweek, 3 managers, 3 tactics, swap + Auto Sub + Bonus no-show | `test_golden_gameweek_three_managers_three_tactics` |
| (b) idempotent | `test_running_twice_produces_identical_rows` |
| (c) batch boundary, 5 managers at size 2 vs 200 | `test_batch_size_does_not_change_the_result` |
| (d) SIM seasons ignored | `test_simulation_seasons_are_never_scored` (×3), `test_the_real_season_pattern_is_what_decides` |
| (e) gameweek before the epoch | `test_a_gameweek_before_the_epoch_is_not_scored`, `test_no_epoch_row_means_score_from_gameweek_one` |
| (f) invalid selection scored + warned | `test_an_invalid_stored_selection_is_scored_and_logs_a_warning` |
| (g) finance still written | `test_user_gameweek_finance_is_still_written`, `test_the_finance_write_is_also_idempotent` |
| (h) no selection → no score row | `test_a_manager_with_no_selection_gets_no_score_row` |

**(h)'s evidence:** the driving query is
`SELECT ... FROM gw_selections WHERE season = :season AND gameweek = :gameweek`.
A manager without a row is never in the result set — not excluded by a
condition that could be got wrong. Same shape as the old job's
`GW_SELECTIONS_QUERY`.

The golden gameweek's arithmetic is written out in the test's docstring, hand
by hand, so a change to the point values fails with the numbers visible:
manager A (balanced) 27 / 4 / 0 → 31; manager B (defence, swap + Auto Sub)
41 / 5 / 1 → 47; manager C (attack, Bonus no-show) 30 / 6 / 0 → 36.

---

## 5. Step 5 — full-suite comparison by test identity

`8aa1c1d` (pre-4a) against the current tree, both on `fpl_game_test`, same
interpreter, only the code differing. The pre-4a commit was checked out into a
`git worktree` so the working tree was never modified.

**Counting.** pytest emits one `<testcase>` element per test *phase*, so a test
that fails then errors in teardown yields two elements with the same id. Each
test is counted **once**, by precedence `error > failure > skipped > passed`,
with error and failure collapsed into one `fail` bucket.

| Transition | Count |
|---|---|
| pass → pass | 708 |
| **pass → fail** | **0** |
| pass → skip | 0 |
| fail → fail | 108 |
| skip → skip | 34 |
| **skip → pass** | **3** |
| new pass | 13 |
| **removed** | **0** |

```
sums to AFTER : 866 (actual 866) OK
sums to BEFORE: 853 (actual 853) OK
```

The 3 `skip → pass` are the re-pinned concurrency tests. The 13 `new pass` are
the scoring-job tests.

**Failures: 108 total — 105 EXPECTED, 3 UNEXPECTED.**

| EXPECTED cause | Count |
|---|---|
| removed column `captain_id` | 37 |
| removed column `transfer_hits` | 22 |
| removed table `chips` | 19 |
| removed column `chip_used` | 16 |
| new NOT NULL column `tactic` | 10 |
| removed column `vice_captain_id` | 1 |

The 3 UNEXPECTED are the same pre-existing ones as every phase since Phase 1:
two `PATCH /dream11/contests/{contest_id}/team` auth-table entries and
`test_ignores_fixtures_whose_kickoff_is_already_past`. **No test broke.**

---

## 6. Step 6 — memory and duration (report only)

1,000 synthetic managers over a 300-player pool, in `fpl_game_test` under a
dedicated season code, torn down afterwards:

```
seeding 1000 managers over a 300-player pool ...
  gw_selections rows: 1000

batch_size=  50  scored=1000  failed=0   duration=  2.32s  peak_python_memory=   0.79 MiB
batch_size= 200  scored=1000  failed=0   duration=  1.53s  peak_python_memory=   1.16 MiB
```

Memory tracks the batch size as intended — 4× the batch for 1.5× the peak —
and both are trivially within a 1 GB e2-micro. The larger batch is ~34% faster,
from fewer round trips.

**Caveat, stated rather than implied:** `tracemalloc` measures *Python*
allocations. It does not see libpq's own buffers or the driver's C-level
result sets, so these figures are a floor on real process usage, not a ceiling.
The relative shape — memory bounded by, and proportional to, the batch — is what
the measurement establishes.

---

## 7. Ambiguities — none resolved silently

**E1. `Tools/fpl_sim.py` still calls the CLASSIC scorer.** I deliberately did
not repoint it: it drives the `SIM38*` seasons, which the new job refuses by
design. Repointing it would break the simulation harness. So two scorers are
reachable, and which one runs depends on the entry point. Phase 4b should
decide whether `fpl_sim` gets an override or the harness moves to real-shaped
season codes.

**E2. `validate_selection` re-checks swap timing, which D4 says must never be
re-checked.** It is the same function the endpoint uses — which is the point,
because every *other* rule it checks is what we want to hear about. I satisfy
the timing rule by construction: each player is given one notional fixture and
each swap's incoming player a later one. That is a workaround. A
`check_timing=False` flag would be honest, but it changes a Phase 3 signature.

**E3. Two scorers now coexist.** `Results/scoring.py::score_gameweek` is still
on disk and still called by ~30 tests. Production no longer uses it. It cannot
be deleted in 4a because `team_dashboard.py` imports `resolve_autosubs` from
the same module.

**E4. A batch write is one transaction; the old job committed per manager.** If
a batch's write fails, all managers in that batch are unwritten while earlier
batches are already committed — a partial gameweek. The old job's blast radius
was one manager. Errors *inside* scoring are still isolated per manager; this is
only about the write itself.

**E5. `season_total` sums across rule generations.** It is
`SUM(total_points) WHERE gameweek < :gameweek`, and those earlier rows may carry
`rules_version` 1 or 2. A season that changes rulesets mid-way therefore has a
cumulative total spanning two scoring systems. That follows from D5 (earlier
gameweeks stay blank) only if they really are blank; if any are not, the total
mixes them silently.

**E6. A player in a selection but absent from `ml.players` for that season
fails the whole manager** with a `KeyError` from the engine's position lookup.
Surfaced during development by leftover test data. It is per-manager isolated
and logged, but the manager gets no score that run — arguably it should score
the rest and warn, like A7's invalid-selection handling.

**E7. `total_points` and `final_points` are now always equal.** With hits gone,
the two columns can never differ. Keeping both preserves the dashboard's
breakdown contract, but one of them is now redundant.

---

## 8. What 4a did not do

- `Results/scoring.py` is byte-unchanged, including `resolve_autosubs`.
- `team_dashboard.py`, the frontend and Dream11 were not touched.
- No migration was written or run.
- Only `fpl_game_test` was touched, by the test suite and the memory harness.
