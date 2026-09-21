# Phase 4c report — removing the classic scorer and its leftovers

**Rules of evidence.** Commands and their output are quoted.

---

## A — `Results/scoring.py` is deleted (E3)

### Migrated into the tactical test files

Four behaviours the engine must still have, rewritten end to end through the
job in `backend/Tests/test_scoring_job_tactical.py`:

| Deleted test | Replacement |
|---|---|
| `test_gk_autosub_swaps_in_bench_gk` | `test_migrated_gk_autosub_swaps_in_the_bench_keeper` |
| `test_outfield_autosub_respects_formation` | `test_migrated_outfield_autosub_respects_formation` |
| `test_no_valid_autosub_stays_at_zero` | `test_migrated_no_valid_autosub_leaves_the_slot_at_zero` |
| `test_no_user_squads_row_skips_finance_snapshot_without_error` | `test_migrated_a_manager_with_no_user_squads_row_scores_without_a_finance_snapshot` |

### Deleted as classic-only

| Test | Why |
|---|---|
| `test_captain_played_gets_2x` | captaincy is removed |
| `test_captain_zero_minutes_vice_gets_multiplier` | captaincy is removed |
| `test_neither_captain_nor_vice_played_no_multiplier` | captaincy is removed |
| `test_triple_captain_chip_applies_3x` | chips are removed |
| `test_bench_boost_chip_counts_all_15` | chips are removed |
| `test_transfer_hits_deduct_points` | hits are removed |

### Deleted as already covered

| Test | Covered by |
|---|---|
| `test_simple_valid_case_no_autosub` | the unit engine baseline |
| `test_user_without_gw_selection_is_skipped` | `test_a_manager_with_no_selection_gets_no_score_row` |
| `test_rerunning_is_idempotent` | `test_running_twice_produces_identical_rows` |
| `test_season_total_sums_across_gameweeks` | the two E5 `season_total` tests |
| `test_scoring_writes_finance_snapshot_matching_live_squad_state` | `test_user_gameweek_finance_is_still_written` |
| `test_rescoring_upserts_finance_snapshot_not_duplicates` | `test_the_finance_write_is_also_idempotent` |

### `test_scoring_rules.py` rewritten against `general_points`

One test got **simpler**. The classic `_component_score` deferred to FPL's own
`total_points` when a row carried minutes and nothing else, so appearance
points could only be reached by seeding a spare bonus point and netting it out.
The tactical engine has no fallback — the point table **is** the rule — so
minutes alone is enough.

### `simulation/` had three importers that would have broken silently

Nothing there runs in the suite. `production.py` and `real_stack_checks.py` now
call `score_gameweek_tactical` (with `allow_sim_seasons=True`, since that
harness drives the SIM seasons); `test_production_scoring_contract.py` uses
`general_points`.

### Confirmed

```
=== exact-word import check ===
  (only DASHBOARD_DATA.md and PHASE4B_REPORT.md, quoting history)
```

**No Python module imports `Results.scoring` anywhere in the repo.**

---

## B, C, D — the transfer leftovers

**B5.** `MAX_TRANSFERS_PER_GAMEWEEK` and its check are gone. The allowance caps
at 2, so a 20-per-gameweek cap could never fire — and a constant nothing can
reach is worse than no constant, because it reads as a live rule. Deleted with
it: `test_normal_gameweek_rejects_transfer_after_twenty_already_made` and
`test_wildcard_gameweek_allows_more_than_twenty_transfers`.

**B6.** The `FREE_CHIPS` import and the `chip_active` placeholder are gone —
**including one I had missed in `GET /transfers/used`**, which was still reading
`gw_selections.chip_used`, a column dropped in Phase 1.

`chip_active` **survives as a response key**, now a literal `False`. It is
client-facing and Phase 5 owns the frontend, so it stays inert rather than
vanishing from the payload — the same stance the dashboard takes with its
captaincy keys.

**B7.** `_validate_transfers`' `allowance` is required. The default of 0 meant a
caller that forgot it got a confusing "0 free transfers available" rejection
instead of a crash.

The module's DESIGN NOTE claimed the file could not be split because chip state
decided the cap, the free slots and `is_free`. None of that is true any more,
so it now states what is actually load-bearing.

Three more classic-only tests deleted: `test_wildcard_chip_makes_every_transfer_free`,
`test_transfers_used_wildcard_reports_uncapped_free`,
`test_a_chip_gameweek_preserves_the_bank`.

---

## E — `revert_free_hits`

Deleted: the task, the `"revert-free-hits"` Beat entry, the `beat_registry`
import, and `GameEngine/free_hit_revert.py`.

**Five other files imported it** and would have broken:
`Worker/beat_registry.py`, `Tools/fpl_sim.py` (which also mapped it in its task
table) and three test modules. `Gameplay/starting_xi.py` was importing
`restore_free_hit_snapshot` without using it — a Phase 3 leftover.

The seven `test_revert_free_hit_*` tests go with the feature.

---

## F — the migration

**`f2b9c05e7a41`**, `down_revision = "e7c4d81b3a95"` (the head, as reported by
`alembic heads`).

Before writing it, four dead `text()` statements in `starting_xi.py` still
**named** the table without ever executing, and `fpl_sim.py` had a
`FREE_HIT_STATE_QUERY` it still ran. All removed first.

Checked and quoted in the migration: **no triggers, no incoming foreign keys**.

Rehearsed on a scratch copy recreated from a fresh `pg_dump` of `fpl_game`:

```
Running upgrade e7c4d81b3a95 -> f2b9c05e7a41   -> free_hit_squads count: 0
Running downgrade f2b9c05e7a41 -> e7c4d81b3a95 -> count: 1, and all three indexes back:
    free_hit_squads_pkey
    idx_free_hit_squads_pending
    uq_free_hit_squads_user_season_gw_player
Running upgrade e7c4d81b3a95 -> f2b9c05e7a41   -> count: 0
```

Then applied to `fpl_game_test` only (`f2b9c05e7a41 (head)`).

**`fpl_game` is untouched**, still on `a06f58d93f5a` + `c2f6a83e91d4`.

---

## G — remaining captain / vice / chip references in classic files

Counts from `git grep -c`, Dream11, tests and migrations excluded.

| File | Hits | Verdict |
|---|---|---|
| **`Gameplay/chips.py`** | 33 | **DEAD NOW, but removing it is client-facing.** The router is still registered at `Context_assembler/main.py:174`, so **`GET /chips/used` is a live endpoint querying the dropped `chips` table** — it will 500. This is the one finding here worth acting on soon. Grouped with Phase 5 because deleting a route breaks any caller. |
| `Gameplay/starting_xi.py` | 45 | **Mostly dead now.** `DELETE_CHIP_FOR_GW_STMT` and `INSERT_CHIP_STMT` (lines 268-275) are `text()` statements naming the dropped `chips` table that nothing executes, plus the `from Gameplay.chips import (...)` at line 175. The rest is the module docstring describing the old chip flow. Safe to delete with `chips.py`. |
| `Results/team_dashboard.py` | 24 | **Phase 5.** The inert response keys (`chip_used`, `captain_multiplier`, `captain_bonus`, `is_captain`, `is_vice_captain`) kept deliberately so the current frontend renders. |
| `Shared/rules.py` | 16 | **Phase 5.** `CAPTAIN_MULTIPLIER`, `TRIPLE_CAPTAIN_MULTIPLIER`, `RESTRICTED_CHIPS`, `VALID_CHIPS`, `FIRST_HALF_LAST_GAMEWEEK`. `Data/scoring_rules.py` still publishes the multipliers, so they cannot go until that endpoint's contract changes. |
| `Context_assembler/main.py` | 18 | **Split.** The `chips_router` registration is dead now (see above). The `[CURRENT CAPTAIN]` LLM prompt tagging and `CAPTAIN_QUERY` read `gw_selections.captain_id`, a dropped column — dead now, and worth fixing because it is a live code path that will error. |
| `Data/scoring_rules.py` | 8 | **Phase 5.** `GET /scoring-rules` publishes `captain_multiplier` and `triple_captain_multiplier` as part of its response contract. |
| `Gameplay/transfers.py` | 16 | **Phase 5 for the key**, the rest is the DESIGN NOTE recording what used to exist. |
| `Tools/fpl_sim.py` | 9 | **Dead now**, a dev tool. Low priority. |
| `Gameplay/lineup.py`, `Gameplay/transfer_drafts.py`, `Predict/prediction_scheduling.py`, `Worker/tasks.py` | 1 each | Passing mentions in comments. Harmless. |
| `Results/tactical_scoring.py` | 2 | The module docstring saying this game has **no** captain and **no** chips. Correct as written. |

**The two that will actually error in production today** are
`GET /chips/used` (dropped `chips` table) and `Context_assembler/main.py`'s
`CAPTAIN_QUERY` (dropped `captain_id`). Neither was in this phase's brief, so
neither was changed.

---

## H — full suite by test identity

`ac5127a` (pre-4c) against the current tree. Counting: pytest emits one
`<testcase>` per test *phase*, so a test that fails then errors in teardown
yields two elements with the same id; each test is counted once by precedence
`error > failure > skipped > passed`, with error and failure collapsed to
`fail`. EXPECTED means the failure names a column or table the Phase 1
migration removed, read from the assertion text **plus the captured log**.

| Transition | Count |
|---|---|
| pass → pass | 765 |
| **pass → fail** | **0** |
| pass → skip | 0 |
| fail → fail | 74 |
| fail → pass | 5 |
| skip → skip | 34 |
| new pass | 11 |
| **removed** | **26** |

```
sums to AFTER : 889 (actual 889) OK
sums to BEFORE: 904 (actual 904) OK
```

**0 pass → fail among tests that still exist.**

**Failures: 74 — 71 EXPECTED, 3 UNEXPECTED.**

| EXPECTED cause | Count |
|---|---|
| removed column `transfer_hits` | 22 |
| removed table `chips` | 19 |
| removed column `captain_id` | 18 |
| new NOT NULL column `tactic` | 10 |
| removed column `vice_captain_id` | 1 |
| removed column `chip_used` | 1 |

The 3 UNEXPECTED are the same pre-existing ones as every phase since Phase 1.

### The 26 removed tests

**`test_scoring.py` — 16** (the whole file; the module is deleted). Six
classic-only, four migrated, six already covered — see §A for the per-test
reason.

**`test_beat_scheduling.py` — 5:**
`test_revert_free_hit_restores_squad_and_budget_once_gameweek_is_over`,
`..._does_not_fire_while_gameweek_is_still_being_played`,
`..._waits_for_the_last_fixture_of_the_gameweek`, `..._is_idempotent`,
`..._skips_gameweek_with_no_fixtures_ingested` — all exercise a chip that
cannot be activated.

**`test_transfers.py` — 5:**
`test_normal_gameweek_rejects_transfer_after_twenty_already_made` and
`test_wildcard_gameweek_allows_more_than_twenty_transfers` (the deleted
20-cap); `test_wildcard_chip_makes_every_transfer_free`,
`test_transfers_used_wildcard_reports_uncapped_free`,
`test_a_chip_gameweek_preserves_the_bank` (all seed `chip_used`).

---

## Things worth knowing

**G1. `GET /chips/used` is live and will 500.** `Gameplay/chips.py`'s router is
still registered. It was not in this phase's brief and removing a route is
client-facing, so it is flagged rather than done.

**G2. `Context_assembler/main.py`'s `CAPTAIN_QUERY` reads a dropped column.**
Same shape of problem, same reason it was left.

**G3. The structural test for B6 went through three brittle drafts.** Counting
occurrences of `"chip_active"` fails because the docstring discusses it;
asserting the bare string fails for the same reason. It now asserts the two
things that matter: the `Shared.rules` import block does not name them, and
nothing **computes** `chip_active` (only the literal `False`).

**G4. A cleanup regex swept up two of my own new tests.**
`test_revert_free_hit\w*` matched `test_revert_free_hits_task_no_longer_exists`
as well as the real ones. They were re-added. The lesson is that a deletion
regex written against a naming convention will also match anything new that
follows it.

**G5. The new absence-tests live in `test_beat_scheduling.py`, not
`test_celery_wiring.py`.** That module is skipped wholesale when no broker is
reachable, and an assertion that something no longer exists is worthless if it
never runs.
