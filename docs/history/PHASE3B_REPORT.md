# B1: the free-transfer recurrence — rule implemented, anchor blocked

**Rules of evidence.** Commands and their output are quoted below.

---

## 1. What was done

The **rule** is decided, implemented and tested.
`Shared.rules._tactical_free_transfers_available(used_by_gameweek, gameweek,
start_gameweek)` now anchors on a start Gameweek instead of Gameweek 1:

- at `start_gameweek`, every manager has exactly **1**, however long the season
  has been running;
- after that, +1 per Gameweek, unused ones bank, capped at
  `FREE_TRANSFER_BANK_CAP` (2), minus transfers made;
- a later joiner replays the same recurrence from the same start, so an empty
  history accumulates to the cap.

Nine tests, written first and shown failing
(`TypeError: ... takes 2 positional arguments but 3 were given`), cover every
case asked for: start Gameweek → 1; next unused → 2; further unused → 2 (cap);
one used then next → 1; two used across consecutive Gameweeks → 1; a joiner
three Gameweeks after the start → 2. Plus an over-spend leaving no debt, a
Gameweek before the start, and a guard that the classic recurrence is untouched.

**The classic `_free_transfers_available` is unchanged** and still returns 5 for
an empty history at Gameweek 6, pinned by
`test_the_classic_recurrence_is_untouched`.

---

## 2. STOPPING on the start-Gameweek definition

The brief said to stop and report alternatives if I found a concrete problem
with *"the earliest Gameweek in the season that has a row in `gw_selections`"*.
**I found one, so I have not chosen.**

### The empty case works

```
=== with no rows at all ===
 start_gw
----------
         (NULL)
```

`MIN()` over no rows is `NULL`, which the caller can read as "no Gameweek has
been played under these rules yet" and return **1**. That part is fine.

### The problem: the anchor can move, and only ever backwards in time

```
=== triggers on gw_selections ===
enforce_selection_lock :: CREATE TRIGGER enforce_selection_lock
    BEFORE UPDATE ON public.gw_selections FOR EACH ROW ...

=== DELETE triggers on gw_selections ===
0

=== FKs on gw_selections ===
gw_selections_user_id_fkey :: FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
```

Three facts, each verified:

1. **The lock trigger is `BEFORE UPDATE` only.** It does not guard `DELETE`.
2. **There are zero DELETE triggers** on the table. Rows are freely deletable,
   unlike `transfers` and `leaderboard_snapshots`, which do carry immutability
   triggers.
3. **`ON DELETE CASCADE` from `users`.** Deleting one account removes that
   account's selections.

So if the earliest selection in the season belongs to a manager who is later
deleted — or if any first-Gameweek row is removed for any reason — `MIN()`
returns a **later** Gameweek. The anchor moves forward, the recurrence replays
over a shorter span, and **every manager's allowance silently changes**, including
managers with no connection to the deleted account. The allowance is derived
rather than stored (deliberately, so it cannot drift), which is exactly what
makes it recompute from the new anchor on the next request.

### A second, independent problem

```
=== FKs on transfers ===
transfers_user_id_fkey :: FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
```

**`transfers` has no foreign key to `gw_selections`.** A manager can make a
transfer in a Gameweek where nobody has submitted a lineup, so the anchor can
be *later* than transfers that already exist. Those earlier transfers then fall
outside the recurrence's span and stop counting against any allowance.

### Why this is worth stopping over

Both failure modes are silent. Nothing errors; the allowance is simply a
different number than it was yesterday, and because it is recomputed on every
request there is no stored value to compare against or migrate. A manager who
had 2 free transfers could find they have 1, with no event to point at.

---

## 3. Alternatives, for you to choose — I have not picked one

I am **not** proposing a config table or a hand-edited constant, both of which
the brief excluded.

**Option A — derive it, but make the source immutable.** Keep
`MIN(gameweek) FROM gw_selections`, and add a DELETE guard to `gw_selections`
matching the one `transfers` already has (`enforce_transfers_immutability_fn`),
plus changing the `users` FK from `ON DELETE CASCADE` to `ON DELETE RESTRICT`.
Removes both failure modes at the source. Costs a migration and makes user
deletion harder, which may be a problem for account deletion requests.

**Option B — anchor on the migration, not on gameplay.** Use the Gameweek that
was current when the tactical migration ran: the first Gameweek whose deadline
was still in the future at that moment. This is exactly D5's release rule
("the first one whose deadline is still in the future at release time"), so the
anchor would agree with the release decision by construction rather than by
coincidence. It needs somewhere to record one timestamp — the Alembic revision's
own apply time, or a single row written by the migration. It is derived, not
hand-edited, and cannot move afterwards.

**Option C — anchor on the earliest scored Gameweek.** `MIN(gameweek) FROM
gw_scores WHERE rules_version = 3`. `gw_scores` is upserted by the scorer rather
than deleted, and `rules_version = 3` is precisely "scored under these rules".
The weakness is timing: the anchor does not exist until the first Gameweek has
actually been scored, so during the very first Gameweek the allowance has no
anchor and must fall back to 1 — which is the right answer anyway, but it means
the rule is defined by a fallback for its first week.

**Option D — accept the risk.** Keep `MIN(gw_selections.gameweek)` and rely on
the fact that user deletion is rare and the first Gameweek will have thousands
of rows, so the MIN is unlikely to be the deleted one. Cheapest, and honestly
the likeliest to be fine in practice, but it is a silent-wrong-answer risk
rather than a loud one.

**My reading, since you asked for alternatives rather than a choice:** B matches
D5 exactly and cannot move, which is the property that matters. C is close
behind and needs no new storage. I have implemented neither.

---

## 4. Interim state

`transfers.py` passes `FIRST_GAMEWEEK` as the anchor, with a comment saying
why. That is **exactly what the recurrence already assumed** before this change,
so behaviour is identical and no new guess is encoded. The rule is in place and
tested; only the anchor is pending.

---

## 5. Initial squad creation and the allowance (item 3)

**Creating the initial 15 is not a transfer and consumes no allowance.** Code
evidence — `Gameplay/squad_selection.py` never touches the `transfers` table:

```
=== does squad_selection.py write to the transfers table at all? ===
  NO reference to the transfers table anywhere in squad_selection.py

=== what squad_selection.py DOES write ===
  61: INSERT INTO user_squads (user_id, season, budget_remaining, updated_at)
  74: INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active)
```

The allowance is derived by counting `transfers` rows, so rows that are never
written cannot consume it. **Unchanged, as instructed**, and now recorded in
section 1 of the plan so it stops being folklore.

---

## 6. Suite

| | After Phase 3 | Now |
|---|---|---|
| collected | 846 | 855 |
| failures + errors | 122 | **122** |
| EXPECTED | 119 | **119** |
| **UNEXPECTED** | **3** | **3** |

+9 tests, all passing; no new failures. The 3 UNEXPECTED are the same
pre-existing ones.
