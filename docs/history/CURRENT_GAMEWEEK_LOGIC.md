# How the "current gameweek" is decided

Read-only investigation. No code was changed.

---

## 1. `GET /team` has no default gameweek at all

`backend/Results/team_dashboard.py:327`:

```python
@router.get("/team", response_model=TeamDashboardResponse)
def get_team_dashboard(
    season: str,
    gameweek: int,
    current_user: CurrentUser = Depends(get_current_user),
) -> TeamDashboardResponse:
```

**Both `season` and `gameweek` are required query parameters with no default.**
Omitting either is a 422 from Pydantic before the handler runs — verified in
Phase 1:

```
--- GET /team -> HTTP 422
    detail  [{'type': 'missing', 'loc': ['query', 'season'], ...},
             {'type': 'missing', 'loc': ['query', 'gameweek'], ...}]
```

So the question "which gameweek does `/team` default to" has no answer inside
`/team`. **The decision lives one call earlier**, in `GET /gameweeks/current`,
which the frontend calls first and then passes the result to `/team`:

```
frontend/src/api/gameweeks.js:8        request('/gameweeks/current')
frontend/src/config/gameweek.jsx:30    fetchCurrentGameweek()
frontend/src/layout/Layout.jsx:12      "...so every page agrees"
```

---

## 2. The rule is DEADLINE-based, not scoring-based

It does **not** match "the earliest gameweek that is not yet fully scored".
Nothing in this decision reads `gw_scores`, `ml.fixtures.finished`, or any
notion of scoring. There is also no stored `current_gameweek` config.

`backend/Game_logic/fixtures.py:221`:

```sql
WITH gw_deadlines AS (
    SELECT season, gameweek, MIN(kickoff_time) - interval '90 minutes' AS deadline
    FROM ml.fixtures
    GROUP BY season, gameweek
)
(SELECT season, gameweek, deadline FROM gw_deadlines
 WHERE deadline > NOW()
 ORDER BY deadline ASC LIMIT 1)
UNION ALL
(SELECT season, gameweek, deadline FROM gw_deadlines
 ORDER BY deadline DESC LIMIT 1)
LIMIT 1
```

**What it actually does, in two tiers:**

1. **Primary** — the gameweek with the *soonest deadline still in the future*.
   That is "the next gameweek a manager can still set a team for", not the one
   being played and not the one being scored. During a gameweek that has
   kicked off, this already points at the **next** one.
2. **Fallback** — if no deadline is in the future, the gameweek with the
   *latest deadline overall*, i.e. the most recent past one. The source comment
   says this is deliberate: between the last ingested deadline and the next
   gameweek's fixtures arriving there is no future deadline, and returning
   nothing "would strand every page that reads this". A past gameweek renders
   read-only through the existing locked path rather than inviting an edit.

`found: false` when `ml.fixtures` is empty, rather than a 404 — the same
"unstarted state is not an error" stance as `GET /squad` and `GET
/gw_selection`.

### Two properties worth knowing

**It is not season-scoped.** The query has no `WHERE season = ...`. It ranks
deadlines across *every* season present in `ml.fixtures` and returns whichever
row wins, including its season. On a database holding the simulation seasons
(`SIM38OK`, `SIM38TST`, `SIMSMOKE`), whose fixtures carry real timestamps, a
SIM gameweek can win. This is the same class of problem as the season filter
already on the Phase 4 list, and this query is not currently covered by it.

**"Current" means "next deadline", so it moves at the deadline, not at
kickoff and not at full-time.** The moment GW6's deadline passes, this returns
GW7 — while GW6 is still being played and scored.

---

## 3. `GET /team` does not return `is_locked`

`grep -n "is_locked" backend/Results/team_dashboard.py` → **no matches**. The
field is not in `TeamDashboardResponse` and is never computed there.

### What already holds the information

**`gw_selections.is_locked`** — `boolean DEFAULT false`, one row **per manager
per gameweek**. Two mechanisms maintain it:

- **The Beat sweep** sets it, `backend/GameEngine/gameweek_lock.py:84`:
  ```sql
  UPDATE gw_selections SET is_locked = TRUE
  WHERE season = :season AND gameweek = :gameweek AND is_locked = FALSE
  ```
- **The trigger `enforce_selection_lock`** (BEFORE UPDATE on `gw_selections`)
  raises on any update where `OLD.is_locked = TRUE`, so it is the enforcement
  point rather than just a flag.

### What it would take to add it

`get_team_dashboard` **already reads the row that carries it**.
`GW_SELECTION_QUERY` currently selects `id` and `tactic`; adding `is_locked` to
that select list and to the response model is the whole change. No new query,
no new join, no migration.

Two caveats, because the flag is not the whole truth:

- **It only exists once a manager has submitted.** A manager with no
  `gw_selections` row has no flag. `has_lineup` is already `False` in that
  case, so the honest value is `null`, not `false` — `false` would read as
  "still open" for a gameweek that kicked off days ago.
- **The flag alone is not the deadline.** The codebase already treats this as
  two independent sources: `Gameplay/transfers.py`'s docstring says the sweep
  "only ever locks gameweeks that already have a `gw_selections` row", so both
  `starting_xi.py` and `transfers.py` **also** call
  `deadlines.deadline_has_passed` for the never-submitted case. A truthful
  `is_locked` on the dashboard should be the same OR of the two, or it will
  disagree with what `POST /gw_selection` does.

---

## 4. "Fully scored" is not stored anywhere, and each candidate definition has a gap

**There is no gameweek-level table.** No `gameweeks` table, no `scored_at`, no
`finalized_at`, no status column. Confirmed against `fpl_game_test`: the only
`public` table matching `gameweek` is `user_gameweek_finance`. So "fully
scored" cannot be read — it can only be derived, and the three candidates
differ.

### (a) Every fixture has `finished = TRUE`

**Data exists.** `ml.fixtures` carries `season`, `gameweek`, `kickoff_time`,
`finished boolean`. This is what the dashboard **already** uses for
`live_status`, via `GW_FIXTURE_STATUS_QUERY`:

```sql
COUNT(*) AS total,
COUNT(*) FILTER (WHERE finished) AS finished_count,
COUNT(*) FILTER (WHERE kickoff_time IS NOT NULL AND kickoff_time <= now()) AS started_count
```

and `_resolve_live_status` calls it `"final"` only when
`finished_count == total`. Phase 4b's `provisional` flag is the inverse.

**Gap:** `finished` is set by FPL ingestion. It means "the match is over", not
"we have scored it". A gameweek can be `final` seconds before the scoring job
next runs.

### (b) `gw_scores` rows exist for every manager

**Data exists but the denominator does not.** `gw_scores` is keyed
`(user_id, season, gameweek)` and carries `raw_points`, `final_points`,
`total_points`, `season_total`, `rules_version`, `tactical_points`,
`sub_bonus`.

**Gap: there is no roster of who *should* have a row.** The job's driving query
is `SELECT ... FROM gw_selections WHERE season AND gameweek` — a manager who
never submitted is never in the result set and correctly gets no row. So this
can only ever mean "every manager **who submitted** has a row", which is
already true the instant the job finishes, and says nothing about whether the
numbers are final.

### (c) What the system actually behaves as if it means

**Past the 5-day active window.** `GameEngine/gameweek_finalize.py:54` sets
`DEFAULT_ACTIVE_WINDOW_DAYS = 5`, and:

```sql
SELECT season, gameweek FROM ml.fixtures
GROUP BY season, gameweek
HAVING MIN(kickoff_time) IS NOT NULL
   AND MIN(kickoff_time) <= NOW()
   AND MIN(kickoff_time) > NOW() - make_interval(days => :window_days)
```

A gameweek is **re-scored on every 15-minute run while inside that window**, so
its `gw_scores` row can change at any point during it. Once the window closes
nothing revisits it, and the row is frozen in practice. The plan already states
this: *"Sub Bonus is provisional until the Gameweek leaves the 5-day active
window."*

This is the only definition under which "fully scored" means "will not change
again" — but it is **time-based, not state-based**: a gameweek leaves the
window because five days elapsed, not because anything confirmed it was
complete.

### Summary of what is available today

| Question | Answer from |
|---|---|
| Are all matches over? | `ml.fixtures.finished` — **yes, reliable** |
| Has the job run at all? | `gw_scores` row exists — **yes** |
| Which ruleset scored it? | `gw_scores.rules_version` — **yes** |
| Could the score still change? | inferred from the 5-day window — **only by inference** |
| Is this gameweek "done"? | **nothing records this** |

**If "the earliest gameweek that is not yet fully scored" is to become the
rule, it needs a definition chosen from the above, and (c) is the only one that
means "final".** Option (a) is the cheapest and is already computed per request
by the dashboard; combining it with the active window — all fixtures
`finished` **and** first kickoff older than 5 days — is the closest thing to
"fully scored" the current data supports without a new column.
