"""drop the unused user_squads.total_transfers counter

Revision ID: d8f4a2c60b19
Revises: a5c3e81d4b62
Create Date: 2026-08-30 00:00:00.000000

total_transfers was a denormalised counter that nothing consumed. Every
property it might have had turned out not to hold:

  * Nothing read it to make a decision. The only read incremented it.
    The one rule that sounds like it should use it -- the 20-transfers-
    per-gameweek cap -- deliberately does not: it counts the `transfers`
    table live, per gameweek, which is the number that can actually be
    trusted.
  * It was never a lifetime counter, despite a comment in
    Game_logic/scheduling.py saying so. user_squads is UNIQUE
    (user_id, season), so every season already started a fresh row at 0.
  * It was never exposed to a user. It rode along on POST /transfers'
    response and appeared in no frontend code, no documentation, and no
    contract test.
  * And since Free Hit cancellation started reversing transfers
    (e3a91b7c2d40), it was provably wrong: cancelled transfers stayed
    counted here while every query that matters excluded them.

Dropping it rather than fixing it, because there is no consumer to fix
it for. The counts that are load-bearing -- the free-transfer banking
recurrence and the hit deduction -- are both derived live from
`transfers` with the cancelled_transfers exclusion applied, and are
correct without this column.

Irreversible in practice: downgrade re-creates the column with its
DEFAULT 0, but the historical per-season tallies are not recoverable
from it. They ARE recoverable from `transfers` itself, which is
append-only and remains the real record -- that is precisely why this
column was redundant.

Hand-authored, same as every prior revision -- this project has no
SQLAlchemy ORM models to autogenerate from.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = 'd8f4a2c60b19'
down_revision: Union[str, Sequence[str], None] = 'a5c3e81d4b62'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("ALTER TABLE public.user_squads DROP COLUMN IF EXISTS total_transfers")


def downgrade() -> None:
    """Downgrade schema.

    Restores the column and its default, but not the counts -- see the
    module docstring. Re-derive from `transfers` if they are ever wanted.
    """
    op.execute(
        "ALTER TABLE public.user_squads "
        "ADD COLUMN IF NOT EXISTS total_transfers smallint DEFAULT 0 NOT NULL"
    )
