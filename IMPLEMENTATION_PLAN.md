# Tactical Fantasy Football: Implementation Plan v1

Status: planning, agreed with the product owner on 2026-09-20.
Audience: Claude Code, working in the existing FPL-clone repo.
Read this whole file before doing anything. Work one phase at a time and STOP at every gate.

Source documents in the repo (read first): `ARCHITECTURE.md`, `ML_LAYERS.md`.
Baseline DDL reviewed: `public` v1.0 script. It is OLDER than the live schema, so trust `./schema_dump.sql`, not the v1.0 script.

---

## 1. Rulebook v0.3 (locked decisions)

### Squad and lineup
| Area | Rule |
|---|---|
| Squad | 15 players: 2 GK / 5 DEF / 5 MID / 3 FWD. Budget £100.0m (existing half-profit selling rule kept). Max 3 per club. |
| Starting XI | 1 GK, at least 3 DEF, at least 2 MID, at least 1 FWD. |
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
| Swap timing | Incoming player's first kickoff must be after the outgoing player's LAST fixture ends. "Ends" = kickoff + `FIXTURE_DURATION_MIN` (115). In a double Gameweek each player's fixtures are treated as one block. Validated once at submission. |
| Swap scoring | Outgoing player's points are banked. The incoming player scores his own points. Both count. Up to 13 players can score in a Gameweek. |
| Swap edge cases | Outgoing never appeared: swap still happens, he scores 0. Incoming never appeared: the slot scores 0, no cover. **A slot involved in a swap is never Auto Sub covered.** |
| Sub Bonus | +1 per executed swap where the incoming player's full-Gameweek General Points are strictly greater than the outgoing player's. Max +2. Shown separately. Note: if the outgoing player never appeared, the incoming player needs to score above 0 (accepted small giveaway). |

### Points
| Area | Rule |
|---|---|
| General Points | Appearance 60+ min +2, 1-59 min +1. Goals GK +10 / DEF +6 / MID +5 / FWD +4. Assist +3. Clean sheet (60+ min) GK/DEF +4, MID +1, FWD 0. Defensive Contribution +2 (DEF 10 actions, MID/FWD 12). Goals conceded -1 per 2 (GK/DEF). Yellow -1, Red -3, Own goal -2, Penalty missed -2. Saves +1 per 3, Penalty saved +5. **No FPL bonus points, no captain, no chips.** Validated: this table reproduces the FPL archive totals (minus bonus) on 100% of rows. |
| Tactical Points (Bonus Players only, per fixture, summed across a double Gameweek) | **Attack:** goal +3, assist +2. **Defence:** clean sheet (60+ min) +3, plus Defensive Contribution tiers: 6+ actions +1, 10+ +2, 14+ +3. **Balanced:** goal or assist +1 each, plus creativity tiers: 15+ +1, 30+ +2, 50+ +3. **Tiers are NOT stacked: only the highest tier reached counts** (10 actions = +2, not +3). Values chosen ("Option P") so the three tactics have distinct personalities: Attack swingiest, Defence middle, Balanced steadiest, with average returns within about 8% for a form-based manager. They are tuned on 2025-26 only (35 Gameweeks) and MUST be re-tuned after about 20 Gameweeks of 2026-27. |
| Gameweek total | General Points of all scoring slots + Tactical Points + Sub Bonus. Resets each Gameweek; completed totals accumulate into the Season Total. |
| Transfers | 1 free per Gameweek. Unused ones bank, **cap 2** (was 5). **No paid transfers, no hits**: transfers beyond the balance are rejected. `transfers.is_free` stays (always true). |
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
  - Defence: clean_sheet 3, `DC_TIERS = ((6, 1), (10, 2), (14, 3))`.
  - Balanced: goal_or_assist 1, `CREATIVITY_TIERS = ((15, 1), (30, 2), (50, 3))`.
  - Tier semantics: `(minimum, points)`, the HIGHEST tier reached counts (not stacked).
  - **`rules_version` is an INTEGER, not a string.** Phase 0 verified the live column
    is `gw_scores.rules_version smallint NOT NULL` (added by `c9a04e7b53d1`), holding
    values 1 (869 rows) and 2 (121 rows). The earlier `"tactical-v1"` would have raised
    at runtime in the scorer. The next generation is therefore **3**. It is stamped from
    `Shared/rules.py`'s `CURRENT_RULES_VERSION` (currently `= 2`, line 66) and written by
    `Results/scoring.py:398`; bump it to 3 in Phase 2/4, not in a migration.
- The live Alembic history has TWO heads: `a06f58d93f5a` and `c2f6a83e91d4`. Revision `c41a9e27d06b` merges them.

---

## 3. Data model (drafts already written)
Files: `c41a9e27d06b_drop_classic_only_objects.py` and `d58b3f10a7c2_add_tactical_game_structures.py`. They are DRAFTS built from the v1.0 DDL. They must be reconciled against `schema_dump.sql` before use.

Revision 1 (`c41a9e27d06b`, merges both heads): truncate gameplay tables (no CASCADE; **`user_gameweek_finance` is NOT truncated because the dashboard displays bank and team value from it**), reset league points, drop `chips` and its trigger/function, drop captain/vice/chip columns, drop hit columns.
Revision 2 (`d58b3f10a7c2`): add `gw_selections.tactic`; widen `starting_xi` slots to 1-15, add `is_bonus` and a generated `role` column; add `tactical_swaps`; add triggers (exactly 2 Bonus at commit, swap references valid, child rows locked after the deadline); add `gw_scores.tactical_points`, `sub_bonus`, `rules_version` if missing. **No `ml` changes.**

Known risks to resolve in Phase 0:
1. Live schema differs from v1.0 (per `ARCHITECTURE.md`: half-season chips, transfer cancellations, transfer drafts, `league_h2h_fixtures`, free-hit snapshot). Find every live-only table that references a table being truncated or altered.
2. Where the bench is stored today (v1.0 only allows slots 1-11).
3. Auto-generated constraint names (e.g. `starting_xi_position_slot_check`) may differ.
4. Whether `ml.player_gw_stats.creativity` exists.

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
**Phase 0. Verify (read-only).** Backup, schema review of the two drafts against `schema_dump.sql`, dependency scan for every use of columns being dropped, ML dependency scan. Deliver a mismatch list. **GATE: owner confirms.**
**Phase 1. Migrations.** Fix the drafts from the mismatch list. Dry-run on a scratch copy of `fpl_game`: `upgrade head`, `downgrade -2`, `upgrade head`. Run the full test suite on the scratch DB. **GATE: owner confirms before touching `fpl_game`.**
**Phase 2. Rules and scoring engine.** Constants plus pure functions with tests first. No DB in the unit tests. **GATE: tests green.**
**Phase 3. API.** Selection and transfer endpoints with validation, and their tests. **GATE: tests green.**
**Phase 4. Integration.** Wire the engine into `score_gameweek` (idempotent upsert, `rules_version = 3`), remove chips/hits/`revert_free_hits`, run the full suite (524 tests plus new ones) including Dream11 and ML tests. **GATE: full suite green.**
**Phase 5. Frontend.** Screens listed in section 4.
**Phase 6. Re-tune.** After about 20 Gameweeks of 2026-27, re-run `simulate_tactics.py` (it holds the same tier tables in its CONFIG block) and revisit all three tactics, Defence first.

Separate housekeeping (own confirmation, not part of any phase): delete only the 2 stray rows in `ml.player_gw_stats` (`2026-27`, gameweek 38, fixture 1381, player ids 45830 and 45819).

---

## 6. Scoring engine specification
Pure functions, no I/O. Input: one selection snapshot (XI, bench with roles, tactic, Bonus flags, swaps), per-player per-Gameweek stat rows (one per fixture), player positions. Output: `raw_points` (General), `tactical_points`, `sub_bonus`, `total`.

Order of operations:
1. Per-fixture General Points; sum per player. A player "appeared" if total minutes > 0.
2. Resolve Tactical swaps first. Each swapped slot yields outgoing points + incoming points. Mark those slots as not eligible for Auto Sub cover.
3. GK cover: if the starting GK did not appear (and is not in a swap), the backup GK takes the slot if he appeared.
4. Outfield cover: the outfield Auto Sub (slot 13), if he appeared, replaces the lowest-slot non-appearing outfield starter (not in a swap) whose replacement keeps the formation legal (1 GK, >=3 DEF, >=2 MID, >=1 FWD).
5. Tactical Points: for each Bonus Player who appeared himself (not an Auto Sub replacement), apply the tactic's events **per fixture** and sum. Tier events pay the highest tier reached in that fixture (not stacked). Defence's Defensive Contribution tiers apply only when the stat exists for the season.
6. Sub Bonus: for each executed swap, +1 if General(incoming) > General(outgoing) using full-Gameweek sums.
7. Total = General of all scoring slots + Tactical + Sub Bonus.

Test scenarios (each a hand-built Gameweek): double Gameweek swap block; swap with outgoing no-show; incoming no-show; swap slot never Auto Sub covered; Auto Sub priority (two no-shows, one Auto Sub); formation blocking an Auto Sub; Bonus Player no-show with Auto Sub cover; Bonus in a double Gameweek (tiers evaluated per fixture, then summed); Sub Bonus tie gives 0; Sub Bonus with outgoing no-show; unused Tactical Sub; each tactic's events; **tier boundaries** (5/6/9/10/13/14 defensive actions; creativity 14.9/15/29.9/30/49.9/50; highest tier only); 13-scorer maximum; General Points golden test against the archive CSVs (100% match target).
Validation tests: legal/illegal formations, Attack with fewer than 2 FWD, Bonus position mismatch, Bonus as outgoing, cross-position swap, swap kickoff order, budget, club limit, transfer bank cap 2, overspend rejected.

---

## 7. Guardrails for Claude Code
- Never write to or alter the `ml` schema. Never touch `SIM*` seasons.
- Never run migrations on `fpl_game` until the scratch dry-run has passed AND the owner has said so.
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
- Measured with `simulate_tactics.py` (form-based manager, 2025-26): averages 3.86 / 4.06 / 4.20 (Attack / Defence / Balanced), relative swing 0.90 / 0.70 / 0.51, best-of-three shares 43% / 26% / 31%. Attack slightly exceeds the 40% guide; within noise, but watch it.
- A casual manager (picks popular players, no form-reading) averages 2.57 / 3.33 / 2.70, so Defence is the easy default. Watch for a "default tactic" effect.
- Balanced now pays something to about 58% of full-match midfielders, so it behaves partly like a steady participation bonus, not a sharp prediction reward.
- `creativity` column existence in `ml.player_gw_stats` is unverified.
- Sub Bonus giveaway when the outgoing player never appeared.

---

## 9. Balance specification (acceptance tests)
Purpose: decide in advance what "balanced" means, so scoring values are judged against fixed tests instead of being nudged until they "look nice". Run `python balance_check.py` (edit values in `simulate_tactics.py` CONFIG, never in the spec). **Never change a band to make a candidate pass without writing down why.**

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
| | Adopted "P" | Candidate "Q1" |
|---|---|---|
| Defence | Clean sheet +3; DC 6+ +1, 10+ +2, 14+ +3 | Clean sheet +2; DC 8+ +2, 10+ +3 |
| Balanced | G/A +1; creativity 15+ +1, 30+ +2, 50+ +3 | G/A +1; creativity 20+ +1, 40+ +3 |
| Form averages A / D / B | 3.86 / 4.06 / 4.20 | 3.86 / 3.89 / 3.97 |
| Swing A / D / B | 0.90 / 0.70 / 0.51 | 0.90 / 0.72 / 0.58 |
| Best-of-three shares | 43% / 26% / 31% | 42% / 31% / 27% |
| Casual averages A / D / B | 2.57 / 3.33 / 2.70 | 2.57 / 3.02 / 2.42 |
| Passes | T1b, T2a, T2b | T1a, T1b, T2a, T2b, T5 |
| Fails | T1a (Balanced 4.20), T3a/b/c, T4, T5 (7 thresholds) | T3a/b/c, T4 |

### Known structural conflicts (do not hide these)
1. **T3 (casual) vs the Defence swing band.** Across 657 Defence rules with at most 3 tiers, none satisfied the average band, the swing band 0.65-0.78 AND casual within 12% of Attack. The best achievable casual advantage while keeping swing >= 0.65 was about +18%. Reason: clean sheets and defensive actions are team-level events, so an ordinary popular defender earns them; there is little skill premium to reward. Getting casual parity means making Defence steadier (swing about 0.55), which erases the personality.
2. **T4 (win share) vs T2 (Attack swing).** A higher-swing tactic is more often the best of three in a given Gameweek even when averages are equal. Attack sits at 42-43% for both candidates.
3. **The casual test depends on assumptions.** Attack's casual average falls from 2.57 to 2.23 if its pool is 15 instead of 10. Real managers only own 3 FWD, 5 DEF, 5 MID.

### Statistical honesty
With 35 Gameweeks, 90% bootstrap intervals are roughly +-0.9 on each average and +-0.2 to 0.3 on each swing. The spec bands (0.4 wide on averages, 0.13-0.15 on swings) are narrower than that noise, so every PASS/FAIL above is PROVISIONAL. Re-run after about 20 Gameweeks of 2026-27, and treat a test as failed only if the interval also sits outside the band.

### Post-launch monitoring (the real test)
The casual simulation is a proxy. Once live, track the share of managers choosing each tactic per Gameweek. If any tactic is chosen by more than 50% of managers for 3 consecutive Gameweeks, investigate a "default tactic" problem before touching the scoring values.
