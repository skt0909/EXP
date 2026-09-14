"""add user_gameweek_finance for per-gameweek team value/bank history

Revision ID: 44ba08efba6c
Revises: a24438b71445
Create Date: 2026-09-14 00:00:00.000000

The Dashboard's Team Value/"In the Bank" banner has never had a history:
user_squads/squad_players carry no gameweek dimension at all, so a past
gameweek's dashboard view could only ever show TODAY's live figures next
to a settled score -- confirmed by reading Results/team_dashboard.py's
TEAM_VALUE_AND_BANK_QUERY, which filters on (user_id, season) only. This
table is the fix: one row per (user_id, season, gameweek), written at the
same moment gw_scores is (Results/scoring.py's _score_one_user), holding
exactly the two numbers that query already computes live, snapshotted.

bank/team_value are SMALLINT tenths-of-a-pound, same convention as
users.now_cost/squad_players.purchase_price/user_squads.budget_remaining
throughout this schema (BUDGET_CAP = 1000 tenths = GBP100.0m) -- not a new
unit, so the reader can divide by 10 exactly like every other money field
already does. This is the reason the columns store the raw upstream
budget_remaining/SUM(purchase_price) integers rather than a decimal: an
integer tenths column can't drift the way summing floats can.

Unique on (user_id, season, gameweek), upserted -- a gameweek can be
re-scored while it's still in the 5-day active window
(GameEngine/gameweek_finalize.refresh_active_gameweeks), and the snapshot
must track the latest re-score the same way gw_scores itself does, not
accumulate duplicate rows.
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "44ba08efba6c"
down_revision: Union[str, Sequence[str], None] = "a24438b71445"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_UPGRADE_SQL = """
CREATE TABLE public.user_gameweek_finance (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    season VARCHAR(9) NOT NULL,
    gameweek SMALLINT NOT NULL,
    bank SMALLINT NOT NULL,
    team_value SMALLINT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_user_gw_finance UNIQUE (user_id, season, gameweek)
);

CREATE INDEX idx_user_gw_finance_user_season ON public.user_gameweek_finance (user_id, season);
"""

_DOWNGRADE_SQL = """
DROP TABLE IF EXISTS public.user_gameweek_finance;
"""


def upgrade() -> None:
    """Upgrade schema."""
    op.execute(_UPGRADE_SQL)


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(_DOWNGRADE_SQL)
