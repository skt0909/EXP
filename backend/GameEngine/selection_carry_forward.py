"""
selection_carry_forward.py — auto-clones a manager's last submitted
Starting XI into the next open gameweek, so the Dashboard's pitch/bench
view stays the same across gameweeks by default.

WHY THIS EXISTS. Before this module, every gameweek needed its own
POST /gw_selection or GET /team reported has_lineup=false and the
Dashboard fell back to the empty "Set your Starting XI" placeholder --
even when nothing about the manager's team had changed. Real FPL rolls
your team forward unchanged unless you touch it; this gives the tactical
game the same behaviour.

THE RULE THIS ENFORCES, stated once so it does not drift from the code:
carry forward ONLY when the manager's active squad is byte-for-byte the
same 15 players as the prior gameweek's submitted XI + bench. If a
transfer happened since the last selection, this task deliberately does
NOTHING for that manager -- the squad now contains a player who was
never placed in a slot (or is missing one who was), and there is no safe
way to guess where a new signing belongs (starting XI vs bench, which
bench role). That is precisely the case where the manager is expected to
revisit Starting XI themselves, matching how a transfer already requires
a fresh POST /gw_selection today.

Tactical Swaps are NEVER carried forward, even on a clean carry-forward.
A swap plan is a bet on THIS gameweek's fixture kickoff times
(Gameplay/selection_rules.py's last_fixture_end rule); last gameweek's
pairs are meaningless against next gameweek's fixtures, so the new
gw_selections row is written with none, exactly like a manager who has
never planned one.

WHY A RAW INSERT NEEDS ITS OWN DEADLINE CHECK. gw_selections' trigger
(enforce_selection_lock_fn) only fires on an UPDATE of an already-locked
row -- it cannot stop a first-time INSERT into a gameweek whose deadline
has already passed, because there is no prior row for it to compare
against. Gameplay/starting_xi.py pre-checks deadlines.deadline_has_passed
for exactly this reason before its own first-time submissions; this
module does the same, on the SAME (season, gameweek) it is about to
insert into. Skipping this check would let a slow-running task silently
create a "submitted" lineup for a gameweek that already kicked off.

RACE WITH A MANAGER SUBMITTING BY HAND. CARRY_FORWARD_GW_SELECTION_STMT
uses ON CONFLICT ... DO NOTHING rather than DO UPDATE, unlike
starting_xi.py's own upsert. This task must never overwrite a selection
the manager already saved for this gameweek -- DO NOTHING plus checking
the returned id (None means the row already existed) is what keeps a
late-arriving manual submission untouched no matter how this task's own
read-then-write window lines up against it.

Reuses starting_xi.py's own INSERT_STARTING_XI_STMT rather than
restating the shape of that table, so the two writers can never drift on
what a starting_xi row looks like.
"""

import logging

from sqlalchemy import text

from Shared.deadlines import _DEADLINE_EXPR, deadline_has_passed
from Shared.seasons import real_season_sql
from Gameplay.starting_xi import INSERT_STARTING_XI_STMT

logger = logging.getLogger(__name__)

# Mirrors branch B of Game_logic/fixtures.py's CURRENT_GAMEWEEK_QUERY exactly:
# the single nearest-future deadline is "the gameweek open for selection right
# now". Deliberately NOT that query's "earliest unscored gameweek" branch --
# that branch can point at a gameweek whose deadline has ALREADY passed
# (locked, awaiting scoring), which is exactly what this task must not target.
OPEN_GAMEWEEK_QUERY = text(
    f"""
    WITH gw_deadlines AS (
        SELECT season, gameweek, {_DEADLINE_EXPR} AS deadline
        FROM ml.fixtures
        WHERE {real_season_sql()}
        GROUP BY season, gameweek
    )
    SELECT season, gameweek, deadline FROM gw_deadlines
    WHERE deadline > NOW()
    ORDER BY deadline ASC
    LIMIT 1
    """
)

# Every squad owner for this season who has NOT already submitted (by hand or
# by an earlier run of this task) a selection for the target gameweek.
CANDIDATE_USERS_QUERY = text(
    """
    SELECT DISTINCT us.user_id
    FROM user_squads us
    WHERE us.season = :season
      AND NOT EXISTS (
        SELECT 1 FROM gw_selections gs
        WHERE gs.user_id = us.user_id AND gs.season = :season AND gs.gameweek = :gameweek
      )
    """
)

# The most recent gameweek strictly before the target one that this manager
# actually submitted a selection for -- not necessarily target-1, if a week
# was ever skipped.
PREVIOUS_SELECTION_QUERY = text(
    """
    SELECT id AS gw_selection_id, tactic
    FROM gw_selections
    WHERE user_id = :user_id AND season = :season AND gameweek < :gameweek
    ORDER BY gameweek DESC
    LIMIT 1
    """
)

PREVIOUS_STARTING_XI_QUERY = text(
    "SELECT player_id, position_slot, is_bonus FROM starting_xi "
    "WHERE gw_selection_id = :gw_selection_id ORDER BY position_slot"
)

ACTIVE_SQUAD_QUERY = text(
    """
    SELECT sp.player_id
    FROM squad_players sp
    JOIN user_squads us ON us.id = sp.user_squad_id
    WHERE us.user_id = :user_id AND us.season = :season AND sp.is_active = TRUE
    """
)

CARRY_FORWARD_GW_SELECTION_STMT = text(
    """
    INSERT INTO gw_selections (user_id, season, gameweek, tactic, submitted_at)
    VALUES (:user_id, :season, :gameweek, :tactic, now())
    ON CONFLICT (user_id, season, gameweek) DO NOTHING
    RETURNING id
    """
)


def carry_forward_selections(engine) -> dict:
    """Clones each eligible manager's last selection into the next open
    gameweek. Returns a summary dict for logging/heartbeats; never raises
    for an individual manager's skip -- only a genuine DB error propagates.
    """
    with engine.connect() as conn:
        target = conn.execute(OPEN_GAMEWEEK_QUERY).first()

    if target is None:
        logger.info("carry_forward_selections: no open gameweek found -- nothing to do")
        return {"season": None, "gameweek": None, "carried": [], "skipped_changed_squad": [], "skipped_no_previous": []}

    season, gameweek = target.season, target.gameweek

    # Guards against this task itself running late, past the very deadline
    # it just read -- see the module docstring's "WHY A RAW INSERT NEEDS
    # ITS OWN DEADLINE CHECK".
    if deadline_has_passed(engine, season, gameweek):
        logger.info(
            "carry_forward_selections: season=%s gameweek=%s deadline passed between read and write -- skipping this run",
            season, gameweek,
        )
        return {"season": season, "gameweek": gameweek, "carried": [], "skipped_changed_squad": [], "skipped_no_previous": []}

    with engine.connect() as conn:
        candidates = [r.user_id for r in conn.execute(CANDIDATE_USERS_QUERY, {"season": season, "gameweek": gameweek})]

    carried: list[int] = []
    skipped_changed_squad: list[int] = []
    skipped_no_previous: list[int] = []

    for user_id in candidates:
        with engine.connect() as conn:
            prev = conn.execute(
                PREVIOUS_SELECTION_QUERY, {"user_id": user_id, "season": season, "gameweek": gameweek}
            ).first()
            if prev is None:
                skipped_no_previous.append(user_id)
                continue

            prev_rows = conn.execute(
                PREVIOUS_STARTING_XI_QUERY, {"gw_selection_id": prev.gw_selection_id}
            ).all()
            active_squad = {
                r.player_id
                for r in conn.execute(ACTIVE_SQUAD_QUERY, {"user_id": user_id, "season": season})
            }

        prev_player_ids = {r.player_id for r in prev_rows}
        if prev_player_ids != active_squad:
            # A transfer happened since the last selection -- see the
            # module docstring for why this is a deliberate no-op rather
            # than a best-effort reconciliation.
            skipped_changed_squad.append(user_id)
            continue

        with engine.begin() as conn:
            gw_selection_id = conn.execute(
                CARRY_FORWARD_GW_SELECTION_STMT,
                {"user_id": user_id, "season": season, "gameweek": gameweek, "tactic": prev.tactic},
            ).scalar()

            if gw_selection_id is None:
                # Lost the race to a manual submission that landed between
                # our candidate read above and this write -- their real
                # selection is already there; touch nothing.
                continue

            rows = [
                {
                    "gw_selection_id": gw_selection_id,
                    "player_id": r.player_id,
                    "position_slot": r.position_slot,
                    "is_bonus": r.is_bonus,
                }
                for r in prev_rows
            ]
            conn.execute(INSERT_STARTING_XI_STMT, rows)

        carried.append(user_id)

    return {
        "season": season,
        "gameweek": gameweek,
        "carried": carried,
        "skipped_changed_squad": skipped_changed_squad,
        "skipped_no_previous": skipped_no_previous,
    }
