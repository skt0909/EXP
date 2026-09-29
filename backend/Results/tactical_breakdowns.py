"""
tactical_breakdowns.py — the per-player scoring snapshot for one manager's
scored gameweek. Pure: no database, no Celery, no FastAPI.

One place builds it, so the stored snapshot and the Dashboard's live view
cannot disagree in shape or value:

  * Results/scoring_job.py stores it in gw_scores.player_breakdowns, in the
    same UPSERT as that manager's totals, built from the SAME
    SelectionScore those totals came from -- one scoring result, then the
    totals, then the snapshot.
  * Results/team_dashboard.py uses it for a live (unsettled) gameweek, and
    reads the stored copy back for a settled one.

It adds no scoring. Each player's points, role, minutes and flags are the
engine's own PlayerLine values; the category rows come from
tactical_scoring's general_points_breakdown / tactical_points_breakdown,
which mirror the engine rule for rule over the same stat rows
(test_every_breakdown_sums_to_its_total pins that).

Lives here rather than in scoring_job.py so the Dashboard API does not
depend on batch-job orchestration to get it. Imports only
Results.tactical_scoring, which imports only Shared.rules -- no cycle.
"""

from Results.tactical_scoring import general_points_breakdown, tactical_points_breakdown


def merge_breakdown(parts):
    """Sum a player's per-fixture breakdown rows into one list per rule.

    A double gameweek produces two "goals" rows; a manager wants one line
    saying 2 goals, not two lines saying one each. Order is preserved so the
    rules read in the order they are applied."""
    merged, order = {}, []
    for part in parts:
        rule = part["rule"]
        if rule not in merged:
            merged[rule] = 0
            order.append(rule)
        merged[rule] += part["points"]
    return [{"rule": r, "points": merged[r]} for r in order if merged[r]]


def player_snapshot(player, fixtures, position, tactic) -> dict:
    """One engine PlayerLine plus its category breakdown, as plain JSON.

    player   : tactical_scoring.PlayerLine from the SelectionScore being used
    fixtures : that player's stat rows -- the same rows the engine scored
    position : his position for this season (required -- the engine fails
               loudly on an unknown position, and so does this)
    tactic   : the selection's tactic
    """
    gen_parts, tac_parts = [], []
    for fixture in fixtures:
        gen_parts.extend(general_points_breakdown(fixture, position))
        # Only a Bonus Player can have Tactical Points; which categories apply
        # is tactical_points_breakdown's call, not the aggregate total's.
        if player.is_bonus:
            tac_parts.extend(tactical_points_breakdown(fixture, position, tactic))
    return {
        "player_id": player.player_id,
        "position_slot": player.position_slot,
        "role": player.role,
        "is_bonus": bool(player.is_bonus),
        "counted": bool(player.counted),
        "minutes": int(player.minutes),
        "general_points": int(player.general_points),
        "tactical_points": int(player.tactical_points),
        "general_breakdown": merge_breakdown(gen_parts),
        "tactical_breakdown": merge_breakdown(tac_parts),
    }


def selection_snapshot(score, stats_by_player, positions, tactic) -> dict:
    """The whole selection's snapshot, from one SelectionScore.

    `score` must be the result whose totals are being persisted or shown;
    this never scores anything itself."""
    return {
        "players": [
            player_snapshot(
                p,
                stats_by_player.get(p.player_id, []),
                positions[p.player_id],
                tactic,
            )
            for p in score.players
        ]
    }
