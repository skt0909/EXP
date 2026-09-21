"""Drop free_hit_squads: the Free Hit snapshot table.

Revision ID: f2b9c05e7a41
Revises: e7c4d81b3a95
Create Date: 2026-09-22

Free Hit is a chip, and chips were removed in Phase 1 (revision c41a9e27d06b
dropped the `chips` table and `gw_selections.chip_used`). This table held the
pre-chip squad so the revert could put it back. With no way to activate the
chip, nothing writes it and nothing reads it.

WHY IT SURVIVED THREE PHASES. Phase 1 deliberately left it: `revert_free_hits`
still read it, and dropping a table out from under running code is how you turn
a dead feature into an outage. Phase 4c deleted the task, the Beat entry and
GameEngine/free_hit_revert.py first, then the last dead `text()` statements in
Gameplay/starting_xi.py that still named the table without ever executing.
Only then is this drop safe.

CHECKED BEFORE WRITING THIS: the table has no triggers and nothing references
it by foreign key. Its three indexes (the primary key,
uq_free_hit_squads_user_season_gw_player and idx_free_hit_squads_pending) go
with the table, so there is nothing to drop separately.

    SELECT tgname FROM pg_trigger
     WHERE tgrelid = 'free_hit_squads'::regclass AND NOT tgisinternal;   -- 0 rows
    SELECT conname FROM pg_constraint
     WHERE confrelid = 'free_hit_squads'::regclass;                      -- 0 rows
"""
from alembic import op

revision = "f2b9c05e7a41"
down_revision = "e7c4d81b3a95"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # No CASCADE: nothing references this table, and if that ever stopped being
    # true the error should name the dependency rather than silently take it.
    op.execute("DROP TABLE IF EXISTS free_hit_squads")


def downgrade() -> None:
    """Recreates the SHAPE only. The snapshots themselves are data and are not
    recoverable -- and could not be used if they were, because nothing can
    activate a Free Hit to make one meaningful again.

    Reproduced from migration c4e1a7b92f30, which created it.
    """
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS free_hit_squads (
            id               SERIAL      PRIMARY KEY,
            user_id          INTEGER     NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            season           VARCHAR(9)  NOT NULL,
            gameweek         SMALLINT    NOT NULL,
            player_id        INTEGER     NOT NULL,
            purchase_price   SMALLINT    NOT NULL,
            budget_remaining SMALLINT    NOT NULL,
            created_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
            reverted_at      TIMESTAMPTZ,
            CONSTRAINT uq_free_hit_squads_user_season_gw_player
                UNIQUE (user_id, season, gameweek, player_id)
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_free_hit_squads_pending "
        "ON free_hit_squads (user_id, season, gameweek) WHERE reverted_at IS NULL"
    )
