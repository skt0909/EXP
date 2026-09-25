# Status, as of 2026-09-22

Quick-read companion to `IMPLEMENTATION_PLAN.md` (the living plan and
rulebook). This file records the current code state, independently
re-verified item-by-item against `IMPLEMENTATION_PLAN.md` section 5 on
2026-09-22 (a prior version of this file made the same "Phase 5 complete"
claim without stating how it was checked — this version says exactly what was
verified and how, per item).

## Phase 5 (frontend): VERIFIED COMPLETE

Every item below was checked directly against source, not assumed from a
prior report. "Read" = source inspected. "Confirmed live" = also exercised
against the real, migrated `fpl_game` database via FastAPI's `TestClient` or
a direct SQL query.

**5a — Selection submission.** `frontend/src/api/gwSelection.js` sends
`tactic`, `bonus_player_ids`, `swaps`, `player_ids`, `bench_order` — no
`captain_id`/`vice_captain_id`/`chip_used` anywhere in it (read). No file
`frontend/src/api/chips.js` exists; no reference to it or `/chips/used`
anywhere in `frontend/src` (grep, zero hits). *Confirmed live*: `POST
/gw_selection` with a `{tactic, bonus_player_ids, swaps, player_ids,
bench_order}` body against a real registered user is accepted by the schema
and reaches business logic (rejected only for the gameweek being locked, not
for a missing/invalid field).

**5b — Starting XI page.** `frontend/src/pages/StartingXI/StartingXIPage.jsx`
(read in full, 1077 lines): tactic selector for `attack`/`defence`/`balanced`;
exactly 2 Bonus Players enforced, chosen only from starters, position-matched
per tactic (Attack→FWD, Defence→DEF, Balanced→MID); Attack disabled when the
XI holds fewer than 2 FWD; formation validation requires exactly 1 GK, ≥3 DEF,
**≥3 MID** (not the classic ≥2), ≥1 FWD; no `5-2-3` preset exists in
`FORMATION_OPTIONS`; bench roles are labelled `12 Auto GK` / `13 Auto Sub` /
`14 Tactical` / `15 Tactical`, not ordinal 1st–4th; the swap planner caps at 2,
rejects a Bonus Player as the outgoing side, restricts incoming to slots
14/15, and enforces same-position; backend 422s are surfaced via a
`submitError` banner in addition to client-side checks. Zero occurrences of
`captain`/`vice_captain`/`chip`/`CHIP_TYPES`/`triple_captain`/`free_hit`/
`wildcard`/`bench_boost` anywhere in the file.

**5c — Dashboard.** `frontend/src/pages/DashboardPage/DashboardPage.jsx` (read
in full, 673 lines): renders `tactic`, per-player `role`, `is_bonus`,
`general_points`, `tactical_points`, `sub_bonus`, swap detail, `is_locked`,
`provisional`, `scored`; zero captain/chip/hit tiles or fields. The four
states are distinguished by `has_lineup` + squad existence (states 1/2),
`is_locked`/`live_status`/`provisional` (state 3), and `scored` (state 4) —
matching the plan's "Dashboard state" table exactly. Bonus Players get a
distinct "Bonus" pill badge, not an armband. *Confirmed live*: `GET /team` for
a genuinely fresh user returns `has_lineup=false, is_locked=true (GW1's
deadline has passed), scored=false, tactic=null, team_value_available=false,
team_value=0.0` — the fresh-user cosmetic bug from Phase 1 stays fixed
(`team_value_available` no longer reads `true` beside a `0.0` value).

Shared components `PlayerJersey`, `PlayerMarker`, `PlayerPointsSheet`,
`OpponentTeamPanel` still contain captain/vice-captain code — this is
correct, not a gap: `PlayerJersey` is genuinely shared with Dream11 (which
has real captain/vice mechanics) and no classic call site
(`StartingXIPage`/`DashboardPage`/`TransfersPage`) ever passes a
`captain`/`viceCaptain` prop into it, so no captain UI renders in classic
flows. `PlayerMarker`, `PlayerPointsSheet` and `OpponentTeamPanel` trace only
to Dream11 pages in the current import graph. Per the standing instruction not
to touch Dream11 without a genuine shared-component need, none of these were
changed.

**5d — Rules and copy.** `frontend/src/data/scoringRules.js`'s
`buildClassicRules` (the tactical builder, read in full): no captain, vice
captain, Triple Captain, Bench Boost, Free Hit, Wildcard, transfer-hit, or
classic-FPL-bonus language. "Rollover Cap"/"Bank Cap" copy describes the
tactical free-transfer bank cap dynamically from the API payload
(`tactical.free_transfer_bank_cap`), not a hardcoded classic value.
`ScoringPage.jsx` and `LandingPage.jsx`: zero matches for any classic-only
term. `buildDream11Rules` (same file, second half) legitimately keeps
captain/vice copy for Dream11 — correctly out of scope.
`frontend/src/api/scoringRules.js` (the fetch) and
`frontend/src/data/scoringRules.js` (the shaper) are both live, not
duplicates — `useScoringRules.js` imports the former. *Confirmed live*: `GET
/scoring-rules` returns a `tactical` block with `rules_version: 3` and the
exact Defence tier thresholds (8→2, 10→3) from the rulebook.

**5e — Compatibility fields.** Grepped all of `backend/` and `frontend/src/`
for `chip_used`, `captain_multiplier`, `captain_bonus`, `transfer_hits`,
`hit_deductions`, `is_captain`, `is_vice_captain`, `chip_active`: every hit in
live application code is either Dream11-scoped (has its own real
captain/vice/credits mechanic — correctly excluded) or inside a migration
file's `downgrade()` (which legitimately re-adds dropped columns so a
rollback works — not a live schema element). *Confirmed live*: queried the
migrated `fpl_game` schema directly — `gw_selections.captain_id`,
`.vice_captain_id`, `.chip_used`, `starting_xi.is_captain`,
`.is_vice_captain`, `gw_scores.transfer_hits`, `.hit_deductions` are all
physically **absent** from the live tables, not just hidden from the response
model. `backend/Results/team_dashboard.py` and `backend/Gameplay/transfers.py`
have zero matches for any of the eight fields. Per-player scoring uses
`general_points` as the live field; the bare `points` key that remains is an
internal `{rule, points}` breakdown-line attribute, not a competing top-level
field. `total_points`/`final_points` are both still written (always equal,
since there are no hits under tactical rules) — correctly NOT removed, since
the plan never places their removal in Phase 5 scope.

**Legacy test files still reference the old fields** (`chip_used`,
`transfer_hits`, `is_captain`, etc. in `test_full_season_scenario.py`,
`test_gameweek_lifecycle.py`, `test_starting_xi.py`, `test_team_dashboard.py`
and others) and would fail against the current API. This is real, but it is
the plan's own **MERGE GATE** item, not a Phase 5 frontend/API-contract gap —
and per this task's explicit constraints, the automated test suite was not
run and these were not touched.

## What's outstanding

- **The MERGE GATE test cleanup** (see above) — rewriting or deleting the
  stale classic tests, per `IMPLEMENTATION_PLAN.md`'s MERGE GATE section.
  Deliberately deferred; tests were not run or edited this session.
- **Server release** — `pitchside_db` is unmigrated and untouched. Release
  only per the plan's section 10 procedure, after the test cleanup above.
- **Phase 6** (tactical balance re-tuning) — not started, intentionally out
  of scope until ~20 gameweeks of 2026-27 data exist.
- **Deferred SIM-season data purge** — `SIM38OK`/`SIM38TST`/`SIMSMOKE` rows
  still exist in `ml.fixtures`/`ml.players`/`ml.teams` and in a few real
  `user_squads`/`mini_leagues` rows that reference them. Inert (filtered out
  by `Shared/seasons.py` everywhere it matters), left in place by explicit
  choice, not forgotten.

## Data flow, end to end

```
FPL API / ingestion (Data/fpl_ingest.py, Data/live_poll.py)
        |
        v
ml.* tables (fixtures, players, teams, player_gw_stats)   <- READ-ONLY to game code
        |
        v
Gameplay/*.py  (squad_selection, starting_xi, transfers)
   writes: user_squads, squad_players, gw_selections, starting_xi,
           tactical_swaps, transfers
        |
        v
Results/scoring_job.py :: score_gameweek_tactical
   reads: gw_selections, starting_xi, tactical_swaps, ml.player_gw_stats
   writes: gw_scores (raw_points, tactical_points, sub_bonus, total_points,
           final_points), user_gameweek_finance, gameweeks.scored_at
        |
        v
Results/team_dashboard.py :: GET /team
   reads everything above at request time, computes autosubs live,
   returns the tactical TeamDashboardResponse the frontend now fully consumes
```

## Databases

| Database | Role | State |
|---|---|---|
| `fpl_game` | real dev DB | Migrated to Alembic head `b4e1f37c920d`. Holds real 2026-27 fixtures (380) and player stats for Gameweeks 1–4. `gw_scores` was truncated by the migration, so `GET /gameweeks/current` currently answers Gameweek 1 until the scoring job re-runs for Gameweeks 1–4 (via Celery Beat's normal schedule, or by hand). |
| `fpl_game_test` | tests only | Already at head since Phase 1. Never point the dev server at it. |
| `fpl_game_scratch` | rehearsal only | Created and dropped to rehearse the migration; not persistent. |
| `pitchside_db` | server (test users only) | Unverified, untouched. Not touched in this audit. |

## Not touched, by design

- **Dream11 mode** — its own captain/vice-captain mechanics, contest rules,
  and every component that traces only to a Dream11 page. Confirmed genuinely
  unrelated to any Phase 5 item, not just skipped.
- **Celery/Beat/Redis infrastructure**, **Phase 6**, **`pitchside_db`**,
  **the automated test suite** — untouched, per this audit's explicit
  constraints.
