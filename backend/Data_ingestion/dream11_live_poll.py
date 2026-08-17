"""
dream11_live_poll.py — checkpoint polling for Dream11 contests.

Reuses fpl_ingest.py's fetch_json/COUNTING_COLS -- not reinventing the
HTTP request logic. Calls FPL's live per-gameweek stats endpoint
(event/{gameweek}/live/), filtered down to just the players belonging
to a given fixture's two teams, and upserts ml.player_gw_stats for
them at a 'halftime' or 'fulltime' checkpoint.

'halftime' writes is_live=TRUE -- enforce_gw_stats_immutability_fn
only blocks UPDATEs where OLD.is_live=FALSE, so these rows stay freely
correctable. 'fulltime' writes is_live=FALSE (settled) -- from that
point on, that same trigger blocks ANY further UPDATE to these rows,
by design (mirrors fpl_ingest.py's own "no corrections after the
gameweek is finished" stance, just enforced at the row level here
instead of at ingest-time via a refuse-if-unfinished gate).

Re-polling an already-fulltime-settled row hits that trigger. That's
caught per-player here and treated as an expected no-op (already
settled), not a failure -- a retried/duplicate fulltime poll (e.g. a
Celery retry after a transient error on a LATER player in the same
batch) shouldn't error out on players it already successfully settled.
"""

import logging

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from fpl_ingest import fetch_json, COUNTING_COLS

logger = logging.getLogger(__name__)

VALID_CHECKPOINTS = {"halftime", "fulltime"}

# enforce_gw_stats_immutability_fn's exact RAISE EXCEPTION text -- see
# module docstring. Matching on this substring is inherently fragile if
# that message is ever reworded, same caveat as elsewhere in this project.
SETTLED_ERROR_SUBSTRING = "Cannot modify settled GW stats row"

FIXTURE_QUERY = text(
    "SELECT id, season, gameweek, home_team_id, away_team_id FROM ml.fixtures WHERE id = :fixture_id"
)

FIXTURE_TEAM_PLAYERS_QUERY = text(
    "SELECT id AS internal_id, fpl_id FROM ml.players "
    "WHERE season = :season AND (team_id = :home_team_id OR team_id = :away_team_id)"
)

UPSERT_GW_STAT_STMT = text(
    f"""
    INSERT INTO ml.player_gw_stats (
        player_id, season, gameweek, fixture_id, is_live, value, {", ".join(COUNTING_COLS)}
    ) VALUES (
        :player_id, :season, :gameweek, :fixture_id, :is_live, :value, {", ".join(f":{c}" for c in COUNTING_COLS)}
    )
    ON CONFLICT (player_id, season, gameweek) DO UPDATE SET
        fixture_id = EXCLUDED.fixture_id,
        is_live = EXCLUDED.is_live,
        value = EXCLUDED.value,
        {", ".join(f"{c} = EXCLUDED.{c}" for c in COUNTING_COLS)}
    """
)


def poll_fixture_checkpoint(engine, fixture_id: int, checkpoint: str) -> dict:
    """Returns {"updated": [fpl_id, ...], "already_settled": [fpl_id, ...], "unresolved": [fpl_id, ...]}."""
    if checkpoint not in VALID_CHECKPOINTS:
        raise ValueError(f"checkpoint must be one of {sorted(VALID_CHECKPOINTS)}, got {checkpoint!r}")

    with engine.connect() as conn:
        fixture_row = conn.execute(FIXTURE_QUERY, {"fixture_id": fixture_id}).first()
    if fixture_row is None:
        raise ValueError(f"fixture_id {fixture_id} does not exist")

    with engine.connect() as conn:
        team_players = conn.execute(
            FIXTURE_TEAM_PLAYERS_QUERY,
            {"season": fixture_row.season, "home_team_id": fixture_row.home_team_id, "away_team_id": fixture_row.away_team_id},
        ).all()
    fpl_to_internal = {p.fpl_id: p.internal_id for p in team_players}

    live = fetch_json(f"event/{fixture_row.gameweek}/live/")
    elements = live.get("elements", [])

    is_live = checkpoint == "halftime"

    updated = []
    already_settled = []
    unresolved = []
    for el in elements:
        fpl_id = int(el["id"])
        internal_id = fpl_to_internal.get(fpl_id)
        if internal_id is None:
            continue  # not part of this fixture's two teams -- expected, not an error

        s = el.get("stats", {})
        row = {
            "player_id": internal_id,
            "season": fixture_row.season,
            "gameweek": fixture_row.gameweek,
            "fixture_id": fixture_id,
            "is_live": is_live,
            # "value" isn't on the live endpoint (see fpl_ingest.py's own
            # docstring note) -- not this poller's concern, defaulted to 0.
            "value": s.get("value") or 0,
            **{c: (s.get(c) or 0) for c in COUNTING_COLS},
        }
        try:
            with engine.begin() as conn:
                conn.execute(UPSERT_GW_STAT_STMT, row)
            updated.append(fpl_id)
        except SQLAlchemyError as e:
            if SETTLED_ERROR_SUBSTRING in str(e):
                logger.info("poll_fixture_checkpoint: fpl_id=%s already settled (fulltime), skipping", fpl_id)
                already_settled.append(fpl_id)
            else:
                logger.error("poll_fixture_checkpoint: failed to upsert fpl_id=%s: %s: %s", fpl_id, type(e).__name__, e)
                unresolved.append(fpl_id)

    return {"updated": updated, "already_settled": already_settled, "unresolved": unresolved}
