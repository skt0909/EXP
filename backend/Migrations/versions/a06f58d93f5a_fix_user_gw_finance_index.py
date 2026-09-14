"""widen idx_user_gw_finance_user_season to include gameweek

Revision ID: a06f58d93f5a
Revises: 44ba08efba6c
Create Date: 2026-09-15 00:00:00.000000

44ba08efba6c shipped this index as (user_id, season) -- one column short
of the intended (user_id, season, gameweek). Every real read of this table
(Results/team_dashboard.py's GAMEWEEK_FINANCE_SNAPSHOT_QUERY, the writer's
own ON CONFLICT upsert in Results/scoring.py) filters on all three columns
together, same as the table's own uq_user_gw_finance unique constraint --
the two-column index left gameweek to be filtered client-side against
whatever (user_id, season) rows the index narrowed down to, rather than
letting the index answer the query directly.

DROP + CREATE, not ALTER: Postgres has no ALTER INDEX for changing which
columns are indexed. Not wrapped in a data migration -- an index is a pure
performance structure, nothing reads its existence or absence as a signal,
so there's no row data to preserve or backfill here.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "a06f58d93f5a"
down_revision: Union[str, Sequence[str], None] = "44ba08efba6c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
DROP INDEX IF EXISTS public.idx_user_gw_finance_user_season;
CREATE INDEX idx_user_gw_finance_user_season
    ON public.user_gameweek_finance (user_id, season, gameweek);
"""

_DOWNGRADE_SQL = """
DROP INDEX IF EXISTS public.idx_user_gw_finance_user_season;
CREATE INDEX idx_user_gw_finance_user_season
    ON public.user_gameweek_finance (user_id, season);
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
