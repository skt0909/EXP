"""
standings.py — pure mini-league standings computation:
gw_scores / league_h2h_fixtures -> league_members + leaderboard_snapshots.

compute_league_standings(engine, season, gameweek) processes every
mini_leagues row for `season`, each isolated in its own try/except (one
broken league can't abort the whole batch) -- same philosophy as
Results/scoring.py's per-user isolation.

SEMANTIC DISTINCTION (the main way this could go subtly wrong, so
stated explicitly here too, not just in commit history):
- last_gw_points (league_members) / points_this_gw (leaderboard_
  snapshots) ALWAYS mean the user's real FPL points that gameweek
  (gw_scores.total_points) -- identical meaning under both scoring
  types, independent of league rules.
- season_points (league_members) / total_points (leaderboard_
  snapshots) mean DIFFERENT things per scoring_type:
    classic: gw_scores.season_total (real cumulative FPL points)
    head_to_head: cumulative MATCH points from this league's results
      (3/win, 1/draw, 0/loss, 3/bye) -- NOT an FPL point total at all.
  A user who wins a H2H match 40-38 has last_gw_points=40 (their real
  score) but their match-point contribution to season_points is +3,
  not +40. These two numbers are computed from entirely separate
  sources in this file (gw_scores directly vs. summed
  league_h2h_fixtures results) specifically so they can't get crossed.

leaderboard_snapshots is APPEND-ONLY (enforce_snapshot_immutability_fn
blocks UPDATE/DELETE, same shape as transfers.py's
enforce_transfers_immutability_fn) -- so re-running for an
already-snapshotted (league, user, season, gameweek) is handled by
skipping the insert (INSERT ... WHERE NOT EXISTS), not an upsert; the
schema gives no other option. league_members has no such trigger and is
refreshed with a plain UPDATE every run, so the "live" standings always
reflect the latest computation even after that gameweek's permanent
snapshot has already been frozen by an earlier run.

H2H schedule: generated ONCE per (league_id, season), gated on "no
league_h2h_fixtures rows exist yet for this league+season" -- via a
standard circle-method round-robin over the league's CURRENT members,
shuffled into a random starting order, locking in membership at
generation time (decided earlier). ASSUMPTION, flagged as requested:
nothing in this repo derives a season length, so
SEASON_LENGTH_GAMEWEEKS = 38 (a real Premier League season) is an
assumed constant -- it lives in Shared/rules.py with the other pure
rules, and is used here purely to know how many gameweek-slots to fill
when generating the schedule upfront. If the round-robin cycle (n-1 rounds
for n members, odd counts padded with a bye) is shorter than the
number of remaining gameweeks, the SAME shuffled cycle repeats
(wraps) rather than being reshuffled on each wrap -- keeps the
schedule stable and predictable across a season rather than
unpredictably different each time it repeats.
"""

import logging
import random

from sqlalchemy import text

from Shared.rules import (
    BYE_POINTS,
    DRAW_POINTS,
    LOSS_POINTS,
    SEASON_LENGTH_GAMEWEEKS,
    WIN_POINTS,
)

logger = logging.getLogger(__name__)


LEAGUES_QUERY = text("SELECT id AS league_id, scoring_type FROM mini_leagues WHERE season = :season")

LEAGUE_MEMBERS_QUERY = text(
    "SELECT user_id, season_points, rank, last_gw_points FROM league_members WHERE league_id = :league_id"
)

GW_SCORES_FOR_MEMBERS_QUERY = text(
    "SELECT user_id, total_points, season_total FROM gw_scores "
    "WHERE season = :season AND gameweek = :gameweek AND user_id = ANY(:user_ids)"
)

UPDATE_LEAGUE_MEMBER_STATS_STMT = text(
    "UPDATE league_members SET last_gw_points = :last_gw_points, season_points = :season_points "
    "WHERE league_id = :league_id AND user_id = :user_id"
)

UPDATE_LEAGUE_MEMBER_RANK_STMT = text(
    "UPDATE league_members SET rank = :rank WHERE league_id = :league_id AND user_id = :user_id"
)

PREVIOUS_SNAPSHOT_RANK_QUERY = text(
    """
    SELECT DISTINCT ON (user_id) user_id, rank
    FROM leaderboard_snapshots
    WHERE league_id = :league_id AND season = :season AND gameweek < :gameweek AND user_id = ANY(:user_ids)
    ORDER BY user_id, gameweek DESC
    """
)

# WHERE NOT EXISTS instead of ON CONFLICT -- leaderboard_snapshots has no
# UPDATE path available at all (immutability trigger), so "idempotent
# re-run" can only mean "insert once, skip thereafter."
INSERT_SNAPSHOT_IF_NOT_EXISTS_STMT = text(
    """
    INSERT INTO leaderboard_snapshots (league_id, user_id, season, gameweek, points_this_gw, total_points, rank, rank_movement)
    SELECT :league_id, :user_id, :season, :gameweek, :points_this_gw, :total_points, :rank, :rank_movement
    WHERE NOT EXISTS (
        SELECT 1 FROM leaderboard_snapshots
        WHERE league_id = :league_id AND user_id = :user_id AND season = :season AND gameweek = :gameweek
    )
    """
)

H2H_FIXTURES_EXIST_QUERY = text(
    "SELECT EXISTS (SELECT 1 FROM league_h2h_fixtures WHERE league_id = :league_id AND season = :season)"
)

INSERT_H2H_FIXTURE_STMT = text(
    """
    INSERT INTO league_h2h_fixtures (league_id, season, gameweek, user_id_1, user_id_2)
    VALUES (:league_id, :season, :gameweek, :user_id_1, :user_id_2)
    """
)

THIS_GW_FIXTURES_QUERY = text(
    "SELECT id, user_id_1, user_id_2 FROM league_h2h_fixtures "
    "WHERE league_id = :league_id AND season = :season AND gameweek = :gameweek"
)

RESOLVE_FIXTURE_STMT = text(
    "UPDATE league_h2h_fixtures SET points_1 = :points_1, points_2 = :points_2, result = :result WHERE id = :id"
)

# Every resolved fixture this season contributes match points from BOTH
# perspectives (user_id_1's result and user_id_2's, when not a bye) --
# unioned and summed per user. Never touches gw_scores; this is
# season_points for head_to_head, deliberately kept separate from any
# real-FPL-points source.
SEASON_MATCH_POINTS_QUERY = text(
    """
    SELECT user_id, SUM(match_points) AS match_points FROM (
        SELECT user_id_1 AS user_id,
               CASE result WHEN 'win_1' THEN :win WHEN 'draw' THEN :draw WHEN 'bye' THEN :bye ELSE :loss END AS match_points
        FROM league_h2h_fixtures
        WHERE league_id = :league_id AND season = :season AND result IS NOT NULL
        UNION ALL
        SELECT user_id_2 AS user_id,
               CASE result WHEN 'win_2' THEN :win WHEN 'draw' THEN :draw ELSE :loss END AS match_points
        FROM league_h2h_fixtures
        WHERE league_id = :league_id AND season = :season AND result IS NOT NULL AND user_id_2 IS NOT NULL
    ) sub
    GROUP BY user_id
    """
)


def _rank_members(scored: list[tuple[int, int]]) -> dict[int, int]:
    """scored: [(user_id, points), ...]. Standard competition ranking:
    ties share a rank, next distinct rank skips accordingly (two tied
    for 1st -> next person is rank 3, not rank 2)."""
    ranked = sorted(scored, key=lambda pair: -pair[1])
    ranks: dict[int, int] = {}
    prev_points = None
    prev_rank = 0
    for i, (user_id, points) in enumerate(ranked, start=1):
        rank = i if points != prev_points else prev_rank
        ranks[user_id] = rank
        prev_points = points
        prev_rank = rank
    return ranks


def _round_robin_rounds(participants: list[int]) -> list[list[tuple]]:
    """Standard circle-method round-robin. Returns n-1 rounds (n =
    len(participants), padded to even with a None bye slot), each a
    list of n//2 (a, b) pairs."""
    players = list(participants)
    if len(players) % 2 == 1:
        players.append(None)
    n = len(players)
    fixed, rotating = players[0], players[1:]
    rounds = []
    for _ in range(n - 1):
        round_players = [fixed] + rotating
        pairs = [(round_players[i], round_players[n - 1 - i]) for i in range(n // 2)]
        rounds.append(pairs)
        rotating = [rotating[-1]] + rotating[:-1]
    return rounds


def _generate_and_insert_h2h_schedule(conn, league_id: int, season: str, member_user_ids: list[int], starting_gameweek: int) -> None:
    shuffled = list(member_user_ids)
    random.shuffle(shuffled)
    rounds = _round_robin_rounds(shuffled)

    remaining_gameweeks = max(0, SEASON_LENGTH_GAMEWEEKS - starting_gameweek + 1)
    for i in range(remaining_gameweeks):
        gw = starting_gameweek + i
        round_pairs = rounds[i % len(rounds)]  # wrap the same shuffled cycle once exhausted
        for a, b in round_pairs:
            if a is None:
                a, b = b, a  # bye must land on user_id_2 -- user_id_1 is NOT NULL
            conn.execute(
                INSERT_H2H_FIXTURE_STMT,
                {"league_id": league_id, "season": season, "gameweek": gw, "user_id_1": a, "user_id_2": b},
            )


def _insert_snapshots_and_update_ranks(conn, league_id: int, season: str, gameweek: int, entries: list[dict]) -> None:
    """entries: [{"user_id", "points_this_gw", "total_points"}, ...] for
    whichever members are getting a snapshot this run. Ranks by
    total_points, updates league_members.rank for these members, and
    inserts (or skips, if already present) the permanent snapshot row."""
    if not entries:
        return

    ranks = _rank_members([(e["user_id"], e["total_points"]) for e in entries])
    user_ids = [e["user_id"] for e in entries]
    prev_ranks = {
        r.user_id: r.rank
        for r in conn.execute(
            PREVIOUS_SNAPSHOT_RANK_QUERY, {"league_id": league_id, "season": season, "gameweek": gameweek, "user_ids": user_ids}
        )
    }

    for e in entries:
        uid = e["user_id"]
        rank = ranks[uid]
        conn.execute(UPDATE_LEAGUE_MEMBER_RANK_STMT, {"league_id": league_id, "user_id": uid, "rank": rank})
        rank_movement = prev_ranks[uid] - rank if uid in prev_ranks else 0
        conn.execute(
            INSERT_SNAPSHOT_IF_NOT_EXISTS_STMT,
            {
                "league_id": league_id,
                "user_id": uid,
                "season": season,
                "gameweek": gameweek,
                "points_this_gw": e["points_this_gw"],
                "total_points": e["total_points"],
                "rank": rank,
                "rank_movement": rank_movement,
            },
        )


def _process_classic_league(conn, league_id: int, season: str, gameweek: int) -> None:
    members = conn.execute(LEAGUE_MEMBERS_QUERY, {"league_id": league_id}).all()
    if not members:
        return
    member_user_ids = [m.user_id for m in members]

    gw_score_rows = {
        r.user_id: r
        for r in conn.execute(
            GW_SCORES_FOR_MEMBERS_QUERY, {"season": season, "gameweek": gameweek, "user_ids": member_user_ids}
        )
    }

    # Members with no gw_scores row this gameweek are left untouched --
    # not an error, not zeroed (partial/live scoring: they may just not
    # have been scored yet).
    for m in members:
        if m.user_id in gw_score_rows:
            row = gw_score_rows[m.user_id]
            conn.execute(
                UPDATE_LEAGUE_MEMBER_STATS_STMT,
                {
                    "league_id": league_id,
                    "user_id": m.user_id,
                    "last_gw_points": row.total_points,
                    "season_points": row.season_total,
                },
            )

    entries = [
        {"user_id": uid, "points_this_gw": row.total_points, "total_points": row.season_total}
        for uid, row in gw_score_rows.items()
    ]
    _insert_snapshots_and_update_ranks(conn, league_id, season, gameweek, entries)


def _process_h2h_league(conn, league_id: int, season: str, gameweek: int) -> None:
    members = conn.execute(LEAGUE_MEMBERS_QUERY, {"league_id": league_id}).all()
    if not members:
        return
    member_user_ids = [m.user_id for m in members]

    fixtures_exist = conn.execute(H2H_FIXTURES_EXIST_QUERY, {"league_id": league_id, "season": season}).scalar()
    if not fixtures_exist:
        _generate_and_insert_h2h_schedule(conn, league_id, season, member_user_ids, starting_gameweek=gameweek)

    this_gw_fixtures = conn.execute(
        THIS_GW_FIXTURES_QUERY, {"league_id": league_id, "season": season, "gameweek": gameweek}
    ).all()
    if not this_gw_fixtures:
        return  # nothing scheduled this gameweek (e.g. schedule already exhausted)

    gw_score_rows = {
        r.user_id: r
        for r in conn.execute(
            GW_SCORES_FOR_MEMBERS_QUERY, {"season": season, "gameweek": gameweek, "user_ids": member_user_ids}
        )
    }

    for fx in this_gw_fixtures:
        # A member with no gw_scores row yet this gameweek is treated as
        # 0 for match-result purposes (partial/live scoring, same
        # COALESCE-to-0 philosophy scoring.py uses elsewhere) -- not
        # explicitly specified for H2H, flagged here as the assumption.
        points_1 = gw_score_rows[fx.user_id_1].total_points if fx.user_id_1 in gw_score_rows else 0
        if fx.user_id_2 is None:
            points_2, result = None, "bye"
        else:
            points_2 = gw_score_rows[fx.user_id_2].total_points if fx.user_id_2 in gw_score_rows else 0
            if points_1 > points_2:
                result = "win_1"
            elif points_2 > points_1:
                result = "win_2"
            else:
                result = "draw"
        conn.execute(RESOLVE_FIXTURE_STMT, {"id": fx.id, "points_1": points_1, "points_2": points_2, "result": result})

    match_points = {
        r.user_id: r.match_points
        for r in conn.execute(
            SEASON_MATCH_POINTS_QUERY,
            {
                "league_id": league_id,
                "season": season,
                "win": WIN_POINTS,
                "draw": DRAW_POINTS,
                "loss": LOSS_POINTS,
                "bye": BYE_POINTS,
            },
        )
    }

    for m in members:
        season_points = match_points.get(m.user_id, 0)
        last_gw_points = gw_score_rows[m.user_id].total_points if m.user_id in gw_score_rows else 0
        conn.execute(
            UPDATE_LEAGUE_MEMBER_STATS_STMT,
            {"league_id": league_id, "user_id": m.user_id, "last_gw_points": last_gw_points, "season_points": season_points},
        )

    entries = [
        {
            "user_id": m.user_id,
            "points_this_gw": gw_score_rows[m.user_id].total_points if m.user_id in gw_score_rows else 0,
            "total_points": match_points.get(m.user_id, 0),
        }
        for m in members
    ]
    _insert_snapshots_and_update_ranks(conn, league_id, season, gameweek, entries)


def compute_league_standings(engine, season: str, gameweek: int) -> dict:
    """Process every mini_leagues row for `season`. Each league is
    isolated in its own transaction and try/except -- one broken league
    can't abort the batch.

    Returns {"processed": [league_id, ...], "failed": [(league_id, error_message), ...]}.
    """
    with engine.connect() as conn:
        leagues = conn.execute(LEAGUES_QUERY, {"season": season}).all()

    processed = []
    failed = []
    for lg in leagues:
        try:
            with engine.begin() as conn:
                if lg.scoring_type == "classic":
                    _process_classic_league(conn, lg.league_id, season, gameweek)
                elif lg.scoring_type == "head_to_head":
                    _process_h2h_league(conn, lg.league_id, season, gameweek)
                else:
                    raise ValueError(f"unknown scoring_type {lg.scoring_type!r} for league_id={lg.league_id}")
            processed.append(lg.league_id)
        except Exception as e:
            logger.error(
                "compute_league_standings: failed to process league_id=%s (season=%s, gameweek=%s): %s: %s",
                lg.league_id, season, gameweek, type(e).__name__, e,
            )
            failed.append((lg.league_id, str(e)))

    return {"processed": processed, "failed": failed}
