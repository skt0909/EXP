# Phase 2 report — rules constants and the scoring engine

Scope: add the tactical constants and a pure scoring engine, with tests first.
**No existing scoring, endpoint or migration code was modified. No constant was
removed** — removals are Phase 4. Nothing is wired in yet.

**Rules of evidence.** Commands and their output are quoted. Claims I could not
verify are labelled *unverified*.

---

## 1. Files

| File | Status | Notes |
|---|---|---|
| `backend/Shared/rules.py` | **modified, additions only** | new tactical block under its own banner; zero-import rule preserved |
| `backend/Results/tactical_scoring.py` | **added** | the pure engine |
| `backend/Tests/unit/conftest.py` | **added** | makes this subtree database-free |
| `backend/Tests/unit/test_tactical_rules.py` | **added** | constants, `tier_points`, `general_points`, `tactical_points` |
| `backend/Tests/unit/test_tactical_scoring.py` | **added** | `score_selection` scenarios |
| `backend/Tests/unit/test_general_points_golden.py` | **added** | 48-row golden table + optional archive check |

### Why `Results/tactical_scoring.py`

`Results/` is the scoring box: `scoring.py` and `standings.py` already live
there. The new engine sits beside `scoring.py` rather than inside it because
`scoring.py` owns the queries and this owns the arithmetic — and because Phase 2
may not modify it. It imports only the standard library and `Shared.rules`, so
it adds no edge to the package graph.

### Why a new `backend/Tests/unit/` directory

`backend/Tests/conftest.py:74` declares `_clean_ml_test_data` as an **autouse**
fixture that opens a connection before and after *every* test:

```python
@pytest.fixture(autouse=True)
def _clean_ml_test_data(engine):
    def _wipe():
        with engine.begin() as conn:
```

So a test placed anywhere under `backend/Tests/` reaches Postgres whether it
wants to or not, and a "no database" claim made there would be false. The new
directory's `conftest.py` shadows both `_clean_ml_test_data` and `engine` by
same-name override — the pattern already used elsewhere in this suite. `engine`
is overridden to **raise**, not to return `None`, so a test that reaches for a
database fails loudly instead of quietly receiving something falsy.

**Verified, not assumed** — the whole unit directory passes with both database
URLs pointed at a server that does not exist:

```
$ DATABASE_URL=postgresql://nobody:nobody@127.0.0.1:1/does_not_exist  (and TEST_DATABASE_URL)
$ python -m pytest backend/Tests/unit -q
128 passed, 1 skipped in 0.14s
```

---

## 2. Evidence gathered before coding

### `ml.players.position` — the exact codes (item 4)

Queried on `fpl_game_test`, the only database this phase was allowed to touch:

```
 position | count
----------+-------
 DEF      |   200
 FWD      |   120
 GK       |    80
 MID      |   200
(4 rows)
```

and the column is constrained to exactly those four:

```
players_position_check :: CHECK ((("position")::text = ANY (ARRAY['GK','DEF','MID','FWD'])))
```

Those are the codes the engine uses. A player whose position is not one of them
raises `KeyError` rather than scoring 0.

### `RULES_VERSION = 3` — where it was decided

`IMPLEMENTATION_PLAN.md` section 2:

> **`rules_version` is an INTEGER, not a string.** Phase 0 verified the live column
> is `gw_scores.rules_version smallint NOT NULL` (added by `c9a04e7b53d1`), holding
> values 1 (869 rows) and 2 (121 rows). … The next generation is therefore **3**.

Added as a **new name**, `RULES_VERSION = 3`, alongside the existing
`CURRENT_RULES_VERSION = 2`, which is untouched because the live scorer still
stamps it. Phase 4 repoints the scorer; until then both exist and neither is
ambiguous. A test pins `CURRENT_RULES_VERSION == 2` so Phase 2 cannot move it by
accident.

### Decimal, not float

`creativity` is `numeric(6,1)` and psycopg2 returns `Decimal`. `CREATIVITY_TIERS`
is therefore built from `Decimal`, and `tier_points` **rejects floats outright**:

```python
if isinstance(value, float):
    raise TypeError(f"tier_points refuses float ({value!r}): tier boundaries must be exact...")
```

Refusing rather than coercing is deliberate: a silent `Decimal(str(x))` would
paper over a caller that had already lost precision upstream. The boundary cases
19.9 / 20 / 39.9 / 40 are asserted exactly, and a dedicated test asserts the
`TypeError`.

This forced the one import into `Shared/rules.py`. The zero-import rule forbids
**project** modules, connections and environment reads; `decimal` is standard
library, pure and always available, and the import carries a comment saying so.

---

## 3. Constants added

All under a banner in `Shared/rules.py`, additions only:

`RULES_VERSION = 3`, `FREE_TRANSFER_BANK_CAP = 2`, `FIXTURE_DURATION_MIN = 115`,
`TACTICS`, `BENCH_SLOT_ROLES` (12 `auto_gk`, 13 `auto_outfield`, 14/15
`tactical`), `FORMATION_MIN` / `FORMATION_MAX`, `ATTACK_POINTS`,
`DEFENCE_CLEAN_SHEET_POINTS = 2`, `DC_TIERS = ((8, 2), (10, 3))`,
`BALANCED_GOAL_OR_ASSIST_POINTS = 1`,
`CREATIVITY_TIERS = ((Decimal("20"), 1), (Decimal("40"), 3))`.

`BENCH_SLOT_ROLES` mirrors the generated `role` column from migration
`d58b3f10a7c2`, so the application and the database agree on what a slot means.

---

## 4. Tests

```
128 passed, 1 skipped in 0.20s
```

The 1 skip is the optional archive check (§5). Test counts by file:
`test_tactical_rules.py` 60, `test_tactical_scoring.py` 27,
`test_general_points_golden.py` 49 (48 golden rows + a size assertion) + 1
skipped.

### Tests were written first, and failed first

```
$ python -m pytest backend/Tests/unit -q
E   ModuleNotFoundError: No module named 'Results.tactical_scoring'
ERROR backend/Tests/unit/test_general_points_golden.py
ERROR backend/Tests/unit/test_tactical_rules.py
ERROR backend/Tests/unit/test_tactical_scoring.py
3 errors in 0.30s
```

### Scenario coverage (section 6's list)

| Scenario | Test |
|---|---|
| double gameweek swap block | `test_general_points_are_summed_across_a_double_gameweek` |
| swap with outgoing no-show | `test_an_outgoing_player_who_never_appeared_still_swaps_and_scores_zero` |
| incoming no-show | `test_an_incoming_player_who_never_appeared_scores_zero_with_no_cover` |
| swap slot never Auto Sub covered | `test_a_slot_involved_in_a_swap_is_never_auto_sub_covered` |
| Auto Sub priority, lowest legal slot | `test_the_outfield_auto_sub_covers_the_lowest_slot_that_keeps_the_formation_legal` |
| formation blocking an Auto Sub | `test_a_formation_that_would_become_illegal_blocks_the_cover` |
| Auto Sub skips illegal, covers later legal | `test_the_auto_sub_skips_an_illegal_slot_and_covers_a_later_legal_one` |
| GK cover | `test_the_backup_gk_covers_only_the_starting_gk`, `test_a_backup_gk_who_also_did_not_play_cannot_cover` |
| Bonus Player no-show | `test_a_bonus_player_who_did_not_appear_earns_no_tactical_points` |
| Bonus no-show with cover | `test_bonus_status_does_not_pass_to_the_auto_sub_who_covers_him` |
| Bonus in a double gameweek, tiers per fixture | `test_tactical_tiers_are_evaluated_per_fixture_then_summed_in_a_double_gameweek`, `test_defensive_contribution_tiers_are_also_per_fixture` |
| Sub Bonus tie gives 0 | `test_a_sub_bonus_tie_gives_nothing` |
| Sub Bonus with outgoing no-show | `test_sub_bonus_when_the_outgoing_player_never_appeared_needs_only_a_positive_score` |
| unused Tactical Sub | `test_an_unused_tactical_sub_stays_on_the_bench_and_scores_nothing` |
| each tactic's events | `test_attack_pays_...`, `test_defence_pays_...`, `test_balanced_pays_...` |
| tier boundaries 7/8/9/10 and 19.9/20/39.9/40 | `test_defensive_contribution_tier_boundaries`, `test_creativity_tier_boundaries_are_exact_with_decimal` |
| highest tier only, not stacked | `test_tiers_are_not_stacked_only_the_highest_reached_counts` |
| 13-scorer maximum | `test_up_to_thirteen_players_can_score_in_one_gameweek` |
| General Points golden | `test_general_points_golden_table` (48 rows) |

**Validation tests (formations, Attack with <2 FWD, Bonus position mismatch,
cross-position swap, kickoff order, budget, club limit, bank cap) are NOT here.**
Those are endpoint validation and belong to Phase 3; the engine scores what it is
given and validates nothing. Stated so the gap is visible rather than assumed
covered.

---

## 5. The golden table

48 hand-checked player-fixture rows, each with its arithmetic in a comment:
appearance boundaries (0/1/59/60), each position's goal value, assists, clean
sheets by position and the 60-minute gate, defensive contributions at 9/10 for
DEF and 11/12 for MID/FWD and never for GK, goals conceded at 1/2/3/4 with the
GK/DEF restriction, saves at 2/3/6 and the GK restriction, penalties saved and
missed, cards, own goals, and five realistic multi-rule combinations.

### The optional archive check

`test_general_points_matches_the_archive_minus_bonus` is **skipped unless
`FPL_ARCHIVE_GOLDEN=1`** is set, because the default suite must not need
network. It downloads a season of `merged_gw.csv` and asserts recomputed General
Points equal `total_points - bonus`.

**I have not run it, so its result is *unverified*.** It needs network, and
nothing in this phase required me to go out to the internet. It is wired and
skipping cleanly; whoever wants the assurance can set the variable. Two things
it may surface: seasons before 2025-26 have no defensive-contribution column (it
skips those), and FPL's historical totals may include rules this table does not
model.

---

## 6. Full suite against the Phase 1 baseline

Run on `fpl_game_test`, the only database permitted:

| | Phase 1 baseline | Now | Change |
|---|---|---|---|
| tests run | 658 | **787** | +129 (the new unit tests) |
| passed | 471 | **599** | +128 |
| skipped | 5 | **6** | +1 (the archive check) |
| failed | 155 | **155** | **0** |
| errors | 27 | **27** | **0** |
| failures+errors | 182 | **182** | **0** |
| EXPECTED | 179 | **179** | 0 |
| UNEXPECTED | 3 | **3** | 0 |

```
155 failed, 599 passed, 6 skipped, 3 warnings, 27 errors in 29.95s
```

**No new failures.** The 3 UNEXPECTED are the same pre-existing ones from Phase 1
(two `PATCH /dream11/contests/{contest_id}/team` auth-table entries, and
`test_ignores_fixtures_whose_kickoff_is_already_past`), and the 179 EXPECTED are
still the removed-column/table failures that Phase 4 will clear.

---

## 7. Ambiguities found in the spec

**None of these were resolved silently.** Each names what I did and why, and
each is a decision someone should confirm.

**A1. The role vocabulary has no name for a replaced starter.** Section 6 lists
five roles — starter, swapped_out, swapped_in, auto_sub_cover, bench_unused —
but a starter who *was* covered by an Auto Sub is none of them and needs to be
distinguishable, or the dashboard cannot grey him out. I kept his role as
`starter`, set `counted=False`, and put the link on the cover's
`covers_player_id`. A sixth role (`auto_subbed_out`) would be the alternative.
*This tripped me during implementation:* three of my own tests initially asserted
`counted is False` for an **uncovered** no-show starter, which is wrong — he is
still a scoring slot that happens to score 0. Only a *replaced* starter stops
counting. The tests now assert via `covers_player_id`.

**A2. Does a player with 0 minutes still take his deductions?** A red card shown
after the whistle, or an own goal in a fixture where minutes are recorded as 0.
Section 1 gates only the appearance points on minutes. I apply cards, own goals
and missed penalties regardless of minutes; the engine's
`test_a_player_who_did_not_appear_still_takes_his_deductions` pins it.
Consequence: such a player can score negative while "not having appeared", and
he is still not eligible for Bonus (which requires appearing).

**A3. Formation MAXIMA are inferred, not stated.** Section 1 gives only minima
(1 GK, ≥3 DEF, ≥2 MID, ≥1 FWD). Auto Sub legality needs upper bounds too, or a
cover could produce 6 defenders. I took DEF ≤5, MID ≤5, FWD ≤3 from the squad
composition, which also matches the `DEF 3-5, MID 2-5, FWD 1-3` already written
in `scoring.py`'s docstring. Confirm the maxima are intended.

**A4. Is the starting GK identified by slot or by position?** Section 1 implies
slot 1 but does not say. `scoring.py` states the opposite rule explicitly —
*"Nothing guarantees position_slot 1 is the GK … GK identity is ALWAYS resolved
via ml.players.position"*. I followed `scoring.py`. Same for the bench GK, which
I resolve by position rather than assuming slot 12.

**A5. May the outfield Auto Sub cover a goalkeeper?** Section 1 says the backup
GK covers only the starting GK, but does not say the reverse. I excluded GK
starters from the outfield Auto Sub's candidates. If the starting GK and the
backup GK both fail to appear, the slot simply scores 0.

**A6. The Sub Bonus cap is structural, not enforced.** Section 1 says "Max +2".
That follows from there being two tactical bench slots, and the database
enforces it (`uq_swaps_sel_in`, and only slots 14/15 have `role = 'tactical'`).
The engine does **not** cap the total — hand it three swaps and it returns 3.
Should the engine defend against that, or keep trusting the trigger?

**A7. The engine validates nothing.** It does not check that exactly 2 Bonus
Players were named, that the submitted XI is a legal formation, that a swap's
players share a position, or that the kickoff ordering holds. Those are Phase 3
plus the `enforce_bonus_count` / `enforce_swap_refs` triggers. Scoring an
invalid selection therefore produces a number rather than an error. Deliberate,
but worth confirming.

**A8. A Bonus Player can never be swapped, in either direction** — outgoing is
blocked by the rules and by `enforce_swap_refs`, incoming by
`ck_starting_xi_bonus_is_starter` (bonus requires slot ≤ 11, tactical subs are
14/15). The engine does not rely on this, but no test can construct the case
legally, so that combination is untested by design.

**A9. Sub Bonus compares General Points only.** Section 1 says "General Points",
which I read as excluding Tactical Points and read as full-gameweek sums
including both fixtures of a double. Pinned by
`test_sub_bonus_compares_general_points_only_not_tactical`.

**A10. "Defensive Contribution" means two different rules.** General Points pays
a flat +2 at 10 actions (DEF) or 12 (MID/FWD); the Defence *tactic* pays tiered
2 or 3 starting at 8. Both are implemented and both are tested, but the shared
name is an easy source of future confusion — worth renaming one of them.

---

## 8. What Phase 2 did not do

- Nothing is wired into `score_gameweek`; `scoring.py` is byte-unchanged.
- No constant was removed; `CURRENT_RULES_VERSION` is still 2.
- No endpoint, migration or frontend file was touched.
- No database was migrated; `fpl_game` and the server are untouched.
- Batching over managers is Phase 4's, per section 10.
