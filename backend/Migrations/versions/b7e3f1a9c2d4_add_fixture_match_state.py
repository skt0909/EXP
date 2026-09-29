"""add FPL match state to ml.fixtures

Revision ID: b7e3f1a9c2d4
Revises: 7c098b3189de
Create Date: 2026-09-29 12:00:00.000000

Live-poll checkpoints (Data/live_poll.py's DUE_CHECKPOINTS_QUERY) were due
from the SCHEDULED kickoff: halftime at kickoff+50, fulltime at kickoff+115.
A delayed kickoff broke both -- fulltime could be written while the match
was still being played. FPL's fixtures/ feed already reports the real
state, and Data/fpl_ingest.py's ingest_upcoming_fixtures now persists it:

  started               the match has kicked off
  finished_provisional  the final whistle has gone (result not yet confirmed)
  minutes               FPL's fixture-level minutes. Stored for observation
                        only: its behaviour during live play is unverified,
                        so nothing reads it yet.

Defaults describe "not started", which every row that predates this is
treated as until the next fixture refresh overwrites it. Finished rows are
unaffected in practice: the 'final' checkpoint needs only `finished`.
enforce_fixture_identity guards only fpl_id/home_team_id/away_team_id, so
these columns update freely.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "b7e3f1a9c2d4"
down_revision: Union[str, Sequence[str], None] = "7c098b3189de"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE ml.fixtures
    ADD COLUMN started boolean NOT NULL DEFAULT false,
    ADD COLUMN finished_provisional boolean NOT NULL DEFAULT false,
    ADD COLUMN minutes smallint NOT NULL DEFAULT 0;
"""

_DOWNGRADE_SQL = """
ALTER TABLE ml.fixtures
    DROP COLUMN IF EXISTS minutes,
    DROP COLUMN IF EXISTS finished_provisional,
    DROP COLUMN IF EXISTS started;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
