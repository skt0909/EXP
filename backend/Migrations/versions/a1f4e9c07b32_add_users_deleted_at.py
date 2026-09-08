"""add users.deleted_at for account deletion (soft delete + anonymize)

Revision ID: a1f4e9c07b32
Revises: d4a8f209c1e6
Create Date: 2026-09-08 00:00:00.000000

WHY SOFT DELETE, NOT A REAL DELETE. public.transfers carries
enforce_transfers_immutability_fn (BEFORE DELETE, unconditional RAISE), and
it fires even when the DELETE arrives via transfers_user_id_fkey's own
ON DELETE CASCADE from users -- so `DELETE FROM users` fails outright for
any account that has ever made a transfer, which is the normal state for
anyone who has actually played, not an edge case (see migration
b7d2f4a91c05's note on why several test files carry undeletable users for
exactly this reason). leaderboard_snapshots carries the same kind of
immutability trigger and can block the cascade the same way if the account
created a league that has already been scored.

So "delete account" here means: mark deleted_at, then scrub email/
username/team_name/password_hash to per-user-id placeholders that can
never collide with a real registration and can never be logged in with
again -- Data/auth.py's DELETE /auth/me endpoint does the scrubbing. Every
historical row this user is referenced from (transfers, gw_scores,
squad_players, league standings, Dream11 teams) stays exactly as it was,
so other users' leagues and leaderboards keep working. No index needed:
the only read pattern is by user_id (WHERE id = :user_id AND
deleted_at IS NULL, in auth.py's USER_BY_ID_QUERY and LOGIN_LOOKUP_QUERY),
already covered by the primary key.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a1f4e9c07b32"
down_revision: Union[str, Sequence[str], None] = "d4a8f209c1e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE public.users ADD COLUMN deleted_at timestamp with time zone;
"""

_DOWNGRADE_SQL = """
ALTER TABLE public.users DROP COLUMN IF EXISTS deleted_at;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
