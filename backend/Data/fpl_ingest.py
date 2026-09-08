"""
fpl_ingest.py

Loads an FPL season into the `ml` schema from either of two sources:

  --source archive  (default)
      The community archive (vaastav/Fantasy-Premier-League). Covers
      *completed* seasons only, and lags the live game by days-to-weeks.
        data/<season>/teams.csv        -> ml.teams
        data/<season>/players_raw.csv  -> ml.players
        data/<season>/fixtures.csv     -> ml.fixtures      (filtered by event)
        data/<season>/gws/gw<N>.csv    -> ml.player_gw_stats

  --source live
      FPL's public API. The only way to reach the *current* season, which
      the archive has not published yet.
        bootstrap-static/  -> ml.teams, ml.players
        fixtures/?event=N  -> ml.fixtures
        event/N/live/      -> ml.player_gw_stats

Both sources are normalised to one canonical row shape and share a single
set of INSERT statements, so a correctness fix lands in both at once.

Usage:
    python fpl_ingest.py --season 2025-26 --bootstrap
    python fpl_ingest.py --season 2025-26 --gw 1
    python fpl_ingest.py --season 2025-26 --gw-range 1 4
    python fpl_ingest.py --season 2026-27 --source live --bootstrap
    python fpl_ingest.py --season 2026-27 --source live --bootstrap --upcoming-fixtures
    python fpl_ingest.py --season 2026-27 --source live --gw 1
    python fpl_ingest.py --season 2025-26 --verify

Correctness notes (each of these was a bug in the earlier API-only version
of this script, kept here so they are not reintroduced):
  * was_home is taken from the archive, or derived per-player by comparing
    the player's team to the fixture's home team. It is never inferred from
    the set of home teams, which mislabels blanks and doubles.
  * cost_start = now_cost - cost_change_start (the true season-start price)
    rather than now_cost (the price at scrape time).
  * team_h_score / team_a_score are populated.
  * Empty row batches are skipped instead of raising StatementError.
  * Connections are always used as context managers.
  * A gameweek is refused unless its fixtures are actually finished.

Fields the live API does not expose, and what happens to them:
  * `selected` (raw selection count) — bootstrap-static only carries
    selected_by_percent, which is a different quantity. Written as NULL.
  * `value` — taken from bootstrap-static now_cost, i.e. the price *now*,
    not the price during the gameweek. Exact only if you ingest promptly
    after the gameweek finishes. The archive carries the true in-GW value.
  * `transfers_in`/`transfers_out` — from transfers_in_event /
    transfers_out_event, which reset each gameweek. transfers_balance is
    computed as the difference.
"""

import argparse
import io
import sys
from functools import lru_cache

import pandas as pd
import requests
from sqlalchemy import text

from utils.db_utils import get_engine, safe_url

ARCHIVE = "https://raw.githubusercontent.com/vaastav/Fantasy-Premier-League/master/data"
API = "https://fantasy.premierleague.com/api"

POSITION_MAP = {1: ("GK", 0), 2: ("DEF", 1), 3: ("MID", 2), 4: ("FWD", 3)}

COUNTING_COLS = [
    "minutes", "goals_scored", "assists", "clean_sheets", "goals_conceded",
    "saves", "bonus", "bps", "yellow_cards", "red_cards", "own_goals",
    "penalties_saved", "penalties_missed", "defensive_contributions", "total_points",
]

# Our column names match both upstream sources' field names one-for-one
# with a single exception: the defensive-contribution tally is
# `defensive_contribution` (SINGULAR) on event/N/live/ and in the archive
# CSVs, while our column -- and Results/scoring.py -- call it
# `defensive_contributions`. Reading the plural name off the API returned
# None for every player, which num() then floored to 0, so the stat
# ingested as a column of zeros and the DefCon scoring rule never fired.
# Kept as an explicit map rather than renaming our column, because the
# column name is load-bearing in scoring.py and a migration.
#
# The value is a COUNT of defensive actions, not points: DEF are credited
# clearances+blocks+interceptions+tackles, MID/FWD additionally
# recoveries. Both sources already apply that position rule themselves,
# so this is read straight through rather than recomputed from the
# component columns -- scoring.py owns the 10/12 threshold, and deriving
# the tally here as well would put the position rule in two places.
SOURCE_STAT_KEYS = {"defensive_contributions": "defensive_contribution"}

# Every column ingest_gameweek_stats() reads. Both sources must emit all of
# them; a source that cannot fill one emits it as NA rather than omitting it,
# so a genuine upstream schema change surfaces here instead of as silent NULLs.
GW_STATS_COLUMNS = COUNTING_COLS + [
    "element", "fixture", "was_home", "team_h_score", "team_a_score",
    "ict_index", "influence", "creativity", "threat",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "value", "selected", "transfers_in", "transfers_out", "transfers_balance",
]


# ---------------------------------------------------------------------
# fetching
#
# CACHING IS OPT-IN HERE, AND DELIBERATELY SO. READ BEFORE ADDING A CALL.
#
# fetch_json used to be @lru_cache'd itself. That was safe for the CLI it
# was written for -- a one-shot process that fetches bootstrap-static/
# three times in a few seconds and then exits -- but live_poll.py later
# imported it and called it from a Celery worker as
# fetch_json(f"event/{gameweek}/live/"), which is the opposite situation:
# a LONG-LIVED process re-reading a resource that changes throughout a
# match.
#
# The result was silent data corruption. The first poll of a gameweek
# populated the cache; every later poll of that SAME gameweek -- the
# full-time checkpoint, and every other fixture in the same gameweek --
# was served the first poll's payload and never made an HTTP request at
# all. Worse, the worker runs --pool=solo (one process, see
# Worker/celery_app.py), so there is no per-process partitioning to make
# some polls accidentally fresh, and the entry is never evicted in the
# window that matters: maxsize is per DISTINCT KEY, and a gameweek is one
# key that stays most-recently-used all matchday. The only thing that
# cleared it was a worker restart.
#
# That mattered because live_poll's full-time checkpoint writes
# is_live=FALSE, and ml.enforce_gw_stats_immutability_fn then blocks any
# further UPDATE to that row. A gameweek settled from a stale cached
# payload could not be corrected in place afterwards.
#
# So the default is now UNCACHED, and caching is a separate function you
# have to ask for by name. A new call site is therefore correct by
# default, and reaching for the cached variant is a visible decision in
# the diff rather than an inherited one.
# ---------------------------------------------------------------------

# Safe to cache unconditionally: the community archive holds COMPLETED
# seasons as static files in a git repo. A given (season, path) is
# immutable, so re-fetching it could only ever return the same bytes.
@lru_cache(maxsize=32)
def fetch_csv(season: str, path: str) -> pd.DataFrame:
    url = f"{ARCHIVE}/{season}/{path}"
    resp = requests.get(url, timeout=90)
    if resp.status_code == 404:
        raise SystemExit(f"Not in archive: {url}")
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text))


def fetch_json(path: str):
    """GET {API}/{path}, uncached -- every call is a real request.

    This is the right default for everything the FPL API serves: live
    stats, fixtures and bootstrap-static all change while a gameweek is
    in progress. Use it for anything read more than once over time.

    For the narrow case of reference data re-read several times within a
    single short-lived CLI run, use fetch_json_cached below.
    """
    resp = requests.get(
        f"{API}/{path}", headers={"User-Agent": "Mozilla/5.0"}, timeout=90
    )
    resp.raise_for_status()
    return resp.json()


def _season_from_events(events) -> str | None:
    """"2026-27" from bootstrap-static's events list, or None if it is
    empty. The first event's deadline year IS the season's start year.

    Split out so check_season() and current_live_season() below share one
    definition of the formula while fetching bootstrap-static through
    different paths -- the CLI's cached one and the worker's uncached one.
    """
    events = events or []
    if not events:
        return None
    year = int(events[0]["deadline_time"][:4])
    return f"{year}-{str(year + 1)[-2:]}"


def current_live_season() -> str | None:
    """The season FPL's API is serving right now, or None if it has no
    events yet (the gap between seasons).

    The API only ever carries one season, so this is what "current
    season" means for any caller that must not hardcode it -- notably
    Worker/tasks.py's refresh_fixtures, which would otherwise need a
    constant edited every August.

    Deliberately uses the UNCACHED fetch_json even though the answer
    changes once a year: this is called from a long-lived worker, and a
    process-lifetime cache would pin it to whatever season was current
    when that worker booted. That is the same failure the fetching
    section above exists to prevent, just on a twelve-month fuse.
    """
    return _season_from_events(fetch_json("bootstrap-static/").get("events"))


@lru_cache(maxsize=8)
def fetch_json_cached(path: str):
    """fetch_json, memoized FOR THE LIFETIME OF THE PROCESS.

    Only correct where BOTH hold:

      1. the process is short-lived (the CLI in main(), not a Celery
         worker), so "for the lifetime of the process" means seconds; and
      2. the caller would otherwise re-request the same path several
         times in that window.

    Exactly one call site qualifies: LiveSource._bootstrap(), which
    check_season/teams/players each call, giving three requests for one
    payload within one `python fpl_ingest.py` invocation.

    Nothing that polls should ever call this. If you are reaching for it
    from a worker, a task, or anything with a loop, the answer is
    fetch_json -- and if the request volume genuinely needs bounding,
    that is a TTL cache keyed to the resource's real update rate, not
    this.
    """
    return fetch_json(path)


# ---------------------------------------------------------------------
# sources — each returns the canonical frames the ingest functions consume
# ---------------------------------------------------------------------
class ArchiveSource:
    """Community archive CSVs. The canonical column names *are* the CSV's."""

    label = "archive"

    def __init__(self, season: str):
        self.season = season

    def check_season(self):
        """fetch_csv already exits on a 404 for an unknown season directory."""

    def teams(self) -> pd.DataFrame:
        return fetch_csv(self.season, "teams.csv")

    def players(self) -> pd.DataFrame:
        return fetch_csv(self.season, "players_raw.csv")

    def fixtures(self, gameweek: int) -> pd.DataFrame:
        fx = fetch_csv(self.season, "fixtures.csv")
        return fx[fx.event == gameweek].copy()

    def gw_stats(self, gameweek: int) -> pd.DataFrame:
        df = fetch_csv(self.season, f"gws/gw{gameweek}.csv")
        if "defensive_contributions" not in df.columns:
            source_col = SOURCE_STAT_KEYS["defensive_contributions"]
            if source_col in df.columns:
                df["defensive_contributions"] = df[source_col]
            else:
                # Seasons before 2025-26 predate the rule entirely -- the
                # archive has no such column and 0 is the correct value,
                # not a placeholder for missing data. Confirmed against
                # the 2024-25 CSVs, which carry none of the defensive
                # columns at all.
                df["defensive_contributions"] = 0
        return df


class LiveSource:
    """FPL's public API, reshaped into the archive's column names."""

    label = "live"

    def __init__(self, season: str):
        self.season = season

    def _bootstrap(self):
        # The one cached call in the codebase -- check_season(), teams()
        # and players() each call this within a single CLI run. See
        # fetch_json_cached's docstring for why nothing else qualifies.
        return fetch_json_cached("bootstrap-static/")

    def check_season(self):
        """
        Refuse to write current-season data under a different season label.

        The API only ever serves the season in progress, so --season is not a
        selector here — it is an assertion, and an unchecked mismatch would
        silently mislabel every row.
        """
        serving = _season_from_events(self._bootstrap().get("events"))
        if serving is None:
            return
        if serving != self.season:
            raise SystemExit(
                f"--source live is serving {serving}, but --season is {self.season}. "
                f"The live API only ever carries the current season; use "
                f"--source archive to load {self.season}."
            )

    def teams(self) -> pd.DataFrame:
        return pd.DataFrame(self._bootstrap()["teams"])

    def players(self) -> pd.DataFrame:
        return pd.DataFrame(self._bootstrap()["elements"])

    def fixtures(self, gameweek: int) -> pd.DataFrame:
        data = fetch_json(f"fixtures/?event={gameweek}")
        if not data:
            return pd.DataFrame(
                columns=["id", "event", "team_h", "team_a", "kickoff_time",
                         "team_h_score", "team_a_score", "finished"]
            )
        return pd.DataFrame(data)

    def gw_stats(self, gameweek: int) -> pd.DataFrame:
        """
        One row per player, matching the archive's post-aggregation shape.

        event/N/live/ reports a player's stats already summed across the
        gameweek, so a double gameweek yields a single aggregate row here.
        fixture and was_home follow the player's *first* fixture, which is
        the same convention the archive aggregation uses.
        """
        live = fetch_json(f"event/{gameweek}/live/")
        elements = live.get("elements", [])
        if not elements:
            raise SystemExit(
                f"event/{gameweek}/live/ returned no player data — GW{gameweek} "
                f"has probably not started yet."
            )

        fixtures = self.fixtures(gameweek)
        # fpl team id -> that team's chronologically first fixture of the
        # gameweek. Ordering by kickoff rather than fixture id matters in a
        # double gameweek, where the lower id is often the later match.
        by_team = {}
        order = fixtures.sort_values(["kickoff_time", "id"], na_position="last")
        for f in order.to_dict("records"):
            for side in ("team_h", "team_a"):
                by_team.setdefault(int(f[side]), f)

        meta = {
            int(p["id"]): p
            for p in self._bootstrap()["elements"]
        }

        rows = []
        for el in elements:
            eid = int(el["id"])
            s = el.get("stats", {})
            p = meta.get(eid, {})
            team = p.get("team")
            fx = by_team.get(int(team)) if team is not None else None

            t_in = p.get("transfers_in_event")
            t_out = p.get("transfers_out_event")

            rows.append({
                "element": eid,
                "fixture": None if fx is None else int(fx["id"]),
                "was_home": None if fx is None else int(fx["team_h"]) == int(team),
                "team_h_score": None if fx is None else fx.get("team_h_score"),
                "team_a_score": None if fx is None else fx.get("team_a_score"),
                # stats present on event/N/live/. SOURCE_STAT_KEYS covers
                # the one column whose API key differs from ours; every
                # other name matches, and a name that stops matching
                # surfaces as a NULL rather than an error, so keep the
                # map in sync if the API renames anything else.
                **{c: s.get(SOURCE_STAT_KEYS.get(c, c)) for c in COUNTING_COLS},
                "ict_index": s.get("ict_index"),
                "influence": s.get("influence"),
                "creativity": s.get("creativity"),
                "threat": s.get("threat"),
                "expected_goals": s.get("expected_goals"),
                "expected_assists": s.get("expected_assists"),
                "expected_goal_involvements": s.get("expected_goal_involvements"),
                # not on the live endpoint — see the module docstring
                "value": p.get("now_cost"),
                "selected": None,
                "transfers_in": t_in,
                "transfers_out": t_out,
                "transfers_balance": (
                    None if t_in is None or t_out is None else int(t_in) - int(t_out)
                ),
            })

        return pd.DataFrame(rows)


def make_source(name: str, season: str):
    return {"archive": ArchiveSource, "live": LiveSource}[name](season)


def _require_columns(df: pd.DataFrame, columns, what: str):
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise SystemExit(
            f"{what} is missing expected column(s): {', '.join(missing)}. "
            f"The upstream feed's schema has probably changed."
        )


# ---------------------------------------------------------------------
# db helpers
# ---------------------------------------------------------------------
def _executemany(engine, stmt, rows, label):
    """Run an executemany, skipping empty batches (which SQLAlchemy rejects)."""
    if not rows:
        print(f"  {label}: nothing to write (0 rows) — skipped")
        return 0
    with engine.begin() as conn:
        conn.execute(stmt, rows)
    return len(rows)


def _count(engine, table, season, gw=None):
    q = f"SELECT count(*) FROM ml.{table} WHERE season = :s"
    p = {"s": season}
    if gw is not None:
        q += " AND gameweek = :g"
        p["g"] = gw
    with engine.connect() as conn:
        return conn.execute(text(q), p).scalar()


def _id_map(engine, table, season, key="fpl_id", val="id"):
    with engine.connect() as conn:
        return dict(
            conn.execute(
                text(f"SELECT {key}, {val} FROM ml.{table} WHERE season = :s"),
                {"s": season},
            ).fetchall()
        )


def num(v, cast=float, default=None):
    return default if pd.isna(v) else cast(v)


def flag(v):
    """Tri-state bool: NA stays NULL rather than becoming True."""
    return None if pd.isna(v) else bool(v)


# ---------------------------------------------------------------------
# 1. bootstrap — teams + players
# ---------------------------------------------------------------------
def ingest_bootstrap(engine, source):
    season = source.season
    teams = source.teams()
    _require_columns(
        teams,
        ["id", "name", "short_name", "strength_overall_home", "strength_overall_away",
         "strength_attack_home", "strength_attack_away",
         "strength_defence_home", "strength_defence_away"],
        "teams feed",
    )

    team_stmt = text("""
        INSERT INTO ml.teams (
            fpl_id, season, name, short_name,
            strength_overall_home, strength_overall_away,
            strength_attack_home, strength_attack_away,
            strength_defence_home, strength_defence_away, updated_at
        ) VALUES (
            :fpl_id, :season, :name, :short_name,
            :soh, :soa, :sah, :saa, :sdh, :sda, now()
        )
        ON CONFLICT (fpl_id, season) DO UPDATE SET
            name = EXCLUDED.name,
            short_name = EXCLUDED.short_name,
            strength_overall_home = EXCLUDED.strength_overall_home,
            strength_overall_away = EXCLUDED.strength_overall_away,
            strength_attack_home = EXCLUDED.strength_attack_home,
            strength_attack_away = EXCLUDED.strength_attack_away,
            strength_defence_home = EXCLUDED.strength_defence_home,
            strength_defence_away = EXCLUDED.strength_defence_away,
            updated_at = now()
    """)

    team_rows = [{
        "fpl_id": int(t["id"]), "season": season,
        "name": t["name"], "short_name": t["short_name"],
        "soh": int(t["strength_overall_home"]), "soa": int(t["strength_overall_away"]),
        "sah": int(t["strength_attack_home"]), "saa": int(t["strength_attack_away"]),
        "sdh": int(t["strength_defence_home"]), "sda": int(t["strength_defence_away"]),
    } for t in teams.to_dict("records")]

    _executemany(engine, team_stmt, team_rows, "teams")
    team_map = _id_map(engine, "teams", season)

    players = source.players()
    _require_columns(
        players,
        ["id", "first_name", "second_name", "web_name", "element_type",
         "team", "now_cost", "cost_change_start", "status"],
        "players feed",
    )

    player_stmt = text("""
        INSERT INTO ml.players (
            fpl_id, season, fpl_name, web_name, position, position_encoded,
            team_id, cost_start, now_cost, status, updated_at
        ) VALUES (
            :fpl_id, :season, :fpl_name, :web_name, :position, :position_encoded,
            :team_id, :cost_start, :now_cost, :status, now()
        )
        ON CONFLICT (fpl_id, season) DO UPDATE SET
            fpl_name = EXCLUDED.fpl_name,
            web_name = EXCLUDED.web_name,
            position = EXCLUDED.position,
            position_encoded = EXCLUDED.position_encoded,
            team_id = EXCLUDED.team_id,
            cost_start = EXCLUDED.cost_start,
            now_cost = EXCLUDED.now_cost,
            status = EXCLUDED.status,
            updated_at = now()
    """)

    player_rows, unknown_pos, unmapped_team = [], 0, 0
    for p in players.to_dict("records"):
        pos = POSITION_MAP.get(int(p["element_type"]))
        if pos is None:
            unknown_pos += 1
            continue
        team_id = team_map.get(int(p["team"]))
        if team_id is None:
            unmapped_team += 1
        player_rows.append({
            "fpl_id": int(p["id"]), "season": season,
            "fpl_name": f"{p['first_name']} {p['second_name']}".strip(),
            "web_name": p["web_name"],
            "position": pos[0], "position_encoded": pos[1],
            "team_id": team_id,
            # true start-of-season price, not the price at scrape time
            "cost_start": int(p["now_cost"]) - int(p["cost_change_start"]),
            "now_cost": int(p["now_cost"]),
            "status": p["status"],
        })

    _executemany(engine, player_stmt, player_rows, "players")

    print(f"Bootstrap {season} [{source.label}]: "
          f"{len(team_rows)} teams, {len(player_rows)} players")
    if unknown_pos:
        print(f"  note: {unknown_pos} rows skipped (element_type not in 1-4, e.g. managers)")
    if unmapped_team:
        print(f"  WARNING: {unmapped_team} players had no matching team")


# ---------------------------------------------------------------------
# 2. fixtures
# ---------------------------------------------------------------------
def ingest_fixtures(engine, source, gameweek: int):
    season = source.season
    gw_fix = source.fixtures(gameweek)
    if gw_fix.empty:
        print(f"  WARNING: no fixtures listed for GW{gameweek}")
        return
    _require_columns(
        gw_fix,
        ["id", "event", "team_h", "team_a", "kickoff_time",
         "team_h_score", "team_a_score", "finished"],
        "fixtures feed",
    )

    unfinished = int((~gw_fix.finished.astype(bool)).sum())
    if unfinished:
        raise SystemExit(
            f"Refusing to ingest GW{gameweek}: {unfinished}/{len(gw_fix)} fixtures are "
            f"not finished. ml.player_gw_stats has an immutability trigger, so partial "
            f"data written now could not be corrected later."
        )

    team_map = _id_map(engine, "teams", season)
    if not team_map:
        raise SystemExit("ml.teams is empty for this season — run --bootstrap first.")

    stmt = text("""
        INSERT INTO ml.fixtures (
            fpl_id, season, gameweek, home_team_id, away_team_id,
            kickoff_time, home_score, away_score, finished, updated_at
        ) VALUES (
            :fpl_id, :season, :gameweek, :home_team_id, :away_team_id,
            :kickoff_time, :home_score, :away_score, :finished, now()
        )
        ON CONFLICT (fpl_id, season) DO UPDATE SET
            home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            finished = EXCLUDED.finished,
            updated_at = now()
    """)
    # home_team_id/away_team_id are intentionally NOT in the UPDATE SET —
    # the enforce_fixture_identity trigger blocks changes to them.

    rows = [{
        "fpl_id": int(f["id"]), "season": season, "gameweek": int(f["event"]),
        "home_team_id": team_map.get(int(f["team_h"])),
        "away_team_id": team_map.get(int(f["team_a"])),
        "kickoff_time": f["kickoff_time"],
        "home_score": num(f["team_h_score"], int),
        "away_score": num(f["team_a_score"], int),
        "finished": bool(f["finished"]),
    } for f in gw_fix.to_dict("records")]

    _executemany(engine, stmt, rows, "fixtures")
    print(f"Fixtures GW{gameweek}: {len(rows)} written (all finished)")


# ---------------------------------------------------------------------
# 2b. upcoming fixtures (incremental, no finished-gate)
# ---------------------------------------------------------------------
def ingest_upcoming_fixtures(engine, season: str) -> None:
    """
    Pulls the FULL current-season fixture list from FPL's live API
    (fixtures/, no ?event= filter) and UPSERTs into ml.fixtures --
    deliberately WITHOUT ingest_fixtures' all-finished gate, so this is
    the only path that ever writes a fixture row with a future
    kickoff_time and finished=FALSE. See the module's ingest_fixtures
    docstring/comments for why that gate exists: it protects
    ml.player_gw_stats (hardcoded is_live=FALSE + ON CONFLICT DO
    NOTHING there means a row written against an unfinished gameweek
    could never be corrected), not ml.fixtures itself. This function
    never touches ml.player_gw_stats, so that risk doesn't apply here.

    Live-API only (no ArchiveSource equivalent) -- the community
    archive covers completed seasons exclusively, so there is never a
    genuine "upcoming" fixture to pull from it; --season is passed
    straight to LiveSource for the same season-mismatch guard every
    other live-API call in this module uses.

    Safe to call at any cadence (e.g. Celery Beat -- see
    Worker/tasks.py's schedule_fixture_polls, which depends on this
    having already run): every column except identity (fpl_id/season/
    home_team_id/away_team_id) is upserted, and identity is never in
    the UPDATE SET below -- enforce_fixture_identity_fn blocks changes
    to those columns regardless, so this cannot corrupt an
    already-settled fixture's teams even if called with stale data.
    """
    live = LiveSource(season)
    live.check_season()

    team_map = _id_map(engine, "teams", season)
    if not team_map:
        raise SystemExit("ml.teams is empty for this season -- run --bootstrap first.")

    data = fetch_json("fixtures/")
    if not data:
        print(f"  WARNING: fixtures/ returned no data for {season}")
        return
    fx = pd.DataFrame(data)
    _require_columns(
        fx,
        ["id", "event", "team_h", "team_a", "kickoff_time",
         "team_h_score", "team_a_score", "finished"],
        "fixtures feed",
    )

    # Blank gameweeks (e.g. a postponed fixture not yet rescheduled into a
    # gameweek) carry event = null -- can't file those under any
    # gameweek, so they're skipped rather than written with a bogus value.
    unscheduled = int(fx.event.isna().sum())
    fx = fx[fx.event.notna()].copy()

    stmt = text("""
        INSERT INTO ml.fixtures (
            fpl_id, season, gameweek, home_team_id, away_team_id,
            kickoff_time, home_score, away_score, finished, updated_at
        ) VALUES (
            :fpl_id, :season, :gameweek, :home_team_id, :away_team_id,
            :kickoff_time, :home_score, :away_score, :finished, now()
        )
        ON CONFLICT (fpl_id, season) DO UPDATE SET
            kickoff_time = EXCLUDED.kickoff_time,
            home_score = EXCLUDED.home_score,
            away_score = EXCLUDED.away_score,
            finished = EXCLUDED.finished,
            updated_at = now()
    """)
    # home_team_id/away_team_id/fpl_id are intentionally NOT in the UPDATE
    # SET -- enforce_fixture_identity_fn blocks changes to them anyway
    # (same as ingest_fixtures above). kickoff_time IS updated here
    # (unlike ingest_fixtures), since a genuinely upcoming fixture's
    # kickoff can legitimately be rescheduled before it's played.

    rows = [{
        "fpl_id": int(f["id"]), "season": season, "gameweek": int(f["event"]),
        "home_team_id": team_map.get(int(f["team_h"])),
        "away_team_id": team_map.get(int(f["team_a"])),
        "kickoff_time": f["kickoff_time"],
        "home_score": num(f["team_h_score"], int),
        "away_score": num(f["team_a_score"], int),
        "finished": bool(f["finished"]),
    } for f in fx.to_dict("records")]

    _executemany(engine, stmt, rows, "fixtures (upcoming/incremental)")
    print(f"Upcoming fixtures {season}: {len(rows)} written/updated (no finished-gate)")
    if unscheduled:
        print(f"  note: {unscheduled} fixture(s) skipped -- no gameweek assigned yet (postponed/unscheduled)")


def refresh_current_season_fixtures(engine) -> dict:
    """ingest_upcoming_fixtures for whatever season the API is serving --
    the Beat-callable entry point.

    WHY THIS EXISTS. ingest_upcoming_fixtures has always documented itself
    as "safe to call at any cadence (e.g. Celery Beat)", but nothing ever
    called it on a cadence: it was reachable only from main(), i.e. only
    when a human ran the script. So ml.fixtures held whatever the last
    manual run happened to capture. In the dev database that was a single
    run mid-GW1, which left six played fixtures carrying real scores and
    finished=FALSE forever, and GW2 with no scores at all. The upsert was
    correct the whole time -- `finished = EXCLUDED.finished` is in its
    UPDATE SET -- it simply never ran again.

    Two things separate this from calling ingest_upcoming_fixtures
    directly, both of which matter only outside the CLI:

      * The season is DERIVED, not passed. A Beat task cannot hardcode
        one without needing an edit every August, and passing the wrong
        one trips check_season's SystemExit.
      * SystemExit is converted to RuntimeError. fpl_ingest is a script
        and raises SystemExit for operator errors, but SystemExit derives
        from BaseException, so a Celery task's `except Exception` would
        not catch it and the worker would take the exit instead of
        logging a failed task.

    Returns a small summary dict rather than printing, since the caller
    is a task whose return value is logged.
    """
    season = current_live_season()
    if season is None:
        # Between seasons: the API is up but carries no events yet.
        # Nothing to refresh, and not an error worth retrying.
        return {"season": None, "refreshed": False, "reason": "API is serving no events yet"}

    try:
        ingest_upcoming_fixtures(engine, season)
    except SystemExit as e:
        raise RuntimeError(f"fixture refresh aborted for {season}: {e}") from e

    return {"season": season, "refreshed": True}


# ---------------------------------------------------------------------
# 3. player_gw_stats
# ---------------------------------------------------------------------
def ingest_gameweek_stats(engine, source, gameweek: int):
    season = source.season
    df = source.gw_stats(gameweek)
    _require_columns(df, GW_STATS_COLUMNS, f"gameweek {gameweek} stats feed")

    # The archive contains a handful of byte-identical duplicate rows.
    # Drop those, but keep genuine double-gameweeks (same element, *different*
    # fixture) so we can detect and aggregate them explicitly.
    before = len(df)
    df = df.drop_duplicates(subset=["element", "fixture"], keep="first")
    exact_dupes = before - len(df)

    dgw_elements = df.element[df.element.duplicated()].unique()
    if len(dgw_elements):
        print(f"  DOUBLE GAMEWEEK: {len(dgw_elements)} players have 2+ fixtures in GW{gameweek}; keeping fixture rows.")

    player_map = _id_map(engine, "players", season)
    fixture_map = _id_map(engine, "fixtures", season)
    if not player_map:
        raise SystemExit("ml.players is empty for this season — run --bootstrap first.")

    stmt = text("""
        INSERT INTO ml.player_gw_stats (
            player_id, fixture_id, season, gameweek, is_live,
            minutes, goals_scored, assists, clean_sheets, goals_conceded,
            saves, bonus, bps, yellow_cards, red_cards, own_goals,
            penalties_saved, penalties_missed, defensive_contributions, ict_index, influence,
            creativity, threat, expected_goals, expected_assists,
            expected_goal_involvements, value, selected, transfers_in,
            transfers_out, transfers_balance, was_home,
            team_h_score, team_a_score, total_points
        ) VALUES (
            :player_id, :fixture_id, :season, :gameweek, FALSE,
            :minutes, :goals_scored, :assists, :clean_sheets, :goals_conceded,
            :saves, :bonus, :bps, :yellow_cards, :red_cards, :own_goals,
            :penalties_saved, :penalties_missed, :defensive_contributions, :ict_index, :influence,
            :creativity, :threat, :expected_goals, :expected_assists,
            :expected_goal_involvements, :value, :selected, :transfers_in,
            :transfers_out, :transfers_balance, :was_home,
            :team_h_score, :team_a_score, :total_points
        )
        ON CONFLICT DO NOTHING
    """)
    # is_live is hardcoded FALSE and unfinished gameweeks are refused above, so
    # every row written here is final. DO NOTHING because the
    # enforce_gw_stats_immutability trigger blocks UPDATEs on finished rows —
    # re-running is therefore a safe no-op. Mid-gameweek (is_live=TRUE) ingest
    # would need its own ON CONFLICT DO UPDATE path gated on is_live = TRUE.

    rows, unmapped_player, unmapped_fixture = [], 0, 0
    for r in df.to_dict("records"):
        pid = player_map.get(int(r["element"]))
        if pid is None:
            unmapped_player += 1
            continue
        fid = fixture_map.get(int(r["fixture"])) if not pd.isna(r["fixture"]) else None
        if fid is None:
            unmapped_fixture += 1
        rows.append({
            "player_id": pid, "fixture_id": fid, "season": season, "gameweek": gameweek,
            "minutes": num(r["minutes"], int, 0), "goals_scored": num(r["goals_scored"], int, 0),
            "assists": num(r["assists"], int, 0), "clean_sheets": num(r["clean_sheets"], int, 0),
            "goals_conceded": num(r["goals_conceded"], int, 0), "saves": num(r["saves"], int, 0),
            "bonus": num(r["bonus"], int, 0), "bps": num(r["bps"], int, 0),
            "yellow_cards": num(r["yellow_cards"], int, 0), "red_cards": num(r["red_cards"], int, 0),
            "own_goals": num(r["own_goals"], int, 0),
            "penalties_saved": num(r["penalties_saved"], int, 0),
            "penalties_missed": num(r["penalties_missed"], int, 0),
            "defensive_contributions": num(r.get("defensive_contributions", 0), int, 0),
            "ict_index": num(r["ict_index"]), "influence": num(r["influence"]),
            "creativity": num(r["creativity"]), "threat": num(r["threat"]),
            "expected_goals": num(r["expected_goals"]),
            "expected_assists": num(r["expected_assists"]),
            "expected_goal_involvements": num(r["expected_goal_involvements"]),
            "value": num(r["value"], int), "selected": num(r["selected"], int),
            "transfers_in": num(r["transfers_in"], int, 0),
            "transfers_out": num(r["transfers_out"], int, 0),
            "transfers_balance": num(r["transfers_balance"], int, 0),
            "was_home": flag(r["was_home"]),
            "team_h_score": num(r["team_h_score"], int),
            "team_a_score": num(r["team_a_score"], int),
            "total_points": num(r["total_points"], int, 0),
        })

    before_n = _count(engine, "player_gw_stats", season, gameweek)
    _executemany(engine, stmt, rows, "player_gw_stats")
    after_n = _count(engine, "player_gw_stats", season, gameweek)

    print(f"GW{gameweek} stats: {len(rows)} rows prepared, "
          f"{after_n - before_n} inserted, {len(rows) - (after_n - before_n)} already present")
    if exact_dupes:
        print(f"  note: dropped {exact_dupes} byte-identical duplicate row(s) from archive")
    if unmapped_player:
        print(f"  WARNING: {unmapped_player} rows had no matching player — run --bootstrap")
    if unmapped_fixture:
        print(f"  WARNING: {unmapped_fixture} rows had no matching fixture_id (left NULL)")


# ---------------------------------------------------------------------
# 4. verification
# ---------------------------------------------------------------------
def verify(engine, season: str):
    print(f"\n=== VERIFY {season} ===")
    with engine.connect() as conn:
        for t in ("teams", "players", "fixtures", "player_gw_stats"):
            n = conn.execute(
                text(f"SELECT count(*) FROM ml.{t} WHERE season = :s"), {"s": season}
            ).scalar()
            print(f"  ml.{t:18} {n}")

        print("\n  per-gameweek:")
        for row in conn.execute(text("""
            SELECT s.gameweek, count(*) AS players, sum(s.total_points) AS pts,
                   max(s.total_points) AS best, count(*) FILTER (WHERE s.minutes > 0) AS played,
                   count(*) FILTER (WHERE s.fixture_id IS NULL) AS no_fixture
            FROM ml.player_gw_stats s WHERE s.season = :s
            GROUP BY s.gameweek ORDER BY s.gameweek
        """), {"s": season}):
            print(f"    GW{row[0]:<3} players={row[1]:<5} total_pts={row[2]:<6} "
                  f"max={row[3]:<4} played={row[4]:<5} null_fixture={row[5]}")

        print("\n  top scorer per gameweek:")
        for row in conn.execute(text("""
            SELECT DISTINCT ON (s.gameweek) s.gameweek, p.web_name, t.short_name,
                   s.minutes, s.goals_scored, s.assists, s.bonus, s.total_points
            FROM ml.player_gw_stats s
            JOIN ml.players p ON p.id = s.player_id
            LEFT JOIN ml.teams t ON t.id = p.team_id
            WHERE s.season = :s
            ORDER BY s.gameweek, s.total_points DESC
        """), {"s": season}):
            print(f"    GW{row[0]:<3} {row[1]:<18} {str(row[2]):<5} "
                  f"min={row[3]:<4} g={row[4]} a={row[5]} bonus={row[6]} pts={row[7]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--season", required=True, help="e.g. 2025-26")
    ap.add_argument("--source", choices=("archive", "live"), default="archive",
                    help="archive = community CSVs (completed seasons); "
                         "live = FPL API (current season). Default: archive")
    ap.add_argument("--bootstrap", action="store_true", help="Load teams + players")
    ap.add_argument(
        "--upcoming-fixtures",
        action="store_true",
        help="Load the full current-season fixture calendar from the live API, including unfinished fixtures",
    )
    ap.add_argument("--gw", type=int, help="Single gameweek to load")
    ap.add_argument("--gw-range", type=int, nargs=2, metavar=("FROM", "TO"),
                    help="Inclusive gameweek range, e.g. --gw-range 1 4")
    ap.add_argument("--verify", action="store_true", help="Print counts and spot checks")
    args = ap.parse_args()

    if not any([args.bootstrap, args.upcoming_fixtures, args.gw, args.gw_range, args.verify]):
        ap.error("nothing to do -- pass --bootstrap, --upcoming-fixtures, --gw, --gw-range or --verify")

    engine = get_engine()
    source = make_source(args.source, args.season)
    source.check_season()
    print(f"DB: {safe_url()}")
    print(f"Source: {source.label}")

    if args.bootstrap:
        ingest_bootstrap(engine, source)

    if args.upcoming_fixtures:
        if args.source != "live":
            raise SystemExit("--upcoming-fixtures requires --source live")
        ingest_upcoming_fixtures(engine, args.season)

    gws = []
    if args.gw:
        gws.append(args.gw)
    if args.gw_range:
        gws.extend(range(args.gw_range[0], args.gw_range[1] + 1))

    for gw in gws:
        print(f"\n--- GW{gw} ---")
        ingest_fixtures(engine, source, gw)
        ingest_gameweek_stats(engine, source, gw)

    if args.verify:
        verify(engine, args.season)


if __name__ == "__main__":
    sys.exit(main())
