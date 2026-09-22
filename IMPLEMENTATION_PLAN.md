# Tactical Fantasy Football: Implementation Plan v1

Status: planning, agreed with the product owner on 2026-09-20.
Audience: Claude Code, working in the existing FPL-clone repo.
Read this whole file before doing anything. Work one phase at a time and STOP at every gate.

Source documents in the repo (read first): `ARCHITECTURE.md`, `ML_LAYERS.md`.
Baseline DDL reviewed: `public` v1.0 script. It is OLDER than the live schema, so trust `./schema_dump.sql`, not the v1.0 script.
**`schema_dump.sql` is a PRE-MIGRATION snapshot of `fpl_game`,** taken in Phase 0. It is valid only for that database, and only until Phase 4 migrates it. It does **not** describe `fpl_game_test`, which is already at `d58b3f10a7c2`. **Take a fresh dump after Phase 4.**

---

## 1. Rulebook v0.3 (locked decisions)

### Squad and lineup
| Area | Rule |
|---|---|
| Squad | 15 players: 2 GK / 5 DEF / 5 MID / 3 FWD. Budget £100.0m (existing half-profit selling rule kept). Max 3 per club. |
| Starting XI | 1 GK, at least 3 DEF, **at least 3 MID**, at least 1 FWD. The MID floor is 3, not the classic game's 2, which removes **exactly one formation: 5-2-3**. Maxima are implied by the 2/5/5/3 squad and are not a separate rule. |
| Bench | 4 players in fixed slots: **12 backup GK (Auto Sub)**, **13 outfield Auto Sub**, **14 and 15 Tactical Subs**. No player holds two roles. The bench is always exactly 1 GK + 3 outfield. |
| Deadline | Unchanged: one per Gameweek, first kickoff minus 90 minutes. Everything (XI, tactic, Bonus Players, bench roles, swaps) locks at the deadline. |

### Tactic and Bonus Players
| Area | Rule |
|---|---|
| Tactic | Chosen each Gameweek: `attack`, `defence` or `balanced`. |
| Bonus Players | Exactly 2, chosen from the starting XI. Attack = 2 FWD, Defence = 2 DEF, Balanced = 2 MID (literal positions; GK never Bonus). |
| Attack constraint | Needs at least 2 FWD in the XI, so 4-5-1 and 5-4-1 cannot choose Attack. Defence and Balanced are always available. |
| Bonus lock | A Bonus Player can never be the outgoing player of a Tactical swap. |
| No-show | A Bonus Player who does not appear loses Bonus status for that Gameweek. It does NOT pass to an Auto Sub. |
| Reset | XI, bench roles, tactic, Bonus Players and swaps are chosen again every Gameweek. |

### Substitutions
| Area | Rule |
|---|---|
| Auto Subs | Cover a starter with no appearance (0 minutes across all their fixtures that Gameweek). Backup GK covers only the starting GK. The outfield Auto Sub covers the **lowest slot number** whose replacement keeps the formation legal. An Auto Sub covering a Bonus Player earns General Points only. |
| Tactical Subs | 0, 1 or 2 planned swaps, set before the deadline. A Tactical Sub with no planned swap stays on the bench and scores 0. |
| Swap validity | Outgoing = a non-Bonus starter. Incoming = a Tactical Sub slot (14/15). **Same position** as outgoing. Both players must have a fixture that Gameweek. |
| Swap timing | Incoming player's first kickoff must be after the outgoing player's LAST fixture ends. "Ends" = kickoff + `FIXTURE_DURATION_MIN` (115). In a double Gameweek each player's fixtures are treated as one block. **Validated once at submission and locked at the deadline. Never re-validated if a kickoff later moves.** |
| Postponements | A fixture that is postponed or rescheduled is **not** re-validated against the swap it was part of. Points follow the Gameweek in which the fixture is **actually played**, as reported by the external API. A player whose fixture was postponed earns nothing that Gameweek and **counts as not having appeared** — so he can be Auto Sub covered, and if he was a Bonus Player he loses Bonus status. The 15-minute scoring job recomputes the active window, so a moved fixture corrects itself without manual intervention. |
| Swap scoring | Outgoing player's points are banked. The incoming player scores his own points. Both count. Up to 13 players can score in a Gameweek. |
| Swap edge cases | Outgoing never appeared: swap still happens, he scores 0. Incoming never appeared: the slot scores 0, no cover. **A slot involved in a swap is never Auto Sub covered.** |
| Sub Bonus | +1 per executed swap where the incoming player's full-Gameweek General Points are strictly greater than the outgoing player's. Max +2, which follows structurally from there being two Tactical Sub slots. **The engine does not enforce the cap** — the database already limits a team to two swaps, and Phase 3 validation must reject more than two. Shown separately. Note: if the outgoing player never appeared, the incoming player needs to score above 0 (accepted small giveaway). |

**The six roles** a scored selection reports per player. The dashboard renders these directly, and the last two are the ones easily confused:

| Role | Meaning | Counts towards the total? |
|---|---|---|
| `starter` | named in the XI and not otherwise disturbed | yes |
| `swapped_out` | the outgoing player of an executed Tactical swap; his points are banked | yes |
| `swapped_in` | the incoming Tactical Sub | yes |
| `auto_sub_cover` | a bench player who came on to cover a no-show; carries `covers_player_id` | yes |
| `auto_sub_replaced` | a starter who did not play **and was replaced** by an Auto Sub | **no** |
| `bench_unused` | a bench player who never came on | no |

A starter who did not play and was **not** replaced (no Auto Sub was available, or covering him would have broken the formation) stays `starter` and still counts, contributing 0. That is a different state from `auto_sub_replaced` and the two must not be collapsed.

### Points
| Area | Rule |
|---|---|
| General Points | Appearance 60+ min +2, 1-59 min +1. Goals GK +10 / DEF +6 / MID +5 / FWD +4. Assist +3. Clean sheet (60+ min) GK/DEF +4, MID +1, FWD 0. Defensive Contribution +2 (DEF 10 actions, MID/FWD 12). Goals conceded -1 per 2 (GK/DEF). Yellow -1, Red -3, Own goal -2, Penalty missed -2. Saves +1 per 3, Penalty saved +5. **No FPL bonus points, no captain, no chips.** Validated: this table reproduces the FPL archive totals (minus bonus) on 100% of rows. |
| Tactical Points (Bonus Players only, per fixture, summed across a double Gameweek) | **Attack:** goal +3, assist +2. **Defence:** clean sheet (60+ min) +2, plus Defensive Contribution tiers: 8+ actions +2, 10+ +3. **Balanced:** goal or assist +1 each, plus creativity tiers: 20+ +1, 40+ +3. **Tiers are NOT stacked: only the highest tier reached counts** (10 actions = +3, not +2 +3; creativity 39.9 = +1, not +3). These are rules **"Q1"**, judged against the balance spec in section 9. Values chosen so the three tactics have distinct personalities: Attack swingiest, Defence middle, Balanced steadiest, with average returns within about 3% for a form-based manager. They are tuned on 2025-26 only (35 Gameweeks) and MUST be re-tuned after about 20 Gameweeks of 2026-27. |
| Gameweek total | General Points of all scoring slots + Tactical Points + Sub Bonus. Resets each Gameweek; completed totals accumulate into the Season Total. |
| Transfers | **At the first Gameweek under these rules every manager has exactly 1 free transfer**, however long the season has been running. After that: **+1 per Gameweek, unused ones bank, cap 2** (was 5), minus transfers made. A manager who joins later replays the same recurrence from that same start Gameweek, so an empty history accumulates to the cap — there is no "joined at Gameweek N" concept. **No paid transfers, no hits**: transfers beyond the balance are rejected, not charged. `transfers.is_free` stays (always true). Creating the initial 15 is **not** a transfer and consumes no allowance — `Gameplay/squad_selection.py` writes only `user_squads` and `squad_players` and never touches the `transfers` table. |
| Removed | Chips (all four), captain / vice-captain, transfer hits, FPL bonus points. |

### Scoring timing
- Tactical Points and Sub Bonus are computed in the **same idempotent recompute pass** as General Points (the existing 15-minute job that upserts on `(user_id, season, gameweek)`). No separate per-match increments.
- Sub Bonus is provisional until the Gameweek leaves the 5-day active window.

---

## 2. Architecture decisions
- Convert classic FPL **in place** (no third game mode). Dream11 is untouched.
- Stack: FastAPI, Celery, SQLAlchemy Core with hand-written `text()` SQL (no ORM), Alembic (hand-authored), Postgres 18.4 on localhost:5432, DB `fpl_game` (tests use `fpl_game_test`).
- Existing gameplay data is dev-only and may be wiped.
- **The `ml` schema is read-only for the game layer.** `ml.player_gw_stats` also feeds the ML pipeline. No migration or game code may write to it or alter it. The scorer reads existing columns only: `minutes, goals_scored, assists, clean_sheets, goals_conceded, saves, penalties_saved, penalties_missed, own_goals, yellow_cards, red_cards, defensive_contributions` (plural in the DATABASE; the archive CSVs call it `defensive_contribution`, singular), `creativity` (verify it exists).
- **Do not delete** seasons `SIM38OK`, `SIM38TST`, `SIMSMOKE`. They belong to the `simulation/` harness. Game queries must filter to real seasons.
- Scoring reads per-fixture rows and sums them (double Gameweeks). Keys and ids stay in fpl-id space on the classic side.
- `Shared/rules.py` keeps its zero-import rule. New constants go there: `FREE_TRANSFER_BANK_CAP = 2`, `FIXTURE_DURATION_MIN = 115`, bench slot map, tactic definitions, `CURRENT_RULES_VERSION = 3`, and the Tactical Points tables as pure data:
  - Attack: goal 3, assist 2.
  - Defence: clean_sheet 2, `DC_TIERS = ((8, 2), (10, 3))`.
  - Balanced: goal_or_assist 1, `CREATIVITY_TIERS = ((20, 1), (40, 3))`.
  - Tier semantics: `(minimum, points)`, the HIGHEST tier reached counts (not stacked).
  - **`rules_version` is an INTEGER, not a string.** Phase 0 verified the live column
    is `gw_scores.rules_version smallint NOT NULL` (added by `c9a04e7b53d1`), holding
    values 1 (869 rows) and 2 (121 rows). The earlier `"tactical-v1"` would have raised
    at runtime in the scorer. The next generation is therefore **3**. It is stamped from
    `Shared/rules.py`'s `CURRENT_RULES_VERSION` (currently `= 2`, line 66) and written by
    `Results/scoring.py:398`; bump it to 3 in Phase 2/4, not in a migration.
- The live Alembic history has TWO heads: `a06f58d93f5a` and `c2f6a83e91d4`. Revision `c41a9e27d06b` merges them.

---

## 3. Data model (as committed)

Files: `backend/Migrations/versions/c41a9e27d06b_drop_classic_only_objects.py` and
`d58b3f10a7c2_add_tactical_game_structures.py`.

**Status:** reconciled against the live schema in Phase 0 and fixed in Phase 1
(commit `2f0a8f8`), rehearsed on a scratch copy of `fpl_game` (guard refusal, upgrade,
schema verification, downgrade round trip, full test suite), and **applied to
`fpl_game_test`**. **NOT applied to `fpl_game`** — that happens in Phase 4 — **and not
to the server.** See section 10.

Every statement below is taken from the committed files and cites its line.

### Revision 1 — `c41a9e27d06b`, "Drop classic-only objects"

**Merges the two heads.** `down_revision = ("a06f58d93f5a", "c2f6a83e91d4")` (line 20);
the tuple form is what makes this a merge revision.

**The guard, `_refuse_to_wipe_real_data()` (lines 25-60).** Runs before anything else
(line 64). It sums rows across six tables unconditionally — `gw_selections`,
`gw_scores`, `transfers`, `leaderboard_snapshots`, `cancelled_transfers`,
`user_gameweek_finance` (lines 36-47) — then adds a **conditional** seventh count:

```sql
SELECT COUNT(*) FROM league_h2h_fixtures WHERE result IS NOT NULL   -- line 53
```

Conditional because this revision only resets that table's derived columns: an unplayed
row loses nothing and must not trip the guard (lines 48-51). If the total is non-zero
and `ALLOW_GAMEPLAY_WIPE` does not equal `current_database()`, it raises and nothing is
changed (lines 55-60).

**The TRUNCATE list (lines 96-107)** — eight tables, `RESTART IDENTITY`, deliberately
no `CASCADE` (lines 69-72):

```
leaderboard_snapshots, gw_scores, user_gameweek_finance,
starting_xi, gw_selections, chips, cancelled_transfers, transfers
```

- `cancelled_transfers` **must** be in the list: it carries
  `cancelled_transfers_transfer_id_fkey -> transfers(id)`, and Postgres refuses to
  truncate a table referenced by an FK from one that is not also being truncated —
  without it the statement aborts and the revision fails (lines 74-78).
- `user_gameweek_finance` **is** truncated, reversing the earlier "keep it" note
  (lines 80-89). `Results/scoring.py` writes one row per scored gameweek, so keeping
  them while emptying `gw_scores` would show a real bank and team value beside
  gameweeks that no longer have a score.

**League standings reset (line 108):**

```sql
UPDATE league_members SET season_points = 0, rank = 0, last_gw_points = 0
```

**`league_h2h_fixtures` — targeted three-column reset, pairings kept (lines 135-143).**
This table is **not** truncated (lines 91-95): `_generate_and_insert_h2h_schedule`
writes the season's **pairings** upfront and regeneration is gated on "no rows for this
league+season", so truncating would reshuffle who plays whom.

```sql
UPDATE league_h2h_fixtures
   SET points_1 = NULL, points_2 = NULL, result = NULL
 WHERE points_1 IS NOT NULL OR points_2 IS NOT NULL OR result IS NOT NULL
```

The pairing columns (`league_id, season, gameweek, user_id_1, user_id_2`) are never
touched. All three reset columns are recomputed by
`Results/standings.py::_process_h2h_league` from `gw_scores` (lines 114-124), and
`'bye'` survives because both the writer and the reader derive it from
`user_id_2 IS NULL`, never from `result` (lines 126-129).

**Chips removed (lines 150-152):** `DROP TRIGGER IF EXISTS enforce_chip_limit ON chips`,
`DROP FUNCTION IF EXISTS enforce_chip_limit_fn()`, `DROP TABLE IF EXISTS chips`.

**Captaincy columns dropped (lines 157-167):** `gw_selections.captain_id`,
`vice_captain_id`, `chip_used`; `starting_xi.is_captain`, `is_vice_captain`.

**Hit columns dropped (lines 173-177):** `gw_scores.transfer_hits`, `hit_deductions`.
`transfers.is_free` is left in place, always TRUE from now on (line 171).

### Revision 2 — `d58b3f10a7c2`, "Add tactical game structures"

**`gw_selections.tactic` — added with a default that is then dropped (lines 40-46):**

```sql
ALTER TABLE gw_selections
    ADD COLUMN tactic VARCHAR(10) NOT NULL DEFAULT 'balanced'
        CONSTRAINT ck_gw_selections_tactic
        CHECK (tactic IN ('attack', 'defence', 'balanced'));
ALTER TABLE gw_selections ALTER COLUMN tactic DROP DEFAULT;
```

The default backfills whatever is present, and dropping it immediately restores the
intent that every future row states its tactic explicitly. This removes a silent
order-dependency on the previous revision's truncate (lines 33-39).

**`starting_xi` (lines 59-77):** `is_bonus BOOLEAN NOT NULL DEFAULT FALSE`, and `role`
as a **generated stored column** derived from `position_slot` (`<= 11` starter, `12`
auto_gk, `13` auto_outfield, else tactical) so the two can never disagree. Then two
constraints:

- `ck_starting_xi_bonus_is_starter CHECK (NOT is_bonus OR position_slot <= 11)` —
  Bonus only on starters.
- `uq_starting_xi_sel_slot UNIQUE (gw_selection_id, position_slot)` — **this was
  genuinely added.** Phase 1 confirmed no equivalent existed; the pre-existing
  `uq_starting_xi_sel_player` is on `(gw_selection_id, player_id)`, a different pair.

**No slot widening happens (lines 51-57).** Phase 0 verified the live
`starting_xi_position_slot_check` is *already*
`CHECK (position_slot >= 1 AND position_slot <= 15)` — the bench already lives at slots
12-15. An earlier draft dropped and re-added it as `ck_starting_xi_slot` believing the
live check was 1-11; that was a rename of an equivalent constraint. Left exactly as it
is.

**`tactical_swaps` (lines 84-94):** `gw_selection_id` FK with `ON DELETE CASCADE`,
`player_out_id`, `player_in_id`, plus `ck_swaps_distinct`, `uq_swaps_sel_out` and
`uq_swaps_sel_in`. At most 2 rows per selection follows from `player_in` having to be
one of the two tactical bench slots.

**Three trigger functions, four triggers:**

| Function | Trigger(s) | Enforces |
|---|---|---|
| `enforce_bonus_count_fn` (101-127) | `enforce_bonus_count` on `starting_xi`, `DEFERRABLE INITIALLY DEFERRED` (128-133) | exactly 2 Bonus Players, checked at COMMIT so the app can delete-and-reinsert the XI in one transaction; skipped when the selection has no rows |
| `enforce_swap_refs_fn` (141-166) | `enforce_swap_refs` on `tactical_swaps`, deferred (167-172) | outgoing player is a non-Bonus starter; incoming is a Tactical Sub on the same selection |
| `enforce_child_lock_fn` (179-203) | `enforce_starting_xi_lock` and `enforce_tactical_swaps_lock`, created in a loop over both tables (204-209) | child rows cannot change once `gw_selections.is_locked`; a missing parent counts as unlocked so `ON DELETE CASCADE` keeps working |

**`gw_scores` score columns (lines 226-230):** `tactical_points SMALLINT NOT NULL
DEFAULT 0` and `sub_bonus SMALLINT NOT NULL DEFAULT 0`. Convention going forward
(lines 212-215): `raw_points` = General Points, `final_points` = `raw_points +
tactical_points + sub_bonus`, `total_points` = `final_points` (no hits any more).

**`rules_version` is NOT added (lines 217-224).** It already exists as `smallint NOT
NULL`, added by `c9a04e7b53d1`, with live values 1 and 2. The draft carried
`ADD COLUMN IF NOT EXISTS rules_version VARCHAR(20)`, which was a silent no-op against
that column — the migration would pass and the scorer would then fail at runtime trying
to store a string in a smallint. The version stays an integer; the next generation is 3,
set in `Shared/rules.py` in a later phase, not in a migration.

**Nothing in the `ml` schema is changed (lines 232-238)**, on purpose. The game layer
only ever reads it, and the columns the new scorer needs already exist.

### What neither migration touches: `free_hit_squads`

**Both revisions leave `free_hit_squads` in place.** It is not in the TRUNCATE list, it
is not dropped, and nothing in either file references it. Phase 1 verified it survives
the upgrade (it happened to hold 0 rows already).

That is deliberate ordering, not an oversight: `revert_free_hits` still reads the table,
so dropping it now would break running code. **It is dropped by a small migration in
Phase 4, after that code is removed** — the same rule the rest of this release follows,
that the schema change and the code change travel together.

### Revision 3 — `e7c4d81b3a95`, "Add ruleset_epochs"

Added in Phase 3 to anchor the free-transfer recurrence (decision B1, option B).
`down_revision = "d58b3f10a7c2"`.

Creates `ruleset_epochs (season VARCHAR(9), rules_version SMALLINT,
first_gameweek SMALLINT, created_at TIMESTAMPTZ DEFAULT now(),
PRIMARY KEY (season, rules_version))`, plus
`enforce_ruleset_epochs_immutability`, a `BEFORE UPDATE OR DELETE` trigger that
raises — the same append-only pattern `transfers` uses. **No foreign keys**, so
nothing cascades into it and no other table's deletions can move the anchor.

It then inserts **one row for this database**, `ON CONFLICT DO NOTHING`:

- **Current season**, database-side: the season holding the next kickoff still
  to come, restricted to real season codes with
  `season ~ '^[0-9]{4}-[0-9]{2}$'` (which excludes `SIM38OK`, `SIM38TST` and
  `SIMSMOKE`). The application's own `current_live_season()`
  (`Data/fpl_ingest.py:187`) asks the FPL API, which a migration must not do.
- **First Gameweek**, rule D5: the first Gameweek whose deadline —
  `MIN(kickoff_time) - interval '90 minutes'`, the same expression
  `Shared/deadlines.py` builds — is still in the future at migration time.
- **Nothing to insert is a normal outcome**, not a failure: a fresh CI database
  has no fixtures, and between seasons there is no future kickoff. Both print a
  notice and insert nothing.

The computed row is printed in the migration output. **The epoch is per
database** — dev, test and the server are released at different moments, so
each computes its own. `downgrade()` drops the table; the stored epoch is data
and is not recoverable from the schema.

### Revision 4 — `f2b9c05e7a41`, "Drop free_hit_squads"

Added in Phase 4c. `down_revision = "e7c4d81b3a95"`.

Drops `free_hit_squads`, the Free Hit snapshot table. Chips went in Phase 1;
this table survived because `revert_free_hits` still read it, and dropping a
table out from under running code turns a dead feature into an outage. Phase 4c
deleted the task, the Beat entry, `GameEngine/free_hit_revert.py` and the last
dead `text()` statements naming the table, and only then dropped it.

Checked and quoted in the migration: **no triggers, no incoming foreign keys**.
Its three indexes belong to the table and go with it. `downgrade()` recreates
the shape only, reproduced from `c4e1a7b92f30`; the snapshots are data and are
not recoverable, and would be meaningless if they were.

### Revision 5 — `b4e1f37c920d`, "Add gameweeks: when a gameweek was finished being scored" (Phase 4e)

`down_revision = "f2b9c05e7a41"` (Phase 4c's `free_hit_squads` drop, which was head).

```sql
CREATE TABLE IF NOT EXISTS gameweeks (
    season    VARCHAR(9)  NOT NULL,
    gameweek  SMALLINT    NOT NULL,
    scored_at TIMESTAMPTZ,
    PRIMARY KEY (season, gameweek)
)
```

**Why a new table at all.** Nothing in this schema recorded *"this gameweek is done"*.
Every pre-existing signal is an inference:

| Signal | What it actually says |
|---|---|
| `ml.fixtures.finished` | the matches are over — not that they were scored |
| a `gw_scores` row | the job ran once — not that it will not run again |
| the 5-day active window | "probably final" by **elapsed time**, not by state |

`scored_at` is the first state-based answer, and section 5's Phase 4e rewrite of
`GET /gameweeks/current` is built on it.

**Written by `Results/scoring_job.py::_mark_gameweek_scored`, under two conditions,
both required:** the batch run completed, **and** every fixture in that gameweek has
`finished = TRUE`. Scores written mid-play are persisted but deliberately left
unmarked, because the 15-minute job revisits them for five days and the numbers move.
A gameweek with **no fixtures at all** is not marked either — `bool_and` over zero rows
is NULL, and the explicit `total = 0` check makes that intent visible rather than
resting on the quirk.

**Idempotent** via `ON CONFLICT (season, gameweek) DO UPDATE ... WHERE
gameweeks.scored_at IS NULL`, so five days of re-runs keep the timestamp of the run
that *first* completed it. Without the `WHERE` this silently becomes a "last touched"
column. `_mark_gameweek_scored` never raises: a failed mark must not turn a successful
scoring run into a failed one.

**`scored_at` is NULLABLE on purpose.** A row with NULL means "known about, not
finished" — a different statement from having no row, and the current-gameweek query
reads both as "not scored".

**No foreign keys.** Seasons and gameweeks are not entities here; they exist as
`(season, gameweek)` pairs across `ml.fixtures`, `gw_selections` and `gw_scores` with
no parent table to point at. An FK would invent one and make this table deletable by
cascade — exactly the property `ruleset_epochs` was designed to avoid.

Rehearsed on a scratch copy recreated from a fresh `pg_dump` of `fpl_game` (upgrade →
shape check → downgrade → upgrade), then applied to **`fpl_game_test` only**.
`fpl_game` is untouched, still on `a06f58d93f5a` + `c2f6a83e91d4`.

### Downgrade semantics

**Structure only. Wiped data is NOT restored** — not the truncated tables, and not the
H2H results, which are data rather than schema. Recovery is the backup taken before the
run.

Revision 2's downgrade drops exactly what it added, and deliberately **keeps the bench
rows**: slots 12-15 were legal before it ran and remain legal after it is undone
(lines 241-275). An earlier draft deleted every `starting_xi` row above slot 11 and
restored a 1-11 check; both were wrong and are gone.

**Working syntax past the merge revision.** `alembic downgrade -1` works once, to the
mergepoint; a second `-1` fails with `ERROR: Ambiguous walk`, and `c41a9e27d06b^` cannot
be located. Name the parent instead, which restores both heads:

```
alembic downgrade a06f58d93f5a
```

### Phase 0 findings (historical, all four resolved)
1. Live schema differs from v1.0 (half-season chips, transfer cancellations, transfer
   drafts, `league_h2h_fixtures`, free-hit snapshot); find every live-only table
   referencing one being truncated or altered. — **RESOLVED:** `cancelled_transfers`
   found blocking the TRUNCATE and added to the list; `league_h2h_fixtures` handled by
   the targeted column reset.
2. Where the bench is stored today (v1.0 only allows slots 1-11). — **RESOLVED:** it is
   already at slots 12-15 and the live check already allows 1-15, so no widening is
   needed and the draft's downgrade would have destroyed the bench.
3. Auto-generated constraint names may differ. — **RESOLVED:** the live name is
   `starting_xi_position_slot_check` and it is left untouched; `ck_starting_xi_slot` is
   never created.
4. Whether `ml.player_gw_stats.creativity` exists. — **RESOLVED: both columns exist.**
   `PHASE0_REPORT.md` §3 ("`ml.player_gw_stats` columns (step 3) — both present")
   queried the live database:

   ```
          column_name       | data_type
   -------------------------+-----------
    creativity              | numeric
    defensive_contributions | smallint
   ```

   Corroborated by `schema_dump.sql`, inside `CREATE TABLE ml.player_gw_stats (`
   which opens at line 364:

   ```
   386:    creativity numeric(6,1),
   401:    defensive_contributions smallint DEFAULT 0 NOT NULL
   ```

   `creativity` is `numeric`, so the Balanced tier thresholds compare cleanly without
   integer rounding — which matters for the 19.9-vs-20 boundary case. The database
   spelling is the **plural** `defensive_contributions`; the archive CSVs use the
   singular.


---

## 4. Code change map (discover, do not assume)
Find real file names first. Expected areas:
- `Shared/rules.py`: constants above; remove chip/hit constants.
- Scoring (Results): pure functions, listed in section 6.
- Gameweek engine: remove `revert_free_hits` and any chip lifecycle; keep window/lock/finalize logic.
- Selection endpoint (`/gw_selection`): replace captain/vice/chip with tactic, Bonus Players, bench roles, swaps. Collect all validation errors at once (existing 422 pattern).
- Transfers endpoint: cap 2, no hits, reject overspend. Keep budget and club-limit validation.
- Remove chip endpoints (`/chips/used` etc.) and chip UI.
- Frontend: pick-team screen (tactic selector, Bonus picker, bench roles, swap planner), scoring rules screen (remove Hit Penalty and Chip tiles, add Tactical Points and Sub Bonus, reword "Standard Fantasy Premier League rules"), team dashboard showing General / Tactical / Sub Bonus.

---

## 5. Phases and gates
**Phase 0. Verify (read-only). — DONE.** Backup, schema review of the two drafts against `schema_dump.sql`, dependency scan for every use of columns being dropped, ML dependency scan. Delivered `PHASE0_REPORT.md` with 2 blockers and 3 mismatches. **GATE passed.**
**Phase 1. Migrations. — DONE** (commit `2f0a8f8`, `PHASE1_REPORT.md`). Both drafts fixed. Rehearsed on a scratch copy of `fpl_game`: guard refusal, `upgrade head`, schema verification, downgrade round trip (`alembic downgrade a06f58d93f5a`, then `upgrade head`), full test suite. Applied to **`fpl_game_test` only**; 179 of 182 failures are EXPECTED (they use a removed column or table), the other 3 fail identically before the migration.
  - **Remaining for `fpl_game`:** still on `a06f58d93f5a` + `c2f6a83e91d4`. It is migrated in **Phase 4**, alongside the code that needs the new columns — not before, because the migration drops columns today's code still selects.
  - **Remaining for the server (`pitchside_db`):** untouched and its state unverified. Released in one step with the code, per section 10. The branch is not pushed or merged until Phase 4 is finished.
**Phase 2. Rules and scoring engine.** Constants plus pure functions with tests first. No DB in the unit tests. **GATE: tests green.**
**Phase 3. API.** Selection and transfer endpoints with validation, and their tests. **GATE: tests green.**
  - **Formation rule (D1).** Enforce 1 GK, >=3 DEF, **>=3 MID**, >=1 FWD. The classic code still enforces a 2-MID minimum and Phase 2 deliberately did not touch it. Every place that encodes the old minimum, to change here:

    | File | Line | What it encodes |
    |---|---|---|
    | `backend/Gameplay/starting_xi.py` | 383-384 | `if not (2 <= mid_count <= 5)` and the message `"MID count must be between 2 and 5, got {mid_count}"` — the live validation |
    | `backend/Results/scoring.py` | 212 | `2 <= counts.get("MID", 0) <= 5` in `_formation_legal`, used by the classic autosub |
    | `backend/Results/scoring.py` | 30 | docstring: "the resulting formation (DEF 3-5, MID 2-5, FWD 1-3)" |
    | `frontend/src/pages/StartingXI/StartingXIPage.jsx` | 33 | `{ label: '5-2-3', counts: { DEF: 5, MID: 2, FWD: 3 } }` — the formation this decision removes; delete the option |
    | `frontend/src/pages/StartingXI/StartingXIPage.jsx` | 75 | `counts.MID < 2 \|\| counts.MID > 5` and the message "Midfielders must be between 2 and 5" |

    Not in the list, checked and excluded: `backend/Game_logic/dream11.py:893` already requires 3-5 MID but is **Dream11 and out of scope**; `backend/Tests/test_starting_xi.py:177` builds a 2-MID XI only to assert a *DEF* error; `backend/Tests/test_scoring.py:200` exercises formation legality with 4 MID and does not encode the floor.
  - **Two-swap validation (D7).** Reject more than two Tactical swaps at the endpoint. The database limits it (`uq_swaps_sel_in`, and only slots 14/15 carry `role = 'tactical'`) and the scoring engine deliberately does not check it — hand the engine three swaps and it returns a Sub Bonus of 3. The endpoint is the place this is caught.
**Phase 4a. The scoring job. — DONE** (`PHASE4A_REPORT.md`). The batched tactical job is live: `Results/scoring_job.py::score_gameweek_tactical`, `SCORING_BATCH_SIZE = 200`, one stats query per batch, season filter, epoch gate, advisory validation (A7). `GameEngine/gameweek_finalize.py` and `Worker/tasks.py` are repointed. The idempotent upsert and the `user_gameweek_finance` write are preserved exactly. 1,000 managers score in 1.53s at 1.16 MiB peak. Suite: 0 pass→fail, 0 removed, 3 skip→pass, 105 EXPECTED / 3 UNEXPECTED.
  - The three `test_transfer_concurrency.py` tests were re-pinned **first**, and mutation-tested against a disabled advisory lock.

**Phase 4b. The dashboard read model. — DONE** (`PHASE4B_REPORT.md`). `GET /team` computes each manager's per-player points at request time by calling the tactical engine for that manager and gameweek — no new tables. `resolve_autosubs` is no longer imported from the classic scorer. The response is additive: every existing key is still present (captaincy and chip keys are inert, always null/false/0), plus `tactic`, per-player `role`/`is_bonus`/`general_points`/`tactical_points`/rule breakdowns, per-swap detail, manager totals, `provisional` and `scored`. Suite: 0 pass→fail, 0 removed, 14 fail→pass, 91 EXPECTED / 3 UNEXPECTED. Cost: 14 queries, 162 ms for one manager.
  - **E4 closed:** a failed batch write now falls back to per-manager writes with a SAVEPOINT each, and `refresh_active_gameweeks` no longer discards the scoring summary — a gameweek with failed managers is reported as failed, not refreshed.
  - **E1 closed:** `allow_sim_seasons` is an explicit argument passed only by `Tools/fpl_sim.py`. **No production caller of the classic `score_gameweek` remains.**
  - **E2, E5, E6, E7 and F1 to F6 are closed** — see section 8. **E3 stays open until Phase 4c.**

**Phase 4c. The classic removals. — DONE** (`PHASE4C_REPORT.md`). `Results/scoring.py` is deleted; no module imports it anywhere. `GameEngine/free_hit_revert.py`, the `revert_free_hits` task and its Beat entry are gone, and migration `f2b9c05e7a41` drops `free_hit_squads`. `MAX_TRANSFERS_PER_GAMEWEEK`, the `FREE_CHIPS` import and the `chip_active` placeholder are gone from `transfers.py`, and `_validate_transfers`' `allowance` is required. Suite: 0 pass→fail, 26 tests removed (all classic-only), 71 EXPECTED / 3 UNEXPECTED.

**Phase 4d. The two live breaks. — DONE.** Both classic references that would have 500'd the moment `fpl_game` is migrated are gone.
  - **`GET /chips/used` deleted**, with `Gameplay/chips.py` entirely — nothing else used it (`starting_xi.py` imported four names from it and used none). Ten tests removed, all exercising chip availability.
    - **The frontend still calls this route** and now gets a 404: `frontend/src/api/chips.js:3` (`fetchChipsUsed`), used by `frontend/src/pages/StartingXI/StartingXIPage.jsx:21,290,647` on page load and after each submit. **Phase 5 must remove those calls.** Not editable here by instruction, but it is a user-visible consequence, not a tidy-up.
  - **`CAPTAIN_QUERY` deleted** from `Context_assembler/main.py`. It read `gw_selections.captain_id` on **every `POST /chat` request**. Nothing in the response depended on it — `ChatResponse` is a single `response: str` — so it was deleted outright rather than nulled; the additive rule only applies to fields the frontend renders. The `[CURRENT CAPTAIN]` instructions went with it, so the model is no longer told to expect a tag that can never appear.

**Phase 4e. The current gameweek, and locked/scored state on the dashboard. — DONE.** Three fixes, one theme: `GET /gameweeks/current` was answering the wrong question, in the wrong season.
  - **A. The season leak.** `Game_logic/fixtures.py::CURRENT_GAMEWEEK_QUERY` ranked deadlines across **every** season in `ml.fixtures` with no filter, and the simulation seasons carry real timestamps — so a `SIM38OK` fixture could win and every page in the app would show a simulation gameweek. Proven first by `test_a_simulation_season_never_wins_the_current_gameweek`, which asserted `'SIM38OK' != 'SIM38OK'` before the fix. Filter added: `season ~ '^[0-9]{4}-[0-9]{2}$'`.
    - **Six other production queries have the same missing filter** and are **not** fixed: `GameEngine/gameweek_finalize.py:60`, `GameEngine/gameweek_lock.py:59` and `:71`, `Game_logic/live_poll.py:76` and `:123`, `Game_logic/prediction_scheduling.py:32`. The last one **corroborates** a long-standing UNEXPECTED failure — `test_schedule_predictions_no_op_when_nothing_needs_predicting` — which is the same root cause, now confirmed structurally rather than guessed. Six further Dream11 queries are out of scope.
  - **B. "Current" now means "earliest not yet scored".** The old rule was "the gameweek whose deadline is soonest in the future", which moves the instant a deadline passes — so from Saturday 11:30, while gw8 was still being played and its scores still changing, every page already showed gw9. `test_a_locked_but_unscored_gameweek_is_still_current` asserted the broken answer (`assert 9 == 8`) before the rewrite. A gameweek now stays current through its deadline, kickoff, the 90 minutes and the scoring run, and stops being current only when `gameweeks.scored_at` is set.
    - **Latest season only**, and this is the subtle part: `ml.fixtures` holds years of history that was never marked scored, because `scored_at` did not exist when it was played. Ranking unscored gameweeks across all seasons would return the first gameweek of the *oldest* season, forever. `max(season)` is well defined because the real-season filter admits only `YYYY-YY`, where lexical and chronological order coincide.
    - **The two deadline tiers are kept below the scored rule**, as the fallback for the two cases where it selects nothing: no real-season fixtures ingested at all (season start → `found=false`, not a 404), and every gameweek of the latest season scored (end of season → the most recent past gameweek, which renders read-only rather than inviting an edit).
  - **A-bis. CORRECTION — the season filter was not tight enough, and B made it worse.** `^[0-9]{4}-[0-9]{2}$` excludes the `SIM*` seasons but **not** the numeric sentinels. `fpl_game` holds a season literally named **`9998-00`** (one fixture, gameweek 5), and the test suite's `TEST_SEASON` is **`9999-00`**. Both match four-digits-dash-two, and both sort **above** every real season, so B's `max(season)` selected them outright. Measured against a `pg_dump` clone of `fpl_game`:

    ```
    OLD (deadline-based)     -> ('2026-27', 6)
    '^[0-9]{4}-[0-9]{2}$'    -> ('9998-00', 5)   <-- wrong season, live
    '^20[0-9]{2}-[0-9]{2}$'  -> ('2026-27', 5)
    ```

    Note the direction: **the latest-season clause made this worse than the rule it replaced.** Under the old deadline ranking `9998-00` lost because its single kickoff was in the past; under `max(season)` it wins. Fixed in `Game_logic/fixtures.py` by requiring the century prefix, `^20[0-9]{2}-[0-9]{2}$`. Found only by running against real data — no test would have caught it, because the suite's own season is one of the two values that slip through.
    - **`Results/scoring_job.py:63` still has the loose pattern** (`REAL_SEASON_RE = ^\d{4}-\d{2}$`), so the scoring job would treat `9998-00` as a real season. Deliberately **not** changed in the same commit: `TEST_SEASON` is `9999-00` and the entire suite depends on the job accepting it, so tightening it means migrating `TEST_SEASON` first. Decide together with the six unfiltered queries above.
    - **Known consequence, accepted:** tightening `fixtures.py` alone means the Part A/B tests, which use `TEST_SEASON = '9999-00'` as a stand-in for a real season, no longer hold. They were not re-run (testing paused by instruction) and must be reconciled when the suite resumes — either by moving `TEST_SEASON` to a `20YY-YY` value or by pinning these two queries to a fixture season.
  - **C. The `gameweeks` table** — migration `b4e1f37c920d`, section 3, Revision 5.
  - **D. `GET /team` returns `is_locked`**, computed with the formula already in `Gameplay/transfers.py:447-448`, copied rather than re-derived: `selection_row is not None and selection_row.is_locked`, OR'd with `deadlines.deadline_has_passed`. `Gameplay/starting_xi.py` enforces the identical pair by a different mechanism (the `enforce_selection_lock_fn` trigger plus the same deadline pre-check). All three now agree by construction; a fourth implementation would let the dashboard invite an edit the write endpoints then reject with a 422.
  - Suite: **0 pass→fail, 0 removed**, 12 added (all passing), 63 EXPECTED / 3 UNEXPECTED — identical split before and after.

### Dashboard state: the four states a gameweek is actually in

`has_lineup` was carrying this alone and cannot: it is `False` both the day before the deadline, when the manager can still act, and a week after it, when they cannot. Three fields separate the four real states.

| State | `has_lineup` | `is_locked` | scored (`gameweeks.scored_at`) | What the manager should see |
|---|---|---|---|---|
| **1. No squad yet** | `false` | `false` | `null` | "Pick your squad" — nothing exists, and this is not an error |
| **2. Squad, no lineup, pre-deadline** | `false` | `false` | `null` | "Set your lineup" — still editable, deadline shown as a countdown |
| **3. Locked, in progress** | `true` *or* `false` | **`true`** | `null` | Read-only. Points are **partial** and will change; `provisional` is `true` |
| **4. Scored** | `true` | `true` | set | Final. The number will not move again |

States 1 and 2 are distinguished by the squad, not by these three fields — `GET /squad` returns an empty list in state 1. State 3 covers a manager who never submitted **and** one who did: the never-submitted case is exactly why `is_locked` cannot be read from the `gw_selections` flag alone.

**`scored` is the only state-based signal of finality.** Before Phase 4e the dashboard inferred it — all fixtures `finished` means the matches are over, not that they were scored, and the 5-day active window means "probably final" by elapsed time. `gameweeks.scored_at` is written only when the batch run completed *and* every fixture finished, so states 3 and 4 are now distinguishable rather than guessed at.

### Pre-4e checklist: everything still naming a Phase-1-dropped object

Found by scanning `text()` blocks in production code (Dream11, tests and migrations excluded). **Nothing here is fixed yet — decide before 4e.**

| File:line | Names | Reachable? |
|---|---|---|
| `Gameplay/starting_xi.py:262` | `DELETE_CHIP_FOR_GW_STMT` → `chips` table | **DEAD.** Defined, never executed; the only other mention is a docstring. Phase 3 orphaned it. |
| `Gameplay/starting_xi.py:266` | `INSERT_CHIP_STMT` → `chips` table | **DEAD**, same reason. |
| `Tools/fpl_sim.py:94` | `captain_id`, `vice_captain_id`, `chip_used` | **EXECUTED, not request-reachable.** `SELECTION_STATE_QUERY`, run by the `status` subcommand at `:200`. A dev tool, so no user hits it — but it will error the moment anyone runs it against a migrated database. |
| `Tools/fpl_sim.py:140` | `chips` table | **EXECUTED, not request-reachable.** `CHIPS_STATE_QUERY` at `:204`. |
| `Tools/fpl_sim.py:150` | `transfer_hits`, `hit_deductions` | **EXECUTED, not request-reachable.** `SCORES_STATE_QUERY` at `:206`. |

**No production SQL reachable by an HTTP request names a dropped object any more.** The remaining mentions in `Results/team_dashboard.py` (lines 189-190, 239, 251-252, 351, 417-418, 447-448, 556, 561-562) and `Gameplay/transfers.py` are **response fields set to constants** — `False`, `0`, `None` — kept deliberately so the current frontend renders, with no database access behind them. They are Phase 5's, listed below.

**Phase 5. Frontend. — NOT STARTED.** The backend is fully converted; the classic React app is not. This entry previously carried seven sub-bullets that were **Phase 4 items mis-parented here**, and the header appeared twice. Where each of those actually landed: re-pinning `test_transfer_concurrency.py` → 4a; B5 `MAX_TRANSFERS_PER_GAMEWEEK`, B6 `chip_active`/`FREE_CHIPS`, B7 required `allowance`, and the `free_hit_squads` drop (`f2b9c05e7a41`) → 4c; the season filter → 4e part A, **with six production queries still unfiltered** (listed under 4e). Only one is still open and it is restated below: **migrating `fpl_game`**.

#### What is actually broken today (measured, not inferred)

Against the running app — backend on `:8000`, frontend on `:5173` — with a registered user:

- **`POST /gw_selection` fails for every manager.** `frontend/src/api/gwSelection.js:12-32` sends `captain_id`, `vice_captain_id` and `chip_used`; `Gameplay/starting_xi.py:296-306` requires `tactic` and `bonus_player_ids`. Sending exactly the body the frontend builds returns:

  ```
  POST /gw_selection -> 422
      missing ['body', 'tactic'] Field required
      missing ['body', 'bonus_player_ids'] Field required
  ```

  This is not cosmetic drift — **the Starting XI page cannot submit at all.** It is the single highest-priority item in the phase.
- **`GET /chips/used` 404s on page load.** `frontend/src/api/chips.js:3-5`, called from `pages/StartingXI/StartingXIPage.jsx:21,290,647`. Deleted in 4d with `Gameplay/chips.py`.
- **`GET /gw_selection` no longer returns what the page reads.** Live keys: `bench_order`, `bonus_player_ids`, `gameweek`, `has_selection`, `player_ids`, `season`, `swaps`, `tactic`, `user_id`. No `captain_id`, no `vice_captain_id`, no `chip_used`.
- **`GET /team` returns both models at once**, which is why the classic dashboard still renders rather than crashing. The Phase 4b inert keys (`captain_multiplier` = 1, `chip_used` = null, `captain_bonus`/`transfer_hits`/`hit_deductions` = 0) sit alongside the real ones: `tactic`, `is_locked`, `general_points`, `tactical_points`, `sub_bonus`, `swaps`, `scored`, `scored_reason`, `provisional`, `score_source`, `has_score`, `live_status`.
- **No classic frontend file reads a single tactical field.** `grep -rln "tactic\|is_locked\|sub_bonus\|tactical_points"` over `frontend/src` matches only `api/dream11.js`, `pages/Dream11/*` and `pages/Matches/MatchDetailPage.jsx` — all out of scope.

#### File-by-file change map

| File | What it encodes | Change |
|---|---|---|
| `api/gwSelection.js:12-32` | sends `captain_id`/`vice_captain_id`/`chip_used` | send `tactic`, `bonus_player_ids`, `swaps`; drop the other three |
| `api/chips.js` | `fetchChipsUsed` → deleted route | delete the file |
| `pages/StartingXI/StartingXIPage.jsx` | `CHIP_TYPES` (`:40`), `chipAvailable` (`:45-49`), captain/vice validation (`:62-81`), captain popover (`:115,140,143,262`), chip state (`:252,260`) | the largest rewrite: tactic selector, 2 Bonus Players, bench **roles** not 1st/2nd/3rd/4th, swap planner |
| `pages/DashboardPage/DashboardPage.jsx` (570 lines) | C/V badges (`:44,64,377`), `bench_boost` (`:356`), `captain_multiplier` (`:379`), Captain Bonus tile (`:424-426`), `hit_deductions` (`:432`) | the four states; General/Tactical/Sub Bonus replacing Captain Bonus and Hit Penalty |
| `pages/Transfers/TransfersPage.jsx:126` | `chip_active` | drop; allowance is a flat 2, no hits |
| `data/scoringRules.js:95,101,208,215,225-240,255` | Hit Penalty, Chip Boost, Captain Multiplier, Triple Captain, Vice-Captaincy Protocol, Bench Boost, Rollover Cap | rewrite for the tactical ruleset |
| `components/PlayerJersey`, `PlayerMarker`, `PlayerPointsSheet`, `OpponentTeamPanel` | `captain` / `viceCaptain` props | Bonus star replaces the armband |
| `pages/LandingPage/LandingPage.jsx` | marketing copy naming captaincy/chips | reword |

#### Order of work, each its own commit

- **5a. Unbreak submission.** `api/gwSelection.js` + the minimum of `StartingXIPage` to send `tactic` and `bonus_player_ids`. Delete `api/chips.js` and its three call sites. After this the app is usable again.
- **5b. Starting XI proper.** Tactic selector, Bonus Player picker (exactly 2, both starters, both the tactic's position), bench roles (slot 12 backup GK, 13 Auto Sub, 14/15 Tactical Subs), swap planner capped at 2. The bench is already drag-ordered via `@dnd-kit` — the ordinals become roles, so the interaction survives.
- **5c. Dashboard.** The four states from the "Dashboard state" note above, driven by `has_lineup`, `is_locked` and `scored`. Reference designs exist: the Stitch project *FPL Chat Assistant* (`6082680430787009104`) has **Dashboard — No Squad & GW Locked** (`192f20ee…`) and **Dashboard with Horizontal Reorder Bench** (`48c92560…`), and the canvas *PitchSide Dashboard States* holds State 3 and State 4. **The Stitch screens are pre-tactical** — they still show C/V badges and a 1st/2nd/3rd/4th bench, so take the layout and the state pills from them, not the bench or captaincy model.
- **5d. Scoring rules screen and copy.** `data/scoringRules.js`, `ScoringPage`, `LandingPage`.
- **5e. Retire the inert keys.** Only once nothing renders them: `chip_used`, `captain_multiplier`, `captain_bonus`, `transfer_hits`, `hit_deductions`, per-player `is_captain`/`is_vice_captain`, the deprecated `points`, and `chip_active` on `GET /transfers/used`. Backend and frontend in one commit, since this is the point the additive contract ends.

#### Still open from Phase 4

- **Migrate `fpl_game`.** Still on `a06f58d93f5a` + `c2f6a83e91d4` — none of `c41a9e27d06b`, `d58b3f10a7c2`, `e7c4d81b3a95`, `f2b9c05e7a41`, `b4e1f37c920d`. Note the contradiction this resolves: Phase 1's entry says it is migrated "in Phase 4", and Phase 4 is finished without it. It is sequenced **with the code**, because the migration drops columns the pre-4c code still selected; rehearse on a scratch copy first, as every prior revision was.
- **The six unfiltered season queries** listed under Phase 4e.

#### Running it locally (cost several attempts; recorded so it costs none next time)

The ASGI app is **`backend/Context_assembler/main.py`**, not `backend/main.py`, and `main:app` only resolves once the sub-package directories `conftest.py` inserts are on `sys.path` (`backend/Tests/conftest.py:21-29`):

```
PYTHONPATH="backend;backend\Context_assembler;backend\Feature_engineering;backend\Predict;backend\Worker;backend\Game_logic;backend\Data" \
  python -m uvicorn main:app --app-dir backend --host 127.0.0.1 --port 8000
```

`vite.config.js` proxies `/api` → `127.0.0.1:8000`, so the frontend is useless without it. **Do not point it at `fpl_game`** (unmigrated, and out of bounds) **or at `fpl_game_test`** (app writes there leak into `gw_average`/`overall_rank`, which are computed season-wide over `gw_scores`). Use a scratch clone — `CREATE DATABASE fpl_game_ui TEMPLATE fpl_game_test` — and pass `DATABASE_URL` to that one process rather than editing `.env`. A clone has no real-season fixtures, so `GET /gameweeks/current` answers `found: false` and the whole app shows "No season is scheduled yet" (`config/gameweek.jsx:34,63`) until teams, players and fixtures are seeded. `ml.players.position_encoded` is **0-based** (`players_position_encoded_check` is `>= 0 AND <= 3`).
**Phase 6. Re-tune.** After about 20 Gameweeks of 2026-27, re-run `simulate_tactics.py` (it holds the same tier tables in its CONFIG block) and revisit all three tactics, Defence first.

Separate housekeeping (own confirmation, not part of any phase): delete only the 2 stray rows in `ml.player_gw_stats` (`2026-27`, gameweek 38, fixture 1381, player ids 45830 and 45819).

### MERGE GATE

**The branch does not merge until every newly skipped test is either rewritten
or deleted, each with a written reason.** A skip is a deferral, not a decision:
Phase 3 skipped 31 test cases (29 functions) whose endpoints were rewritten, and
`PHASE3_RECONCILIATION.md` found a 32nd that should have been in that set. Left
alone they become permanent silent gaps, and the count only grows each phase.

Specifically, before merge:

1. **The three `test_transfer_concurrency.py` tests are re-pinned FIRST** in
   Phase 4 — see above. They guard a concurrency property that is still live in
   the code.
2. Every other skipped test from `PHASE3_REPORT.md` §7 is rewritten against the
   new endpoints, or deleted with its reason recorded in the commit.
3. `test_starting_xi.py::test_passed_deadline_rejects_first_ever_submission`
   (the `pass->fail` in `PHASE3_RECONCILIATION.md` §0) is resolved the same way.

---

## 6. Scoring engine specification
Pure functions, no I/O. Input: one selection snapshot (XI, bench with roles, tactic, Bonus flags, swaps), per-player per-Gameweek stat rows (one per fixture), player positions. Output: `raw_points` (General), `tactical_points`, `sub_bonus`, `total`.

Order of operations:
1. Per-fixture General Points; sum per player. A player "appeared" if total minutes > 0.
2. Resolve Tactical swaps first. Each swapped slot yields outgoing points + incoming points. Mark those slots as not eligible for Auto Sub cover.
3. GK cover: if the starting GK did not appear (and is not in a swap), the backup GK takes the slot if he appeared.
4. Outfield cover: the outfield Auto Sub (slot 13), if he appeared, replaces the lowest-slot non-appearing outfield starter (not in a swap) whose replacement keeps the formation legal (1 GK, >=3 DEF, **>=3 MID**, >=1 FWD). A starter actually replaced in step 3 or 4 is reported with the sixth role, `auto_sub_replaced`, and stops counting; a non-appearing starter who was **not** replaced stays `starter` and still counts, at 0. No maximum is checked: an Auto Sub of a position is only on the bench when the XI holds fewer than the squad's allocation, so a cover can never exceed it.
5. Tactical Points: for each Bonus Player who appeared himself (not an Auto Sub replacement), apply the tactic's events **per fixture** and sum. Tier events pay the highest tier reached in that fixture (not stacked). Defence's Defensive Contribution tiers apply only when the stat exists for the season.
6. Sub Bonus: for each executed swap, +1 if General(incoming) > General(outgoing) using full-Gameweek sums.
7. Total = General of all scoring slots + Tactical + Sub Bonus.

Test scenarios (each a hand-built Gameweek): double Gameweek swap block; swap with outgoing no-show; incoming no-show; swap slot never Auto Sub covered; Auto Sub priority (two no-shows, one Auto Sub); formation blocking an Auto Sub; Bonus Player no-show with Auto Sub cover; Bonus in a double Gameweek (tiers evaluated per fixture, then summed); Sub Bonus tie gives 0; Sub Bonus with outgoing no-show; unused Tactical Sub; each tactic's events; **tier boundaries** (7/8/9/10 defensive actions; creativity 19.9/20/39.9/40; highest tier only); 13-scorer maximum; General Points golden test against the archive CSVs (100% match target).
Validation tests: legal/illegal formations, Attack with fewer than 2 FWD, Bonus position mismatch, Bonus as outgoing, cross-position swap, swap kickoff order, budget, club limit, transfer bank cap 2, overspend rejected.

---

## 7. Guardrails for Claude Code
- Never write to or alter the `ml` schema. Never touch `SIM*` seasons.
- Never run migrations on `fpl_game` until the scratch dry-run has passed AND the owner has said so, **and not before Phase 4, because the migration drops columns today's code still selects.**
- No `TRUNCATE ... CASCADE`. If a live-only table blocks a truncate, add it to the list and report it.
- Always back up before a destructive step: `pg_dump -U postgres fpl_game > fpl_game_backup.sql`.
- Tests first for Phases 2 to 4. Keep the zero-import rule in `Shared/rules.py`.
- Hand-written `text()` SQL only, no ORM. Follow the existing 422 collect-all-errors pattern.
- One phase at a time. At each gate: summarise what changed, what was tested, what is uncertain, then wait.
- Do not invent facts about the schema or code. If something is missing, say so.

---

## 8. Open items and risks
- The FPL API is undocumented and not a licensed public API; check its terms before any commercial launch.
- Avoid FPL branding and vocabulary in the product ("Fantasy Premier League", "FPL", possibly "Gameweek").
- **Tactical Points values are tuned on ONE season (2025-26, 35 Gameweeks).** Bootstrap uncertainty on the averages is large. Re-tune in Phase 6.
- Measured with `simulate_tactics.py` under the adopted **Q1** rules (form-based manager, 2025-26): averages **3.86 / 3.89 / 3.97** (Attack / Defence / Balanced), relative swing **0.90 / 0.72 / 0.58**, best-of-three shares **42% / 31% / 27%**. Attack slightly exceeds the 40% guide; within noise, but watch it.
- A casual manager (picks popular players, no form-reading) averages **2.57 / 3.02 / 2.42** under Q1, so Defence is still the easier default. Watch for a "default tactic" effect.
- Balanced now pays something to about 58% of full-match midfielders, so it behaves partly like a steady participation bonus, not a sharp prediction reward.
- **Attack managers cannot use forward swaps.** A forward swap needs 2 Bonus forwards + 1 non-Bonus forward starter + 1 bench forward = 4 forwards, and the squad holds 3. Accepted for the MVP; defender and midfielder swaps remain possible.
- **The app must show each tactic's tier thresholds to managers.** The tiers are not stacked and the boundaries are not guessable: a creativity of 39.9 earns +1, not +3.
- Cosmetic: `team_value_available` is `True` beside `team_value` `0.0` for a fresh user (found in Phase 1). Fix in Phase 5.

### Phase 2 ambiguities now closed by owner decision

`PHASE2_REPORT.md` section 7 lists ten. Three are settled:

- **A1 — the role vocabulary had no name for a replaced starter. CLOSED by D2.**
  A sixth role, `auto_sub_replaced`, is added. A starter who did not play and
  was replaced carries it and stops counting; one who did not play and was not
  replaced stays `starter` and still counts, at 0. Implemented and tested in
  commit `ef7bcb0`.
- **A3 — formation maxima were inferred rather than stated. CLOSED by D1.**
  Maxima are implied by the 2/5/5/3 squad and are **not** a separate rule, so
  `FORMATION_MAX` is gone. It had been read in logic, but the check was
  unreachable: an Auto Sub of a position is only on the bench when the XI holds
  fewer than the squad's allocation, so a cover lands at that allocation at
  most. The MID **minimum** moved 2 → 3 in the same decision.
- **A10 — "Defensive Contribution" names two different rules. CLOSED as
  "keep, distinguish in UI labels".** Nothing is renamed in code. The General
  Points rule (flat +2 at 10 actions for DEF, 12 for MID/FWD) is labelled
  **"Defensive Contribution"** in the UI; the Defence tactic's tiers (2 at 8+,
  3 at 10+) are labelled **"Defence tactic bonus"**. A Phase 5 task, not a
  refactor.

Six more are settled by Phase 3's owner decisions:

- **A2 — does a 0-minute player still take his deductions? CLOSED: KEEP.**
  A player with 0 minutes still takes deductions; the golden test matched
  29,757 rows.
- **A4 — is the goalkeeper identified by slot or by position? CLOSED: by
  POSITION, never by slot.** Validation must also require exactly 1 GK among
  slots 1-11 and a GK in slot 12.
- **A5 — may the outfield Auto Sub cover a goalkeeper? CLOSED: no.** The
  outfield Auto Sub never covers a GK; if both GKs miss, the slot scores 0.
- **A7 — the engine validates nothing. CLOSED: that is the design.** The engine
  stays pure and validates nothing. Validation is a separate pure function
  (`Gameplay/selection_rules.py`). In Phase 4 the scoring job logs a
  data-integrity warning for an invalid stored selection and **still scores
  it**, so a data bug never leaves a manager with no score.
- **A8 — a Bonus Player can never be swapped in either direction. CLOSED:
  accepted, informational.**
- **A9 — what does Sub Bonus compare? CLOSED:** General Points only, over the
  full Gameweek.

All ten Phase 2 ambiguities are now closed. New ones raised in Phase 3 are in
`PHASE3_REPORT.md`.

### Phase 3 ambiguities

- **B1 — the free-transfer recurrence and its anchor. CLOSED, rule and anchor.**

  **The rule:** 1 free transfer at the first Gameweek under these rules, then
  +1 per Gameweek banking to a cap of 2, minus transfers made. A later joiner
  replays the same recurrence from the same start, so an empty history reaches
  the cap. No paid transfers.

  **The anchor (option B): stored, never derived.** The first new-rules
  Gameweek lives in `ruleset_epochs`, one row per (season, rules_version),
  written by migration `e7c4d81b3a95` at release time using rule D5 — the first
  Gameweek whose deadline was still in the future when that database was
  migrated. The table is **append-only** (a `BEFORE UPDATE OR DELETE` trigger
  raises, following the `transfers` pattern) and has **no foreign keys**, so
  nothing cascades into it.

  The rejected alternative was "the earliest Gameweek with a `gw_selections`
  row". It was checked against the live schema and is unsafe: those rows carry
  no DELETE guard and cascade from `users`, so deleting one account could move
  the anchor forward and silently restate every manager's allowance.
  `test_deleting_a_user_does_not_move_any_other_managers_allowance` is the test
  that would have failed.

  **A missing row is not an error** — it means the database has never known any
  other ruleset, so the answer is Gameweek 1. Transfers recorded before the
  epoch are ignored by the recurrence and logged as a data-integrity warning.

- **B2 — strictly-after, or at-the-instant, for swap timing? CLOSED: a kickoff
  exactly at the end instant is REFUSED.** The incoming player's first kickoff
  must be *strictly* after the outgoing player's last fixture ends
  (`kickoff + FIXTURE_DURATION_MIN`); a tie is not "after". Already implemented
  and pinned by the 114/115/116-minute boundary test.
- **B3 — must every squad player be given a slot? CLOSED: yes**, and the
  "missing player" error is reported only when 15 distinct ids were submitted,
  so a manager who submits 14 gets one count error rather than a count error
  plus a spurious missing-player error for the same mistake. As implemented.
- **B4 — a blank-gameweek player can be named but not swapped. CLOSED: as
  implemented.** Having no fixture is not an error for a starter; it is one for
  a swap, because the timing rule has nothing to compare against. Consistent
  with D4: swaps are validated once at submission and never re-validated, so a
  later fixture change cannot retrospectively invalidate one.

- B5, B6 and B7 are not ambiguities but cleanup, and have moved to the Phase 4
  task list in section 5.

### Phase 4c closures

- **E3 — two scorers coexisted. CLOSED:** `Results/scoring.py` is deleted. The
  behaviour the tactical engine must still have (Auto Sub logic, formation
  legality, appearance points, the finance snapshot) was migrated into the
  tactical test files; the captain/chip/hit tests were deleted with the rules
  they encoded.
- **B5 — `MAX_TRANSFERS_PER_GAMEWEEK` was unreachable. CLOSED: deleted**, with
  the two tests that existed only to exercise it.
- **B6 — the `chip_active` placeholder and `FREE_CHIPS` import. CLOSED:
  deleted.** The `chip_active` **response key** survives as a literal `False`,
  because it is client-facing; Phase 5 removes it.
- **B7 — `_validate_transfers`' `allowance` defaulted to 0. CLOSED: required**,
  with no default.

### Phase 4a and 4b ambiguities

- **E1 — the simulation harness could not drive the new scorer. CLOSED:**
  `allow_sim_seasons` is an explicit keyword argument, passed only by
  `Tools/fpl_sim.py`. Never an environment variable.
- **E2 — swap timing was re-checked at scoring time. CLOSED:**
  `validate_selection(..., check_timing=False)`, keyword-only. The endpoint
  keeps the default; the job and the dashboard switch it off, per D4. The
  notional-fixture workaround is deleted.
- **E3 — two scorers coexist. OPEN until Phase 4c.** No production module
  imports `Results/scoring.py` any more, so deleting it is unblocked; the tests
  that still import it are merge-gate work.
- **E4 — a batch write was one transaction. CLOSED:** it falls back to
  per-manager writes with a SAVEPOINT each, and `refresh_active_gameweeks`
  reports a gameweek with failed managers as failed rather than refreshed.
- **E5 — `season_total` summed across rule generations. CLOSED:** bounded below
  by the ruleset epoch and above by the current gameweek. No epoch row means
  every earlier gameweek, exactly as before.
- **E6 — a player missing from `ml.players` failed the whole manager.
  CLOSED:** he scores 0, counts towards **no formation minimum** (so an Auto
  Sub cover is judged on the other ten, which makes cover stricter rather than
  looser), and cannot be used **as** a cover — though he can still **be**
  covered, since a player with no stats row is a no-show. A data-integrity
  warning names the user, season, gameweek and player ids.
- **E7 — `total_points` and `final_points` are always equal. CLOSED: keep
  both.** Hits were the only thing that separated them. A comment and a test
  pin the equality. **One of the two is dropped after the frontend phase**,
  since removing a key is a response-shape change.
- **F1 — `points` changed meaning without changing name. CLOSED: DEPRECATED.**
  It was FPL's `total_points` and is now the engine's General Points. It stays
  so the current frontend keeps rendering; clients should use `general_points`.
  Marked deprecated in the model, removed in the frontend phase.
- **F2 — `raw_points` / `final_total` had two sources. CLOSED:** the response
  carries `score_source` — `"committed"` from `gw_scores`, `"live"` from the
  engine because the job has not run yet, `null` when not scored.
- **F3 — an unscored gameweek returned populated points. CLOSED:** every points
  field is `null` when `scored` is false, while the lineup structure (names,
  positions, clubs, roles) stays so the team still draws.
- **F4 — 14 queries and 162 ms per dashboard request. NO CHANGE, recorded.**
  Measured on a near-empty `fpl_game_test`, so a floor. The engine call adds
  three queries. Nothing is cached between requests.
- **F5 — `tactical_breakdown` is empty both for a non-Bonus player and for a
  Bonus Player who earned nothing. NO CHANGE, recorded.** `is_bonus`
  distinguishes the two.
- **F6 — `captain_multiplier` type. CLOSED: it stays an INTEGER, always 1.**
  Null would break any client doing `points * captain_multiplier`; 1 keeps that
  arithmetic correct while captaincy is removed. `is_captain` and
  `is_vice_captain` are `false`; the chip keys stay `null`.
- **`team_value_available`. CLOSED:** true only when a `user_gameweek_finance`
  row exists for that user and gameweek. It used to be true for any non-final
  gameweek, which made it true for a fresh user with no squad and presented
  `0.0` as a real figure.
- ~~`creativity` column existence in `ml.player_gw_stats` is unverified.~~ **Resolved in
  Phase 0:** both `creativity` (`numeric`) and `defensive_contributions` (`smallint`,
  plural in the database) exist. Evidence quoted in section 3, Phase 0 finding 4.
- Sub Bonus giveaway when the outgoing player never appeared.

---

## 9. Balance specification (acceptance tests)
Purpose: decide in advance what "balanced" means, so scoring values are judged against fixed tests instead of being nudged until they "look nice". Run `cd tools && python balance_check.py` (edit values in `simulate_tactics.py` CONFIG, never in the spec). **Never change a band to make a candidate pass without writing down why.**

Design principle: the three tactics should have overlapping average returns (so no tactic is the obvious pick) but clearly different swing (that is where the personalities come from): Attack = high risk, Defence = medium, Balanced = most consistent. "Swing" = standard deviation divided by average of the 2 Bonus Players' Tactical Points per Gameweek.

| Test | Rule | Notes |
|---|---|---|
| T1a | Each form-based average within 3.7 to 4.1 | Absolute band; tied to the current sample |
| T1b | Each average within +-5% of the mean of the three | Relative version of T1a |
| T2a | Swing: Attack >= 0.80, Defence 0.65 to 0.78, Balanced 0.50 to 0.65 | The personalities |
| T2b | Adjacent swings at least 0.10 apart | Keeps the personalities distinguishable |
| T3a / T3b | Casual (random-pick) average: max/min <= 1.12 target, <= 1.25 hard limit | Guards against an "easy default" tactic |
| T3c | T3b also holds with equal pool sizes (all 15) | Casual result depends on the pool-size assumption |
| T4 | No tactic best in more than 40% of Gameweeks (form-based) | |
| T5 | At most 6 thresholds in total (clean sheet + all tier thresholds) | Explainability |

Definitions: "form-based" = picks the 2 players with the best trailing 5-Gameweek Tactical Points from the pool of the most-owned players (Attack pool 10, Defence 15, Balanced 15). "Casual" = picks 2 at random from the same pool.

### Status of candidate rules (2025-26, 35 Gameweeks)
| | Retired "P" | **ADOPTED "Q1"** |
|---|---|---|
| Defence | Clean sheet +3; DC 6+ +1, 10+ +2, 14+ +3 | Clean sheet +2; DC 8+ +2, 10+ +3 |
| Balanced | G/A +1; creativity 15+ +1, 30+ +2, 50+ +3 | G/A +1; creativity 20+ +1, 40+ +3 |
| Form averages A / D / B | 3.86 / 4.06 / 4.20 | 3.86 / 3.89 / 3.97 |
| Swing A / D / B | 0.90 / 0.70 / 0.51 | 0.90 / 0.72 / 0.58 |
| Best-of-three shares | 43% / 26% / 31% | 42% / 31% / 27% |
| Casual averages A / D / B | 2.57 / 3.33 / 2.70 | 2.57 / 3.02 / 2.42 |
| Passes | T1b, T2a, T2b | **T1a, T1b, T2a, T2b, T5** |
| Fails | T1a (Balanced 4.20), T3a/b/c, T4, T5 (7 thresholds) | **T3a, T3b, T3c, T4** |

**Q1 is ADOPTED; P is retired.** Q1 gains T1a (Balanced falls from 4.20 to 3.97, inside
the 3.7-4.1 band) and T5 (5 thresholds instead of P's 7, against a limit of 6), while
keeping the three personalities intact.

Q1's two remaining failures, **T3a/b/c (casual parity) and T4 (win share), are recorded
as known structural misses to monitor, not bugs.** Both are explained below: they are
properties of what the underlying events reward, not of the numbers chosen, and no
candidate among the 657 searched fixes T3 without erasing Defence's personality.
Treat them as monitoring targets — see "Post-launch monitoring" — rather than as defects
to be tuned away.

### Known structural conflicts (do not hide these)
1. **T3 (casual) vs the Defence swing band.** Across 657 Defence rules with at most 3 tiers, none satisfied the average band, the swing band 0.65-0.78 AND casual within 12% of Attack. The best achievable casual advantage while keeping swing >= 0.65 was about +18%. Reason: clean sheets and defensive actions are team-level events, so an ordinary popular defender earns them; there is little skill premium to reward. Getting casual parity means making Defence steadier (swing about 0.55), which erases the personality.
2. **T4 (win share) vs T2 (Attack swing).** A higher-swing tactic is more often the best of three in a given Gameweek even when averages are equal. Attack sits at 42-43% for both candidates.
3. **The casual test depends on assumptions.** Attack's casual average falls from 2.57 to 2.23 if its pool is 15 instead of 10. Real managers only own 3 FWD, 5 DEF, 5 MID.

### Statistical honesty
With 35 Gameweeks, 90% bootstrap intervals are roughly +-0.9 on each average and +-0.2 to 0.3 on each swing. The spec bands (0.4 wide on averages, 0.13-0.15 on swings) are narrower than that noise, so every PASS/FAIL above is PROVISIONAL. Re-run after about 20 Gameweeks of 2026-27, and treat a test as failed only if the interval also sits outside the band.

### Post-launch monitoring (the real test)
The casual simulation is a proxy. Once live, track the share of managers choosing each tactic per Gameweek. If any tactic is chosen by more than 50% of managers for 3 consecutive Gameweeks, investigate a "default tactic" problem before touching the scoring values.

---

## 10. Environments and release

### Local databases
| Database | Role | State |
|---|---|---|
| `fpl_game` | dev | **Migrated at Phase 4**, not before. Stays on its current schema until then. |
| `fpl_game_test` | tests only | Already migrated (Phase 1). |
| `fpl_game_scratch` | temporary rehearsal copy | Drop it when the rehearsal is done. |

### Server
- Database `pitchside_db` on a Google e2-micro, holding **test users only**.
- Roughly 8.6 GB disk with about 3.1 GB free, about 1 GB RAM, and a single-task Celery
  worker (`--pool=solo`).
- **Its state is unverified until the owner supplies it.** Do not guess at its schema,
  its Alembic revision or what runs on it.

### Release: the database change and the code change must reach the server together
Neither half works alone — the migration drops columns today's code still selects, and
the new code needs columns only the migration adds. Deploy as one step:

1. Stop the app processes (API and Celery worker).
2. Back up: `pg_dump -U postgres pitchside_db > pitchside_db_backup.sql`.
3. Pull the code.
4. Migrate, with the override set **for that one command only**:
   `ALLOW_GAMEPLAY_WIPE=pitchside_db alembic upgrade head`.
   **Never store it in any `.env` file** — it exists to make the wipe a deliberate,
   one-off act, and a stored copy turns the guard into decoration.
5. Restart the app processes.
6. Smoke check.
7. **Check the epoch.** After migrating each database run:

   ```sql
   SELECT * FROM ruleset_epochs;
   ```

   Confirm `first_gameweek` is the first Gameweek whose deadline was still
   ahead. **Migrate INSIDE the release window** — between the end of one
   Gameweek's last fixture and the next deadline — because the epoch is computed
   from `now()` at migration time. Migrate outside it and the epoch is a
   Gameweek that is already under way, which cannot be corrected in place: the
   table is append-only, so fixing it means a new `rules_version`.

### Server rules
- **Never run tests against `pitchside_db`.** `ALLOW_GAMEPLAY_WIPE` must never name it in
  a stored file, and no test configuration may point at it.
- **The branch is not pushed or merged until Phase 4 is finished.**

### Which Gameweek is scored first, and when to release (D5)

**The first Gameweek scored under the new rules is the first one whose deadline
is still in the future at release time.** Earlier Gameweeks stay blank — they
are not back-scored, and the Phase 1 migration has already emptied `gw_scores`,
so there is nothing to convert.

**Release in the window between the end of one Gameweek's last fixture and the
next deadline.** That window is the only time when no Gameweek is mid-flight:
release during a Gameweek and managers would have picked a team under one set
of rules and be scored under another.

### Tactic-pick monitoring (D6)

**No monitoring is being built now.** The section 9 post-launch check — "if any
tactic is chosen by more than 50% of managers for 3 consecutive Gameweeks,
investigate" — is run by hand:

```sql
SELECT gameweek, tactic, COUNT(*) FROM gw_selections GROUP BY gameweek, tactic ORDER BY gameweek, tactic;
```

### Operational notes
- The scoring job must process managers in **batches of a few hundred**. The e2-micro has
  about 1 GB RAM and a single-task worker, so a whole-league pass in one transaction is
  not safe there.
- Downgrading past the merge revision needs the explicit parent, because `downgrade -1`
  twice hits "Ambiguous walk" at the mergepoint:
  ```
  alembic downgrade a06f58d93f5a
  ```
