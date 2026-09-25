# Phase 3 test reconciliation, test by test

Commit `0aef907` ("Phase 3") against its parent `45c0107`, both run against
`fpl_game_test` with the same interpreter. Only the checked-out code differs —
each commit was checked out into its own `git worktree`, so the working tree
was never modified.

---

## 0. THE GATE FAILED — one `pass->fail`

**`backend/Tests/test_starting_xi.py::test_passed_deadline_rejects_first_ever_submission`
passed at `45c0107` and fails at `0aef907`.** It still fails at current HEAD.

I am reporting this rather than fixing it, as instructed. **My Phase 3 claim of
"no new failures" was wrong**, and it was wrong because I compared summary
totals rather than test identities — exactly what this reconciliation was for.

### What it is

```
resp = client.post("/gw_selection", json=_base_payload(...), headers=...)
assert resp.status_code == 422
assert resp.json()["detail"] == "This gameweek's selection is locked and can no longer be changed"
E   assert [{'type': 'missing', 'loc': ['body', 'tactic'], 'msg': 'Field required', ...}]
        == "This gameweek's selection is locked and can no longer be changed"
```

The test sends the **old payload**, which has no `tactic`. Pydantic rejects it
with a field-required 422 *before* the endpoint body runs, so the deadline
check never executes and the detail is a validation list rather than
`LOCKED_DETAIL`. The status code is still 422.

### Assessment

**This is an 11th superseded test that I missed** when I listed the other 10 in
`PHASE3_REPORT.md` §7. It belongs in exactly that set, with the same reason.

**The behaviour it guarded is not broken.** Its replacement,
`test_gw_selection_tactical.py::test_a_submission_after_the_deadline_is_rejected`,
covers the same first-ever-submission-after-deadline case with a valid payload
and asserts `LOCKED_DETAIL`. Verified just now:

```
backend/Tests/test_starting_xi.py::test_passed_deadline_rejects_first_ever_submission   1 failed
backend/Tests/test_gw_selection_tactical.py::test_a_submission_after_the_deadline_is_rejected   1 passed
```

So the correct action is to skip it with the same reason as the other ten — but
that is a code change, and this task is read-only. **Awaiting your word.**

`removed` is **0**, as required.

---

## 1. How tests are counted

This matters, because it is the whole explanation of the two discrepancies.

**pytest emits one `<testcase>` element per test PHASE that produced an
outcome, not one per test.** A test that fails in its call phase and then
errors in teardown produces **two** elements with the same id — one carrying
`<failure>`, one carrying `<error>`.

Measured:

| | elements | unique test ids | ids duplicated |
|---|---|---|---|
| before (`45c0107`) | 794 | **769** | 25 (all `failure` + `error`) |
| after (`0aef907`) | 846 | **833** | 13 (all `failure` + `error`) |

For the transition table each test is counted **once**, by the precedence
`error > failure > skipped > passed`, and `error` and `failure` are then
collapsed into a single `fail` bucket. That partitions the unique-id set
exactly, which is why the columns sum.

---

## 2. Transition table

| Transition | Count |
|---|---|
| pass → pass | 605 |
| pass → skip | 0 |
| **pass → fail** | **1** |
| fail → fail | 108 |
| fail → skip | 31 |
| fail → pass | 18 |
| skip → skip | 6 |
| skip → pass | 0 |
| skip → fail | 0 |
| new pass | 64 |
| new skip | 0 |
| new fail | 0 |
| removed | 0 |

**The sums check out exactly:**

```
sums to AFTER collected : 833  (actual 833)  OK
sums to BEFORE collected: 769  (actual 769)  OK
```

- BEFORE = pass→pass + pass→fail + fail→* + skip→* = 605 + 1 + (108+31+18) + 6 = **769**
- AFTER = everything except `removed`, plus the `new` rows = 605 + 1 + 108 + 31 + 18 + 6 + 64 = **833**

---

## 3. The lists asked for

### pass → skip (0)

None.

### pass → fail (1) — the gate failure

- `backend.Tests.test_starting_xi::test_passed_deadline_rejects_first_ever_submission`

### fail → pass (18)

All were failing because `POST /transfers` was a blanket 500
(`column "chip_used" does not exist`) or because the selection endpoint could
not run. None was edited; they pass because the endpoints were fixed.

- `test_auth_enforcement::test_a_stale_user_id_cannot_borrow_another_users_squad_to_transfer`
- `test_transfer_concurrency::test_a_rejected_batch_releases_the_lock`
- `test_transfer_concurrency::test_two_different_managers_do_not_block_each_other`
- `test_transfer_drafts::test_drafts_do_not_affect_transfers_used`
- `test_transfers::test_budget_boundary_exact_and_over`
- `test_transfers::test_budget_one_over_boundary_fails`
- `test_transfers::test_cumulative_club_cap_violation_rejected`
- `test_transfers::test_duplicate_player_in_id_rejected`
- `test_transfers::test_duplicate_player_out_id_rejected`
- `test_transfers::test_future_deadline_still_allows_transfers`
- `test_transfers::test_multiple_simultaneous_violations_all_reported_together`
- `test_transfers::test_normal_gameweek_rejects_transfer_after_twenty_already_made`
- `test_transfers::test_passed_deadline_rejects_transfers_with_no_gw_selection_row`
- `test_transfers::test_position_mismatch_rejected`
- `test_transfers::test_rebuying_a_sold_player_keeps_budget_arithmetic_correct`
- `test_transfers::test_self_swap_rejected`
- `test_transfers::test_selling_then_rebuying_the_same_player_reactivates_the_original_row`
- `test_transfers::test_transfers_used_no_transfers_yet_reports_full_free_slot`

Two of these are worth noting: `test_a_rejected_batch_releases_the_lock` and
`test_two_different_managers_do_not_block_each_other` are
`test_transfer_concurrency` tests that now **pass**, which is some reassurance
about the TOCTOU fix even while the three harder concurrency tests in that file
sit skipped.

### removed (0)

None.

---

## 4. The 3-test gap: 690 + 122 + 37 = 849 vs 846

pytest's summary line counts **phases**, and a test can contribute to two of
them. Measured on the after-run XML:

```
846 testcase elements = 687 childless (passed)
                      + 104 <failure>
                      +  37 <skipped>
                      +  18 <error>
```

and pytest reported `104 failed, 690 passed, 37 skipped, 18 errors` = 849.

**The difference is exactly 3, and they are identifiable.** Three tests
**passed their call phase and then errored in teardown**, so pytest counts each
once as passed and once as an error, while the XML carries one element:

```
[TEARDOWN] test_gameweek_lifecycle::test_next_gameweek_reopens_transfers_after_the_previous_one_locked
[TEARDOWN] test_gameweek_lifecycle::test_free_transfer_allowance_banks_when_a_gameweek_goes_unused
[TEARDOWN] test_gameweek_lifecycle::test_sell_price_uses_half_profit_rounded_down_after_a_price_rise
      failed on teardown with "...(psycopg2.errors.UndefinedTable) relation "chips" does not exist"
```

687 childless + 3 = **690**, pytest's passed count. The gap is fully accounted
for. All three are the same cause: that file's cleanup still does
`DELETE FROM chips`, a table the Phase 1 migration dropped — a Phase 4 cleanup,
not a behaviour problem.

---

## 5. The 64 vs 52 discrepancy

`PHASE3_REPORT.md` said collected went **794 → 846, +52**. The true figure is
**769 → 833, +64**.

**I quoted element counts and called them collected tests.** The arithmetic:

```
before: 794 elements − 25 duplicates = 769 tests
after : 846 elements − 13 duplicates = 833 tests

element delta:  846 − 794 = +52
test delta:     833 − 769 = +64
difference:     12 = the 12 fewer failure+error duplicates (25 → 13)
```

64 − 12 = 52. The "+52" was 64 genuinely new tests masked by 12 tests that
stopped erroring in teardown as well as failing. The three new files collect
37 + 16 + 11 = **64**, which matches the transition table's `new pass` exactly.

---

## 6. Method

```
git worktree add <scratch>/wt_after  0aef907
git worktree add <scratch>/wt_before 0aef907^        # 45c0107
pytest backend/Tests -q --tb=no --junit-xml=... -p no:cacheprovider
```

`.env` and `.env.test` were copied into each worktree (they are gitignored, and
`conftest.py` finds them by walking up from the test file). `DATABASE_URL` and
`TEST_DATABASE_URL` were set explicitly to `fpl_game_test`; `python-dotenv` does
not override an already-set variable, so the explicit values won.
`ALLOW_GAMEPLAY_WIPE` and `FPL_ARCHIVE_GOLDEN` were cleared so no wipe was
unlocked and no test reached the network.

**Caveat, stated rather than hidden:** both runs used the database as it is
*now*, which is at `e7c4d81b3a95` and therefore has `ruleset_epochs`. Neither
commit's code knows about that table, so the extra table is inert for them —
but this is a comparison of two codebases against one schema, not a
reconstruction of each commit's original environment.
