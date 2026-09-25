"""add dream11.teams.entry_name

Revision ID: a3d7f92c1e60
Revises: f9a3c7e18d62
Create Date: 2026-09-25 00:00:00.000000

The leaderboard's per-row label (Game_logic/dream11.py's
CONTEST_LEADERBOARD_QUERY) has always come from public.users.team_name --
the manager's ACCOUNT-level team name, set once at registration, shared by
every contest they're in. dream11.teams itself has no name column at all,
so there was never anywhere for a per-entry name to live.

That surfaced as a real, confusing gap once saved teams (f9a3c7e18d62)
existed: a manager could name a saved lineup "Guaranteed Cheap Team", pick
it while creating a league, and then see their account's team name on the
leaderboard instead -- the name they just typed was never persisted
anywhere past the saved_teams row itself. PickTeamPage.jsx's own "Team
Name" box had the same gap in the other direction: a real input, sent
nowhere, purely local (its own comment already said so).

entry_name is nullable and optional everywhere it's read: a NULL falls
back to the account's team_name exactly as before (COALESCE in the
leaderboard query), so a team submitted without ever naming it looks
identical to today. Only when a name IS given -- typed in the picker, or
carried over from the saved team it was submitted from -- does the
leaderboard show that instead.

Not stored on saved_teams' own player rows or copied automatically by any
trigger: it is set explicitly by submit_team/edit_team's request body,
same as every other field on a submission.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a3d7f92c1e60"
down_revision: Union[str, Sequence[str], None] = "f9a3c7e18d62"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE dream11.teams ADD COLUMN entry_name character varying(25);
"""

_DOWNGRADE_SQL = """
ALTER TABLE dream11.teams DROP COLUMN IF EXISTS entry_name;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
