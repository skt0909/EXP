# Dashboard data: what `GET /team` returns today, and what Phase 4 must replace

**From code only.** Nothing here was obtained by running the app; the column
list is from `\d gw_scores` on `fpl_game_test`, which is the only database this
task may touch. Every claim cites file and line.

---

## 1. The endpoint

| | |
|---|---|
| Route | `GET /team` |
| Handler | `backend/Results/team_dashboard.py:284` — `get_team_dashboard(season, gameweek, current_user)` |
| Response model | `TeamDashboardResponse`, `team_dashboard.py:198` |
| Auth | `Depends(get_current_user)`; `user_id` comes from the token, never a parameter |
| Query params | `season: str`, `gameweek: int` — **both required** (omitting them is a 422, not a default) |

### The read model — five queries, `team_dashboard.py`

| Query | Line | Reads |
|---|---|---|
| `USER_QUERY` | ~60 | `users.username`, `users.team_name` |
| `GW_SELECTION_QUERY` | 69 | `gw_selections.id`, **`chip_used`** |
| `STARTING_XI_ROWS_QUERY` | 86 | `starting_xi` ⋈ `ml.players` ⋈ `ml.teams` ⟕ `ml.player_gw_stats` |
| `GW_SCORE_QUERY` | 108 | `gw_scores` — `raw_points, final_points, transfer_hits, hit_deductions, total_points, season_total` |
| `GW_FIXTURE_STATUS_QUERY` | — | fixture states, for `live_status`; also imported by `Results/leagues.py:47` |

---

## 2. Manager-level fields

| Field | Type | Source |
|---|---|---|
| `user_id` | `int` | token |
| `username` | `str \| None` | `users` |
| `team_name` | `str \| None` | `users` |
| `season` | `str` | request |
| `gameweek` | `int` | request |
| `deadline` | `str \| None` | `resolve_gameweek_deadline()` |
| `has_lineup` | `bool` | whether a `gw_selections` row exists |
| **`chip_used`** | `str \| None` | `gw_selections.chip_used` — **column dropped in Phase 1** |
| **`captain_multiplier`** | `int` | 3 under `triple_captain`, else 2 (`team_dashboard.py:306`) |
| `gw_points` | `int` | `gw_scores.total_points` |
| `gw_average` | `float \| None` | mean across all managers that gameweek |
| `season_total` | `int` | `gw_scores.season_total` |
| `has_score` | `bool` | whether a `gw_scores` row exists — the flag that stops "0 points" reading as a real score |
| `raw_points` | `int` | `gw_scores.raw_points` |
| **`captain_bonus`** | `int` | computed as `final_points - raw_points` (`team_dashboard.py:102-107`) |
| **`transfer_hits`** | `int` | `gw_scores.transfer_hits` — **column dropped in Phase 1** |
| **`hit_deductions`** | `int` | `gw_scores.hit_deductions` — **column dropped in Phase 1** |
| `final_total` | `int` | `gw_scores.final_points` |
| `live_status` | `str` | `'upcoming' \| 'live' \| 'final'` |
| `overall_rank` | `int \| None` | season-wide rank |
| `overall_rank_total` | `int \| None` | number of ranked managers |
| `team_value_available` | `bool` | False for a `final` gameweek with no finance row |
| `team_value` | `float` | £m |
| `bank` | `float` | £m |
| `lineup` | `Lineup` | `{GK, DEF, MID, FWD}`, each a `list[PlayerLine]` (`team_dashboard.py:191`) |
| `bench` | `list[PlayerLine]` | bench in slot order |

**Bold = Phase 4 must replace.** Six manager-level fields are captain/chip/hit
concepts that no longer exist.

---

## 3. Per-player fields — `PlayerLine`, `team_dashboard.py:170`

| Field | Type | Source |
|---|---|---|
| `player_id` | `int` | `ml.players.fpl_id` |
| `name` | `str` | `ml.players.web_name` |
| `position` | `str` | `ml.players.position` (`GK`/`DEF`/`MID`/`FWD`) |
| `club` | `str` | `ml.teams.short_name` — the frontend resolves a kit graphic from it |
| `points` | `int` | `COALESCE(ml.player_gw_stats.total_points, 0)` |
| **`is_captain`** | `bool` | `starting_xi.is_captain` — **column dropped in Phase 1** |
| **`is_vice_captain`** | `bool` | `starting_xi.is_vice_captain` — **column dropped in Phase 1** |
| **`is_autosubbed_in`** | `bool` (default False) | recomputed at request time |
| **`is_autosubbed_out`** | `bool` (default False) | recomputed at request time |

### Per-player points are NOT stored — they are read live from the ML feed

`STARTING_XI_ROWS_QUERY`, `team_dashboard.py:90`:

```sql
COALESCE(pgs.total_points, 0) AS points,
COALESCE(pgs.minutes, 0) AS minutes
...
LEFT JOIN ml.player_gw_stats pgs
    ON pgs.player_id = mp.id AND pgs.season = :season AND pgs.gameweek = :gameweek
```

So a player's displayed points are **`ml.player_gw_stats.total_points` read at
request time** — FPL's own number, not anything this app computed. There is no
per-player points table.

**This is the single biggest Phase 4 problem for the dashboard.** Under the
tactical rules a player's contribution is *not* `total_points`: it is General
Points from the new table (no FPL bonus), plus Tactical Points if he is a Bonus
Player, and it depends on the manager's tactic. Two managers holding the same
player will see different per-player values. The current query cannot express
that, because it never asks who the manager is when fetching points.

`gw_scores` stores only manager-level totals, so **nothing persists a
per-player breakdown**. Phase 4 must either recompute the breakdown per request
by calling `Results/tactical_scoring.score_selection` (which already returns a
full `PlayerLine` per player — role, general, tactical, counted), or persist it.
The engine was built to return exactly this, so recomputing is the cheaper path.

### Autosub flags are recomputed per request, not stored

`team_dashboard.py:181-188`:

> *"Autosub outcome for THIS gameweek, recomputed for display via
> `scoring.resolve_autosubs` (the swaps aren't persisted anywhere -- scoring
> derives them in memory and only stores the resulting totals)."*

`from Results.scoring import resolve_autosubs` (`team_dashboard.py:55`), used at
`team_dashboard.py:315` via `_autosub_player_ids(rows, chip_used)`. **This is a
direct import of the classic scorer**, so Phase 4 cannot replace `scoring.py`
without also changing the dashboard.

---

## 4. `gw_scores` columns, post-migration

From `\d gw_scores` on `fpl_game_test`:

| Column | Type | Holds |
|---|---|---|
| `id` | `integer` | surrogate key |
| `user_id` | `integer` | FK → `users`, `ON DELETE CASCADE` |
| `season` | `varchar(9)` | |
| `gameweek` | `smallint` | |
| `raw_points` | `smallint NOT NULL DEFAULT 0` | classic: effective XI at true base value, captain **not** doubled. Tactical: General Points of every scoring slot |
| `final_points` | `smallint NOT NULL DEFAULT 0` | classic: `raw_points` + captain bonus. Tactical: `raw + tactical_points + sub_bonus` |
| `total_points` | `smallint NOT NULL DEFAULT 0` | classic: `final_points - hit_deductions`. Tactical: equals `final_points` (no hits) |
| `season_total` | `integer NOT NULL DEFAULT 0` | cumulative, recomputed each run |
| `rules_version` | `smallint NOT NULL` | 1, 2 live; **3** is the tactical ruleset |
| `tactical_points` | `smallint NOT NULL DEFAULT 0` | **added Phase 1, still always 0** — nothing writes it yet |
| `sub_bonus` | `smallint NOT NULL DEFAULT 0` | **added Phase 1, still always 0** — nothing writes it yet |

Unique on `(user_id, season, gameweek)` — the upsert key that makes scoring
idempotent. **Dropped in Phase 1:** `transfer_hits`, `hit_deductions`, which
`GW_SCORE_QUERY` (`team_dashboard.py:109`) still selects.

---

## 5. Other endpoints that show points

| Endpoint | File:line | Points-bearing response keys |
|---|---|---|
| `GET /team` | `team_dashboard.py:284` | §2 and §3 above |
| `GET /leagues` | `leagues.py:371` | per league: `league_id`, `name`, `code`, `scoring_type`, `member_count`, `your_rank`, `your_points` |
| `GET /leagues/{id}/table` | `leagues.py:384` | `entries[]` with `user_id`, `team_name`, `username`, `rank`, `rank_movement`, `season_points`, `last_gw_points` |
| `GET /leagues/{id}/h2h` | `leagues.py:465` | `fixtures[]` → `fixture_id`, `gameweek`, `side_1`/`side_2` (`user_id`, `team_name`, `username`, **`points`**, `is_current_user`), `result`, `is_bye`, `outcome_for_current_user`; `records[]` → `wins`, `draws`, `losses`, `byes`, `match_points`; plus `status`, `is_provisional`, `your_fixture` |
| `GET /scoring-rules` | `scoring_rules.py:129` | publishes the point table itself, including **`captain_multiplier`** and **`triple_captain_multiplier`** (`scoring_rules.py:90-91, 168-169`) |

`leagues.py` imports `GW_FIXTURE_STATUS_QUERY` and `_resolve_live_status` from
`team_dashboard.py` (`leagues.py:47`) so both screens answer "is this gameweek
finished?" identically — a Phase 4 change to that definition moves both.

`season_points` means different things per league type: cumulative FPL points
for classic, cumulative **match** points (3/1/0/3) for H2H
(`standings.py:16-25`). Not affected by the tactical change, but easy to
conflate.

---

## 6. What Phase 4 must replace, by location

### Captain / vice-captain
- `team_dashboard.py:53` — `from Shared.rules import CAPTAIN_MULTIPLIER, TRIPLE_CAPTAIN_MULTIPLIER`
- `team_dashboard.py:88` — query selects `sx.is_captain, sx.is_vice_captain` (**columns dropped**)
- `team_dashboard.py:179-180` — `PlayerLine.is_captain`, `.is_vice_captain`
- `team_dashboard.py:207` — `captain_multiplier`
- `team_dashboard.py:217` — `captain_bonus`, derived as `final_points - raw_points`
- `team_dashboard.py:306` — the 3x/2x selection
- `scoring_rules.py:90-91, 168-169` — published to clients

### Chips
- `team_dashboard.py:69` — `GW_SELECTION_QUERY` selects `chip_used` (**column dropped**)
- `team_dashboard.py:206` — `chip_used` in the response
- `team_dashboard.py:305, 315` — `chip_used` drives the multiplier and is passed to the autosub helper
- `Gameplay/chips.py` — the whole module, and `GET /chips/used`

### Hits
- `team_dashboard.py:109` — `GW_SCORE_QUERY` selects `transfer_hits, hit_deductions` (**both dropped**)
- `team_dashboard.py:218-219` — both in the response

### Bench order and autosub
- `team_dashboard.py:55` — `from Results.scoring import resolve_autosubs`
- `team_dashboard.py:315` — `_autosub_player_ids(rows, chip_used)`
- `team_dashboard.py:187-188` — `is_autosubbed_in` / `is_autosubbed_out`
- `team_dashboard.py:234` — `bench: list[PlayerLine]`

The bench is now **role-bearing**: slot 12 backup GK, 13 outfield Auto Sub,
14–15 Tactical Subs, with `role` a generated column. The response exposes the
bench as a flat list with no role, so a client cannot tell a Tactical Sub from
an Auto Sub. Phase 4 (or Phase 5) needs to surface `role`, plus the new
`auto_sub_replaced` state and the executed swaps.

### Not yet surfaced anywhere
`gw_scores.tactical_points` and `gw_scores.sub_bonus` exist and are always 0.
No endpoint reads them. `tactical_swaps` rows are written by `POST /gw_selection`
and read back by `GET /gw_selection`, but **no points screen shows them**.

---

## 7. Open questions for Phase 4

**D1. Per-player points must become manager-relative.** Today `points` is FPL's
`total_points`, identical for every manager holding that player. Under the
tactical rules it depends on the manager's tactic and Bonus Players. The current
query shape cannot express it.

**D2. Recompute or persist?** `score_selection` already returns a full
per-player breakdown, so the dashboard could call it per request — consistent
with how autosubs are handled today. Persisting would need a new table and would
have to stay consistent with the idempotent rescore.

**D3. `captain_bonus` has no successor.** It is `final_points - raw_points`. The
same subtraction now yields `tactical_points + sub_bonus` combined, which the
UI probably wants split — both columns exist and are already 0-filled.

**D4. `GET /scoring-rules` still publishes captain multipliers** as part of its
contract. Changing it changes a client-facing payload, not just internals.
