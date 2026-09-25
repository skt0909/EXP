"""add fixture checkpoint polled_at columns and dream11 contest void

Revision ID: d8c2e5a1f374
Revises: b4e1f37c920d
Create Date: 2026-09-25 12:00:00.000000

ml.fixture_poll_schedule records when each of a fixture's three live-poll
checkpoints actually RAN (Worker/tasks.py's poll_due_fixtures). This replaces
booking one-off Celery ETAs: the old halftime_scheduled/fulltime_scheduled
booleans said a poll had been queued, which a Redis restart could silently
undo. A NULL polled_at means "not done yet", so a missed checkpoint is simply
picked up on the next tick. The two booleans are left in place (NOT NULL,
now always written FALSE by new rows) and are no longer read.

dream11.contests.voided_at / void_reason close a contest whose match will
not produce a result: postponed (the fixture lost its kickoff_time) or
abandoned (still not finished 7 days after kickoff). A voided contest also
has finalized_at set, so every existing "is this contest over" check and
the result-immutability trigger treat it as closed.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "d8c2e5a1f374"
down_revision: Union[str, Sequence[str], None] = "b4e1f37c920d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
ALTER TABLE ml.fixture_poll_schedule
    ADD COLUMN halftime_polled_at timestamp with time zone,
    ADD COLUMN fulltime_polled_at timestamp with time zone,
    ADD COLUMN final_polled_at timestamp with time zone;

ALTER TABLE dream11.contests
    ADD COLUMN voided_at timestamp with time zone,
    ADD COLUMN void_reason character varying(20);
"""

_DOWNGRADE_SQL = """
ALTER TABLE dream11.contests
    DROP COLUMN IF EXISTS void_reason,
    DROP COLUMN IF EXISTS voided_at;

ALTER TABLE ml.fixture_poll_schedule
    DROP COLUMN IF EXISTS final_polled_at,
    DROP COLUMN IF EXISTS fulltime_polled_at,
    DROP COLUMN IF EXISTS halftime_polled_at;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
