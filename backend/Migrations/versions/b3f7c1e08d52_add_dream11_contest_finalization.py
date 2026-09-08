"""add dream11 contest finalization

Revision ID: b3f7c1e08d52
Revises: c9a04e7b53d1
Create Date: 2026-09-02 00:00:00.000000

Gives a Dream11 contest a permanent, frozen result.

WHY. Until now a contest had a *result* but no *finalization*.
dream11.contest_members.total_points was written twice by a one-off task
(kickoff+50min, kickoff+115min) and then simply never touched again,
while Game_logic/dream11.py's get_user_team recomputed the same team's
points from raw ml.player_gw_stats on EVERY view, forever. Those two
numbers agreed only for as long as the scoring constants did. Changing
ASSIST_POINTS from 3 to 20 broke that: a contest scored under the old
constant kept its stored total while the live recompute returned a
different one, and the frontend renders the stored value on the
leaderboard and the recomputed value on the team panel -- two totals for
the same team on adjacent screens.

Classic FPL does not have this problem because gw_scores rows fall out of
refresh_active_gameweeks' 5-day window and are never revisited, with
rules_version recording which ruleset produced each row. Dream11 has no
recurring sweep to fall out of, so it needs an explicit positive signal
instead of an implicit one.

THREE PIECES:

  * dream11.contests.finalized_at -- the signal. On the CONTEST, not on
    contest_members: a contest is scoped to exactly one fixture, that
    fixture finishes once, and every member finalizes together. A
    per-member column would permit a half-finalized contest, which is not
    a state that means anything. It also costs get_user_team nothing to
    read, since that endpoint already selects the contest row.

  * dream11.team_players.final_points / final_minutes -- the frozen
    per-player breakdown. get_user_team must return each player's points
    without touching ml.player_gw_stats once the contest is final, so the
    breakdown has to live somewhere that isn't recomputed. Written on
    every scoring pass (so the stored breakdown always sums to the stored
    total) and simply stops changing when finalized_at is stamped.
    final_minutes is stored for the same reason: it is displayed
    per-player, and reading it live would defeat the point.

  * enforce_contest_result_immutability -- the guarantee. Dream11's
    scoring path already refuses to rescore a finalized contest in
    application code; this makes it true at the database level too, the
    same stance ml.enforce_gw_stats_immutability_fn takes for settled
    gameweek stats. Deliberately narrow: it fires only when total_points
    or rank actually change, so unrelated UPDATEs to a contest_members
    row are unaffected.

The partial index supports the finalization sweep
(Game_logic/dream11_scoring.py's find_contests_needing_finalization),
which repeatedly asks for contests with finalized_at IS NULL -- a set
that shrinks to near-empty in steady state, which is exactly what a
partial index is for.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b3f7c1e08d52"
down_revision: Union[str, Sequence[str], None] = "c9a04e7b53d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE dream11.contests
    ADD COLUMN IF NOT EXISTS finalized_at timestamp with time zone;

ALTER TABLE dream11.team_players
    ADD COLUMN IF NOT EXISTS final_points smallint;

ALTER TABLE dream11.team_players
    ADD COLUMN IF NOT EXISTS final_minutes smallint;

CREATE INDEX IF NOT EXISTS idx_d11_contests_unfinalized
    ON dream11.contests (fixture_id)
    WHERE finalized_at IS NULL;

CREATE FUNCTION dream11.enforce_contest_result_immutability_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF (NEW.total_points IS DISTINCT FROM OLD.total_points
         OR NEW.rank IS DISTINCT FROM OLD.rank)
       AND EXISTS (
           SELECT 1 FROM dream11.contests c
           WHERE c.id = OLD.contest_id AND c.finalized_at IS NOT NULL
       )
    THEN
        RAISE EXCEPTION
            'Cannot modify a finalized Dream11 contest result: contest_id=%, user_id=%',
            OLD.contest_id, OLD.user_id;
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER enforce_contest_result_immutability
    BEFORE UPDATE ON dream11.contest_members
    FOR EACH ROW EXECUTE FUNCTION dream11.enforce_contest_result_immutability_fn();
"""

_DOWNGRADE_SQL = """
DROP TRIGGER IF EXISTS enforce_contest_result_immutability ON dream11.contest_members;
DROP FUNCTION IF EXISTS dream11.enforce_contest_result_immutability_fn();
DROP INDEX IF EXISTS dream11.idx_d11_contests_unfinalized;
ALTER TABLE dream11.team_players DROP COLUMN IF EXISTS final_minutes;
ALTER TABLE dream11.team_players DROP COLUMN IF EXISTS final_points;
ALTER TABLE dream11.contests DROP COLUMN IF EXISTS finalized_at;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
