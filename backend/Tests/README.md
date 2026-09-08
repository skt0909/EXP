# Tests

Run with `pytest backend/Tests -v` from the project root (venv activated).

## Database

Tests hit a real Postgres database, not mocks. By default they run against a
**dedicated test database**, `fpl_game_test`, kept entirely separate from the
dev database (`fpl_game`) so a test run can never touch dev data.

- `.env` (project root, gitignored, not committed) sets `DATABASE_URL` —
  used by the app itself (`uvicorn`, Celery) and as pytest's fallback if no
  test DB is configured.
- `.env.test` (project root, gitignored like `.env` — not committed, each
  clone creates its own) sets `TEST_DATABASE_URL` — loaded automatically by
  `backend/Tests/conftest.py`, and preferred over `DATABASE_URL` by every
  `db_utils.py` in this project (so the FastAPI app under `TestClient`, the
  Celery task functions, and the tests' own fixtures all read/write the same
  database). This only affects the pytest process — a separately-running
  `uvicorn`/Celery process never loads `.env.test` and is unaffected.

**One-time setup for a fresh clone:**

```bash
# 1. create .env.test at the project root
echo "TEST_DATABASE_URL=postgresql://postgres:postgres@localhost:5432/fpl_game_test" > .env.test
echo "TEST_REDIS_URL=redis://localhost:6379/0" >> .env.test

# 2. create the test DB itself (name/credentials must match .env.test)
python -c "import psycopg2; c=psycopg2.connect('postgresql://postgres:postgres@localhost:5432/postgres'); c.autocommit=True; c.cursor().execute('CREATE DATABASE fpl_game_test')"

# 3. build its schema from migrations (from the project root)
DATABASE_URL="postgresql://postgres:postgres@localhost:5432/fpl_game_test" python -m alembic upgrade head

# 4. start a local Redis for test_celery_wiring.py (the one file that needs a
#    real, reachable broker -- see below). Not required for the rest of the suite.
docker run -p 6379:6379 redis:7
```

`TEST_REDIS_URL` is needed because `Worker/celery_app.py` resolves its broker
from the environment and has no localhost fallback (deliberately, matching
`DATABASE_URL`), so importing it raises without one. Most test files that
import it — `test_beat_scheduling.py`, `test_fpl_live_polling.py`,
`test_worker.py` — never actually connect to Redis; they read the Beat schedule
or run tasks eagerly. `test_celery_wiring.py` is the one exception: it exercises
the real broker/worker round trip Track A (`test_full_season_scenario.py`) and
everything else deliberately bypasses, so it needs an actual Redis reachable at
`TEST_REDIS_URL` and skips the whole file with a visible reason (not a silent
pass) if one isn't running — start one with the `docker run` command above.

(CI doesn't need `.env.test` — `.github/workflows/backend-tests.yml` sets
`TEST_DATABASE_URL`/`DATABASE_URL`/`REDIS_URL` directly as job env vars against a fresh
Postgres service container, and runs the same migration step.)

Isolation on top of the dedicated DB: `ml.*` fixture data lives under a fake
season (`TEST_SEASON = "9999-00"`, see `conftest.py`), wiped before and after
every test via cascading foreign keys; `public` schema fixtures (users,
squads, etc.) are inserted/torn down per-test by each test file.

## Known gap: 3 tests require real seeded data

`test_context_assembler.py` has 3 tests (`test_no_squad_returns_message_and_never_calls_groq`,
`test_real_squad_prompt_never_contains_raw_predicted_points`,
`test_groq_failure_returns_fallback_not_500`) that deliberately depend on
real `2025-26` GW1+ data already ingested into whichever DB they run
against — by design, to avoid duplicating ingestion machinery just for
these tests (see the file's own docstring). Against the fresh `fpl_game_test`
DB (which has no ingested season data), they skip cleanly with a reason
instead of failing; run them by pointing `TEST_DATABASE_URL` at the dev DB
(or unsetting it, since `DATABASE_URL` falls back to dev) if you need that
coverage.

## Known rule gaps (pinned by characterization tests)

`test_gameweek_lifecycle.py` walks a gameweek end to end — squad selection →
transfers → deadline → Beat lock → scoring → Free Hit revert → next gameweek —
and along the way it pins the places where this codebase knowingly differs from
real FPL.

**Those assertions encode current behavior, not desired behavior.** Each one is
written to fail the moment the gap is closed. When you implement one of these
rules, the matching test failing is the signal that it worked — fix the code,
then update the assertion. Do not "correct" the assertion on its own.

| Deviation from real FPL | What this codebase does | Pinned by | Production reference |
|---|---|---|---|
| A blank gameweek is distinguishable from a no-show | It is not — a blanking player simply has no stats row, which `COALESCE`s to 0 minutes, so autosub treats them exactly like a dropped player. The outcome happens to match FPL; the gap is that no rule *could* separate the two | `test_a_blank_gameweek_starter_is_scored_as_a_zero_minute_no_show` | `scoring.py:68-82` |
| Free Hit reverts at the next deadline | Reverts when the free-hit gameweek is **over** (every fixture finished, or kicked off more than `FREE_HIT_REVERT_BUFFER_HOURS` ago) — deliberate, so the free-hit squad stays visible through `GET /squad` while its gameweek is played | `test_free_hit_snapshot_reverts_only_once_the_gameweek_is_over`, `test_free_hit_does_not_revert_merely_because_the_next_deadline_passed` | `free_hit_revert.py` (`REVERTABLE_FREE_HITS_QUERY`) |

Recently closed rule gaps now covered by positive tests:

| Rule | Coverage |
|---|---|
| Normal gameweeks cap transfers at 20; Wildcard/Free Hit remain uncapped | `test_normal_gameweek_rejects_transfer_after_twenty_already_made`, `test_wildcard_gameweek_allows_more_than_twenty_transfers` |
| Banked free transfers survive Wildcard/Free Hit gameweeks | `test_a_chip_gameweek_preserves_the_bank` |
| Sell price uses current price with half-profit rounded down | `test_sell_price_uses_half_profit_rounded_down_after_a_price_rise` |
| Chips are one set per half, and first-half chips do not carry over | `test_a_spent_restricted_chip_is_replenished_in_the_second_half`, `test_wildcards_are_one_per_half_of_the_season` |
| Free Hit cannot be used in consecutive gameweeks | `test_free_hit_cannot_be_used_in_consecutive_gameweeks` |
| Double Gameweeks can store multiple fixture rows and score both | `test_a_double_gameweek_stores_and_scores_two_fixture_rows` |
| Classic scoring can compute from component stats | `test_classic_scoring_computes_from_components_with_total_points_fallback` |

### Rules versioning — read this before shipping a scoring change

Every `gw_scores` row carries a `rules_version` (smallint, comparable), stamped
at write time from `CURRENT_RULES_VERSION` in `Shared/rules.py`. Version 1
is the original wholesale-`total_points` era; version 2 is everything in the
table above.

**When you ship a change to scoring, banking or pricing that alters what a
manager's points would be, increment `CURRENT_RULES_VERSION` in the same PR.**
Only then — it is deliberately a manual, semantic judgement, because "does this
change what a score comes out as?" is not something a migration hash or commit
SHA can answer, and a version that moved on every unrelated commit would carry
no information.

Bumping it **does not rescore anything**. History stays frozen under the rules
that produced it; only gameweeks scored after the bump carry the new version.
That is a deliberate decision, not an omission: rescoring 2025-26 under the
current rules would produce a *differently wrong* number, because
`defensive_contributions` was never ingested for that season and its stat rows
are frozen by `enforce_gw_stats_immutability`. See migration `c9a04e7b53d1`.

One consequence to be aware of: re-scoring a gameweek restamps it with the
current version, since it genuinely was recomputed under current rules. In
practice `refresh_active_gameweeks` only revisits a 5-day window, which is what
keeps older gameweeks frozen.

### Decided: `season_total` may span rule versions, and that is accepted

`gw_scores.season_total` sums prior `total_points` for the season with **no
version filter** (`scoring.SEASON_TOTAL_PRIOR_QUERY`). If a rules change shipped
mid-season, a manager's season total would combine gameweeks scored under two
rule sets, and the league tables built from it (`standings.py`,
`team_dashboard.py`) would inherit that.

**Decision: accept it.** `rules_version` is a cross-season and historical tag
only. `season_total` keeps summing every gameweek in the season regardless of
version, and there is no per-version split.

The reasoning is that the problem is avoidable at the source. **Ship rules
changes at season boundaries**, and a season never spans two versions in the
first place. Building per-version splitting now would solve a situation the team
controls whether to create — and it would not come for free: it immediately
raises a second question with no clean answer, namely what a league table is
supposed to show when its members' totals straddle a version boundary. Trading a
hypothetical problem for a real unresolved one is a bad exchange.

If a mid-season rules change ever becomes genuinely unavoidable, revisit this
with the actual case in hand rather than a hypothetical — the concrete situation
will say more about what a league table should show than any decision made in
advance can.

Practical consequences:

- `SEASON_TOTAL_PRIOR_QUERY` stays as it is. Do not add a version filter to it
  as a tidy-up; it is deliberate.
- `rules_version` remains useful for exactly what it was added for: telling
  apart gameweeks scored under different rule generations, which is what makes
  the frozen 2025-26 data legible rather than mysterious.
- The guidance above still holds — bump `CURRENT_RULES_VERSION` when shipping a
  scoring/banking/pricing change. This decision is about `season_total`, not
  about whether to version at all.

### A note on testing time

`test_gameweek_lifecycle.py` uses **no clock library**, and neither should
anything added to it. Almost every deadline comparison in this app runs in
Postgres via SQL `NOW()` (`deadlines.DEADLINE_PASSED_QUERY`), so `freezegun`
and `time-machine` would move the Python clock and silently do nothing. The
working approach — and the one the whole suite uses — is to seed
`ml.fixtures.kickoff_time` relative to now, then `UPDATE` it into the past to
cross a deadline. That is legal because `ml.enforce_fixture_identity_fn` guards
only `home_team_id`/`away_team_id`/`fpl_id`, leaving `kickoff_time` and
`finished` writable.

For the same reason there are no saved JSON API snapshots: nothing in
`Game_logic/` or `Context_assembler/main.py` makes an HTTP call at all, so the
lifecycle suite has no external seam to mock. Only `Data` talks to the
FPL API, and it is mocked by monkeypatching `fetch_json` on the *consuming*
module (see `test_fpl_live_polling.py` and `test_dream11.py`).
