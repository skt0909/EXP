# Phase 4b report — the dashboard read model

Scope: Parts 1 and 2 (the 4a fixes E4 and E1) and Part 3, the dashboard. No
migrations. The frontend and Dream11 were not touched.

**Rules of evidence.** Commands and their output are quoted.

---

## 1. Part 1 — E4, failure isolation

The 4a job computed each manager in its own `try/except` but wrote the whole
batch in ONE transaction, so a failed write took all 200 managers — strictly
worse than the per-manager job it replaced. `_persist` now tries the batch
first (one round trip for 200 managers) and on failure falls back to writing
managers one at a time, each in its own `SAVEPOINT` inside a single
transaction, so a manager who fails again rolls back alone.

**A real reporting gap was found and fixed.** `refresh_active_gameweeks:151`
called `score_gameweek(...)` and **discarded the summary**, so a gameweek where
managers failed was appended to `refreshed` and the run looked fully
successful. It now inspects the summary, logs an ERROR naming every offender,
and reports the gameweek under `failed`. Standings still run first, because the
managers who *did* score must appear in them.

**Where failures are recorded** — the existing mechanism, unchanged:

- `Worker/tasks.py:194-199` already logs a WARNING listing every failed user id
  when `summary["failed"]` is non-empty, and returns the summary as the task
  result.
- `refresh_active_gameweeks` returns `{"refreshed": [...], "failed": [...]}`,
  which is what its caller records; `Worker/task_health.py:64`'s
  `record_task_heartbeat(engine, task_name, success, error)` is the heartbeat
  side.

Six tests. The poison is the real failure shape found in 4a (ambiguity E6): a
`starting_xi` row pointing at a player absent from `ml.players` for that
season. Self-healing is pinned — fix the data, re-run, and the skipped manager
is scored, because the upsert is idempotent and nothing about the failure
persists.

---

## 2. Part 2 — E1, the simulation harness

`score_gameweek_tactical(..., allow_sim_seasons=False)`. Only
`Tools/fpl_sim.py` passes `True`, and passing it logs a WARNING naming the
season. An **argument, not an environment variable**: an env var is ambient and
could end up set on the production worker by accident, where an argument has to
be typed at the one call site that wants it.

`test_the_production_task_never_allows_sim_seasons` spies on the call
`Worker/tasks.py` makes and asserts neither a positional `True` nor the keyword
appears.

**Who still uses the classic scorer:**

```
=== production (non-test) callers of the classic score_gameweek ===
  NONE
```

The only production import from `Results.scoring` was
`team_dashboard.py:55`'s `resolve_autosubs` — **removed in Part 3**. Three test
files still import `score_gameweek` directly (`test_scoring.py`,
`test_gameweek_lifecycle.py`, `test_full_season_scenario.py`) and
`test_scoring_rules.py` imports `_component_score`. Those are the classic tests
already failing EXPECTED since Phase 1; retiring them is merge-gate work.

---

## 3. Part 3 — the dashboard

**Per-player points are now computed at request time** by
`scoring_job.score_manager(conn, season, gameweek, gw_selection_id, tactic)`,
which reuses the batch job's own queries and row mapping. That reuse is the
point: a second mapping here could disagree with the one that wrote
`gw_scores`, and the dashboard would be explaining a number it did not produce.
No new tables.

**`from Results.scoring import resolve_autosubs` is gone.** `_autosub_player_ids`
went with it — the engine already reports who covered whom
(`role = "auto_sub_cover"`, with `covers_player_id`) and who was replaced
(`auto_sub_replaced`), so a second implementation of the rules is not needed to
draw the badge. Nothing else referenced it (`git grep`).

Three queries still selected dropped columns and were fixed:
`GW_SELECTION_QUERY` (`chip_used` to `tactic`), `STARTING_XI_ROWS_QUERY`
(`is_captain, is_vice_captain` to `is_bonus`, and it no longer needs the stats
join at all), and `GW_SCORE_QUERY` (`transfer_hits, hit_deductions` to
`tactical_points, sub_bonus`).

### Every existing key, and what it now holds

| Key | Now |
|---|---|
| `user_id`, `username`, `team_name`, `season`, `gameweek`, `deadline` | unchanged |
| `has_lineup` | unchanged — `selection_row is not None` |
| **`chip_used`** | **always null** (chips removed) |
| **`captain_multiplier`** | **always null** (type widened to nullable int) |
| `gw_points` | unchanged — `gw_scores.total_points` |
| `gw_average`, `season_total` | unchanged |
| `has_score` | unchanged — whether a `gw_scores` row exists |
| `raw_points` | `gw_scores.raw_points`, **or the engine's General Points** when not yet scored |
| **`captain_bonus`** | **always 0** (no captaincy) |
| **`transfer_hits`**, **`hit_deductions`** | **always 0** (no hits) |
| `final_total` | `gw_scores.total_points`, or the engine's total when not yet scored |
| `live_status` | unchanged |
| `overall_rank`, `overall_rank_total` | unchanged |
| `team_value_available`, `team_value`, `bank` | unchanged |
| `lineup`, `bench` | unchanged shape; each player gains the fields below |
| per player `points` | **General Points from the engine**, not FPL's `total_points` |
| per player **`is_captain`**, **`is_vice_captain`** | **always false** |
| per player `is_autosubbed_in` / `_out` | now derived from the engine's role |

**Inert-but-present keys, as asked:** `chip_used` (null),
`captain_multiplier` (null), `captain_bonus` (0), `transfer_hits` (0),
`hit_deductions` (0), and per player `is_captain` (false),
`is_vice_captain` (false). All stay until the frontend phase removes them.

### Added

Manager level: `tactic`, `general_points`, `tactical_points`, `sub_bonus`,
`total`, `swaps`, `provisional`, `scored`, `scored_reason`.

Per player: `role` (one of the six engine roles), `is_bonus`,
`general_points`, `tactical_points`, `general_breakdown`,
`tactical_breakdown`, `counted`.

Per swap: `player_out_id`, `player_in_id`, `general_out`, `general_in`,
`sub_bonus`.

### The rule breakdown

`general_points_breakdown` and `tactical_points_breakdown` were added to
`Results/tactical_scoring.py`. They mirror the two scoring functions rule for
rule and read the same constants, so they cannot drift in **value**. They can
drift in **coverage** — a rule added above and forgotten below — which is what
`test_every_general_points_breakdown_sums_to_its_total` catches: it asserts
`sum(breakdown)` equals the function's own answer over a matrix of 24 rows x 4
positions (and x 3 tactics for the tactical one). Per-fixture rows are merged
per rule, so a double gameweek shows one "goals: 2" line rather than two.

### `provisional` and `scored`

`provisional` is true while **any** fixture is unfinished. Source, quoted, from
`GW_FIXTURE_STATUS_QUERY`:

```sql
COUNT(*) FILTER (WHERE finished) AS finished_count
```

i.e. `ml.fixtures.finished`. It is `finished_count < total`. Points and
creativity only settle at full-time.

`scored` is false in two cases, each with a `scored_reason`: a gameweek before
`ruleset_epochs.first_gameweek`, and a manager with no selection. **Today's
`has_lineup` behaviour is preserved exactly** — it is still
`selection_row is not None`, and an unstarted state is still a 200, the same
stance `GET /squad` and `GET /gw_selection` take.

---

## 4. Tests and cost

`9 passed` in `backend/Tests/test_team_dashboard_tactical.py`.

A hand-computed gameweek per tactic through the endpoint, including a swap, an
Auto Sub and a Bonus no-show; the numbers match the 4a golden gameweek
(41/5/1 and 30/6/0). The consistency test asserts the dashboard's
`general_points`, `tactical_points`, `sub_bonus` and `total` equal the
`gw_scores` row the job wrote. Plus a pre-epoch gameweek, a provisional case,
the fresh-user 200, and a legacy-key check.

**Cost, measured:**

```
GET /team: 14 SQL queries, 161.9 ms (fpl_game_test, one manager, gameweek 13)
```

**The first version of this measurement was wrong and reported `0 SQL
queries`** — it attached the listener to the test fixture's engine, not the
app's. It now attaches to `Shared.db_utils.get_engine()` (lru_cached, so the
endpoint and the listener see the same object) and asserts the count is
non-zero, so that mistake cannot recur silently.

---

## 5. Full suite by test identity

`8aa1c1d` (pre-Part-1) against the current tree. Counting: pytest emits one
`<testcase>` per test *phase*, so a test that fails then errors in teardown
yields two elements with the same id; each test is counted once by precedence
`error > failure > skipped > passed`, with error and failure collapsed to
`fail`.

| Transition | Count |
|---|---|
| pass → pass | 708 |
| **pass → fail** | **0** |
| pass → skip | 0 |
| fail → fail | 94 |
| fail → pass | 14 |
| skip → skip | 34 |
| skip → pass | 3 |
| new pass | 34 |
| **removed** | **0** |

```
sums to AFTER : 887 (actual 887) OK
sums to BEFORE: 853 (actual 853) OK
```

**Failures: 94 — 91 EXPECTED, 3 UNEXPECTED.**

| EXPECTED cause | Count |
|---|---|
| removed column `captain_id` | 37 |
| removed column `transfer_hits` | 22 |
| removed table `chips` | 19 |
| new NOT NULL column `tactic` | 10 |
| removed column `chip_used` | 2 |
| removed column `vice_captain_id` | 1 |

EXPECTED means the failure names a column or table the Phase 1 migration
removed, read from the assertion text **plus the captured log** (the app turns
a `ProgrammingError` into a generic 500). The 3 UNEXPECTED are the same
pre-existing ones as every phase since Phase 1.

`chip_used` failures fell from 16 to 2 and `fail → pass` is 14, because the
dashboard no longer selects dropped columns.

---

## 6. New ambiguities

**F1. `points` changed meaning without changing name.** It was FPL's
`total_points`; it is now the engine's General Points. The key, type and
position are identical, so the current frontend keeps rendering — but a client
comparing against the FPL app will see different numbers with no signal that
the definition moved. Renaming it would break the frontend, which is the
frontend phase's call.

**F2. `raw_points` and `final_total` have two sources.** They read `gw_scores`
when the job has run and fall back to the engine's live figures when it has
not, so a gameweek mid-scoring shows a coherent total instead of 0. That means
the same key can be a committed number or a just-computed one, and nothing in
the response says which. `has_score` is the nearest signal.

**F3. The dashboard scores a selection the job may have refused.** The
dashboard calls the engine directly, with no epoch gate on the *computation* —
only `scored: false` on the response. So a pre-epoch gameweek still returns
populated per-player points alongside `scored: false`. Arguably the players
should be empty; showing them lets a client render a lineup for a gameweek that
will never be scored.

**F4. 14 queries and 162 ms for one manager.** Measured on a near-empty
`fpl_game_test`, so it is a floor. The engine call adds three queries (slots,
swaps, stats) on top of the existing reads. Nothing is cached between requests.

**F5. `tactical_breakdown` is only populated when the player earned tactical
points.** The condition is `p.is_bonus and p.tactical_points`, so a Bonus
Player who earned nothing gets `[]` rather than a list of zero-point rules.
Consistent with breakdowns never listing zeroes, but it means "empty" covers
both "not a Bonus Player" and "Bonus Player who earned nothing" — the
`is_bonus` flag is what distinguishes them.

**F6. `captain_multiplier` changed type from int to nullable int.** That is
not strictly additive. A frontend doing arithmetic on it would now see null.
The brief specified false-or-null for the captaincy keys, so this follows the
instruction, but it is the one change here that could break a client.
