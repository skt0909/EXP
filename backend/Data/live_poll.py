"""
live_poll.py — checkpoint polling for a single fixture, shared by both
game modes.

Originally lived under Dream11 (Data/dream11_live_poll.py) but
poll_fixture_checkpoint has never been Dream11-specific -- it polls one
fixture's live stats and writes ml.player_gw_stats, which tactical
scoring (Results/scoring_job.py) and Quick 11 contest scoring
(Game_logic/dream11_scoring.py) both read. Worker/tasks.py's
poll_due_fixtures calls it once per fixture per checkpoint, then rescores
both modes from that one write.

Reuses fpl_ingest.py's fetch_json/COUNTING_COLS -- not reinventing the
HTTP request logic. That reuse is also where this module once corrupted
data: fetch_json was @lru_cache'd for the CLI that owns it, and this
module calls it from a long-lived Celery worker on the same
event/{gameweek}/live/ path all matchday, so every poll after the first
was served the first one's payload -- including the checkpoint that
settles the row. fetch_json is uncached now and caching is opt-in
(fetch_json_cached); see the fetching section of fpl_ingest.py. Anything
added here must keep calling the uncached one. poll_fixture_checkpoint
also accepts an already-fetched payload, so a tick with several fixtures
due in one gameweek makes one request, not one per fixture.

CHECKPOINTS. Three, found by find_due_checkpoints below rather than booked
ahead as Celery ETAs:

  * 'halftime'  kickoff + 50 min   is_live = TRUE
  * 'fulltime'  kickoff + 115 min  is_live = TRUE
  * 'final'     once ml.fixtures.finished is TRUE   is_live = FALSE

Only 'final' settles. enforce_gw_stats_immutability_fn blocks any UPDATE
to a row whose OLD.is_live is FALSE, so settling at kickoff+115 -- which is
what 'fulltime' used to do -- froze a guess at when the match ended, before
FPL's late corrections. FPL sets finished only once bonus is confirmed, so
waiting for it settles the numbers FPL itself considers final.

Re-polling an already-settled row hits that trigger. That's caught
per-player here and treated as an expected no-op (already settled), not a
failure.

DOUBLE GAMEWEEKS. event/{gameweek}/live/ reports each player's stats
summed across the whole gameweek. Written as-is under each fixture, a
player with two fixtures would count twice, because both modes score each
fixture row separately. The per-fixture `explain` block cannot be used
instead: it lists only stats that earned points (a midfielder's
goals_conceded is absent) and carries no creativity. So a fixture's row
for such a player is the gameweek total MINUS what is already stored for
that player's team's earlier fixtures in the gameweek. And once a later
fixture has kicked off, the earlier one is no longer re-polled from the
total (it would absorb the later match's stats); its final checkpoint
settles the values it already holds.
"""

import logging
from decimal import Decimal

from sqlalchemy import bindparam, text
from sqlalchemy.exc import SQLAlchemyError

from Shared.seasons import real_season_sql
from fpl_ingest import fetch_json, COUNTING_COLS, SOURCE_STAT_KEYS

logger = logging.getLogger(__name__)

VALID_CHECKPOINTS = ("halftime", "fulltime", "final")

HALFTIME_OFFSET_MINUTES = 50
FULLTIME_OFFSET_MINUTES = 115

# Checkpoints are looked for within this long after kickoff. Long enough to
# catch up a gameweek missed while the worker was down, short enough never
# to reach last season's fixtures -- event/N/live/ only ever serves the
# CURRENT season, so polling an old fixture would write this season's
# numbers under it.
POLL_LOOKBACK = "14 days"

# Decimal stats the live endpoint carries alongside COUNTING_COLS. Same
# names in the payload and in ml.player_gw_stats. creativity feeds the
# tactical creativity tier (Results/tactical_scoring.py), so a poller that
# skipped it never paid that rule on live data.
DECIMAL_COLS = [
    "ict_index", "influence", "creativity", "threat",
    "expected_goals", "expected_assists", "expected_goal_involvements",
]
STAT_COLS = COUNTING_COLS + DECIMAL_COLS

# --- which checkpoint is due -----------------------------------------
#
# At most one checkpoint per fixture per tick, the latest that applies:
# a fixture already finished goes straight to 'final'; one past kickoff+115
# skips a halftime it missed. A fixture with no poll-schedule row yet has
# every polled_at NULL, i.e. nothing done.
DUE_CHECKPOINTS_QUERY = text(
    f"""
    SELECT * FROM (
        SELECT f.id, f.season, f.gameweek, f.kickoff_time,
               CASE
                   WHEN f.finished IS TRUE AND ps.final_polled_at IS NULL
                       THEN 'final'
                   WHEN f.finished IS NOT TRUE
                        AND NOW() >= f.kickoff_time + INTERVAL '{FULLTIME_OFFSET_MINUTES} minutes'
                        AND ps.fulltime_polled_at IS NULL
                       THEN 'fulltime'
                   WHEN f.finished IS NOT TRUE
                        AND NOW() >= f.kickoff_time + INTERVAL '{HALFTIME_OFFSET_MINUTES} minutes'
                        AND ps.halftime_polled_at IS NULL
                        AND ps.fulltime_polled_at IS NULL
                       THEN 'halftime'
               END AS checkpoint
        FROM ml.fixtures f
        LEFT JOIN ml.fixture_poll_schedule ps ON ps.fixture_id = f.id
        WHERE {real_season_sql('f.season')}
          AND f.kickoff_time IS NOT NULL
          AND f.kickoff_time <= NOW() - INTERVAL '{HALFTIME_OFFSET_MINUTES} minutes'
          AND f.kickoff_time > NOW() - INTERVAL '{POLL_LOOKBACK}'
    ) due
    WHERE checkpoint IS NOT NULL
    ORDER BY season, gameweek, kickoff_time, id
    """
)

# halftime_scheduled/fulltime_scheduled are the columns of the ETA-booking
# design this replaced; NOT NULL, so new rows write FALSE. Nothing reads them.
_MARK_CHECKPOINT_SQL = """
    INSERT INTO ml.fixture_poll_schedule (fixture_id, halftime_scheduled, fulltime_scheduled, {col})
    VALUES (:fixture_id, FALSE, FALSE, now())
    ON CONFLICT (fixture_id) DO UPDATE SET {col} = now()
"""
MARK_CHECKPOINT_STMTS = {
    cp: text(_MARK_CHECKPOINT_SQL.format(col=f"{cp}_polled_at")) for cp in VALID_CHECKPOINTS
}


def find_due_checkpoints(engine) -> list:
    """Fixtures with a checkpoint due now. Each row exposes
    .id/.season/.gameweek/.kickoff_time/.checkpoint."""
    with engine.connect() as conn:
        return conn.execute(DUE_CHECKPOINTS_QUERY).all()


def mark_checkpoint_done(engine, fixture_id: int, checkpoint: str) -> None:
    """Record that this fixture's checkpoint ran, so it is not due again."""
    if checkpoint not in MARK_CHECKPOINT_STMTS:
        raise ValueError(f"checkpoint must be one of {VALID_CHECKPOINTS}, got {checkpoint!r}")
    with engine.begin() as conn:
        conn.execute(MARK_CHECKPOINT_STMTS[checkpoint], {"fixture_id": fixture_id})


# --- when ml.fixtures needs refreshing -------------------------------
#
# refresh_fixtures (Worker/tasks.py) is on Beat every 15 minutes but only
# calls the FPL API when this says so: a fixture has kicked off and is not
# yet finished (the finished flag is what triggers 'final'), or nothing has
# been refreshed for a day (rescheduled kickoffs, postponements).
FIXTURES_REFRESH_NEEDED_QUERY = text(
    f"""
    SELECT
        EXISTS (
            SELECT 1 FROM ml.fixtures f
            WHERE {real_season_sql('f.season')}
              AND f.kickoff_time <= NOW()
              AND f.kickoff_time > NOW() - INTERVAL '3 days'
              AND f.finished IS NOT TRUE
        ) AS in_play,
        COALESCE(
            (SELECT MAX(updated_at) FROM ml.fixtures f WHERE {real_season_sql('f.season')})
                < NOW() - INTERVAL '1 day',
            TRUE
        ) AS stale
    """
)


def fixtures_refresh_reason(engine) -> str | None:
    """Why ml.fixtures should be refreshed now, or None if it needn't be."""
    with engine.connect() as conn:
        row = conn.execute(FIXTURES_REFRESH_NEEDED_QUERY).one()
    if row.in_play:
        return "a fixture has kicked off and is not finished"
    if row.stale:
        return "not refreshed in the last day"
    return None


# enforce_gw_stats_immutability_fn's exact RAISE EXCEPTION text -- see
# module docstring. Matching on this substring is inherently fragile if
# that message is ever reworded, same caveat as elsewhere in this project.
SETTLED_ERROR_SUBSTRING = "Cannot modify settled GW stats row"

FIXTURE_QUERY = text(
    "SELECT id, season, gameweek, kickoff_time, home_team_id, away_team_id, home_score, away_score "
    "FROM ml.fixtures WHERE id = :fixture_id"
)

FIXTURE_TEAM_PLAYERS_QUERY = text(
    "SELECT id AS internal_id, fpl_id, team_id FROM ml.players "
    "WHERE season = :season AND (team_id = :home_team_id OR team_id = :away_team_id)"
)

# This fixture's teams' OTHER fixtures in the same gameweek -- only ever
# non-empty in a double gameweek. `started` is judged by the DB clock.
SIBLING_FIXTURES_QUERY = text(
    """
    SELECT f.id, f.kickoff_time, f.home_team_id, f.away_team_id,
           (f.kickoff_time <= NOW()) AS started
    FROM ml.fixtures f
    WHERE f.season = :season AND f.gameweek = :gameweek AND f.id <> :fixture_id
      AND f.kickoff_time IS NOT NULL
      AND (f.home_team_id IN (:home_team_id, :away_team_id)
           OR f.away_team_id IN (:home_team_id, :away_team_id))
    """
)

STORED_STATS_QUERY = text(
    f"""
    SELECT player_id, {", ".join(STAT_COLS)}
    FROM ml.player_gw_stats
    WHERE season = :season AND gameweek = :gameweek
      AND fixture_id IN :fixture_ids AND player_id IN :player_ids
    """
).bindparams(bindparam("fixture_ids", expanding=True), bindparam("player_ids", expanding=True))

SETTLE_EXISTING_STMT = text(
    """
    UPDATE ml.player_gw_stats SET is_live = FALSE
    WHERE fixture_id = :fixture_id AND player_id IN :player_ids AND is_live
    """
).bindparams(bindparam("player_ids", expanding=True))

_ROW_COLS = STAT_COLS + ["was_home", "team_h_score", "team_a_score"]

UPSERT_GW_STAT_STMT = text(
    f"""
    INSERT INTO ml.player_gw_stats (
        player_id, season, gameweek, fixture_id, is_live, value, {", ".join(_ROW_COLS)}
    ) VALUES (
        :player_id, :season, :gameweek, :fixture_id, :is_live, :value, {", ".join(f":{c}" for c in _ROW_COLS)}
    )
    ON CONFLICT (player_id, season, gameweek, (COALESCE(fixture_id, -1))) DO UPDATE SET
        fixture_id = EXCLUDED.fixture_id,
        is_live = EXCLUDED.is_live,
        value = EXCLUDED.value,
        {", ".join(f"{c} = EXCLUDED.{c}" for c in _ROW_COLS)}
    """
)


def _payload_stats(s: dict) -> dict:
    """The stat columns from one element's live `stats`, typed for storage.
    SOURCE_STAT_KEYS translates the one column whose API key differs from
    ours: event/N/live/ calls it `defensive_contribution` (SINGULAR).
    Reading the plural name straight off the payload returned None for
    every player and the `or 0` wrote a hard zero, so DefCon never fired on
    polled data; the map is imported from fpl_ingest.py so the two paths
    cannot drift."""
    out = {c: int(s.get(SOURCE_STAT_KEYS.get(c, c)) or 0) for c in COUNTING_COLS}
    out.update({c: Decimal(str(s.get(c) or 0)) for c in DECIMAL_COLS})
    return out


def _subtract(total: dict, earlier: list[dict]) -> dict:
    """Gameweek total minus earlier fixtures' stored rows, floored at 0 (an
    FPL correction to an earlier match can make a difference go negative)."""
    out = {}
    for c in STAT_COLS:
        zero = Decimal(0) if c in DECIMAL_COLS else 0
        v = total[c] - sum((row[c] or zero) for row in earlier)
        out[c] = v if v > zero else zero
    return out


def poll_fixture_checkpoint(engine, fixture_id: int, checkpoint: str, live: dict | None = None) -> dict:
    """Write this fixture's players' stats at one checkpoint.

    `live` is an already-fetched event/{gameweek}/live/ payload for the
    fixture's gameweek; fetched here when omitted.

    Returns {"updated": [fpl_id, ...], "already_settled": [...],
    "unresolved": [...], "held": [...]}. "held" are double-gameweek
    players whose team's later fixture has already kicked off, so this
    fixture keeps (and at 'final', settles) the values it already had.
    """
    if checkpoint not in VALID_CHECKPOINTS:
        raise ValueError(f"checkpoint must be one of {list(VALID_CHECKPOINTS)}, got {checkpoint!r}")

    with engine.connect() as conn:
        fixture_row = conn.execute(FIXTURE_QUERY, {"fixture_id": fixture_id}).first()
        if fixture_row is None:
            raise ValueError(f"fixture_id {fixture_id} does not exist")
        team_players = conn.execute(
            FIXTURE_TEAM_PLAYERS_QUERY,
            {"season": fixture_row.season, "home_team_id": fixture_row.home_team_id, "away_team_id": fixture_row.away_team_id},
        ).all()
        siblings = conn.execute(
            SIBLING_FIXTURES_QUERY,
            {"season": fixture_row.season, "gameweek": fixture_row.gameweek, "fixture_id": fixture_id,
             "home_team_id": fixture_row.home_team_id, "away_team_id": fixture_row.away_team_id},
        ).all()

    by_fpl_id = {p.fpl_id: p for p in team_players}

    # Per team of this fixture: its earlier fixtures this gameweek (their
    # stored stats get subtracted), and whether a later one has started.
    earlier_by_team: dict[int, list[int]] = {}
    later_started: set[int] = set()
    for team_id in (fixture_row.home_team_id, fixture_row.away_team_id):
        if fixture_row.kickoff_time is None:
            break  # no kickoff to order by; only reachable from a manual call
        for sib in siblings:
            if team_id not in (sib.home_team_id, sib.away_team_id):
                continue
            if sib.kickoff_time < fixture_row.kickoff_time:
                earlier_by_team.setdefault(team_id, []).append(sib.id)
            elif sib.started:
                later_started.add(team_id)

    stored_earlier: dict[int, list[dict]] = {}
    earlier_ids = sorted({fid for ids in earlier_by_team.values() for fid in ids})
    if earlier_ids:
        dgw_player_ids = [p.internal_id for p in team_players if p.team_id in earlier_by_team]
        if dgw_player_ids:
            with engine.connect() as conn:
                for r in conn.execute(STORED_STATS_QUERY, {
                    "season": fixture_row.season, "gameweek": fixture_row.gameweek,
                    "fixture_ids": earlier_ids, "player_ids": dgw_player_ids,
                }).mappings():
                    stored_earlier.setdefault(r["player_id"], []).append(dict(r))

    if live is None:
        live = fetch_json(f"event/{fixture_row.gameweek}/live/")
    elements = live.get("elements", [])

    is_live = checkpoint != "final"

    updated, already_settled, unresolved, held = [], [], [], []
    held_ids = []
    for el in elements:
        fpl_id = int(el["id"])
        player = by_fpl_id.get(fpl_id)
        if player is None:
            continue  # not part of this fixture's two teams -- expected, not an error

        if player.team_id in later_started:
            held.append(fpl_id)
            held_ids.append(player.internal_id)
            continue

        s = el.get("stats", {})
        stats = _payload_stats(s)
        if player.team_id in earlier_by_team:
            stats = _subtract(stats, stored_earlier.get(player.internal_id, []))

        row = {
            "player_id": player.internal_id,
            "season": fixture_row.season,
            "gameweek": fixture_row.gameweek,
            "fixture_id": fixture_id,
            "is_live": is_live,
            # "value" isn't on the live endpoint (see fpl_ingest.py's own
            # docstring note) -- not this poller's concern, defaulted to 0.
            "value": s.get("value") or 0,
            "was_home": player.team_id == fixture_row.home_team_id,
            "team_h_score": fixture_row.home_score,
            "team_a_score": fixture_row.away_score,
            **stats,
        }
        try:
            with engine.begin() as conn:
                conn.execute(UPSERT_GW_STAT_STMT, row)
            updated.append(fpl_id)
        except SQLAlchemyError as e:
            if SETTLED_ERROR_SUBSTRING in str(e):
                logger.info("poll_fixture_checkpoint: fpl_id=%s already settled, skipping", fpl_id)
                already_settled.append(fpl_id)
            else:
                logger.error("poll_fixture_checkpoint: failed to upsert fpl_id=%s: %s: %s", fpl_id, type(e).__name__, e)
                unresolved.append(fpl_id)

    if held_ids and checkpoint == "final":
        with engine.begin() as conn:
            conn.execute(SETTLE_EXISTING_STMT, {"fixture_id": fixture_id, "player_ids": held_ids})

    return {"updated": updated, "already_settled": already_settled, "unresolved": unresolved, "held": held}
